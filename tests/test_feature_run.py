"""Independent feature-runner timelines with no outcome-data dependency."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from ds_m01.common import write_jsonl
from ds_m01.feature_run import (
    FEATURE_INPUT_FILES, load_feature_inputs, prepare_folds, run,
)


PROJECT = Path(__file__).resolve().parents[1]


def source_config():
    return json.loads((PROJECT / 'config/m2.json').read_text())


def settings():
    result = json.loads((PROJECT / 'config/m3a.json').read_text())
    result['folds'] = result['folds'][:1]
    return result


def source_rows():
    """Two independent tenants, each with one fit and one validation trip.

    Repeated trip, snapshot and route-version identifiers are intentional:
    every join must use the tenant as part of its key. No generator, label,
    baseline output or hidden scenario truth supplies these observations.
    """
    rows = {relative: [] for relative in FEATURE_INPUT_FILES}
    for tenant, distance in (('T01', 100.0), ('T02', 240.0)):
        rows['data/raw/eta_route_version.jsonl'].append(dict(
            tenant_id=tenant, route_id='ROUTE-01', route_version_id='ROUTE-V1',
            planned_distance_km=distance, known_at='2025-12-01T00:00:00Z',
            valid_from='2025-12-01T00:00:00Z', valid_to=None,
        ))
        for trip, day, offset in (
            ('FIT', '2026-01-10', '-08:00'),
            ('VAL', '2026-03-10', '-07:00'),
        ):
            departure = f'{day}T08:00:00{offset}'
            at = f'{day}T08:30:00{offset}'
            common = dict(tenant_id=tenant, trip_id=trip)
            rows['data/processed/eta_split_assignment.jsonl'].append(dict(
                common, split_role='train', label_cutoff_at='2026-07-01T07:00:00Z',
            ))
            rows['data/processed/trip_membership.jsonl'].append(dict(
                common, split_role='train', vehicle_id=f'{tenant}-V01',
                route_id='ROUTE-01', unseen_vehicle=False, unseen_route=False,
            ))
            rows['data/raw/eta_trip_event.jsonl'].append(dict(
                common, trip_event_id='DEPART-'+trip, event_type='departure',
                occurred_at=departure, available_at=departure,
                trip_status='in_transit',
            ))
            rows['data/processed/eta_snapshot.jsonl'].append(dict(
                common, snapshot_id='S-'+trip, snapshot_time=at,
                route_version_id='ROUTE-V1', vehicle_id=f'{tenant}-V01',
                trip_version_id='PLAN-'+trip, promise_version_id='PROMISE-'+trip,
                elapsed_trip_seconds=1800, departure_time_bucket='6-08@America/Los_Angeles',
                position_validity='no_position', selected_position_event_id=None,
                last_position_time=None, position_available_at=None,
                position_age_seconds=None, gnss_speed_kmh=None,
                remaining_distance_km=None, latitude_deg=None, longitude_deg=None,
                feature_definition_version='m1-features-v1', trip_status_as_of='in_transit',
            ))
            rows['data/processed/feature_lineage.jsonl'].append(dict(
                tenant_id=tenant, snapshot_id='S-'+trip,
                max_source_available_time=departure,
                feature_columns=[
                    'latitude_deg', 'longitude_deg', 'position_age_seconds',
                    'gnss_speed_kmh', 'remaining_distance_km',
                    'departure_time_bucket', 'elapsed_trip_seconds', 'position_validity',
                ], parent_provenance_ids=['HAND-SOURCE'],
            ))
    return rows


def write_inputs(root, rows):
    for relative, values in rows.items():
        write_jsonl(Path(root) / relative, values)


def prepared(root):
    return prepare_folds(load_feature_inputs(root, source_config()), source_config(), settings())


class FeatureRunnerTests(unittest.TestCase):
    def test_snapshot_vehicle_must_be_nonempty_identifier(self):
        for value in (None, '', ' ', 123):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                rows = source_rows()
                rows['data/processed/eta_snapshot.jsonl'][0]['vehicle_id'] = value
                write_inputs(root, rows)
                with self.assertRaises(ValueError):
                    load_feature_inputs(root, source_config())

    def test_feature_input_allowlist_has_no_outcome_or_promise_source(self):
        self.assertEqual(set(FEATURE_INPUT_FILES), {
            'data/raw/eta_trip_event.jsonl',
            'data/raw/eta_route_version.jsonl',
            'data/processed/eta_snapshot.jsonl',
            'data/processed/eta_split_assignment.jsonl',
            'data/processed/trip_membership.jsonl',
            'data/processed/feature_lineage.jsonl',
        })

    def test_missing_and_poisoned_outcome_files_do_not_change_features(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_inputs(root, source_rows())
            expected = prepared(root)
            for name in ('eta_trip_outcome', 'eta_promise_version'):
                (root / 'data/raw' / f'{name}.jsonl').write_text('not json at all\n')
            (root / 'data/processed/eta_snapshot_label.jsonl').write_text('not json\n')
            (root / 'validation').mkdir()
            (root / 'validation/latent_truth.jsonl').write_text('not json\n')
            self.assertEqual(prepared(root), expected)

    def test_target_and_scenario_columns_never_reach_feature_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = source_rows()
            write_inputs(root, rows)
            expected = prepared(root)
            for row in rows['data/processed/eta_snapshot.jsonl']:
                row.update(actual_arrival_time='2099-01-01T00:00:00Z',
                           remaining_duration_seconds=999999, delay_outcome=True,
                           synthetic_scenario_id='forbidden-scenario', latent_duration=123)
            write_inputs(root, rows)
            self.assertEqual(prepared(root), expected)

    def test_protected_payload_is_discarded_before_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = source_rows()
            write_inputs(root, rows)
            expected = prepared(root)
            for identifier, role in (('CAL', 'calibration'), ('TEST', 'test'), ('EX', 'excluded')):
                common = dict(tenant_id='T01', trip_id=identifier)
                for relative in ('data/processed/eta_split_assignment.jsonl',
                                 'data/processed/trip_membership.jsonl'):
                    rows[relative].append(dict(common, split_role=role))
                rows['data/raw/eta_trip_event.jsonl'].append(dict(common, occurred_at='bad'))
                rows['data/processed/eta_snapshot.jsonl'].append(dict(
                    common, snapshot_id='S-'+identifier, snapshot_time='bad',
                ))
                rows['data/processed/feature_lineage.jsonl'].append(dict(
                    tenant_id='T01', snapshot_id='S-'+identifier,
                    max_source_available_time='bad',
                ))
            write_inputs(root, rows)
            result = prepared(root)
            self.assertEqual(result['matrix_rows'], expected['matrix_rows'])
            self.assertEqual(result['transformers'], expected['transformers'])
            self.assertEqual(result['memberships'], expected['memberships'])

    def test_duplicate_and_missing_lineage_fail_closed(self):
        for mode in ('duplicate', 'missing'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                rows = source_rows()
                lineages = rows['data/processed/feature_lineage.jsonl']
                if mode == 'duplicate':
                    lineages.append(copy.deepcopy(lineages[0]))
                else:
                    lineages.pop(0)
                write_inputs(tmp, rows)
                with self.assertRaises(ValueError):
                    load_feature_inputs(tmp, source_config())

    def test_future_lineage_and_route_knowledge_fail_closed(self):
        for relative, field in (
            ('data/processed/feature_lineage.jsonl', 'max_source_available_time'),
            ('data/raw/eta_route_version.jsonl', 'known_at'),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                rows = source_rows()
                rows[relative][0][field] = '2026-12-01T00:00:00Z'
                write_inputs(tmp, rows)
                with self.assertRaises(ValueError):
                    load_feature_inputs(tmp, source_config())

    def test_snapshot_and_membership_duplicates_fail_closed(self):
        for relative in ('data/processed/eta_snapshot.jsonl',
                         'data/processed/trip_membership.jsonl',
                         'data/processed/eta_split_assignment.jsonl'):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                rows = source_rows()
                rows[relative].append(copy.deepcopy(rows[relative][0]))
                write_inputs(tmp, rows)
                with self.assertRaises(ValueError):
                    load_feature_inputs(tmp, source_config())

    def test_tenant_scoped_route_join_preserves_colliding_identifiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_inputs(tmp, source_rows())
            inputs = load_feature_inputs(tmp, source_config())
            distances = {(r['tenant_id'], r['trip_id']): r['numeric']['route_distance_km']
                         for r in inputs['feature_rows']}
            self.assertEqual(distances, {('T01', 'FIT'): 100, ('T01', 'VAL'): 100,
                                         ('T02', 'FIT'): 240, ('T02', 'VAL'): 240})
            result = prepare_folds(inputs, source_config(), settings())
            self.assertEqual(len(result['matrix_rows']), 4)
            self.assertEqual({(r['tenant_id'], r['trip_id'], r['fold_role'])
                              for r in result['matrix_rows']},
                             {('T01', 'FIT', 'fit'), ('T01', 'VAL', 'validation'),
                              ('T02', 'FIT', 'fit'), ('T02', 'VAL', 'validation')})

    def test_changing_other_tenant_does_not_change_this_tenant(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = source_rows()
            write_inputs(tmp, rows)
            before = prepared(tmp)
            rows['data/raw/eta_route_version.jsonl'][1]['planned_distance_km'] = 99999.0
            rows['data/raw/eta_route_version.jsonl'][1]['route_id'] = 'OTHER-ROUTE'
            for row in rows['data/processed/trip_membership.jsonl']:
                if row['tenant_id'] == 'T02':
                    row['route_id'] = 'OTHER-ROUTE'
            write_inputs(tmp, rows)
            after = prepared(tmp)
            for field in ('matrix_rows', 'transformers'):
                original = [r for r in before[field] if r['tenant_id'] == 'T01']
                changed = [r for r in after[field] if r['tenant_id'] == 'T01']
                self.assertEqual(original, changed)

    def test_missing_tenant_specific_route_cannot_fall_back_across_tenants(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = source_rows()
            rows['data/raw/eta_route_version.jsonl'].pop(0)
            write_inputs(tmp, rows)
            with self.assertRaises(ValueError):
                load_feature_inputs(tmp, source_config())

    def test_heldout_entities_introduced_at_snapshot_are_rejected(self):
        for heldout_kind in ('vehicle', 'route'):
            with self.subTest(heldout_kind=heldout_kind), tempfile.TemporaryDirectory() as tmp:
                rows = source_rows()
                if heldout_kind == 'vehicle':
                    rows['data/processed/eta_snapshot.jsonl'][0]['vehicle_id'] = 'T01-V10'
                else:
                    rows['data/raw/eta_route_version.jsonl'][0]['route_id'] = 'ROUTE-12'
                write_inputs(tmp, rows)
                with self.assertRaises(ValueError):
                    load_feature_inputs(tmp, source_config())

    def test_unavailable_departure_and_elapsed_inconsistency_are_rejected(self):
        for mismatch in ('future_departure_notice', 'elapsed'):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as tmp:
                rows = source_rows()
                if mismatch == 'future_departure_notice':
                    rows['data/raw/eta_trip_event.jsonl'][0]['available_at'] = '2026-01-11T00:00:00Z'
                else:
                    rows['data/processed/eta_snapshot.jsonl'][0]['elapsed_trip_seconds'] = 10
                write_inputs(tmp, rows)
                with self.assertRaises(ValueError):
                    load_feature_inputs(tmp, source_config())

    def test_fit_and_validation_snapshot_cutoff_boundaries_are_enforced(self):
        for trip, at in (('FIT', '2026-03-01T08:00:00Z'),
                         ('VAL', '2026-03-01T07:59:59Z')):
            with self.subTest(trip=trip), tempfile.TemporaryDirectory() as tmp:
                write_inputs(tmp, source_rows())
                inputs = load_feature_inputs(tmp, source_config())
                row = next(r for r in inputs['feature_rows'] if r['trip_id'] == trip)
                row['snapshot_time'] = at
                with self.assertRaises(ValueError):
                    prepare_folds(inputs, source_config(), settings())

    def test_whole_trip_snapshots_share_fold_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = source_rows()
            extra = copy.deepcopy(rows['data/processed/eta_snapshot.jsonl'][1])
            extra.update(snapshot_id='S-VAL-2', snapshot_time='2026-03-10T09:00:00-07:00',
                         elapsed_trip_seconds=3600)
            rows['data/processed/eta_snapshot.jsonl'].append(extra)
            lineage = copy.deepcopy(rows['data/processed/feature_lineage.jsonl'][1])
            lineage['snapshot_id'] = 'S-VAL-2'
            rows['data/processed/feature_lineage.jsonl'].append(lineage)
            write_inputs(tmp, rows)
            result = prepared(tmp)
            target = [r for r in result['matrix_rows'] if r['tenant_id'] == 'T01' and r['trip_id'] == 'VAL']
            self.assertEqual(len(target), 2)
            self.assertEqual({r['fold_role'] for r in target}, {'validation'})

    def test_validation_tenant_without_fit_support_is_retained_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = source_rows()
            for relative in ('data/processed/eta_snapshot.jsonl',
                             'data/processed/eta_split_assignment.jsonl',
                             'data/processed/trip_membership.jsonl',
                             'data/raw/eta_trip_event.jsonl'):
                rows[relative] = [r for r in rows[relative]
                                  if not (r['tenant_id'] == 'T02' and r['trip_id'] == 'FIT')]
            rows['data/processed/feature_lineage.jsonl'] = [
                r for r in rows['data/processed/feature_lineage.jsonl']
                if not (r['tenant_id'] == 'T02' and r['snapshot_id'] == 'S-FIT')]
            write_inputs(tmp, rows)
            result = prepared(tmp)
            target = [r for r in result['matrix_rows'] if r['tenant_id'] == 'T02']
            self.assertEqual(len(target), 1)
            self.assertEqual(target[0]['status'], 'unsupported')
            self.assertIsNone(target[0]['vector'])

    def test_prepare_is_deterministic_and_does_not_mutate_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_inputs(tmp, source_rows())
            inputs = load_feature_inputs(tmp, source_config())
            original = copy.deepcopy(inputs)
            first = prepare_folds(inputs, source_config(), settings())
            self.assertEqual(first, prepare_folds(inputs, source_config(), settings()))
            self.assertEqual(inputs, original)

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / 'evidence'
            output.mkdir()
            marker = output / 'keep.txt'
            marker.write_text('original evidence\n')
            with self.assertRaises(FileExistsError):
                run(root, root / 'missing-settings.json', output)
            self.assertEqual(marker.read_text(), 'original evidence\n')


if __name__ == '__main__':
    unittest.main()
