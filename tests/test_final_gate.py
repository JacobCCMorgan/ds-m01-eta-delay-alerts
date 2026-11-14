"""Synthetic-only final-access gates, immutable registration and audit hooks."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ds_m01 import final_gate as gate

HASH = 'a' * 64


def inventory():
    return dict(trips=1, opportunities=1, trip_keys=[['T1', 'TR1']],
                snapshot_keys=[['T1', 'S1']], membership_sha256=HASH,
                snapshot_context_sha256=HASH, feature_sha256=HASH,
                promise_features_sha256=HASH, outcomes_opened=False, target_based_pruning=False)


def receipt():
    codes = {'src/ds_m01/final_gate.py': HASH}
    qa = dict(status='passed_pre_target_conformance', started_at_utc='2026-11-13T00:00:00Z',
              completed_at_utc='2026-11-13T00:01:00Z', unit_tests=600, failures=0, skips=0,
              final_outcomes_accessed=False, source_sha256=codes, log_sha256=HASH,
              access_guard=dict(version=gate.GUARD_VERSION, scope=gate.GUARD_SCOPE,
                                guarded_processes=1, protected_outcome_opens_allowed=0,
                                events_sha256=HASH, blocked_attempts=0))
    return dict(status='frozen_before_final_target_access', protocol_sha256=gate.PROTOCOL_SHA256,
                base_public_commit=gate.BASE_PUBLIC_COMMIT, recorded_at_utc='2026-11-13T00:02:00Z',
                source_sha256=codes, historical_report_sha256={'reports/prior.json': HASH},
                input_sha256={'data/raw/eta_trip_outcome.jsonl': HASH},
                metadata_inventory=inventory(), metadata_inventory_sha256=gate.metadata_digest(inventory()),
                conformance=qa, final_outcomes_accessed=False,
                july_model_calibration_policy_and_delivery_results_previously_observed=True,
                model_or_calibrator_refitted=False, policy_reselected=False, quality_accepted=False,
                operational_approval=False, derived_reporting_exclusions=sorted(gate.DERIVED_EVIDENCE))


class FinalGateTests(unittest.TestCase):
    def verify(self, value, **kwargs):
        original = receipt()
        with patch.object(gate, 'verify_pins', return_value={'input_sha256': original['input_sha256']}), \
             patch.object(gate, 'source_inventory', return_value=original['source_sha256']), \
             patch.object(gate, 'historical_inventory', return_value=original['historical_report_sha256']), \
             patch.object(gate, 'utc_now', return_value='2026-11-14T00:00:00Z'):
            return gate.verify_registered('/synthetic', value, **kwargs)

    def test_valid_registration_binds_tested_code_inputs_and_metadata(self):
        self.assertEqual(self.verify(receipt(), run_started_at='2026-11-13T00:03:00Z'), receipt())

    def test_failed_skipped_or_exposed_conformance_is_not_a_valid_freeze(self):
        for key, value in [('status', 'failed'), ('failures', 1), ('failures', False),
                           ('skips', 1), ('unit_tests', 577), ('unit_tests', True),
                           ('final_outcomes_accessed', True)]:
            with self.subTest(key=key, value=value):
                r = receipt(); r['conformance'][key] = value
                with self.assertRaises(ValueError): self.verify(r)

    def test_missing_or_false_guard_claim_cannot_authorize_target_access(self):
        for key, value in [('version', 'other'), ('guarded_processes', 0),
                           ('protected_outcome_opens_allowed', False),
                           ('protected_outcome_opens_allowed', 1), ('events_sha256', None)]:
            with self.subTest(key=key):
                r = receipt(); r['conformance']['access_guard'][key] = value
                with self.assertRaises(ValueError): self.verify(r)

    def test_conformance_freeze_and_run_clocks_must_be_ordered(self):
        for field, value in [('started_at_utc', '2026-11-13T00:02:00Z'),
                             ('completed_at_utc', '2026-11-13T00:03:00Z'),
                             ('completed_at_utc', '2026-11-13T00:01:00')]:
            r = receipt(); r['conformance'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError): self.verify(r)
        r = receipt(); r['recorded_at_utc'] = '2026-11-15T00:00:00Z'
        with self.assertRaises(ValueError): self.verify(r)
        with self.assertRaises(ValueError): self.verify(receipt(), run_started_at='2026-11-13T00:01:59Z')

    def test_input_and_source_tampering_rejected(self):
        for key in ('input_sha256', 'source_sha256', 'historical_report_sha256'):
            r = receipt(); first = next(iter(r[key])); r[key][first] = 'b' * 64
            with self.subTest(key=key), self.assertRaises(ValueError): self.verify(r)

    def test_metadata_digest_and_inventory_counts_are_enforced(self):
        for edit in ('digest', 'count', 'duplicate', 'exposure'):
            r = receipt()
            if edit == 'digest': r['metadata_inventory_sha256'] = 'b' * 64
            elif edit == 'count': r['metadata_inventory']['opportunities'] = 2
            elif edit == 'duplicate':
                r['metadata_inventory']['trip_keys'] *= 2; r['metadata_inventory']['trips'] = 2
            else: r['metadata_inventory']['outcomes_opened'] = True
            with self.subTest(edit=edit), self.assertRaises(ValueError): self.verify(r)

    def test_scope_fields_require_literal_booleans_and_original_base(self):
        for key, value in [('final_outcomes_accessed', 0), ('quality_accepted', None),
                           ('operational_approval', 'false'), ('policy_reselected', True),
                           ('base_public_commit', 'other'), ('derived_reporting_exclusions', [])]:
            r = receipt(); r[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.verify(r)

    def test_log_requires_one_complete_unskipped_suite(self):
        good = 'test_fixture (test_final_independent.Example.test_fixture) ... ok\n\nRan 600 tests in 1.0s\n\nOK\n'
        self.assertEqual(gate._check_test_log(good, 600), 600)
        for bad in (good.replace('OK', 'OK (skipped=1)'), good + good,
                    good.replace('600', '577'), good.replace('test_final_independent', 'not_independent')):
            with self.assertRaises(ValueError): gate._check_test_log(bad)

    def test_immutable_json_never_replaces_file_or_dangling_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); file = root/'freeze.json'
            gate.write_json(file, {'original': True}, exclusive=True)
            before = file.read_bytes()
            with self.assertRaises(FileExistsError): gate.write_json(file, {}, exclusive=True)
            self.assertEqual(file.read_bytes(), before)
            link = root/'alias.json'; target = root/'absent.json'; link.symlink_to(target)
            with self.assertRaises(ValueError): gate.write_json(link, {}, exclusive=True)
            self.assertFalse(target.exists())

    def test_source_inventory_excludes_only_three_derived_evidence_indexes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in [*gate.DERIVED_EVIDENCE, 'contracts/fields.json',
                         'contracts/requirement_traceability.csv', 'src/ds_m01/final.py',
                         'tests/test_final.py', 'scripts/check_final.py']:
                file = root/name; file.parent.mkdir(parents=True, exist_ok=True); file.write_text('initial')
            pins = gate.source_inventory(root)
            self.assertEqual(set(pins), {'contracts/fields.json', 'contracts/requirement_traceability.csv',
                                        'src/ds_m01/final.py', 'tests/test_final.py', 'scripts/check_final.py'})
            for name in gate.DERIVED_EVIDENCE: (root/name).write_text('later evidence')
            self.assertEqual(pins, gate.source_inventory(root))
            (root/'contracts/fields.json').write_text('changed schema')
            self.assertNotEqual(pins, gate.source_inventory(root))

    def test_historical_inventory_excludes_pending_and_published_p08_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); old=root/'reports/p07/summary.json'
            old.parent.mkdir(parents=True); old.write_text('historical')
            expected=gate.historical_inventory(root)
            for name in ('reports/p08/summary.json', 'reports/.p08-attempt-hand/inputs/inventory.json'):
                path=root/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text('new P08')
            self.assertEqual(expected,gate.historical_inventory(root))
            (root/'reports/.p08-attempt-hand/inputs/inventory.json').write_text('later partial output')
            self.assertEqual(expected,gate.historical_inventory(root))
            (root/'reports/.p08-attempt-hand/inputs/inventory.json').unlink()
            self.assertEqual(expected,gate.historical_inventory(root))
            old.write_text('changed history')
            self.assertNotEqual(expected,gate.historical_inventory(root))
            old.unlink()
            self.assertNotEqual(expected,gate.historical_inventory(root))

    def test_historical_inventory_never_hides_matching_files_or_nested_old_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            names=('reports/.p08-attempt-file', 'reports/p08',
                   'reports/p07/.p08-attempt-nested/summary.json',
                   'reports/p07/p08/summary.json', 'reports/.p08-attempt-/summary.json',
                   'reports/p08_other/summary.json')
            for name in names:
                path=root/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text(name)
            self.assertEqual(set(gate.historical_inventory(root)),set(names))

    def test_correction_requires_truthful_prior_exposure_and_cannot_claim_untouched(self):
        r = receipt()
        r['status'] = 'registered_correction_after_final_target_exposure'
        r['final_outcomes_accessed'] = True
        r['correction'] = dict(kind='engineering_correction_after_final_target_exposure',
            prior_implementation_freeze_sha256=HASH, prior_exposure_started_at_utc='2026-11-13T00:01:30Z',
            prior_evidence_sha256={name:HASH for name in ('implementation_freeze.json',
                'pretarget_receipt.json','exposure_record.json','failure_record.json')},
            scientific_protocol_or_inputs_changed=False, untouched_final_test_claim=False)
        self.assertEqual(self.verify(r), r)
        for field, value in [('untouched_final_test_claim', True),
                             ('scientific_protocol_or_inputs_changed', True),
                             ('prior_exposure_started_at_utc', '2026-11-13T00:03:00Z')]:
            changed=copy.deepcopy(r); changed['correction'][field]=value
            with self.subTest(field=field), self.assertRaises(ValueError): self.verify(changed)
        changed=copy.deepcopy(r); changed['final_outcomes_accessed']=False
        with self.assertRaises(ValueError): self.verify(changed)
        with self.assertRaises(ValueError): gate.verify_correction_history(r,None)

    def test_correction_history_is_retained_and_bound_without_opening_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            prior=Path(directory)
            old=receipt(); gate.write_json(prior/'implementation_freeze.json',old,exclusive=True)
            gate.write_json(prior/'pretarget_receipt.json',{'final_outcomes_opened':False},exclusive=True)
            gate.write_json(prior/'exposure_record.json',dict(status='final_outcome_access_started',
                started_at_utc='2026-11-13T00:03:00Z',
                pretarget_receipt_sha256=gate.digest_file(prior/'pretarget_receipt.json'),
                expected_source_sha256=HASH),exclusive=True)
            gate.write_json(prior/'failure_record.json',{'final_outcomes_exposed':True},exclusive=True)
            evidence=gate._correction_evidence(prior,prior/'implementation_freeze.json',
                                               {'input_sha256':old['input_sha256']})
            r=dict(status='registered_correction_after_final_target_exposure',correction=evidence)
            self.assertTrue(gate.verify_correction_history(r,prior))
            gate.write_json(prior/'failure_record.json',{'final_outcomes_exposed':False})
            with self.assertRaises(ValueError): gate.verify_correction_history(r,prior)

    def guard_probe(self, body):
        # A nested, hand-authored dataset tests blocking without approaching any
        # project input. Under the outer full-suite guard it is itself a permitted fixture.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); allowed = root/'fresh'; allowed.mkdir()
            protected = root/'eta_trip_outcome.jsonl'; protected.write_text('{"toy":true}\n')
            alias = allowed/'renamed.json'; alias.symlink_to(protected)
            code = '''import json, os, sys, subprocess
from pathlib import Path
from ds_m01.final_gate import install_access_guard
root=Path(sys.argv[1]); allowed=root/'fresh'; protected=root/'eta_trip_outcome.jsonl'
install_access_guard({'temporary_root':str(allowed),'guard_directory':str(root), 'python':sys.executable})
''' + body
            result = subprocess.run([sys.executable, '-c', code, str(root)], text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertEqual(result.returncode, 0, result.stdout)
            return result.stdout

    def test_guard_blocks_protected_read_and_hash_before_bytes_are_opened(self):
        self.guard_probe('''from ds_m01.common import digest_file
for operation in (lambda: protected.read_bytes(), lambda: digest_file(protected), lambda: os.open(protected,os.O_RDONLY)):
    try: operation()
    except PermissionError: pass
    else: raise AssertionError('unguarded outcome read')
''')

    def test_guard_resolves_symlink_alias_before_deciding_fixture_access(self):
        self.guard_probe('''try: (allowed/'renamed.json').read_text()
except PermissionError: pass
else: raise AssertionError('symlink escaped the guard')
''')

    def test_guard_rejects_link_or_rename_of_external_outcome_into_fixture_root(self):
        self.guard_probe('''for operation in (lambda: os.link(protected,allowed/'linked.json'),
                  lambda: os.rename(protected,allowed/'moved.json')):
    try: operation()
    except PermissionError: pass
    else: raise AssertionError('outcome moved into fixture exception')
''')

    def test_guard_allows_only_fresh_temporary_synthetic_outcomes(self):
        self.guard_probe('''fixture=allowed/'eta_trip_outcome.jsonl'
fixture.write_text('{"fixture":true}\\n')
assert json.loads(fixture.read_text()) == {'fixture':True}
''')

    def test_guard_rejects_native_and_isolated_python_child_bypasses(self):
        self.guard_probe('''for command in (['/bin/cat',str(protected)],[sys.executable,'-I','-c','pass']):
    try: subprocess.run(command,check=True)
    except PermissionError: pass
    else: raise AssertionError('unguarded child accepted')
try: os.system('true')
except PermissionError: pass
else: raise AssertionError('native execution accepted')
''')

    def test_inherited_guard_is_active_before_discovery_in_child_python(self):
        if gate.GUARD_ENV in os.environ:
            result = subprocess.run([sys.executable, '-c',
                                     "import sys; assert getattr(sys,'_ds_m01_p08_guard_active',False)"],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertEqual(result.returncode, 0, result.stdout)
        else:
            # Exercise the identical startup path in ordinary unit-test runs.
            with tempfile.TemporaryDirectory() as directory:
                base=Path(directory); fresh=base/'fresh'; fresh.mkdir(); output=base/'evidence'; output.mkdir()
                root=Path(gate.__file__).resolve().parents[2]
                env=gate._guard_environment(root,output,fresh)
                result=subprocess.run([sys.executable,'-c',
                    "import sys; assert getattr(sys,'_ds_m01_p08_guard_active',False)"],env=env,text=True,
                    stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
                self.assertEqual(result.returncode,0,result.stdout)
                events=[json.loads(line) for line in (output/'guard_events.jsonl').read_text().splitlines()]
                self.assertTrue(any(row['event']=='guard_installed' for row in events))


if __name__ == '__main__':
    unittest.main()
