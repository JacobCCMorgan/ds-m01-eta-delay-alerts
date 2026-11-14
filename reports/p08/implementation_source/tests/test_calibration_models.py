"""Hand-computed calibration ranks, objectives, trip weights and clock guards."""

from copy import deepcopy
import json
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ds_m01.calibration_models import (
    fit_interval_calibrator, apply_interval_calibrator, validate_interval_calibrator,
    fit_probability_calibrator, apply_probability_calibrator, validate_probability_calibrator,
)


def rows(n=10):
    return [dict(tenant_id='T1', trip_id=f'TRIP-{i:02}', snapshot_id=f'S-{i:02}',
                 departure_time='2026-07-02T08:00:00-07:00', snapshot_time='2026-07-02T09:00:00-07:00',
                 label_known_at='2026-07-03T08:00:00-07:00', calibration_role='calibration_fit',
                 status='available', eligibility='eligible', lower_seconds=100.0, upper_seconds=200.0,
                 target_seconds=200.0+i, probability=.5, delay_label=i >= n//2) for i in range(n)]


def metadata(values):
    return dict(snapshot_start_at='2026-07-01T00:00:00-07:00', snapshot_end_at='2026-07-15T00:00:00-07:00',
                label_cutoff_at='2026-07-15T00:00:00-07:00',
                fit_trip_keys=[list(k) for k in sorted({(r['tenant_id'], r['trip_id']) for r in values})])


def fit(function, values):
    return function(values, tenant_id='T1', fit_metadata=metadata(values), source_bindings={'source': '1'*64})


def future(**changes):
    return dict(dict(tenant_id='T1', trip_id='ASSESS', snapshot_id='A1', snapshot_time='2026-07-15T07:00:00Z',
                     status='available', lower_seconds=4.0, upper_seconds=20.0, median_seconds=10.0, probability=.5), **changes)


class IntervalCalibrationTests(unittest.TestCase):
    def test_max_per_trip_then_finite_sample_order_statistic(self):
        values = rows()
        # Many easy snapshots from one trip must not change its single score.
        values += [dict(values[0], snapshot_id=f'LONG-{i}', target_seconds=150) for i in range(100)]
        state = fit(fit_interval_calibrator, values)
        self.assertEqual(state['fit_snapshot_count'], 110)
        self.assertEqual(state['fit_trip_count'], 10)
        self.assertEqual(state['ordered_trip_scores_seconds'], list(range(10)))
        self.assertEqual((state['quantile_rank'], state['expansion_seconds']), (9, 8))
        # A lower-bound miss also contributes positively to the trip maximum.
        values[-1]['target_seconds'] = 80
        changed = fit(fit_interval_calibrator, values)
        self.assertEqual(changed['trip_scores'][0]['max_score_seconds'], 20)
        self.assertEqual(changed['expansion_seconds'], 9)
        integer_rank = fit(fit_interval_calibrator, rows(14))
        self.assertEqual((integer_rank['quantile_rank'], integer_rank['expansion_seconds']), (12, 11))

    def test_expansion_zero_floor_and_portable_state(self):
        state = fit(fit_interval_calibrator, rows())
        source = future(target_seconds=object())
        result, = apply_interval_calibrator(json.loads(json.dumps(state)), [source])
        self.assertEqual((result['lower_seconds'], result['upper_seconds'], result['median_seconds']), (0, 28, 10))
        self.assertEqual((result['raw_lower_seconds'], result['raw_upper_seconds']), (4, 20))
        self.assertTrue(result['calibration_applied'])
        self.assertEqual(source['upper_seconds'], 20)
        contained = [dict(r, target_seconds=150) for r in rows()]
        zero = fit(fit_interval_calibrator, contained)
        self.assertEqual(zero['expansion_seconds'], 0)
        self.assertEqual(zero['status'], 'fitted')

    def test_small_or_empty_support_is_explicit_identity(self):
        for n in (0, 9):
            state = fit(fit_interval_calibrator, rows(n))
            self.assertEqual((state['status'], state['reason']), ('identity', 'insufficient_fit_trips'))
            result, = apply_interval_calibrator(state, [future()])
            self.assertFalse(result['calibration_applied'])
            self.assertEqual((result['lower_seconds'], result['upper_seconds']), (4, 20))

    def test_rank_score_membership_and_clock_tampering_fail(self):
        state = fit(fit_interval_calibrator, rows())
        for field, value in (('expansion_seconds', 7), ('quantile_rank', 8), ('fit_snapshot_count', 11),
                             ('coverage_target', .9), ('status', 'identity')):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_interval_calibrator(dict(state, **{field: value}))
        bad = deepcopy(state)
        bad['fit_metadata']['label_cutoff_at'] = '2026-08-01T07:00:00Z'
        with self.assertRaises(ValueError):
            validate_interval_calibrator(bad)


class ProbabilityCalibrationTests(unittest.TestCase):
    def test_weighted_sum_objective_matches_independent_root_solution(self):
        from scipy.optimize import brentq
        values = [dict(r, delay_label=i < 2) for i, r in enumerate(rows())]
        state = fit(fit_probability_calibrator, values)
        # For x=logit(.5)=0, a=1 and derivative in b is 10*sigmoid(b)-2+b.
        expected_b = brentq(lambda b: 10/(1+math.exp(-b))-2+b, -5, 5, xtol=1e-13)
        self.assertAlmostEqual(state['a'], 1, places=10)
        self.assertAlmostEqual(state['b'], expected_b, places=7)
        p = 1/(1+math.exp(-expected_b))
        expected_loss = -2*math.log(p)-8*math.log1p(-p)+.5*expected_b**2
        self.assertAlmostEqual(state['objective_value'], expected_loss, places=9)
        # A mean CE would yield a different intercept; the penalty is one unit,
        # while data weight increases with the number of independent trips.
        self.assertEqual(state['weight_sum'], 10)

    def test_duplicate_snapshots_do_not_overweight_their_trip(self):
        values = [dict(r, probability=.2 if i < 5 else .8, delay_label=i % 3 == 0) for i, r in enumerate(rows())]
        original = fit(fit_probability_calibrator, values)
        expanded = values + [dict(values[0], snapshot_id=f'REPEAT-{i}') for i in range(20)]
        repeated = fit(fit_probability_calibrator, expanded)
        self.assertEqual(repeated['weight_sum'], original['weight_sum'])
        self.assertAlmostEqual(repeated['a'], original['a'], places=8)
        self.assertAlmostEqual(repeated['b'], original['b'], places=8)
        self.assertAlmostEqual(repeated['objective_value'], original['objective_value'], places=9)

    def test_nonnegative_slope_and_monotone_finite_clipped_application(self):
        reverse = [dict(r, probability=.8 if i < 5 else .2) for i, r in enumerate(rows())]
        state = fit(fit_probability_calibrator, reverse)
        self.assertAlmostEqual(state['a'], 0, places=8)
        self.assertAlmostEqual(state['b'], 0, places=8)
        endpoint = [dict(r, probability=float(r['delay_label'])) for r in rows()]
        fitted = fit(fit_probability_calibrator, endpoint)
        self.assertEqual(fitted['clipped_fit_snapshots'], 10)
        sources = [future(snapshot_id=f'A{i}', probability=p, delay_label=object()) for i,p in enumerate((0,.1,.5,.9,1))]
        result = apply_probability_calibrator(json.loads(json.dumps(fitted)), sources)
        predicted = [r['probability'] for r in result]
        self.assertEqual(predicted, sorted(predicted))
        self.assertTrue(all(math.isfinite(p) and 0 <= p <= 1 for p in predicted))
        self.assertEqual([r['raw_probability'] for r in result], [0,.1,.5,.9,1])

    def test_insufficient_trip_or_class_support_preserves_raw_endpoints(self):
        for values, reason in [(rows(9), 'insufficient_fit_trips'),
                               ([dict(r, delay_label=i == 0) for i,r in enumerate(rows())], 'insufficient_class_trip_support')]:
            with patch('scipy.optimize.minimize', side_effect=AssertionError('support fallback must not optimize')):
                state = fit(fit_probability_calibrator, values)
            self.assertEqual(state['reason'], reason)
            result = apply_probability_calibrator(state, [future(probability=0), future(snapshot_id='A2', probability=1)])
            self.assertEqual([r['probability'] for r in result], [0,1])
            self.assertTrue(all(not r['calibration_applied'] for r in result))

    def test_optimizer_failure_is_not_silently_an_identity_fallback(self):
        failed = SimpleNamespace(success=False, message='deliberate failure')
        with patch('scipy.optimize.minimize', return_value=failed), self.assertRaisesRegex(ValueError, 'optimization failed'):
            fit(fit_probability_calibrator, rows())

    def test_class_support_counts_distinct_trips_with_overlapping_classes(self):
        negative = [dict(r, delay_label=False) for r in rows()]
        one_positive_trip = negative + [dict(negative[0], snapshot_id=f'POS-{i}', delay_label=True) for i in range(20)]
        unsupported = fit(fit_probability_calibrator, one_positive_trip)
        self.assertEqual(unsupported['class_support']['true'], {'snapshots': 20, 'trips': 1})
        self.assertEqual(unsupported['reason'], 'insufficient_class_trip_support')
        # Revised promises can give both classes within a trip. Two such trips
        # provide the fixed minimum support without creating extra trip weight.
        mixed = negative + [dict(negative[i], snapshot_id=f'MIX-{i}', delay_label=True) for i in (0, 1)]
        supported = fit(fit_probability_calibrator, mixed)
        self.assertEqual(supported['class_support']['true']['trips'], 2)
        self.assertEqual(supported['class_support']['false']['trips'], 10)
        self.assertEqual((supported['status'], supported['weight_sum']), ('fitted', 10))

    def test_probability_state_tampering_is_rejected(self):
        state = fit(fit_probability_calibrator, rows())
        for field, value in (('a', -.1), ('b', float('nan')), ('probability_clip', .01), ('weight_sum', 1),
                             ('minimum_class_contributing_trips', 1), ('status', 'identity')):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_probability_calibrator(dict(state, **{field: value}))
        state['optimizer']['success'] = False
        with self.assertRaises(ValueError):
            validate_probability_calibrator(state)


class CalibrationBoundaryTests(unittest.TestCase):
    def test_cross_midnight_fit_trip_is_kept_but_new_departure_or_late_label_is_not(self):
        good = [dict(rows(1)[0], departure_time='2026-07-10T23:30:00-07:00',
                     snapshot_time='2026-07-11T02:00:00-07:00', label_known_at='2026-07-14T23:59:59.999999-07:00')]
        fit(fit_interval_calibrator, good)
        for changes in ({'departure_time':'2026-07-11T00:00:00-07:00'},
                        {'label_known_at':'2026-07-15T00:00:00-07:00'},
                        {'calibration_role':'calibration_assessment'}, {'label_known_at':'2026-07-10T00:00:00-07:00'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                fit(fit_interval_calibrator, [dict(good[0], **changes)])

    def test_source_membership_duplicate_and_invalid_forecasts_fail(self):
        for values in ([rows(1)[0], rows(1)[0]], [dict(rows(1)[0], target_seconds=0)],
                       [dict(rows(1)[0], lower_seconds=300)], [dict(rows(1)[0], status='unavailable')]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                fit(fit_interval_calibrator, values)
        with self.assertRaises(ValueError):
            fit_interval_calibrator(rows(1), tenant_id='T1', fit_metadata=metadata([]))
        for changes in ({'probability':float('nan')}, {'probability':1.01}, {'delay_label':0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                fit(fit_probability_calibrator, [dict(rows(1)[0], **changes)])

    def test_application_is_after_fit_and_disjoint_and_keeps_unavailable(self):
        for fitter, apply in ((fit_interval_calibrator, apply_interval_calibrator),
                              (fit_probability_calibrator, apply_probability_calibrator)):
            state = fit(fitter, rows())
            for row in (future(snapshot_time='2026-07-15T06:59:59.999999Z'), future(tenant_id='T2'),
                        future(trip_id=rows(1)[0]['trip_id'])):
                with self.subTest(kind=state['kind'], row=row), self.assertRaises(ValueError):
                    apply(state, [row])
            unavailable = future(status='unavailable', lower_seconds=None, upper_seconds=None, probability=None)
            result, = apply(state, [unavailable])
            self.assertFalse(result['calibration_applied'])
            self.assertEqual(result['calibration_reason'], 'unavailable_forecast')
            self.assertIsNone(result['probability'])
            self.assertIsNone(result['lower_seconds'])
            self.assertIsNone(result['upper_seconds'])


if __name__ == '__main__':
    unittest.main()
