"""Source-bound P09 store integration and adversarial cross-format checks."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import duckdb

from ds_m01.common import digest_file
from workflows.p09.inputs import load_parent, load_settings
from workflows.p09.engine import simulate_workflow
from workflows.p09 import store

ROOT = Path(__file__).resolve().parents[3]


class WorkflowStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ROOT
        cls.inputs = load_parent(cls.root); cls.settings = load_settings(cls.root)
        cls.simulation = simulate_workflow(cls.inputs, cls.settings)
        cls.tables = store.build_tables(cls.root, cls.inputs, cls.simulation,
                                       datetime.now(timezone.utc).isoformat(), cls.settings)
        cls.temp = tempfile.TemporaryDirectory(prefix='p09-store-tests-')
        cls.path = Path(cls.temp.name).resolve() / 'reference'
        cls.receipt = store.write_store(cls.root, cls.path, cls.tables, cls.settings)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def copy_store(self):
        directory = tempfile.TemporaryDirectory(prefix='p09-store-case-')
        self.addCleanup(directory.cleanup)
        target = Path(directory.name).resolve() / 'store'
        shutil.copytree(self.path, target)
        return target

    def refresh_hash(self, directory, relative):
        path = directory / 'verification.json'; data = json.loads(path.read_text())
        data['output_sha256'][relative] = digest_file(directory / relative)
        path.write_text(json.dumps(data, sort_keys=True, indent=2) + '\n')

    def test_actual_s00_roundtrip_is_read_only_and_parent_rows_are_preserved(self):
        before = {str(p.relative_to(self.path)): digest_file(p) for p in self.path.rglob('*') if p.is_file()}
        result = store.verify_store(self.root, self.path, self.tables, self.settings)
        after = {str(p.relative_to(self.path)): digest_file(p) for p in self.path.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(result['differences'], 0)
        self.assertTrue(result['foreign_keys_verified'])
        self.assertFalse(result['quality_accepted']); self.assertFalse(result['operational_approval'])
        for name in store.PRESERVED:
            self.assertEqual(store.digest(self.tables[name]), store.digest(self.inputs['parent_tables'][name]))
        self.assertTrue(all(r['received_or_opened_at'] is None for r in self.tables['eta_notification_delivery']))
        self.assertGreater(len(self.tables['p09_opening_audit']), 0)

    def test_exact_table_and_file_inventory(self):
        self.assertEqual(set(self.receipt['counts']), set(store.ORDER))
        self.assertEqual(len(store.ORDER), 35)
        copied = self.copy_store(); (copied / 'unregistered.txt').write_text('extra')
        with self.assertRaisesRegex(ValueError, 'inventory'): store.verify_store(self.root, copied, self.tables, self.settings)

    def test_jsonl_duckdb_and_parquet_byte_tampering_each_fails(self):
        for relative in ('jsonl/eta_adoption.jsonl', 'experiment.duckdb', 'parquet/eta_adoption.parquet'):
            with self.subTest(relative=relative):
                copied = self.copy_store()
                with (copied / relative).open('ab') as stream: stream.write(b'changed')
                with self.assertRaisesRegex(ValueError, 'file hash changed'):
                    store.verify_store(self.root, copied, self.tables, self.settings)

    def test_rehashed_jsonl_tampering_still_fails_against_source_reconstruction(self):
        copied = self.copy_store(); relative = 'jsonl/p09_opening_audit.jsonl'
        path = copied / relative; rows = [json.loads(x) for x in path.read_text().splitlines()]
        rows[0]['record']['known_at'] = True
        path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n'); self.refresh_hash(copied, relative)
        with self.assertRaisesRegex(ValueError, 'JSONL differs'):
            store.verify_store(self.root, copied, self.tables, self.settings)

    def test_rehashed_parquet_tampering_still_fails(self):
        copied = self.copy_store(); relative = 'parquet/p09_opening_audit.parquet'; path = copied / relative
        temporary = copied / 'replacement.parquet'
        con = duckdb.connect(':memory:')
        try:
            target_sql = str(temporary).replace("'", "''")
            con.execute("COPY (SELECT * REPLACE ('changed' AS record_sha256) FROM read_parquet(?)) TO '" + target_sql + "' (FORMAT PARQUET)",
                        [str(path)])
        finally: con.close()
        temporary.replace(path); self.refresh_hash(copied, relative)
        with self.assertRaisesRegex(ValueError, 'typed logical rows differ'):
            store.verify_store(self.root, copied, self.tables, self.settings)

    def test_rehashed_database_tampering_still_fails(self):
        copied = self.copy_store(); con = duckdb.connect(str(copied / 'experiment.duckdb'))
        try: con.execute("UPDATE p09_opening_audit SET record_sha256='changed'")
        finally: con.close()
        self.refresh_hash(copied, 'experiment.duckdb')
        with self.assertRaisesRegex(ValueError, 'typed logical rows differ'):
            store.verify_store(self.root, copied, self.tables, self.settings)

    def test_rehashed_missing_physical_constraint_fails(self):
        copied = self.copy_store(); con = duckdb.connect(str(copied / 'experiment.duckdb'))
        try:
            # An unconstrained auxiliary replacement has identical data/types;
            # exact physical constraint comparison must still reject it.
            con.execute('CREATE TABLE replacement AS SELECT * FROM p09_event_audit')
            con.execute('DROP TABLE p09_event_audit')
            con.execute('ALTER TABLE replacement RENAME TO p09_event_audit')
        finally: con.close()
        self.refresh_hash(copied, 'experiment.duckdb')
        with self.assertRaisesRegex(ValueError, 'physical constraints'):
            store.verify_store(self.root, copied, self.tables, self.settings)

    def test_forged_acceptance_claim_fails(self):
        copied = self.copy_store(); path = copied / 'verification.json'
        receipt = json.loads(path.read_text()); receipt['quality_accepted'] = True
        path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, 'claims differ'):
            store.verify_store(self.root, copied, self.tables, self.settings)

    def test_existing_destination_is_not_overwritten(self):
        copied = self.copy_store(); before = digest_file(copied / 'verification.json')
        with self.assertRaises(FileExistsError): store.write_store(self.root, copied, self.tables, self.settings)
        self.assertEqual(digest_file(copied / 'verification.json'), before)

    def test_injected_write_failure_leaves_no_partial_published_store(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'store'
            with patch.object(store.duckdb, 'connect', side_effect=RuntimeError('injected connection failure')):
                with self.assertRaisesRegex(RuntimeError, 'injected'):
                    store.write_store(self.root, target, self.tables, self.settings)
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_boolean_handling_seconds_does_not_pass_as_integer(self):
        changed = deepcopy(self.tables); changed['eta_action'][0]['handling_seconds'] = True
        with self.assertRaisesRegex(ValueError, 'Integer required'):
            store.write_store(self.root, self.path.parent / 'invalid', changed, self.settings)

    def test_other_tenant_cannot_take_over_action_or_followup(self):
        for name in ('eta_action', 'eta_action_followup'):
            with self.subTest(table=name):
                changed = deepcopy(self.tables); changed[name][0]['tenant_id'] = 'OTHER'
                with self.assertRaises(ValueError):
                    store.write_store(self.root, self.path.parent / 'invalid', changed, self.settings)

    def test_changed_settings_cannot_reuse_previous_store(self):
        changed = deepcopy(self.settings); changed['observation_cutoff_at'] = '2027-01-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError, 'settings differ'):
            store.verify_store(self.root, self.path, self.tables, changed)

    def test_store_does_not_mutate_input_simulation_or_parent(self):
        before = store.digest(self.inputs), store.digest(self.simulation)
        store.build_tables(self.root, self.inputs, self.simulation, datetime.now(timezone.utc).isoformat(), self.settings)
        self.assertEqual(before, (store.digest(self.inputs), store.digest(self.simulation)))


if __name__ == '__main__': unittest.main()
