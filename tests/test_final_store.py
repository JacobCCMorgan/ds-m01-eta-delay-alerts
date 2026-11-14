"""Final stores use synthetic forecasts only; no final targets are opened."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import duckdb

from ds_m01.common import digest_file
from ds_m01.final_policy import run_policy_delivery
from ds_m01 import final_store as store
from test_final_policy import ROOT, SETTINGS, opportunity, registry


GENERATED_AT = '2026-11-14T12:00:00Z'


class FinalStoreTests(unittest.TestCase):
    def setUp(self):
        self.rows = [opportunity(), opportunity('S2', 1800), opportunity('S3', 3600),
                     opportunity('S4', tenant='T02', trip='TRIP2')]
        self.models = registry(('T01', 'T02'))
        self.bundle = run_policy_delivery(self.rows, SETTINGS)
        self.tables = store.build_tables(self.rows, self.models, self.bundle, GENERATED_AT, SETTINGS)
        self.temporary = tempfile.TemporaryDirectory()
        self.parent = Path(self.temporary.name)
        self.destination = self.parent / 'store'

    def tearDown(self):
        self.temporary.cleanup()

    def test_three_formats_replay_with_real_foreign_keys_without_writing_database(self):
        receipt = store.write_store(ROOT, self.destination, self.tables, SETTINGS)
        before = digest_file(self.destination / 'experiment.duckdb')
        result = store.verify_store(ROOT, self.destination, self.tables, SETTINGS)
        self.assertEqual(before, digest_file(self.destination / 'experiment.duckdb'))
        self.assertEqual(result['differences'], 0)
        self.assertTrue(result['read_only'])
        self.assertEqual(len(receipt['counts']), 27)
        self.assertEqual(len(result['canonical_tables']), 8)
        self.assertEqual(result['population_role'], 'final_test')
        self.assertFalse(result['target_rows_read'])
        self.assertFalse(result['quality_accepted'])
        self.assertFalse(result['operational_approval'])
        for name in store.RESERVED:
            self.assertEqual(receipt['counts'][name], 0)
        with duckdb.connect(str(self.destination / 'experiment.duckdb'), read_only=True) as con:
            self.assertGreater(con.execute("SELECT count(*) FROM duckdb_constraints() WHERE constraint_type='FOREIGN KEY'").fetchone()[0], 20)
            self.assertEqual(con.execute("SELECT DISTINCT json_extract_string(value,'$.value') FROM eta_evaluation_membership, json_each(evaluation_slice) WHERE json_extract_string(value,'$.dimension')='period'").fetchall(), [('final_test',)])
            weights = con.execute("SELECT trip_evaluation_weight FROM eta_evaluation_membership WHERE tenant_id='T01' ORDER BY snapshot_id").fetchall()
            self.assertEqual([r[0] for r in weights], [Decimal('.333333333'), Decimal('.333333333'), Decimal('.333333334')])
        reference = self.tables['eta_alert_rule_version'][0]['threshold_selection_ref']
        manifest = json.loads((self.destination / reference).read_text())
        self.assertEqual(manifest['selected_rule'], self.bundle['rule'])
        self.assertFalse(manifest['threshold_reselected'])

    def test_unavailable_forecasts_keep_inventory_weights_releases_and_explicit_unknown(self):
        rows = deepcopy(self.rows)
        rows[0].update(eta_status='unavailable', eta_reason='insufficient_support', lower_seconds=None,
                       median_seconds=None, upper_seconds=None, lower_seconds_int=None,
                       median_seconds_int=None, upper_seconds_int=None)
        rows[1].update(probability_status='unavailable', probability_reason='unsupported',
                       probability_calibration_status='unavailable', raw_probability=None,
                       calibrated_probability=None, probability=None)
        bundle = run_policy_delivery(rows, SETTINGS)
        tables = store.build_tables(rows, self.models, bundle, GENERATED_AT, SETTINGS)
        for name in ('p08_source_snapshot', 'p08_opportunity_audit', 'p08_metadata_audit',
                     'eta_evaluation_membership', 'eta_output_release'):
            self.assertEqual(len(tables[name]), 4)
        self.assertEqual(len(tables['eta_prediction']), 3)
        prediction = next(r for r in tables['eta_prediction'] if r['snapshot_id'] == 'S2')
        self.assertIsNone(prediction['delay_probability'])
        self.assertEqual(prediction['value_states'], {'delay_probability': 'unknown'})
        store.write_store(ROOT, self.destination, tables, SETTINGS)
        self.assertEqual(store.verify_store(ROOT, self.destination, tables, SETTINGS)['differences'], 0)

    def test_exact_rounding_and_membership_reconstruction_reject_canonical_mutation(self):
        changes = [
            ('eta_prediction', 'delay_probability', .7000004),
            ('eta_prediction', 'remaining_eta_seconds', 4001),
            ('eta_evaluation_membership', 'trip_evaluation_weight', .333333332),
            ('eta_evaluation_membership', 'lead_time_slice', 'past_promise'),
            ('eta_evaluation_membership', 'evaluation_slice', []),
            ('eta_evaluation_membership', 'unseen_route', True),
            ('eta_model_version', 'nominal_interval_coverage', .9),
            ('eta_alert_rule_version', 'threshold_selection_ref', 'selection.json'),
            ('p08_provenance_reference', 'record_key', {'snapshot_id': 'OTHER'}),
            ('p08_delivery_provenance_reference', 'parent_provenance_ids', []),
            ('p08_delivery_event_audit', 'event_order', 999),
        ]
        for table, field, value in changes:
            with self.subTest(table=table, field=field):
                broken = deepcopy(self.tables)
                broken[table][0][field] = value
                with self.assertRaises(ValueError):
                    store.write_store(ROOT, self.destination, broken, SETTINGS)
                self.assertFalse(self.destination.exists())

    def test_policy_bundle_omission_or_changed_success_cannot_be_promoted(self):
        for collection in ('alerts', 'attempts', 'episodes', 'events'):
            with self.subTest(collection=collection):
                broken = deepcopy(self.bundle)
                self.assertTrue(broken['delivery'][collection])
                broken['delivery'][collection].pop()
                with self.assertRaisesRegex(ValueError, 'label-free replay'):
                    store.build_tables(self.rows, self.models, broken, GENERATED_AT, SETTINGS)
        broken = deepcopy(self.bundle)
        broken['threshold_reselected'] = True
        with self.assertRaisesRegex(ValueError, 'label-free replay'):
            store.build_tables(self.rows, self.models, broken, GENERATED_AT, SETTINGS)

    def test_model_tenant_and_cutoff_binding_is_not_interchangeable(self):
        mutations = [
            ('source_bindings', {'tenant_id': 'T02'}),
            ('prediction_version', 'MODEL-T02'),
            ('training_cutoff_at', '2026-08-01T07:00:00Z'),
            ('calibration_cutoff_at', '2026-07-24T07:00:00Z'),
            ('nominal_interval_coverage', 'NaN'),
            ('nominal_interval_coverage', '0.9'),
        ]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                models = deepcopy(self.models)
                models[0][field] = value
                with self.assertRaises(ValueError):
                    store.build_tables(self.rows, models, self.bundle, GENERATED_AT, SETTINGS)

    def test_actual_materialization_and_synthetic_clocks_remain_distinct(self):
        with self.assertRaisesRegex(ValueError, 'observation cutoff'):
            store.build_tables(self.rows, self.models, self.bundle, '2026-10-15T06:59:59Z', SETTINGS)
        self.assertTrue(all(r['released_at'] == GENERATED_AT for r in self.tables['eta_output_release']))
        self.assertTrue(all(r['created_at'].startswith('2026-08-') for r in self.tables['eta_alert']))
        self.assertTrue(all(r['materialized_at'] == GENERATED_AT and r['clock_domain'] == 'synthetic_scenario_time'
                            for r in self.tables['p08_delivery_event_audit']))
        self.assertTrue(all(r['received_or_opened_at'] is None for r in self.tables['eta_notification_delivery']))
        self.assertTrue(all(r['counts_as_of'] == SETTINGS['delivery']['counts_as_of']
                            for r in self.tables['eta_alert_episode']))

    def test_fresh_destination_guards_precede_source_and_schema_reads(self):
        self.destination.mkdir()
        with patch.object(store, '_schema') as schema:
            with self.assertRaises(FileExistsError):
                store.write_store(Path('/does-not-exist'), self.destination, {}, {})
            schema.assert_not_called()
        link = self.parent / 'dangling'
        link.symlink_to(self.parent / 'absent-target')
        with patch.object(store, '_schema') as schema:
            with self.assertRaises(FileExistsError):
                store.write_store(Path('/does-not-exist'), link, {}, {})
            schema.assert_not_called()
        self.assertTrue(link.is_symlink())

    def test_mid_write_failure_cleans_staged_data_without_publishing(self):
        original = store.write_jsonl
        count = 0
        def injected(path, rows):
            nonlocal count
            count += 1
            original(path, rows)
            if count == 4:
                raise RuntimeError('injected serialization failure')
        with patch.object(store, 'write_jsonl', side_effect=injected):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                store.write_store(ROOT, self.destination, self.tables, SETTINGS)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_receipt_and_jsonl_tampering_are_detected_without_database_writes(self):
        store.write_store(ROOT, self.destination, self.tables, SETTINGS)
        original_sha = digest_file(self.destination / 'experiment.duckdb')
        receipt_path = self.destination / 'verification.json'
        receipt = json.loads(receipt_path.read_text())
        receipt['quality_accepted'] = True
        receipt_path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, 'receipt claims'):
            store.verify_store(ROOT, self.destination, self.tables, SETTINGS)
        receipt['quality_accepted'] = False
        receipt_path.write_text(json.dumps(receipt))
        path = self.destination / 'jsonl/eta_prediction.jsonl'
        path.write_text(path.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'file changed'):
            store.verify_store(ROOT, self.destination, self.tables, SETTINGS)
        self.assertEqual(original_sha, digest_file(self.destination / 'experiment.duckdb'))

    def test_scenario_store_keeps_model_rule_and_candidate_identity(self):
        rows = deepcopy(self.rows)
        for row in rows:
            row['scenario_id'] = 'S07_transport_degraded'
        bundle = run_policy_delivery(rows, SETTINGS, 'S07_transport_degraded')
        tables = store.build_tables(rows, self.models, bundle, GENERATED_AT, SETTINGS)
        for name in ('eta_model_version', 'eta_alert_rule_version', 'eta_prediction', 'p08_candidate_audit'):
            self.assertEqual(tables[name], self.tables[name])
        self.assertEqual(tables['p08_run_audit'][0]['scenario_id'], 'S07_transport_degraded')
        self.assertEqual(tables['p08_run_audit'][0]['bundle_sha256'], store.digest(bundle))
        store.write_store(ROOT, self.destination, tables, SETTINGS)
        self.assertEqual(store.verify_store(ROOT, self.destination, tables, SETTINGS)['scenario_id'], 'S07_transport_degraded')

    def test_target_field_cannot_enter_audit_metadata(self):
        rows = deepcopy(self.rows)
        rows[0]['delay_label'] = True
        with self.assertRaisesRegex(ValueError, 'target/undeclared'):
            store.build_tables(rows, self.models, self.bundle, GENERATED_AT, SETTINGS)


if __name__ == '__main__':
    unittest.main()
