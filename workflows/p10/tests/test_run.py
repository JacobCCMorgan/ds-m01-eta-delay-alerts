"""Reject incomplete native proof and preserve failed/existing executions."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workflows.p10 import run


def proof():
    rows = {'input_rows': 1, 'returned_rows': 1, 'input_normalized_sha256': 'a' * 64,
            'returned_normalized_sha256': 'a' * 64, 'exact': True}
    pg = dict(status='passed', commit_completed=True, fresh_connection_exact_readback=True,
              uncommitted_changes_hidden_from_second_connection=True, owned_schema_cleanup_verified=True,
              same_key_changed_payload_rejected=True, operational_export={str(n): {} for n in range(11)},
              groups={name: dict(table_count=count, tables={str(n): deepcopy(rows) for n in range(count)},
                                 failed_writes_rolled_back=True, exact_retry_preserved=True)
                      for name, count in (('operational', 11), ('p09', 35))})
    http = dict(status='passed', source_unchanged=True, postgres_http_projection_checked=True,
                comparisons={'snapshot_vs_http': {}, 'snapshot_vs_postgres_http': {}},
                transport=dict(full_payload_set_equal=True, event_and_available_clocks_equal=True,
                               tenant_event_revision_identity_preserved=True, server_closed=True,
                               transport='actual_loopback_http', retries=1))
    lifecycle = dict(status='passed_owned_cluster_lifecycle', readiness_verified=True,
                     owned_data_directory_confirmed=True, graceful_shutdown=True, listener_closed=True)
    return pg, http, lifecycle


class NativeAcceptanceTests(unittest.TestCase):
    def test_complete_proof_is_required(self):
        run.require_complete(*proof())
        for index, name in ((0, 'fresh_connection_exact_readback'), (0, 'commit_completed'),
                            (0, 'owned_schema_cleanup_verified'), (1, 'source_unchanged'),
                            (1, 'postgres_http_projection_checked'), (2, 'listener_closed'),
                            (2, 'graceful_shutdown')):
            with self.subTest(proof=name):
                values = proof(); values[index][name] = False
                with self.assertRaises(ValueError):
                    run.require_complete(*values)

    def test_matching_row_counts_do_not_hide_changed_native_values(self):
        pg, http, lifecycle = proof()
        pg['groups']['p09']['tables']['0']['returned_normalized_sha256'] = 'b' * 64
        with self.assertRaisesRegex(ValueError, 'typed rows'):
            run.require_complete(pg, http, lifecycle)

    def test_missing_table_or_combined_path_fails(self):
        pg, http, lifecycle = proof()
        pg['groups']['operational']['tables'].pop('0')
        with self.assertRaisesRegex(ValueError, 'row comparison incomplete'):
            run.require_complete(pg, http, lifecycle)
        pg, http, lifecycle = proof()
        http['comparisons'].pop('snapshot_vs_postgres_http')
        with self.assertRaisesRegex(ValueError, 'Three source paths'):
            run.require_complete(pg, http, lifecycle)

    def test_simulated_transport_cannot_be_native_success(self):
        pg, http, lifecycle = proof()
        http['transport']['transport'] = 'mock'
        with self.assertRaisesRegex(ValueError, 'Actual HTTP'):
            run.require_complete(pg, http, lifecycle)

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'repo'; root.mkdir()
            output = Path(temporary) / 'existing'; output.mkdir()
            marker = output / 'previous.json'; marker.write_text('previous evidence')
            with self.assertRaisesRegex(ValueError, 'fresh output'):
                run.verify_native(root, output)
            self.assertEqual(marker.read_text(), 'previous evidence')

    def test_output_inside_checkout_is_rejected_before_services(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(run, 'ownedcluster') as cluster:
                with self.assertRaisesRegex(ValueError, 'outside the checkout'):
                    run.verify_native(root, root / 'run')
                cluster.assert_not_called()

    def test_generation_failure_retains_diagnostic_without_success_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = base / 'repo'; root.mkdir()
            (root / 'config').mkdir(); (root / 'config/m2.json').write_text('{}')
            (root / 'workflows/p10').mkdir(parents=True)
            (root / 'workflows/p10/settings.json').write_text('{}')
            output = base / 'failed'
            with patch.object(run, 'verify_registration', return_value={'status': 'test'}), \
                    patch.object(run, 'generate', side_effect=ValueError('fixture failure')):
                with self.assertRaisesRegex(ValueError, 'fixture failure'):
                    run.verify_native(root, output)
            self.assertEqual(json.loads((output / 'failure.json').read_text())['status'], 'failed')
            self.assertFalse((output / 'run_receipt.json').exists())
            self.assertFalse((output / 'native_integration.json').exists())


class HistoricalOracleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[3]
        old = json.loads((self.root / 'reports/rebuild_report.json').read_text())
        self.entries = {entry['path']: entry for entry in old['files']}
        self.manifest = {'files': [{'path': name, 'sha256': entry['original_sha256']}
                                  for name, entry in self.entries.items() if name.startswith('data/raw/')]}
        mapping = {'snapshots': 'eta_snapshot', 'labels': 'eta_snapshot_label',
                   'split_assignments': 'eta_split_assignment', 'memberships': 'trip_membership'}
        self.http = {'projections': {mode: {'tables': {
            name: {'serialized_jsonl_sha256': self.entries['data/processed/' + filename + '.jsonl']['original_sha256']}
            for name, filename in mapping.items()}} for mode in ('snapshot', 'http')}}

    def test_missing_or_rehashed_raw_source_is_rejected(self):
        for change in ('missing', 'changed'):
            manifest = deepcopy(self.manifest)
            if change == 'missing':
                manifest['files'].pop()
            else:
                manifest['files'][0]['sha256'] = 'f' * 64
            with self.assertRaisesRegex(ValueError, 'historical M2 bytes'):
                run.check_historical_oracle(self.root, manifest, self.http)

    def test_mutually_matching_new_projections_do_not_override_historical_oracle(self):
        for mode in self.http['projections'].values():
            mode['tables']['labels']['serialized_jsonl_sha256'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'Historical M2 projection bytes differ'):
            run.check_historical_oracle(self.root, self.manifest, self.http)

    def test_oracle_scope_does_not_claim_full_warehouse_rebuild(self):
        result = run.check_historical_oracle(self.root, self.manifest, self.http)
        self.assertEqual((result['raw_file_count'], result['projection_file_count']), (13, 4))
        self.assertFalse(result['full_scale_history_lineage_warehouse_rebuilt'])


if __name__ == '__main__':
    unittest.main()
