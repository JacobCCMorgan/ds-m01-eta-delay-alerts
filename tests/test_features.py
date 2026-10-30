"""Hand-authored temporal and missingness cases for the model input boundary."""

import copy
import json
from pathlib import Path
import unittest

from ds_m01.features import (
    CATEGORICAL_FEATURES, FEATURE_DEFINITION_VERSION, NUMERIC_FEATURES,
    build_feature_row,
)
from ds_m01.pipeline import snapshot_for_trip


PROJECT = Path(__file__).resolve().parents[1]


def feature_case():
    fixture = json.loads((PROJECT / 'tests/fixtures/point_in_time_case.json').read_text())
    snapshot = snapshot_for_trip('T01', 'TRIP-1', fixture['snapshot_time'], fixture['tables'])
    route = fixture['tables']['eta_route_version'][0]
    lineage = dict(tenant_id='T01', snapshot_id=snapshot['snapshot_id'],
                   max_source_available_time='2026-06-23T09:56:00Z')
    return snapshot, route, lineage


class FeatureContractTests(unittest.TestCase):
    def test_independent_timeline_has_expected_units_and_allowlist(self):
        snapshot, route, lineage = feature_case()
        result = build_feature_row(snapshot, route, lineage)
        self.assertEqual(result['feature_definition_version'], FEATURE_DEFINITION_VERSION)
        self.assertEqual(tuple(result['numeric']), NUMERIC_FEATURES)
        self.assertEqual(tuple(result['categorical']), CATEGORICAL_FEATURES)
        self.assertEqual(result['numeric'], dict(elapsed_trip_seconds=3600.0,
            route_distance_km=120.0, remaining_distance_km=60.0,
            gnss_speed_kmh=60.0, position_age_seconds=300.0))
        self.assertEqual(result['categorical']['route_id'], 'ROUTE-1')
        for identifier in ('tenant_id', 'trip_id', 'snapshot_id', 'route_version_id'):
            self.assertNotIn(identifier, result['numeric'])
            self.assertNotIn(identifier, result['categorical'])

    def test_labels_hidden_truth_and_extra_distance_cannot_change_features(self):
        snapshot, route, lineage = feature_case()
        expected = build_feature_row(snapshot, route, lineage)
        snapshot.update(actual_arrival_time='bad-time', remaining_duration_seconds=-999,
                        label_eligibility='cancelled', scenario_id='future',
                        route_distance_km=999999, delay_outcome=True,
                        latitude_deg='not-consumed', longitude_deg='not-consumed')
        route['synthetic_scenario_truth'] = object()
        lineage['target'] = object()
        self.assertEqual(build_feature_row(snapshot, route, lineage), expected)

    def test_inputs_are_not_mutated(self):
        rows = feature_case()
        before = copy.deepcopy(rows)
        build_feature_row(*rows)
        self.assertEqual(rows, before)

    def test_stale_speed_and_distance_are_masked_without_losing_opportunity(self):
        snapshot, route, lineage = feature_case()
        snapshot.update(position_validity='low_confidence',
                        last_position_time='2026-06-23T09:40:00Z',
                        position_available_at='2026-06-23T09:41:00Z',
                        position_age_seconds=1200, gnss_speed_kmh=99.0,
                        remaining_distance_km=80.0)
        result = build_feature_row(snapshot, route, lineage)
        self.assertIsNone(result['numeric']['gnss_speed_kmh'])
        self.assertIsNone(result['numeric']['remaining_distance_km'])
        self.assertEqual(result['numeric']['position_age_seconds'], 1200)
        self.assertEqual(result['snapshot_id'], snapshot['snapshot_id'])

    def test_no_position_keeps_missing_values_and_essential_route_features(self):
        snapshot, route, lineage = feature_case()
        snapshot['position_validity'] = 'no_position'
        for field in ('selected_position_event_id', 'last_position_time',
                      'position_available_at', 'position_age_seconds',
                      'gnss_speed_kmh', 'remaining_distance_km'):
            snapshot[field] = None
        result = build_feature_row(snapshot, route, lineage)
        self.assertEqual(result['numeric']['route_distance_km'], 120)
        self.assertIsNone(result['numeric']['gnss_speed_kmh'])
        self.assertIsNone(result['numeric']['position_age_seconds'])
        snapshot['gnss_speed_kmh'] = 0
        with self.assertRaisesRegex(ValueError, 'no_position'):
            build_feature_row(snapshot, route, lineage)

    def test_future_and_understated_lineage_fail(self):
        for value in ('2026-06-23T10:00:01Z', '2026-06-23T09:55:00Z'):
            rows = feature_case()
            rows[2]['max_source_available_time'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_feature_row(*rows)
        rows = feature_case()
        rows[2]['max_source_available_time'] = rows[0]['snapshot_time']
        self.assertEqual(build_feature_row(*rows)['numeric']['gnss_speed_kmh'], 60)

    def test_route_and_lineage_foreign_keys_are_tenant_scoped(self):
        for index, field, value in ((1, 'tenant_id', 'T02'),
                                    (1, 'route_version_id', 'OTHER'),
                                    (2, 'tenant_id', 'T02'),
                                    (2, 'snapshot_id', 'OTHER')):
            rows = feature_case()
            rows[index][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                build_feature_row(*rows)

    def test_route_known_and_valid_intervals_are_point_in_time(self):
        for changes in (
            {'known_at': '2026-06-23T10:00:01Z'},
            {'valid_from': '2026-06-23T10:00:01Z'},
            {'valid_to': '2026-06-23T10:00:00Z'},
            {'valid_to': '2025-12-31T23:59:59Z'},
        ):
            rows = feature_case()
            rows[1].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_feature_row(*rows)

    def test_future_clocks_and_inconsistent_age_or_quality_fail(self):
        for changes in (
            {'last_position_time': '2026-06-23T10:01:00Z'},
            {'position_available_at': '2026-06-23T10:01:00Z'},
            {'position_available_at': '2026-06-23T09:54:00Z'},
            {'position_age_seconds': 301},
            {'position_validity': 'low_confidence'},
            {'position_validity': 'unknown'},
            {'elapsed_trip_seconds': 100},
        ):
            rows = feature_case()
            rows[0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_feature_row(*rows)

    def test_stale_boundary_is_inclusive_and_timezone_offsets_work(self):
        rows = feature_case()
        rows[0].update(last_position_time='2026-06-23T02:45:00-07:00',
                       position_available_at='2026-06-23T02:46:00-07:00',
                       position_age_seconds=900)
        result = build_feature_row(*rows)
        self.assertEqual(result['numeric']['gnss_speed_kmh'], 60)
        with self.assertRaises(ValueError):
            build_feature_row(*rows, position_stale_seconds=899)

    def test_missing_optional_values_remain_missing(self):
        rows = feature_case()
        for field in ('gnss_speed_kmh', 'remaining_distance_km', 'departure_time_bucket'):
            rows[0][field] = None
        result = build_feature_row(*rows)
        self.assertIsNone(result['numeric']['gnss_speed_kmh'])
        self.assertIsNone(result['numeric']['remaining_distance_km'])
        self.assertIsNone(result['categorical']['departure_time_bucket'])

    def test_malformed_numbers_and_identifiers_are_rejected(self):
        for field in ('elapsed_trip_seconds', 'gnss_speed_kmh',
                      'position_age_seconds', 'remaining_distance_km'):
            for value in (True, float('nan'), float('inf'), -1, '12'):
                rows = feature_case()
                rows[0][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    build_feature_row(*rows)
        for field in ('tenant_id', 'trip_id', 'snapshot_id', 'route_version_id'):
            rows = feature_case()
            rows[0][field] = ' '
            with self.subTest(field=field), self.assertRaises(ValueError):
                build_feature_row(*rows)
        for value in (None, 0, True, float('inf')):
            rows = feature_case()
            rows[1]['planned_distance_km'] = value
            with self.subTest(distance=value), self.assertRaises(ValueError):
                build_feature_row(*rows)

    def test_source_version_and_naive_times_are_rejected(self):
        for field, value in (('feature_definition_version', 'future-version'),
                             ('snapshot_time', '2026-06-23T10:00:00')):
            rows = feature_case()
            rows[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                build_feature_row(*rows)

    def test_elapsed_zero_is_valid_but_remaining_distance_cannot_exceed_route(self):
        rows = feature_case()
        rows[0].update(elapsed_trip_seconds=0, position_age_seconds=0,
                       last_position_time=rows[0]['snapshot_time'],
                       position_available_at=rows[0]['snapshot_time'])
        rows[2]['max_source_available_time'] = rows[0]['snapshot_time']
        self.assertEqual(build_feature_row(*rows)['numeric']['elapsed_trip_seconds'], 0)
        rows[0]['remaining_distance_km'] = 121
        with self.assertRaises(ValueError):
            build_feature_row(*rows)

    def test_contract_matches_executable_feature_interface(self):
        contract = json.loads((PROJECT / 'contracts/m3b1_features.json').read_text())
        self.assertEqual(contract['feature_definition_version'], FEATURE_DEFINITION_VERSION)
        self.assertEqual(tuple(row['name'] for row in contract['numeric_features']), NUMERIC_FEATURES)
        self.assertEqual(tuple(row['name'] for row in contract['categorical_features']), CATEGORICAL_FEATURES)


if __name__ == '__main__':
    unittest.main()
