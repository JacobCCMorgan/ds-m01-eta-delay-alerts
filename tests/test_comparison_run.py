"""Safety of the public comparison runner and its frozen decision protocol."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ds_m01.comparison_run import run

PROJECT = Path(__file__).resolve().parents[1]


class ComparisonRunnerTests(unittest.TestCase):
    def test_existing_directory_and_its_evidence_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            existing = Path(folder) / 'accepted'
            existing.mkdir()
            marker = existing / 'evidence'
            marker.write_text('previously accepted')
            with self.assertRaises(FileExistsError):
                run(PROJECT, existing)
            self.assertEqual(marker.read_text(), 'previously accepted')

    def test_relaxed_gate_is_rejected_before_any_outcome_is_read(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'config').mkdir()
            protocol = json.loads((PROJECT / 'config/m3b3.json').read_text())
            protocol['selection']['minimum_relative_anchor_mae_reduction'] = 0
            (root / 'config/m3b3.json').write_text(json.dumps(protocol))
            with patch('ds_m01.comparison_inputs.load_comparison_inputs') as load:
                with self.assertRaisesRegex(ValueError, 'pre-scoring'):
                    run(root, 'out')
            load.assert_not_called()
            self.assertFalse((root / 'out').exists())

    def test_file_failure_after_writing_metrics_leaves_no_partial_publication(self):
        # Isolate filesystem publication from the independently tested scoring path.
        rows = [{'fold_id': f'F0{1 + i % 4}'} for i in range(5163)]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch('ds_m01.comparison_inputs.load_comparison_inputs', return_value=(rows, {})), \
                 patch('ds_m01.comparison_run.compare', return_value=({}, [{'value': 1}], {})), \
                 patch('csv.DictWriter.writerows', side_effect=OSError('disk full')):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    run(PROJECT, root / 'results')
            self.assertEqual(list(root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
