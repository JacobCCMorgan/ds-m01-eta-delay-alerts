import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workflows.p09 import registry


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.module = self.root / 'workflows/p09/registry.py'
        self.module.parent.mkdir(parents=True)
        self.module.write_text('registered verifier fixture')
        self.parent = self.root / 'reports/p08/frozen.json'
        self.parent.parent.mkdir(parents=True)
        self.parent.write_text('{"unchanged":true}')
        self.record = {'schema_version': 1, 'base_public_commit': registry.BASE,
                       'registered_at_utc': '2026-01-01T00:00:00Z',
                       'scope': {'post_p08_outcome_exposure': True, 'new_untouched_final_test': False,
                                 'model_refitted': False, 'quality_accepted': False, 'operational_approval': False},
                       'parent_sha256': {'reports/p08/frozen.json': registry.digest_file(self.parent)},
                       'p08_source_sha256': {}, 'p08_historical_report_sha256': {},
                       'code_sha256': registry.code_inventory(self.root)}
        self.write()
        self.mock = patch.object(registry, '__file__', str(self.module))
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def write(self):
        (self.root / registry.REGISTRATION).write_text(json.dumps(self.record))

    def test_exact_registered_extension_preserves_parent(self):
        self.assertEqual(registry.verify_registration(self.root)['parent_file_count'], 1)
        self.assertEqual(self.parent.read_text(), '{"unchanged":true}')

    def test_changed_parent_fails(self):
        self.parent.write_text('{"unchanged":false}')
        with self.assertRaisesRegex(ValueError, 'Frozen P08 parent changed'):
            registry.verify_registration(self.root)

    def test_new_unregistered_code_fails(self):
        (self.module.parent / 'unregistered.py').write_text('pass')
        with self.assertRaisesRegex(ValueError, 'inventory differs'):
            registry.verify_registration(self.root)

    def test_changed_registered_code_fails(self):
        self.module.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'inventory differs'):
            registry.verify_registration(self.root)

    def test_wrong_checkout_module_fails(self):
        with patch.object(registry, '__file__', str(self.root / 'other.py')):
            with self.assertRaisesRegex(ValueError, 'another checkout'):
                registry.verify_registration(self.root)

    def test_future_or_naive_registration_fails(self):
        for date in ('9999-01-01T00:00:00Z', '2026-01-01T00:00:00'):
            with self.subTest(date=date):
                self.record['registered_at_utc'] = date
                self.write()
                with self.assertRaisesRegex(ValueError, 'registration time'):
                    registry.verify_registration(self.root)

    def test_untouched_test_or_acceptance_claim_fails(self):
        original = copy.deepcopy(self.record)
        for key in ('quality_accepted', 'operational_approval', 'new_untouched_final_test'):
            with self.subTest(key=key):
                self.record = copy.deepcopy(original)
                self.record['scope'][key] = True
                self.write()
                with self.assertRaisesRegex(ValueError, 'overclaims'):
                    registry.verify_registration(self.root)

    def test_path_escape_is_not_a_parent_binding(self):
        self.record['parent_sha256'] = {'../outside': 'a' * 64}
        self.write()
        with self.assertRaisesRegex(ValueError, 'Invalid frozen'):
            registry.verify_registration(self.root)

    def test_evidence_does_not_change_code_registration(self):
        out = self.module.parent / 'evidence'
        out.mkdir()
        (out / 'summary.json').write_text('{}')
        self.assertEqual(registry.verify_registration(self.root)['workflow_file_count'], 1)

    def test_unregistered_old_source_is_not_silently_ignored(self):
        source = self.root / 'src/ds_m01/unregistered.py'
        source.parent.mkdir(parents=True)
        source.write_text('pass')
        with self.assertRaisesRegex(ValueError, 'Original P08 source inventory'):
            registry.verify_registration(self.root)

    def test_new_historical_report_is_not_silently_ignored(self):
        report = self.root / 'reports/local/extra.json'
        report.parent.mkdir(parents=True)
        report.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'Original P08 historical report inventory'):
            registry.verify_registration(self.root)
