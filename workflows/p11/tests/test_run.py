"""The outer receipt rejects failed, partial or overclaimed sub-results."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workflows.p11 import run


def recovery_result():
    names = ('after_database', 'after_first_parquet', 'after_validation',
             'after_generation_rename', 'after_pointer_replace')
    return {'status': 'passed_abrupt_failure_and_recovery', 'faults_executed': 5,
            'database_and_parquet_executed': True, 'subprocess_abrupt_exit': True,
            'cases': [{'fault': name, 'process_exit': 86,
                       'old_database_and_all_parquet_preserved': True,
                       'recovery_mutated_artifacts': False,
                       'clean_and_recovered_logical_tables_equal': True} for name in names]}


def revision_result():
    return {'status': 'passed_revision_contract', 'canonical_events': 2, 'retained_revisions': 4,
            'source_duplicate_rows': 1, 'http_server_closed': True, 'snapshot_witnesses': [{}] * 10,
            'store': {'status': 'passed_typed_revision_store', 'actual_constraint_rejections': [1] * 4,
                      'fresh_connection_exact': True, 'all_column_parquet_exact': True,
                      'snapshot_revision_foreign_key_enforced': True,
                      'retained_revision_replay': {'all_typed_payloads_unchanged': True}}}


def rebuild_result():
    one = {'status': 'passed_full_frozen_m2_rebuild', 'materialized_schemas': 28,
           'populated_data_tables': 17, 'typed_empty_tables': list(range(11)),
           'tables': {str(i): {} for i in range(28)},
           'warehouse_checks': [{'passed': True} for _ in range(27)],
           'source_archive': {'status': 'passed_lossless_owned_source_archive',
                              'all_archives_verified_before_any_plain_stream_removed': True,
                              'files': {str(i): {'full_decode_byte_identity_verified': True} for i in range(21)}},
           'historical_oracle': {'status': 'passed', 'raw_files_compared': 13, 'processed_files_compared': 7}}
    return {'status': 'passed_two_full_frozen_m2_rebuilds', 'runs': [one, copy.deepcopy(one)],
            'repeatability': {'status': 'passed', 'canonical_relations_compared': 28,
                              'schema_count_and_all_row_digests_equal': True}}


class ResultGateTests(unittest.TestCase):
    def test_failed_status_is_never_promoted_to_success(self):
        for checker, value in ((run.check_recovery, recovery_result()),
                               (run.check_revision, revision_result()),
                               (run.check_rebuild, rebuild_result())):
            with self.subTest(checker=checker), self.assertRaises(ValueError):
                checker({**value, 'status': 'failed'})

    def test_missing_crash_or_boolean_coercion_is_rejected(self):
        for change in ({'faults_executed': 4}, {'subprocess_abrupt_exit': 1},
                       {'cases': recovery_result()['cases'][:-1]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                run.check_recovery({**recovery_result(), **change})

    def test_mutated_recovery_or_changed_rows_are_rejected(self):
        for key in ('recovery_mutated_artifacts', 'clean_and_recovered_logical_tables_equal'):
            value = recovery_result(); value['cases'][0][key] = not value['cases'][0][key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                run.check_recovery(value)

    def test_partial_revision_or_store_check_is_rejected(self):
        value = revision_result(); value['store']['all_column_parquet_exact'] = False
        with self.assertRaises(ValueError): run.check_revision(value)
        value = revision_result(); value['snapshot_witnesses'].pop()
        with self.assertRaises(ValueError): run.check_revision(value)

    def test_one_rebuild_or_failed_repeatability_is_rejected(self):
        value = rebuild_result(); value['runs'].pop()
        with self.assertRaises(ValueError): run.check_rebuild(value)
        value = rebuild_result(); value['repeatability']['schema_count_and_all_row_digests_equal'] = False
        with self.assertRaises(ValueError): run.check_rebuild(value)

    def test_partial_historical_oracle_is_rejected(self):
        value = rebuild_result(); value['runs'][1]['historical_oracle']['processed_files_compared'] = 6
        with self.assertRaises(ValueError): run.check_rebuild(value)

    def test_failed_warehouse_invariant_is_rejected(self):
        value = rebuild_result(); value['runs'][1]['warehouse_checks'][0]['passed'] = False
        with self.assertRaisesRegex(ValueError, 'warehouse invariants'): run.check_rebuild(value)

    def test_missing_or_unverified_source_archive_is_rejected(self):
        value = rebuild_result(); value['runs'][0]['source_archive']['files'].pop('0')
        with self.assertRaisesRegex(ValueError, 'source bytes'): run.check_rebuild(value)
        value = rebuild_result(); value['runs'][0]['source_archive']['files']['0']['full_decode_byte_identity_verified'] = False
        with self.assertRaisesRegex(ValueError, 'source bytes'): run.check_rebuild(value)

    def test_output_inside_checkout_or_preexisting_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            with self.assertRaisesRegex(ValueError, 'new P11 output'):
                run.verify(root, root / 'generated')
            with self.assertRaisesRegex(ValueError, 'new P11 output'):
                run.verify(root / 'repo', root)

    def test_failed_witness_leaves_diagnostics_and_no_success_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td).resolve(); root = parent / 'repo'; root.mkdir()
            output = parent / 'output'
            with patch.object(run, '__file__', str(root / 'workflows/p11/run.py')), \
                 patch.object(run, 'verify_registration', return_value={'registration_sha256': 'a' * 64}), \
                 patch('workflows.p11.recovery.verify_recovery', return_value={'status': 'failed'}):
                with self.assertRaises(ValueError): run.verify(root, output)
            self.assertEqual(json.loads((output / 'failure.json').read_text())['status'], 'failed_no_success_receipt')
            self.assertFalse((output / 'receipt.json').exists())
