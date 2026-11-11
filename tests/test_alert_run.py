"""Publication boundaries without reading retained outcomes or sweeping policies."""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ds_m01 import alert_run


class AlertRunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'repo'
        self.inputs = Path(self.temporary.name) / 'inputs'
        self.parent = Path(self.temporary.name) / 'exports'
        for directory in (self.root, self.inputs, self.parent):
            directory.mkdir()
        self.output = self.parent / 'result'

    @contextmanager
    def pipeline(self, *, select=None, store=None, clock=None):
        """Isolate orchestration while retaining real staging and report writes."""
        settings = {
            'source_report': 'reports/frozen_parent',
            'base_public_commit': 'a' * 40,
            'input_sha256': {}, 'source_report_sha256': {},
            'quality_limits': ['research_only'],
            'release': {'canonical_tables': ['eta_prediction'],
                        'remaining_reserved_tables': ['eta_alert']},
        }
        inputs = {
            'opportunities': [{'tenant_id': 'T', 'trip_id': 'TRIP', 'snapshot_id': 'S'}],
            'targets': [{'tenant_id': 'T', 'trip_id': 'TRIP', 'eligibility': 'eligible'}],
            'registry': [], 'context': {'fixture': True},
        }
        selected = {
            'status': 'disabled', 'rule': {'enabled': False, 'threshold': None},
            'trials': [{'enabled': False}], 'selected_metrics': {'fixture': True},
            'selection_manifest': {'research_only': True},
            'simulation': {'decisions': [], 'candidates': [], 'episodes': [], 'summary': {}},
        }
        generated = []

        def build(opportunities, selection, registry, generated_at, supplied_settings):
            generated.append(generated_at)
            return {'eta_prediction': [{'generated_at': generated_at}], 'eta_alert': []}

        def write_store(root, destination, tables, settings):
            destination.mkdir()
            (destination / 'fixture.json').write_text(json.dumps(tables))
            return {'status': 'passed', 'fixture': True}

        dependencies = {
            'verify_settings': Mock(return_value=settings),
            'code_hashes': Mock(return_value={'fixture.py': 'b' * 64}),
            'verify_calibration': Mock(return_value={'status': 'passed'}),
            'prepare_inputs': Mock(return_value=inputs),
            'select_policy': Mock(side_effect=select or (lambda *args: selected)),
            'build_tables': Mock(side_effect=build),
            'write_store': Mock(side_effect=store or write_store),
            'verify_input_files': Mock(), 'verify_source_report': Mock(),
            'digest_file': Mock(return_value=alert_run.PROTOCOL_SHA256),
            'version': Mock(return_value='fixture'),
        }
        with ExitStack() as stack:
            for name, dependency in dependencies.items():
                stack.enter_context(patch.object(alert_run, name, dependency))
            stack.enter_context(patch.object(alert_run.subprocess, 'check_output', return_value='a' * 40 + '\n'))
            if clock is not None:
                stack.enter_context(patch.object(alert_run, 'datetime', clock))
            yield dependencies, selected, generated

    def test_existing_output_stops_before_source_reads_and_preserves_evidence(self):
        self.output.mkdir()
        evidence = self.output / 'previous.json'
        evidence.write_bytes(b'previous evidence\n')
        with self.pipeline() as (dependencies, _, _):
            with self.assertRaisesRegex(ValueError, 'Output already exists'):
                alert_run.run(self.root, self.inputs, self.output)
            for dependency in dependencies.values():
                dependency.assert_not_called()
        self.assertEqual(evidence.read_bytes(), b'previous evidence\n')
        self.assertEqual(list(self.parent.iterdir()), [self.output])

    def test_dangling_output_symlink_stops_before_source_reads(self):
        missing = self.parent / 'missing-target'
        self.output.symlink_to(missing, target_is_directory=True)
        self.assertFalse(self.output.exists())
        with self.pipeline() as (dependencies, _, _):
            with self.assertRaisesRegex(ValueError, 'Output already exists'):
                alert_run.run(self.root, self.inputs, self.output)
            for dependency in dependencies.values():
                dependency.assert_not_called()
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(self.output.readlink(), missing)
        self.assertFalse(missing.exists())
        self.assertEqual(list(self.parent.iterdir()), [self.output])

    def test_store_failure_removes_partial_stage_and_publishes_nothing(self):
        attempted = []

        def failed_store(root, destination, tables, settings):
            attempted.append(destination)
            # Failure occurs after reports and a partial store exist on disk.
            self.assertTrue((destination.parent / 'selected_policy.json').is_file())
            destination.mkdir()
            (destination / 'partial.duckdb').write_bytes(b'incomplete store')
            raise RuntimeError('injected store failure')

        with self.pipeline(store=failed_store) as (dependencies, _, _):
            with self.assertRaisesRegex(RuntimeError, 'injected store failure'):
                alert_run.run(self.root, self.inputs, self.output)
            dependencies['write_store'].assert_called_once()
            dependencies['verify_input_files'].assert_not_called()
        self.assertEqual(len(attempted), 1)
        self.assertFalse(attempted[0].parent.exists())
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_canonical_generation_clock_follows_completed_selection(self):
        start = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
        after_selection = start + timedelta(minutes=10)
        time = {'now': start}
        events = []
        clock = Mock()

        def now(zone):
            self.assertIs(zone, timezone.utc)
            events.append(('clock', time['now']))
            return time['now']

        clock.now.side_effect = now
        with self.pipeline(clock=clock) as (dependencies, selected, generated):
            def select(*args):
                events.append(('selection_started', time['now']))
                time['now'] = after_selection
                events.append(('selection_completed', time['now']))
                return selected

            dependencies['select_policy'].side_effect = select
            alert_run.run(self.root, self.inputs, self.output)
        receipt = json.loads((self.output / 'run_receipt.json').read_text())
        stored = json.loads((self.output / 'store/fixture.json').read_text())
        self.assertEqual(receipt['started_at_utc'], start.isoformat())
        self.assertEqual(generated, [after_selection.isoformat()])
        self.assertEqual(receipt['canonical_generated_at'], after_selection.isoformat())
        self.assertEqual(receipt['selection_completed_at_utc'], after_selection.isoformat())
        self.assertEqual(stored['eta_prediction'][0]['generated_at'], after_selection.isoformat())
        self.assertGreater(events.index(('clock', after_selection)),
                           events.index(('selection_completed', after_selection)))
        self.assertEqual(list(self.parent.iterdir()), [self.output])


if __name__ == '__main__':
    unittest.main()
