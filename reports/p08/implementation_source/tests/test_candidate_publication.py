"""Candidate evidence cannot publish from drifted inputs or partial writes."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ds_m01.candidate_model import CANDIDATE_IDS, CANDIDATE_PARAMETERS
from ds_m01.candidate_run import CANDIDATE_RUN_VERSION, OUTCOME_SOURCE, REQUIRED_PINNED_FILES, run, verify_inputs, verify_runtime
from ds_m01.common import digest_file


PROJECT = Path(__file__).resolve().parents[1]


def fixture_root(root):
    """Tiny retained receipt fixture; no M2 generator or local data required."""
    def write(relative, value):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    for relative in REQUIRED_PINNED_FILES:
        write(relative, '{}\n')
    for relative in ('config/m2.json', 'config/m3a.json', 'requirements.lock', 'requirements-models.lock'):
        write(relative, (PROJECT / relative).read_text())
    write(OUTCOME_SOURCE, '')
    write('reports/m3a/frozen_history.jsonl', '')
    feature_outputs = {name: digest_file(root / ('reports/m3b1/' + name))
                       for name in ('features.jsonl', 'matrices.jsonl', 'memberships.jsonl', 'summary.json', 'transformers.json')}
    write('reports/m3b1/run_receipt.json', json.dumps({'output_sha256': feature_outputs}))
    write('reports/m3a/run_receipt.json', json.dumps({'output_sha256': {'frozen_history.jsonl': digest_file(root / 'reports/m3a/frozen_history.jsonl')}}))
    write('reports/rebuild_report.json', json.dumps({'status': 'passed', 'files': [{'path': OUTCOME_SOURCE, 'original_sha256': digest_file(root / OUTCOME_SOURCE)}]}))
    settings = dict(batch_id='TEST-FIXTURE', candidate_definition_version=CANDIDATE_RUN_VERSION,
                    candidate_ids=list(CANDIDATE_IDS), candidate_parameters=CANDIDATE_PARAMETERS,
                    min_fit_trips=5, expected_retained_sha256={name: digest_file(root / name) for name in REQUIRED_PINNED_FILES},
                    expected_outcome_sha256=digest_file(root / OUTCOME_SOURCE),
                    folds=json.loads((root / 'config/m3a.json').read_text())['folds'])
    write('config/m3b2.json', json.dumps(settings))
    return settings


def fake_result():
    return dict(models=[], training_rows=[], predictions=[], summary={'accuracy_evaluated': False})


class CandidatePublicationTests(unittest.TestCase):
    def test_omitted_pin_and_changed_retained_evidence_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = fixture_root(root)
            omitted = dict(settings, expected_retained_sha256=dict(settings['expected_retained_sha256']))
            omitted['expected_retained_sha256'].pop('reports/m3b1/features.jsonl')
            with self.assertRaisesRegex(ValueError, 'complete retained input set'):
                verify_inputs(root, omitted)
            (root / 'reports/m3b1/features.jsonl').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'Retained candidate input changed'):
                verify_inputs(root, settings)

    def test_changed_outcome_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = fixture_root(root)
            (root / OUTCOME_SOURCE).write_text('{}\n')
            with self.assertRaisesRegex(ValueError, 'Outcome source differs'):
                verify_inputs(root, settings)

    def test_installed_numerical_versions_must_match_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture_root(root)
            with patch('ds_m01.candidate_run.importlib.metadata.version', return_value='0.0.0'), \
                    self.assertRaisesRegex(ValueError, 'Install requirements-models.lock'):
                verify_runtime(root)

    def test_settings_change_during_training_cannot_bind_new_hash_to_old_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture_root(root)
            settings_path = root / 'config/m3b2.json'
            output = root / 'out'
            def changing_run(*args):
                settings_path.write_text(settings_path.read_text() + '\n')
                return fake_result()
            with patch('ds_m01.candidate_run.load_retained_inputs', return_value={}), \
                    patch('ds_m01.candidate_run.verify_runtime', return_value={}), \
                    patch('ds_m01.candidate_run.train_and_predict', side_effect=changing_run), \
                    self.assertRaisesRegex(ValueError, 'settings changed during training'):
                run(root, settings_path, output)
            self.assertFalse(output.exists())

    def test_code_change_during_training_cannot_bind_new_hash_to_old_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture_root(root)
            changed = [False]
            def hash_with_drift(path):
                if changed[0] and Path(path).name == 'candidate_run.py':
                    return '0' * 64
                return digest_file(path)
            def changing_run(*args):
                changed[0] = True
                return fake_result()
            with patch('ds_m01.candidate_run.load_retained_inputs', return_value={}), \
                    patch('ds_m01.candidate_run.verify_runtime', return_value={}), \
                    patch('ds_m01.candidate_run.digest_file', side_effect=hash_with_drift), \
                    patch('ds_m01.candidate_run.train_and_predict', side_effect=changing_run), \
                    self.assertRaisesRegex(ValueError, 'implementation changed during training'):
                run(root, root / 'config/m3b2.json', root / 'out')
            self.assertFalse((root / 'out').exists())

    def test_source_change_during_training_prevents_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture_root(root)
            def changing_run(*args):
                (root / OUTCOME_SOURCE).write_text('{}\n')
                return fake_result()
            with patch('ds_m01.candidate_run.load_retained_inputs', return_value={}), \
                    patch('ds_m01.candidate_run.verify_runtime', return_value={}), \
                    patch('ds_m01.candidate_run.train_and_predict', side_effect=changing_run), \
                    self.assertRaisesRegex(ValueError, 'Outcome source differs'):
                run(root, root / 'config/m3b2.json', root / 'out')
            self.assertFalse((root / 'out').exists())

    def test_interrupted_write_publishes_no_partial_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture_root(root)
            with patch('ds_m01.candidate_run.load_retained_inputs', return_value={}), \
                    patch('ds_m01.candidate_run.verify_runtime', return_value={}), \
                    patch('ds_m01.candidate_run.train_and_predict', return_value=fake_result()), \
                    patch('ds_m01.candidate_run.write_jsonl', side_effect=OSError('Injected disk-write failure')), \
                    self.assertRaisesRegex(OSError, 'Injected disk-write failure'):
                run(root, root / 'config/m3b2.json', root / 'out')
            self.assertFalse((root / 'out').exists())
            self.assertEqual(list(root.glob('.m3b2-stage-*')), [])

    def test_complete_publication_binds_settings_code_inputs_and_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture_root(root)
            with patch('ds_m01.candidate_run.load_retained_inputs', return_value={}), \
                    patch('ds_m01.candidate_run.verify_runtime', return_value={'test': 'fixture'}), \
                    patch('ds_m01.candidate_run.train_and_predict', return_value=fake_result()):
                run(root, root / 'config/m3b2.json', root / 'out')
            receipt = json.loads((root / 'out/run_receipt.json').read_text())
            self.assertEqual(receipt['settings_sha256'], digest_file(root / 'config/m3b2.json'))
            self.assertEqual(receipt['package_versions'], {'test': 'fixture'})
            self.assertEqual(len(receipt['output_sha256']), 4)
            self.assertIn(OUTCOME_SOURCE, receipt['input_sha256'])
            for name, expected in receipt['output_sha256'].items():
                self.assertEqual(digest_file(root / 'out' / name), expected)

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / 'out'
            output.mkdir()
            marker = output / 'existing.txt'
            marker.write_text('retained evidence')
            with self.assertRaises(FileExistsError):
                run(root, root / 'missing-settings.json', output)
            self.assertEqual(marker.read_text(), 'retained evidence')


if __name__ == '__main__':
    unittest.main()
