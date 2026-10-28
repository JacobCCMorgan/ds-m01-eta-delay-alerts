"""Hand-calculated baseline tests independent of generated scenario outcomes."""

import copy
from decimal import Decimal
import unittest

from ds_m01.baseline import predict


CUTOFF = '2026-05-01T07:00:00Z'


def snapshot(**changes):
    row = {
        'tenant_id': 'A', 'trip_id': 'TARGET', 'snapshot_id': 'S1',
        'snapshot_time': '2026-05-02T12:00:00Z',
        'route_id': 'R1', 'departure_time_bucket': '6-04@America/Los_Angeles',
        'route_distance_km': 80.0, 'elapsed_trip_seconds': 200,
    }
    row.update(changes)
    return row


def historical(trip, duration, **changes):
    row = {
        'tenant_id': 'A', 'trip_id': trip, 'route_id': 'R1',
        'departure_time_bucket': '6-04@America/Los_Angeles',
        'route_distance_km': 80.0, 'duration_seconds': duration,
        'label_known_at': '2026-04-20T12:00:00Z',
        'actual_arrival_time': '2026-04-20T11:00:00Z',
    }
    row.update(changes)
    return row


class BaselineTests(unittest.TestCase):
    def predict(self, rows, snap=None, **kwargs):
        return predict(snapshot() if snap is None else snap, rows, CUTOFF, **kwargs)

    def test_condition_before_quantiles_excludes_completed_duration(self):
        rows = [historical(str(i), value) for i, value in enumerate((100, 200, 300, 500, 700))]
        result = self.predict(rows, min_support=3)
        self.assertEqual(result['pool'], 'route_departure')
        self.assertEqual(result['candidate_count'], 5)
        self.assertEqual(result['surviving_count'], 3)
        # Remaining values [100, 300, 500], including neither zero nor negatives.
        self.assertAlmostEqual(result['p10_remaining_seconds'], 140)
        self.assertAlmostEqual(result['p50_remaining_seconds'], 300)
        self.assertAlmostEqual(result['p90_remaining_seconds'], 460)

    def test_minimum_support_is_checked_after_survival_conditioning(self):
        rows = [historical(str(i), value) for i, value in enumerate((100, 100, 100, 300, 500))]
        result = self.predict(rows)
        self.assertEqual(result['status'], 'unsupported')
        self.assertEqual(result['reason'], 'insufficient_surviving_history')
        self.assertEqual(result['candidate_count'], 5)
        self.assertEqual(result['surviving_count'], 2)
        self.assertIsNone(result['p50_remaining_seconds'])

    def test_route_pool_precedes_distance_and_global(self):
        rows = [historical(f'route-{i}', 300) for i in range(5)]
        rows += [historical(f'other-{i}', 900, route_id='R2') for i in range(10)]
        result = self.predict(rows)
        self.assertEqual(result['pool'], 'route_departure')
        self.assertEqual(result['p50_remaining_seconds'], 100)
        self.assertEqual(result['pool_diagnostics'][-1]['surviving_count'], 15)

    def test_sparse_route_falls_back_to_distance_band(self):
        rows = [historical('same-route', 300)]
        rows += [historical(f'other-{i}', 500, route_id='R2') for i in range(4)]
        rows += [historical('far', 9999, route_id='R3', route_distance_km=150)]
        result = self.predict(rows)
        self.assertEqual(result['pool'], 'distance_band')
        self.assertEqual(result['candidate_count'], 5)
        self.assertEqual(result['p50_remaining_seconds'], 300)

    def test_sparse_distance_falls_back_to_tenant_global(self):
        rows = [historical(f'other-{i}', 600, route_id='R2', route_distance_km=150) for i in range(5)]
        result = self.predict(rows)
        self.assertEqual(result['pool'], 'tenant_global')
        self.assertEqual(result['p50_remaining_seconds'], 400)

    def test_distance_band_is_half_open(self):
        rows = [historical(f'below-{i}', 500, route_id='R2', route_distance_km=99.999) for i in range(5)]
        rows += [historical('at-boundary', 5000, route_id='R3', route_distance_km=100)]
        result = self.predict(rows)
        self.assertEqual(result['pool'], 'distance_band')
        self.assertEqual(result['candidate_count'], 5)
        self.assertEqual(result['p50_remaining_seconds'], 300)

    def test_own_trip_other_tenant_and_boundary_label_cannot_enter_history(self):
        rows = [historical('good', 400)]
        rows += [historical('TARGET', 10000), historical('foreign', 10000, tenant_id='B')]
        rows += [historical('cutoff', 10000, label_known_at=CUTOFF)]
        rows += [historical('later', 10000, label_known_at='2026-05-01T07:00:01Z')]
        result = self.predict(rows, min_support=1)
        self.assertEqual(result['candidate_count'], 1)
        self.assertEqual(result['p50_remaining_seconds'], 200)

    def test_snapshot_clock_is_enforced_even_when_fit_cutoff_is_later(self):
        snap = snapshot(snapshot_time='2026-04-21T00:00:00Z')
        rows = [historical('good', 400), historical('at', 900, label_known_at=snap['snapshot_time'])]
        rows += [historical('after', 900, label_known_at='2026-04-22T00:00:00Z')]
        result = self.predict(rows, snap, min_support=1)
        self.assertEqual(result['candidate_count'], 1)

    def test_arrival_at_or_after_limit_cannot_enter_even_with_bad_known_time(self):
        rows = [historical('good', 400), historical('at-fit', 900, actual_arrival_time=CUTOFF)]
        rows += [historical('after-snapshot', 900, actual_arrival_time='2026-05-03T12:00:00Z')]
        result = self.predict(rows, min_support=1)
        self.assertEqual(result['candidate_count'], 1)
        self.assertEqual(result['p50_remaining_seconds'], 200)

    def test_duplicate_trip_versions_do_not_inflate_support(self):
        row = historical('same', 400)
        with self.assertRaisesRegex(ValueError, 'Duplicate eligible historical trip'):
            self.predict([row, dict(row)], min_support=2)

    def test_no_history_and_no_survivors_have_distinct_unavailable_reasons(self):
        for rows, reason in (([], 'no_eligible_history'), ([historical('done', 200)], 'no_surviving_history')):
            with self.subTest(reason=reason):
                result = self.predict(rows, min_support=1)
                self.assertEqual(result['reason'], reason)
                self.assertEqual(result['status'], 'unsupported')
                for field in ('p10_remaining_seconds', 'p50_remaining_seconds', 'p90_remaining_seconds'):
                    self.assertIsNone(result[field])

    def test_missing_essential_snapshot_input_is_explicit(self):
        for key in ('tenant_id', 'trip_id', 'snapshot_time', 'elapsed_trip_seconds'):
            with self.subTest(key=key):
                result = self.predict([], snapshot(**{key: None}))
                self.assertEqual(result['reason'], 'missing_snapshot_inputs')
                self.assertEqual(result['missing_snapshot_fields'], [key])

    def test_missing_grouping_inputs_still_allow_global_baseline_without_gps(self):
        snap = snapshot(route_id=None, route_distance_km=None, departure_time_bucket=None,
                        position_validity='no_position', latitude_deg=None, longitude_deg=None)
        result = self.predict([historical('good', 400)], snap, min_support=1)
        self.assertEqual(result['pool'], 'tenant_global')
        self.assertEqual(result['p50_remaining_seconds'], 200)
        self.assertFalse(result['pool_diagnostics'][0]['applicable'])
        self.assertFalse(result['pool_diagnostics'][1]['applicable'])

    def test_invalid_numbers_and_naive_times_fail_closed(self):
        for value in (-1, float('nan'), float('inf'), True, '200'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.predict([], snapshot(elapsed_trip_seconds=value))
        for value in (0, -1, None, float('nan'), True):
            with self.subTest(history_duration=value):
                with self.assertRaises(ValueError):
                    self.predict([historical('bad', value)], min_support=1)
        with self.assertRaises(ValueError):
            self.predict([], snapshot(snapshot_time='2026-05-02T12:00:00'))
        for settings in ({'min_support': 0}, {'min_support': True}, {'distance_bin_km': 0},
                         {'distance_bin_km': float('inf')}):
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    self.predict([], **settings)

    def test_exact_minimum_support_decimal_distance_and_no_input_mutation(self):
        snap = snapshot(route_distance_km=Decimal('80.000'))
        rows = [historical(f'h-{i}', 400 + i * 100, route_distance_km=Decimal('80.000')) for i in range(5)]
        before = copy.deepcopy((snap, rows))
        result = self.predict(rows, snap)
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['p50_remaining_seconds'], 400)
        self.assertEqual((snap, rows), before)


if __name__ == '__main__':
    unittest.main()
