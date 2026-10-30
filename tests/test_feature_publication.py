"""Publication/frozen-input guards using independent feature-only fixtures."""

import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from ds_m01.common import digest_file
from ds_m01.feature_run import FEATURE_INPUT_FILES, run
from test_feature_run import source_rows, write_inputs


PROJECT = Path(__file__).resolve().parents[1]


def workspace(root):
    write_inputs(root, source_rows())
    for relative in ('config/m2.json', 'config/m3a.json', 'contracts/m3b1_features.json'):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PROJECT / relative, target)
    (root / 'reports').mkdir()
    receipt = {'status': 'passed', 'scope': 'Independent hand-authored test fixture, not the M2 dataset.',
               'files': [{'path': path, 'original_sha256': digest_file(root / path)}
                         for path in FEATURE_INPUT_FILES]}
    (root / 'reports/rebuild_report.json').write_text(json.dumps(receipt))
    settings = json.loads((PROJECT / 'config/m3b1.json').read_text())
    settings['expected_m2_rebuild_receipt_sha256'] = digest_file(root / 'reports/rebuild_report.json')
    (root / 'config/m3b1.json').write_text(json.dumps(settings))
    return root / 'config/m3b1.json'


class FeaturePublicationTests(unittest.TestCase):
    def test_two_runs_publish_identical_core_artifacts_without_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = workspace(root)
            for name in ('first', 'second'):
                run(root, config, root / name)
            first = json.loads((root / 'first/run_receipt.json').read_text())
            second = json.loads((root / 'second/run_receipt.json').read_text())
            self.assertEqual(first['output_sha256'], second['output_sha256'])
            self.assertEqual(set(first['input_sha256']), set(FEATURE_INPUT_FILES))
            for relative, digest in first['input_sha256'].items():
                self.assertEqual(digest_file(root / relative), digest)
            self.assertFalse((root / 'data/raw/eta_trip_outcome.jsonl').exists())

    def test_input_drift_fails_without_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = workspace(root)
            path = root / FEATURE_INPUT_FILES[2]
            path.write_text(path.read_text() + '\n')
            with self.assertRaises(ValueError):
                run(root, config, root / 'out')
            self.assertFalse((root / 'out').exists())

    def test_frozen_configuration_and_receipt_changes_fail(self):
        for relative in ('config/m2.json', 'config/m3a.json', 'reports/rebuild_report.json'):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = workspace(root)
                path = root / relative
                path.write_text(path.read_text() + '\n')
                with self.assertRaises(ValueError):
                    run(root, config, root / 'out')
                self.assertFalse((root / 'out').exists())

    def test_fold_or_feature_definition_changes_fail(self):
        for kind in ('fold', 'version'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = workspace(root)
                settings = json.loads(config.read_text())
                if kind == 'fold':
                    settings['folds'] = settings['folds'][:1]
                else:
                    settings['feature_definition_version'] = 'unreviewed-v2'
                config.write_text(json.dumps(settings))
                with self.assertRaises(ValueError):
                    run(root, config, root / 'out')

    def test_midwrite_failure_leaves_no_published_or_staging_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = workspace(root)
            with patch('ds_m01.feature_run.write_jsonl', side_effect=OSError('injected write failure')):
                with self.assertRaises(OSError):
                    run(root, config, root / 'out')
            self.assertFalse((root / 'out').exists())
            self.assertEqual(list(root.glob('.m3b1-stage-*')), [])


if __name__ == '__main__':
    unittest.main()
