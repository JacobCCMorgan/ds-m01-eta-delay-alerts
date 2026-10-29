"""Hand-calculated checks for support accounting and trip-clustered scoring."""

import copy
import json
import math
import unittest

from ds_m01.baseline_metrics import summarize


def observation(trip='A', snapshot='S1', **changes):
    row = {
        'tenant_id': 'TENANT', 'trip_id': trip, 'snapshot_id': snapshot,
        'fold_id': 'F1', 'label_eligibility': 'eligible',
        'remaining_duration_seconds': 600, 'status': 'available',
        'p10_remaining_seconds': 480, 'p50_remaining_seconds': 660,
        'p90_remaining_seconds': 720, 'pool': 'tenant_global',
    }
    row.update(changes)
    return row


class BaselineMetricsTests(unittest.TestCase):
    def test_long_trip_does_not_dominate_and_rmse_uses_mean_squared_errors(self):
        # Trip A has ten +1-minute errors, B has one +3-minute error.
        rows = [observation('A', str(i)) for i in range(10)]
        rows.append(observation('B', 'B1', p50_remaining_seconds=780, p90_remaining_seconds=900))
        result = summarize(rows)
        self.assertEqual(result['counts']['supported_eligible_snapshots'], 11)
        self.assertEqual(result['counts']['supported_eligible_trips'], 2)
        self.assertAlmostEqual(result['metrics']['MAE_minutes'], 2)
        self.assertAlmostEqual(result['metrics']['RMSE_minutes'], math.sqrt(5))
        self.assertAlmostEqual(result['metrics']['bias_minutes'], 2)

    def test_unknown_and_unsupported_are_retained_without_zero_imputation(self):
        rows = [observation(), observation('B', 'S2', status='unsupported',
                    p10_remaining_seconds=None, p50_remaining_seconds=None, p90_remaining_seconds=None),
                observation('C', 'S3', label_eligibility='not_mature', remaining_duration_seconds=None)]
        result = summarize(rows)
        self.assertEqual(result['counts'], {'snapshots': 3, 'trips': 3, 'eligible_snapshots': 2,
                         'eligible_trips': 2, 'supported_eligible_snapshots': 1,
                         'supported_eligible_trips': 1})
        self.assertEqual(result['eligible_support_rate'], 0.5)
        self.assertEqual(result['label_eligibility_counts'], {'eligible': 2, 'not_mature': 1})
        self.assertEqual(result['prediction_status_counts'], {'available': 2, 'unsupported': 1})
        self.assertEqual(result['metrics']['MAE_minutes'], 1)
        self.assertIsNone(result['mae_bootstrap_95pct']['lower_minutes'])

    def test_coverage_includes_endpoints_and_trip_balanced_width(self):
        rows = [observation('A', 'S1', p10_remaining_seconds=600, p50_remaining_seconds=660),
                observation('A', 'S2', p10_remaining_seconds=300, p50_remaining_seconds=500,
                            p90_remaining_seconds=600),
                observation('B', 'S3', p10_remaining_seconds=700, p50_remaining_seconds=760,
                            p90_remaining_seconds=820)]
        metrics = summarize(rows)['metrics']
        self.assertEqual(metrics['p10_p90_empirical_coverage'], 0.5)
        # A has widths 2 and 5 min, B has width 2 min: mean(3.5, 2).
        self.assertAlmostEqual(metrics['mean_width_minutes'], 2.75)

    def test_pinball_uses_quantile_direction_and_minutes(self):
        metrics = summarize([observation()])['metrics']
        self.assertAlmostEqual(metrics['quantile_pinball_minutes']['0.1'], 0.2)
        self.assertAlmostEqual(metrics['quantile_pinball_minutes']['0.5'], 0.5)
        self.assertAlmostEqual(metrics['quantile_pinball_minutes']['0.9'], 0.2)

    def test_bias_keeps_underprediction_sign(self):
        row = observation(p10_remaining_seconds=300, p50_remaining_seconds=480, p90_remaining_seconds=600)
        metrics = summarize([row])['metrics']
        self.assertEqual(metrics['MAE_minutes'], 2)
        self.assertEqual(metrics['bias_minutes'], -2)

    def test_bootstrap_is_reproducible_and_row_order_independent(self):
        rows = [observation('A', 'S1'), observation('A', 'S2'),
                observation('B', 'S3', p50_remaining_seconds=780, p90_remaining_seconds=900)]
        first = summarize(rows, bootstrap_samples=1000, bootstrap_seed=3)
        second = summarize(reversed(rows), bootstrap_samples=1000, bootstrap_seed=3)
        self.assertEqual(first, second)
        interval = first['mae_bootstrap_95pct']
        self.assertEqual(interval['lower_minutes'], 1)
        self.assertEqual(interval['upper_minutes'], 3)
        self.assertEqual(interval['trip_count'], 2)

    def test_zero_bootstrap_samples_explicitly_disables_interval(self):
        result = summarize([observation(), observation('B', 'S2')], bootstrap_samples=0)
        self.assertEqual(result['mae_bootstrap_95pct']['reason'], 'bootstrap_disabled')
        self.assertIsNone(result['mae_bootstrap_95pct']['upper_minutes'])

    def test_all_unsupported_returns_null_metrics_not_successful_zeros(self):
        result = summarize([observation(status='unsupported', p10_remaining_seconds=None,
                                        p50_remaining_seconds=None, p90_remaining_seconds=None)])
        self.assertEqual(result['eligible_support_rate'], 0)
        self.assertIsNone(result['metrics']['MAE_minutes'])
        self.assertIsNone(result['metrics']['quantile_pinball_minutes']['0.5'])
        self.assertEqual(result['mae_bootstrap_95pct']['reason'], 'fewer_than_two_scored_trips')

    def test_no_eligible_rows_has_null_support_rate_and_empty_input_is_valid(self):
        rows = [observation(label_eligibility='already_arrived', remaining_duration_seconds=None)]
        self.assertIsNone(summarize(rows)['eligible_support_rate'])
        empty = summarize([])
        self.assertEqual(empty['counts']['snapshots'], 0)
        self.assertEqual(empty['counts']['trips'], 0)
        self.assertIsNone(empty['metrics']['MAE_minutes'])
        json.dumps(empty, allow_nan=False)

    def test_trip_ids_are_tenant_scoped(self):
        rows = [observation(), observation(tenant_id='OTHER')]
        self.assertEqual(summarize(rows)['counts']['trips'], 2)

    def test_duplicate_snapshot_and_split_trip_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate validation snapshot'):
            summarize([observation(), observation(trip='B')])
        with self.assertRaisesRegex(ValueError, 'multiple folds'):
            summarize([observation(), observation(snapshot='S2', fold_id='F2')])

    def test_bad_target_is_rejected_even_if_prediction_unsupported(self):
        for value in (None, 0, -1, float('nan'), float('inf'), True, '600'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                summarize([observation(remaining_duration_seconds=value, status='unsupported')])
        with self.assertRaisesRegex(ValueError, 'must be null'):
            summarize([observation(label_eligibility='not_mature', remaining_duration_seconds=0)])

    def test_quantiles_are_validated_only_when_scored(self):
        with self.assertRaisesRegex(ValueError, 'P10 <= P50 <= P90'):
            summarize([observation(p10_remaining_seconds=700)])
        with self.assertRaises(ValueError):
            summarize([observation(p90_remaining_seconds=float('inf'))])
        # Unscored predictions cannot affect errors, even if their raw fields are absent.
        result = summarize([observation(label_eligibility='not_mature', remaining_duration_seconds=None,
                                        p10_remaining_seconds=None, p50_remaining_seconds=None,
                                        p90_remaining_seconds=None)])
        self.assertIsNone(result['metrics']['MAE_minutes'])

    def test_input_validation_and_no_mutation(self):
        rows = [observation()]
        original = copy.deepcopy(rows)
        summarize(rows)
        self.assertEqual(rows, original)
        for kwargs in ({'bootstrap_samples': -1}, {'bootstrap_samples': True},
                       {'bootstrap_seed': False}, {'bootstrap_seed': None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                summarize(rows, **kwargs)
        with self.assertRaises(ValueError):
            summarize([observation(status='success')])
        with self.assertRaises(ValueError):
            summarize([observation(fold_id='')])


if __name__ == '__main__':
    unittest.main()
