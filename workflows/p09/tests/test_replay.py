import copy
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workflows.p09.replay import validate_receipt
from workflows.p09.run import require_passing_audit, run
from workflows.p09.registry import code_inventory
from workflows.p09 import run as runner


class ReceiptBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.receipt = {'schema_version': 1, 'execution_git_head': 'a' * 40,
                        'execution_checkout_changes_present': True,
                        'started_at_utc': '2026-01-02T00:00:00Z',
                        'materialized_at_utc': '2026-01-02T00:00:01Z',
                        'completed_at_utc': '2026-01-02T00:00:02Z'}
        self.registered = '2026-01-01T00:00:00Z'

    def test_valid_actual_clock_order(self):
        validate_receipt(self.receipt, self.registered)

    def test_backdated_start_is_rejected(self):
        self.receipt['started_at_utc'] = '2025-12-31T00:00:00Z'
        with self.assertRaisesRegex(ValueError, 'clock order'):
            validate_receipt(self.receipt, self.registered)

    def test_completion_before_materialization_is_rejected(self):
        self.receipt['completed_at_utc'] = '2026-01-02T00:00:00Z'
        with self.assertRaisesRegex(ValueError, 'clock order'):
            validate_receipt(self.receipt, self.registered)

    def test_missing_or_naive_clock_is_rejected(self):
        for value in (None, '2026-01-02T00:00:02'):
            with self.subTest(value=value):
                self.receipt['completed_at_utc'] = value
                with self.assertRaises(ValueError):
                    validate_receipt(self.receipt, self.registered)

    def test_future_completion_is_rejected(self):
        self.receipt['completed_at_utc'] = '9999-01-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError, 'clock order'):
            validate_receipt(self.receipt, self.registered)

    def test_invalid_git_identity_or_boolean_schema_is_rejected(self):
        for key, value in [('execution_git_head', 'not-a-commit'), ('schema_version', True),
                           ('execution_checkout_changes_present', 1)]:
            with self.subTest(key=key):
                receipt = copy.deepcopy(self.receipt); receipt[key] = value
                with self.assertRaisesRegex(ValueError, 'execution identity'):
                    validate_receipt(receipt, self.registered)

    def test_audit_requires_exact_complete_pass(self):
        require_passing_audit({'status': 'passed', 'failure_count': 0, 'failures': []})
        for record in [dict(status='passed_with_errors', failure_count=0, failures=[]),
                       dict(status='passed', failure_count=1, failures=[]),
                       dict(status='passed', failure_count=False, failures=[]),
                       dict(status='passed', failure_count=0, failures=[{'error': 'hidden'}])]:
            with self.subTest(record=record):
                with self.assertRaisesRegex(ValueError, 'audit failed'):
                    require_passing_audit(record)

    def test_run_refuses_existing_output_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output = root / 'evidence'; output.mkdir()
            marker = output / 'keep.txt'; marker.write_text('keep')
            with self.assertRaisesRegex(ValueError, 'already exists'):
                run(root, output)
            self.assertEqual(marker.read_text(), 'keep')

    def test_run_cannot_publish_into_frozen_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaisesRegex(ValueError, 'reserved P09 evidence'):
                run(root, root / 'reports/p08/new-output')
            self.assertFalse((root / 'reports').exists())

    def test_reserved_output_does_not_pollute_source_inventory_during_run(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary).resolve() / 'checkout'
            source = root / 'workflows/p09'; source.mkdir(parents=True)
            (source / 'code.py').write_text('registered code\n')
            before = code_inventory(root)
            registration = {'registration_sha256': 'a' * 64}

            def registered(checkout):
                self.assertEqual(code_inventory(checkout), before)
                return registration

            def store(checkout, destination, tables, settings):
                destination.mkdir()
                (destination / 'rows.json').write_text('[]\n')
                return {'status': 'passed'}

            def viewer(path, *args):
                path.write_text('<html>read-only</html>')
                return {'status': 'written_read_only_viewer'}

            values = {'verify_registration': registered, 'load_settings': lambda _: {},
                      'load_parent': lambda _: {'source_sha256': {}},
                      'simulate_workflow': lambda *args: {'summary': {}},
                      'score_workflow': lambda *args: {},
                      'audit_workflow': lambda *args: {'status': 'passed', 'failure_count': 0, 'failures': []},
                      'build_tables': lambda *args: {k: [] for k in ('eta_adoption', 'eta_action', 'eta_action_followup')},
                      'write_store': store, 'write_viewer': viewer}
            for name, value in values.items():
                stack.enter_context(patch.object(runner, name, side_effect=value))
            stack.enter_context(patch.object(runner.subprocess, 'check_output', side_effect=['a' * 40, b'']))
            output = source / 'evidence'
            run(root, output)
            self.assertTrue((output / 'run_receipt.json').is_file())
            self.assertEqual(code_inventory(root), before)
