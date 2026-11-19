"""Registration must bind all executable inputs and the actual parent tree."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workflows.p11 import registry as r


class RegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name, content in [('parent.txt', 'immutable parent'),
                              ('workflows/p10/registration.json', 'parent registration'),
                              ('workflows/p11/registry.py', 'fixture verifier'),
                              ('workflows/p11/settings.json', '{}'),
                              (r.CI_WORKFLOW, 'workflow fixture')]:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.parent = {'parent.txt': r.digest_file(self.root / 'parent.txt')}
        self.record = {
            'schema_version': 1, 'protocol_version': r.PROTOCOL_VERSION,
            'base_public_commit': r.BASE, 'mutable_parent_paths': sorted(r.MUTABLE_PARENT_PATHS),
            'scope': dict(r.SCOPE), 'registered_at_utc': datetime.now(timezone.utc).isoformat(),
            'parent_sha256': dict(self.parent), 'code_sha256': r.code_inventory(self.root),
            'p10_registration_sha256': r.digest_file(self.root / 'workflows/p10/registration.json'),
        }
        self.write()
        for name, value in [('__file__', str(self.root / 'workflows/p11/registry.py')),
                            ('parent_inventory', lambda root: dict(self.parent)),
                            ('verify_p10', lambda root: {'status': 'prior chain validated'})]:
            context = patch.object(r, name, value)
            context.start(); self.addCleanup(context.stop)

    def write(self):
        (self.root / r.REGISTRATION).write_text(json.dumps(self.record))

    def test_exact_registration_preserves_prior_chain_verification(self):
        result = r.verify_registration(self.root)
        self.assertEqual(result['parent_file_count'], 1)
        self.assertEqual(result['p10_registration']['status'], 'prior chain validated')

    def test_omitted_parent_cannot_be_excused_by_registration(self):
        self.parent['another.txt'] = 'a' * 64
        with self.assertRaisesRegex(ValueError, 'omits'):
            r.verify_registration(self.root)

    def test_changed_published_parent_is_rejected(self):
        (self.root / 'parent.txt').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'Immutable P10'):
            r.verify_registration(self.root)

    def test_added_executable_input_requires_new_registration(self):
        (self.root / 'workflows/p11/extra.py').write_text('pass')
        with self.assertRaisesRegex(ValueError, 'code, settings'):
            r.verify_registration(self.root)

    def test_changed_ci_workflow_is_rejected(self):
        (self.root / r.CI_WORKFLOW).write_text('changed')
        with self.assertRaisesRegex(ValueError, 'CI workflow'):
            r.verify_registration(self.root)

    def test_future_or_naive_registration_time_is_rejected(self):
        for stamp in (datetime.now().isoformat(),
                      (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                r.validate_record({**self.record, 'registered_at_utc': stamp})

    def test_scientific_scope_and_boolean_types_are_not_relaxable(self):
        for name, expected in r.SCOPE.items():
            for bad in (not expected, int(expected)):
                changed = copy.deepcopy(self.record)
                changed['scope'][name] = bad
                with self.subTest(name=name, bad=bad), self.assertRaises(ValueError):
                    r.validate_record(changed)

    def test_mutable_parent_allowlist_cannot_expand(self):
        self.record['mutable_parent_paths'].append('parent.txt')
        with self.assertRaisesRegex(ValueError, 'allowlist'):
            r.validate_record(self.record)

    def test_parent_registration_binding_cannot_change(self):
        (self.root / 'workflows/p10/registration.json').write_text('replaced')
        with self.assertRaisesRegex(ValueError, 'P10 registration binding'):
            r.verify_registration(self.root)

    def test_symlink_even_in_excluded_evidence_is_rejected(self):
        evidence = self.root / 'workflows/p11/evidence'
        evidence.mkdir()
        (evidence / 'linked').symlink_to(self.root / 'parent.txt')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            r.code_inventory(self.root)

    def test_traversal_path_or_invalid_digest_is_rejected(self):
        for key, digest in (('../escape.py', 'a' * 64), ('safe.py', 'x' * 64)):
            changed = copy.deepcopy(self.record)
            changed['code_sha256'][key] = digest
            with self.subTest(key=key), self.assertRaises(ValueError):
                r.validate_record(changed)

    def test_foreign_checkout_cannot_claim_this_verifier(self):
        with self.assertRaisesRegex(ValueError, 'another checkout'):
            r.verify_registration(self.root / 'foreign')
