"""Published sources and protocol boundaries fail closed before simulation."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from ds_m01.delivery_inputs import verify_settings, verify_parent, verify_freeze

ROOT = Path(__file__).resolve().parents[1]


class DeliveryInputTests(unittest.TestCase):
    def test_protocol_and_schema_mutations_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('config/p07.json', 'config/p06.json', 'contracts/fields.json'):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, target)
            verify_settings(root)
            for name in ('config/p07.json', 'config/p06.json', 'contracts/fields.json'):
                original = (root / name).read_bytes()
                (root / name).write_bytes(original + b' ')
                with self.assertRaises(ValueError):
                    verify_settings(root)
                (root / name).write_bytes(original)

    def test_parent_pin_is_required_before_loading_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'parent').mkdir()
            (root / 'parent/candidates.jsonl').write_text('tampered')
            with self.assertRaises(ValueError):
                verify_parent(root, {'source_report':'parent', 'source_report_sha256':{'candidates.jsonl':'0'*64}})

    def test_freeze_cannot_omit_models_or_claim_final_test(self):
        f = {'status':'frozen_before_delivery_run_and_final_test','frozen_at_utc':'2026-11-11T20:00:00Z',
             'final_test_evaluated':False,'july_assessment_previously_observed':True,
             'p08_scoring_implementation_pending':True,'artifact_sha256':{}}
        for changed in ({}, {'final_test_evaluated':True}, {'july_assessment_previously_observed':False},
                        {'p08_scoring_implementation_pending':False}, {'frozen_at_utc':'2026-11-11T20:00:00'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                verify_freeze(ROOT, dict(f, **changed))


if __name__ == '__main__':
    unittest.main()
