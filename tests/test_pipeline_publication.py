"""Full-scale streaming must preserve old outputs if a build fails midway."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_build_invariants as fixture_cases
from ds_m01 import pipeline


class PipelinePublicationTests(unittest.TestCase):
    def fixture(self, root):
        case = fixture_cases.BuildInvariantTests()
        case.setUp()
        raw = root / 'data/raw'
        raw.mkdir(parents=True)
        for name in pipeline.RAW_TABLES:
            (raw / f'{name}.jsonl').write_text(
                ''.join(json.dumps(row) + '\n' for row in case.tables.get(name, [])),
                encoding='utf-8')
        return case

    def test_failed_history_build_does_not_publish_partial_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = self.fixture(root)
            pipeline.build(root, case.config)
            before = {p.name: p.read_bytes() for p in (root / 'data/processed').iterdir()}
            original = pipeline._derived_provenance

            def fail_after_history_write(table, *args, **kwargs):
                if table == 'eta_history_reference':
                    raise RuntimeError('injected mid-build failure')
                return original(table, *args, **kwargs)

            with patch.object(pipeline, '_derived_provenance', side_effect=fail_after_history_write):
                with self.assertRaisesRegex(RuntimeError, 'injected mid-build failure'):
                    pipeline.build(root, case.config)
            after = {p.name: p.read_bytes() for p in (root / 'data/processed').iterdir()}
            self.assertEqual(before, after)
            self.assertEqual(list(root.glob('.pipeline-stage-*')), [])

    def test_streamed_sources_are_canonical_and_m2_provenance_is_identified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = self.fixture(root)
            case.config.update(source_dataset_id='DS-M01-M2-2026', pipeline_version='0.2.0',
                               summary_filename='m2_summary.json')
            pipeline.build(root, case.config)
            path = root / 'data/processed/provenance_record.jsonl'
            lines = path.read_text(encoding='utf-8').splitlines()
            rows = [json.loads(line) for line in lines]
            for line, row in zip(lines, rows):
                self.assertEqual(line, json.dumps(row, ensure_ascii=False, sort_keys=True,
                                                 separators=(',', ':'), allow_nan=False))
            derived = [r for r in rows if r.get('origin_type') == 'derived']
            self.assertTrue(derived)
            for row in derived:
                self.assertEqual(row['source_dataset_id'], 'DS-M01-M2-2026')
                self.assertEqual(row['record_version'], '0.2.0')
                self.assertTrue(all(v['source_dataset_ids'] == ['DS-M01-M2-2026']
                                    for v in row['field_origins'].values()))
            self.assertTrue((root / 'reports/m2_summary.json').is_file())
            self.assertFalse((root / 'reports/m1_summary.json').exists())
            self.assertEqual(list(root.glob('.pipeline-stage-*')), [])


if __name__ == '__main__':
    unittest.main()
