"""Fail-closed CI context, test inventory and execution-scope checks."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from workflows.p11 import ci


class CIContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ci.WORKFLOW).parent.mkdir(parents=True)
        (self.root / ci.WORKFLOW).write_text('workflow fixture')
        (self.root / 'workflows/p11').mkdir(parents=True)
        (self.root / 'workflows/p11/registration.json').write_text('{}')
        self.head = 'a' * 40
        self.env = {
            'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': ci.PROJECT,
            'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_EVENT_NAME': 'push',
            'GITHUB_REF': 'refs/heads/main', 'GITHUB_SHA': self.head,
            'GITHUB_WORKFLOW_REF': f'{ci.PROJECT}/{ci.WORKFLOW}@refs/heads/main',
            'GITHUB_WORKFLOW_SHA': self.head, 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2',
        }
        patch = mock.patch.object(ci, 'git', side_effect=lambda root, *args:
                                  'b' * 40 if args[-1] == 'HEAD^{tree}' else self.head)
        patch.start()
        self.addCleanup(patch.stop)

    def test_exact_event_checkout_workflow_and_attempt_are_bound(self):
        result = ci.github_context(self.root, self.env)
        self.assertEqual((result['head'], result['run_id'], result['run_attempt']),
                         (self.head, 123, 2))
        self.assertEqual(result['workflow_sha256'], ci.digest(self.root / ci.WORKFLOW))
        self.assertEqual(result['registration_sha256'], ci.digest(self.root / 'workflows/p11/registration.json'))

    def test_local_execution_cannot_claim_github_ci(self):
        with self.assertRaisesRegex(ValueError, 'GitHub Actions'):
            ci.github_context(self.root, {**self.env, 'GITHUB_ACTIONS': 'false'})

    def test_other_repository_branch_event_or_workflow_is_rejected(self):
        for field, bad in (('GITHUB_REPOSITORY', 'other/repo'), ('GITHUB_REF', 'refs/heads/other'),
                           ('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_WORKFLOW_REF', 'other/workflow')):
            with self.subTest(field=field), self.assertRaises(ValueError):
                ci.github_context(self.root, {**self.env, field: bad})

    def test_checkout_or_workflow_sha_mismatch_is_rejected(self):
        for field in ('GITHUB_SHA', 'GITHUB_WORKFLOW_SHA'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                ci.github_context(self.root, {**self.env, field: 'c' * 40})

    def test_invalid_run_attempt_identifiers_are_rejected(self):
        for field in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'):
            for bad in ('', '0', '-1', '1.0', 'abc'):
                with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                    ci.github_context(self.root, {**self.env, field: bad})


class CIResultTests(unittest.TestCase):
    def test_skips_failures_missing_or_duplicate_test_counts_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / 'tests.log'
            for text in ('Ran 3 tests in 0.1s\n\nOK (skipped=1)\n',
                         'Ran 3 tests in 0.1s\n\nFAILED (failures=1)\n',
                         'Ran 2 tests in 0.1s\n\nOK\n', 'OK\n',
                         'Ran 3 tests in 0.1s\nRan 3 tests in 0.1s\n\nOK\n'):
                log.write_text(text)
                with self.subTest(text=text), self.assertRaises(ValueError):
                    ci.check_test_log(log, 3)
            log.write_text('test_actual ... ok\n\nRan 3 tests in 0.1s\n\nOK\n')
            ci.check_test_log(log, 3)

    def test_each_real_execution_field_is_required_with_boolean_type(self):
        base = {'status': ci.SUMMARY_STATUS, **dict.fromkeys(ci.P11_REQUIRED, True),
                **dict.fromkeys(ci.SCOPE_FALSE, False)}
        ci.check_summary(base)
        for field in ci.P11_REQUIRED:
            for bad in (False, 1, 'true', None):
                with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                    ci.check_summary({**base, field: bad})

    def test_summary_cannot_relabel_local_or_quality_acceptance_as_ci(self):
        base = {'status': ci.SUMMARY_STATUS, **dict.fromkeys(ci.P11_REQUIRED, True),
                **dict.fromkeys(ci.SCOPE_FALSE, False)}
        for field in ci.SCOPE_FALSE:
            with self.subTest(field=field), self.assertRaises(ValueError):
                ci.check_summary({**base, field: True})

    def test_failed_status_cannot_be_hidden_by_execution_flags(self):
        with self.assertRaisesRegex(ValueError, 'did not pass'):
            ci.check_summary({'status': 'failed', **dict.fromkeys(ci.P11_REQUIRED, True),
                              **dict.fromkeys(ci.SCOPE_FALSE, False)})


if __name__ == '__main__':
    unittest.main()
