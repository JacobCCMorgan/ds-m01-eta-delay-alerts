"""Run the complete P11 CI gate and retain compact, commit-bound evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = '.github/workflows/p11-integration.yml'
PROJECT = 'JacobCCMorgan/ds-m01-eta-delay-alerts'
SUMMARY_STATUS = 'passed_p11_clean_rebuild_revision_and_recovery'
P11_REQUIRED = ('two_clean_rebuilds_executed', 'revision_http_and_store_executed',
                'abrupt_recovery_executed', 'parent_integrity_verified')
SCOPE_FALSE = ('ci_verified', 'quality_accepted', 'operational_approval')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def clock():
    return datetime.now(timezone.utc).isoformat()


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def github_context(root, env):
    """Bind coherent runner inputs; remote acceptance separately checks GitHub API."""
    head = git(root, 'rev-parse', 'HEAD')
    require(env.get('GITHUB_ACTIONS') == 'true', 'CI runner requires GitHub Actions context')
    require(env.get('GITHUB_REPOSITORY') == PROJECT, 'Unexpected CI repository')
    require(env.get('GITHUB_SERVER_URL') == 'https://github.com', 'Unexpected GitHub server')
    require(env.get('GITHUB_EVENT_NAME') in ('push', 'workflow_dispatch'), 'Unexpected CI event')
    require(env.get('GITHUB_REF') == 'refs/heads/main', 'CI acceptance requires main')
    require(re.fullmatch(r'[0-9a-f]{40}', head) and env.get('GITHUB_SHA') == head,
            'CI checkout does not match the event commit')
    require(env.get('GITHUB_WORKFLOW_REF') == f'{PROJECT}/{WORKFLOW}@refs/heads/main',
            'Unexpected CI workflow reference')
    require(env.get('GITHUB_WORKFLOW_SHA') == head, 'CI workflow is not from the event commit')
    for name in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'):
        require(bool(re.fullmatch(r'[1-9][0-9]*', env.get(name, ''))), 'Invalid ' + name)
    return {'head': head, 'tree': git(root, 'rev-parse', 'HEAD^{tree}'),
            'repository': PROJECT, 'event': env['GITHUB_EVENT_NAME'], 'ref': env['GITHUB_REF'],
            'run_id': int(env['GITHUB_RUN_ID']), 'run_attempt': int(env['GITHUB_RUN_ATTEMPT']),
            'workflow_path': WORKFLOW, 'workflow_sha256': digest(root / WORKFLOW),
            'registration_sha256': digest(root / 'workflows/p11/registration.json'),
            'run_url': f'https://github.com/{PROJECT}/actions/runs/{env["GITHUB_RUN_ID"]}'}


def check_test_log(path, count):
    text = Path(path).read_text()
    require(re.findall(r'^Ran (\d+) tests? in ', text, re.M) == [str(count)]
            and text.rstrip().endswith('\nOK'),
            'Tests failed, skipped, or did not match the frozen count: ' + str(path))


def check_summary(data):
    require(data.get('status') == SUMMARY_STATUS, 'P11 integration did not pass')
    for field in P11_REQUIRED:
        require(data.get(field) is True, 'P11 execution missing: ' + field)
    for field in SCOPE_FALSE:
        require(data.get(field) is False, 'P11 summary overclaims: ' + field)


def _write(path, data):
    Path(path).write_text(json.dumps(data, sort_keys=True, indent=2, allow_nan=False) + '\n')


def run(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    require(root == ROOT, 'CI runner must execute its own checkout')
    require(not output.exists() and not output.is_relative_to(root),
            'CI requires a new output directory outside the checkout')
    context = github_context(root, os.environ)
    require(not git(root, 'status', '--porcelain', '--untracked-files=all'), 'CI checkout is dirty')
    settings = json.loads((root / 'workflows/p11/settings.json').read_text())
    new_count = settings['expected_test_count']
    require(type(new_count) is int and new_count > 0, 'P11 test count must be frozen')
    suites = [
        ('original_unit_tests', 727, ['-m', 'unittest', 'discover', '-s', 'tests', '-v']),
        ('portable_unit_tests', 17, ['-m', 'unittest', 'discover', '-s', 'reproducibility/p08/tests', '-v']),
        ('p09_unit_tests', 115, ['-I', '-B', 'workflows/p09/test_suite.py']),
        ('p10_unit_tests', 67, ['-I', '-B', 'workflows/p10/test_suite.py']),
        ('p11_unit_tests', new_count, ['-I', '-B', 'workflows/p11/test_suite.py']),
    ]
    output.mkdir(parents=True)
    artifact = output / 'artifact'
    artifact.mkdir()
    temporary = Path(tempfile.mkdtemp(prefix='canonical-tmp-', dir=output)).resolve()
    env = dict(os.environ, TMPDIR=str(temporary), PYTHONDONTWRITEBYTECODE='1',
               PYTHONPATH=os.pathsep.join((str(root / 'src'), str(root))))
    for key in tuple(env):
        if key.startswith(('PG', 'POSTGRES')) or key in ('DATABASE_URL', 'DS_M01_PG_DSN'):
            env.pop(key, None)
    started = clock()

    def logged(name, args):
        log = artifact / (name + '.log')
        print('RUN ' + name + '; log: ' + str(log), flush=True)
        with log.open('x') as stream:
            subprocess.run([sys.executable, '-B', *args], cwd=root, env=env,
                           stdout=stream, stderr=subprocess.STDOUT, check=True, text=True)
        return log

    try:
        tests = {}
        for name, count, args in suites:
            log = logged(name, args)
            check_test_log(log, count)
            tests[name] = {'test_count': count, 'log_sha256': digest(log)}
        parent_log = logged('p11_parent_integrity', ['-I', '-B', 'workflows/p11/cli.py',
                             'check-parent', '--root', str(root)])
        parent = json.loads(parent_log.read_text())
        replay_path = output / 'p09_replay.json'
        replay_log = logged('p09_replay', ['-I', '-B', 'workflows/p09/cli.py', 'replay',
                            '--root', str(root), '--report', str(root / 'workflows/p09/evidence'),
                            '--output', str(replay_path)])
        replay = json.loads(replay_log.read_text())
        require(replay.get('status') == 'passed_p09_replay' and replay.get('differences') == 0
                and replay.get('independent_checks', {}).get('failure_count') == 0,
                'Retained P09 replay failed')
        shutil.copyfile(replay_path, artifact / 'p09_replay.json')
        native = output / 'p10_native'
        native_log = logged('p10_native_integration', ['-I', '-B', 'workflows/p10/cli.py',
                            'verify-native', '--root', str(root), '--output', str(native)])
        native_result = json.loads(native_log.read_text())
        require(native_result.get('status') == 'passed_native_integration'
                and native_result.get('native_postgresql_executed') is True
                and native_result.get('real_http_executed') is True
                and native_result.get('owned_cluster_lifecycle', {}).get('graceful_shutdown') is True
                and native_result.get('owned_cluster_lifecycle', {}).get('listener_closed') is True,
                'Actual P10 native execution or shutdown failed')
        (artifact / 'p10_native').mkdir()
        for name in ('native_integration.json', 'run_receipt.json', 'parent_integrity.json',
                     'source_generation.json', 'postgres.json', 'http_snapshot.json', 'historical_oracle.json'):
            shutil.copyfile(native / name, artifact / 'p10_native' / name)
        (artifact / 'p10_native/cluster').mkdir()
        shutil.copyfile(native / 'cluster/lifecycle.json', artifact / 'p10_native/cluster/lifecycle.json')
        integration = output / 'p11_integration'
        integration_log = logged('p11_integration', ['-I', '-B', 'workflows/p11/cli.py',
                                 'verify', '--root', str(root), '--output', str(integration)])
        # Logs retain progress and diagnostics; only the explicit output file
        # is the machine-readable integration result.
        summary = json.loads((integration / 'summary.json').read_text())
        check_summary(summary)
        (artifact / 'p11_integration').mkdir()
        for name in ('summary.json', 'receipt.json', 'parent_integrity.json',
                     'rebuild.json', 'revision.json', 'recovery.json'):
            shutil.copyfile(integration / name, artifact / 'p11_integration' / name)
        check_summary(json.loads((artifact / 'p11_integration/summary.json').read_text()))
        require(github_context(root, os.environ) == context, 'CI context changed during verification')
        require(not git(root, 'status', '--porcelain', '--untracked-files=all'), 'CI checkout changed')
        file_hashes = {p.relative_to(artifact).as_posix(): digest(p)
                       for p in sorted(artifact.rglob('*')) if p.is_file()}
        result = {'status': 'passed_p11_ci_execution', **context,
                  'started_at_utc': started, 'completed_at_utc': clock(),
                  'tests': tests, 'test_count': sum(item['test_count'] for item in tests.values()),
                  'parent_integrity': parent, 'native_postgresql_executed': True,
                  'real_http_executed': True, 'two_clean_rebuilds_executed': True,
                  'revision_http_and_store_executed': True, 'abrupt_recovery_executed': True,
                  'artifact_files_sha256': file_hashes, 'quality_accepted': False,
                  'operational_approval': False,
                  'scope': 'Runner-produced execution evidence for this commit and attempt; '
                           'CI acceptance additionally requires GitHub API job/run and downloaded-artifact verification. '
                           'Synthetic engineering checks, not model-quality or operational approval.'}
        _write(artifact / 'ci_result.json', result)
        return result
    except BaseException as exc:
        _write(artifact / 'ci_failure.json', {'status': 'failed_no_ci_success_receipt', **context,
                'started_at_utc': started, 'failed_at_utc': clock(), 'error': str(exc)})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.output), sort_keys=True, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
