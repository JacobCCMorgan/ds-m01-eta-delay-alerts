"""P10 inventory guards: omission, changed parents, path aliases and overclaims."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'registry.py'
SPEC = importlib.util.spec_from_file_location('p10_registry_test_target', PATH)
registry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(registry)


def record_template():
    return {
        'schema_version': 1, 'protocol_version': registry.PROTOCOL_VERSION,
        'base_public_commit': registry.BASE,
        'registered_at_utc': datetime.now(timezone.utc).isoformat(),
        'mutable_parent_paths': sorted(registry.MUTABLE_PARENT_PATHS),
        'parent_sha256': {'source.py': 'a' * 64},
        'code_sha256': {'workflows/p10/settings.json': 'b' * 64},
        'p09_registration_sha256': 'c' * 64, 'scope': deepcopy(registry.SCOPE),
    }


class RecordTests(unittest.TestCase):
    def test_record_accepts_exact_bounded_scope(self):
        registry._validate_record(record_template())

    def test_scope_flags_cannot_be_omitted_or_truthy_integers(self):
        for key in registry.SCOPE:
            for change in ('omit', 'invert', 'integer'):
                with self.subTest(key=key, change=change):
                    value = record_template()
                    if change == 'omit':
                        value['scope'].pop(key)
                    elif change == 'invert':
                        value['scope'][key] = not value['scope'][key]
                    else:
                        value['scope'][key] = int(value['scope'][key])
                    with self.assertRaises(ValueError):
                        registry._validate_record(value)

    def test_mutable_parent_allowlist_cannot_be_widened_or_reordered(self):
        for paths in ([], sorted(registry.MUTABLE_PARENT_PATHS | {'src/ds_m01/pipeline.py'}),
                      list(reversed(sorted(registry.MUTABLE_PARENT_PATHS)))):
            value = record_template(); value['mutable_parent_paths'] = paths
            with self.assertRaises(ValueError):
                registry._validate_record(value)

    def test_invalid_parent_protocol_and_boolean_schema_are_rejected(self):
        for key, replacement in (('base_public_commit', '0' * 40), ('protocol_version', 'later'),
                                 ('schema_version', True), ('schema_version', 2)):
            value = record_template(); value[key] = replacement
            with self.assertRaises(ValueError):
                registry._validate_record(value)

    def test_naive_future_and_invalid_registration_times_are_rejected(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        for when in ('2026-01-01T00:00:00', future, None, 'not-a-time'):
            value = record_template(); value['registered_at_utc'] = when
            with self.assertRaises(ValueError):
                registry._validate_record(value)

    def test_invalid_paths_and_hashes_are_rejected(self):
        for name in ('/absolute', '../escape', 'one/../escape', './source.py', 'one//two', 'one\\two', ''):
            value = record_template(); value['parent_sha256'] = {name: 'a' * 64}
            with self.assertRaises(ValueError):
                registry._validate_record(value)
        for digest in ('A' * 64, 'z' * 64, 'a' * 63, None):
            value = record_template(); value['code_sha256'] = {'source.py': digest}
            with self.assertRaises(ValueError):
                registry._validate_record(value)


class RegistryFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='p10-registry-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.code = self.root / 'workflows/p10'
        self.code.mkdir(parents=True)

    def put(self, name, text='fixture'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path


class InventoryTests(RegistryFixture, unittest.TestCase):
    def test_code_inventory_is_exhaustive_beyond_python_files(self):
        for name in ('registry.py', 'settings.json', 'requirements-integration.lock', 'tests/new_case.py', 'README.md'):
            self.put('workflows/p10/' + name)
        for name in ('registration.json', 'evidence/receipt.json', '__pycache__/module.pyc', 'tests/cached.pyc'):
            self.put('workflows/p10/' + name)
        actual = registry.code_inventory(self.root)
        self.assertEqual(set(actual), {'workflows/p10/' + p for p in
                         ('registry.py', 'settings.json', 'requirements-integration.lock', 'tests/new_case.py', 'README.md')})
        self.put('workflows/p10/new_unregistered.txt')
        self.assertNotEqual(registry.code_inventory(self.root), actual)

    def test_symlink_files_directories_and_excluded_evidence_are_rejected(self):
        self.put('workflows/p10/settings.json')
        target = self.put('external.txt')
        for relative in ('workflows/p10/linked.py', 'workflows/p10/evidence/linked.json'):
            link = self.root / relative; link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                registry.code_inventory(self.root)
            link.unlink()
        link = self.code / 'linked_directory'; link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            registry.code_inventory(self.root)

    def test_safe_file_rejects_symlink_ancestor(self):
        self.put('actual/source.py')
        (self.root / 'alias').symlink_to(self.root / 'actual', target_is_directory=True)
        with self.assertRaises(ValueError):
            registry._safe_file(self.root, 'alias/source.py')

    def test_generic_inventory_and_missing_files(self):
        self.put('workflows/p10/settings.json', 'one')
        self.assertEqual(registry.inventory(self.code), {'settings.json': registry.digest_file(self.code / 'settings.json')})
        with self.assertRaises(ValueError):
            registry._safe_file(self.root, 'missing.json')


class PublishedParentTests(RegistryFixture, unittest.TestCase):
    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL).decode().strip()

    def setUp(self):
        super().setUp()
        self.put('src/original.py', 'original content\n')
        self.put('README.md', 'mutable original\n')
        self.put('workflows/p09/registry.py', 'frozen P09 code\n')
        self.put('workflows/p09/registration.json', '{}\n')
        self.git('init', '-q')
        self.git('add', 'src', 'README.md', 'workflows/p09')
        self.git('-c', 'user.name=P10 fixture', '-c', 'user.email=p10-fixture@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-q', '-m', 'Registry unit fixture')
        self.base = self.git('rev-parse', 'HEAD')
        self.base_patch = patch.object(registry, 'BASE', self.base)
        self.base_patch.start(); self.addCleanup(self.base_patch.stop)
        self.put('workflows/p10/registry.py', 'P10 registry fixture identity\n')
        self.put('workflows/p10/settings.json', '{}\n')
        self.record = record_template()
        self.record['parent_sha256'] = registry.parent_inventory(self.root)
        self.record['code_sha256'] = registry.code_inventory(self.root)
        self.record['p09_registration_sha256'] = registry.digest_file(self.root / 'workflows/p09/registration.json')
        self.write_record()

    def write_record(self):
        self.put(registry.REGISTRATION, json.dumps(self.record))

    def verify(self):
        # These tests isolate P10 inventory orchestration. The actual frozen P09
        # verifier is exercised on the real checkout by the P10 full run.
        with patch.object(registry, '__file__', str(self.code / 'registry.py')):
            with patch.object(registry, '_verify_p09', return_value={'status': 'unit_fixture'}) as parent:
                result = registry.verify_registration(self.root)
                parent.assert_called_once_with(self.root)
                return result

    def test_actual_git_objects_remain_oracle_when_worktree_changes(self):
        before = registry.parent_inventory(self.root)
        self.put('src/original.py', 'changed\n')
        self.assertEqual(registry.parent_inventory(self.root), before)
        with self.assertRaisesRegex(ValueError, 'Frozen published P09 parent changed'):
            self.verify()

    def test_parent_omission_cannot_hide_modified_file(self):
        self.record['parent_sha256'].pop('src/original.py')
        self.write_record()
        with self.assertRaisesRegex(ValueError, 'omits, adds, or changes'):
            self.verify()

    def test_rewritten_hash_cannot_hide_modified_p09_file(self):
        path = self.put('workflows/p09/registry.py', 'replacement\n')
        self.record['parent_sha256']['workflows/p09/registry.py'] = registry.digest_file(path)
        self.write_record()
        with self.assertRaisesRegex(ValueError, 'omits, adds, or changes'):
            self.verify()

    def test_registered_parent_and_mutable_document_change_are_accepted(self):
        self.put('README.md', 'current P10 documentation\n')
        self.assertEqual(self.verify()['parent_file_count'], 3)

    def test_extra_p10_file_is_rejected(self):
        self.put('workflows/p10/unregistered_module.py')
        with self.assertRaisesRegex(ValueError, 'code/settings/test inventory differs'):
            self.verify()

    def test_bound_p09_registration_digest_is_checked(self):
        self.record['p09_registration_sha256'] = 'f' * 64
        self.write_record()
        with self.assertRaisesRegex(ValueError, 'Bound P09 registration differs'):
            self.verify()

    def test_executed_registry_must_belong_to_checkout(self):
        with self.assertRaisesRegex(ValueError, 'another checkout'):
            registry.verify_registration(self.root)


if __name__ == '__main__':
    unittest.main()
