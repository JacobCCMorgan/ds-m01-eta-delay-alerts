"""Bind the P10 service checks to the complete, unchanged published P09 tree."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess

BASE = '8ae479f5391e6b2f0e3b6ad1131e411629f90966'
PROTOCOL_VERSION = 'p10-native-integration-v1'
REGISTRATION = 'workflows/p10/registration.json'
MUTABLE_PARENT_PATHS = frozenset({
    'README.md', 'docs/change_log.md', 'docs/evidence_index.md',
    'contracts/current_requirement_evidence.csv', 'contracts/current_table_evidence.csv',
    'contracts/current_validation_evidence.csv',
})
SCOPE = {
    'post_p08_outcome_exposure': True,
    'new_untouched_final_test': False,
    'model_refitted': False,
    'quality_accepted': False,
    'operational_approval': False,
    'ci_verified': False,
    'production_integration_claimed': False,
    'full_scale_history_lineage_warehouse_rebuilt': False,
    'multiple_revisions_whole_pipeline_supported': False,
}
_HEX = re.compile(r'^[0-9a-f]{64}$')


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_name(name):
    _require(isinstance(name, str) and bool(name) and '\\' not in name,
             'Invalid registered relative path')
    path = Path(name)
    _require(not path.is_absolute() and '..' not in path.parts and path.as_posix() == name
             and all(part not in ('', '.') for part in name.split('/')),
             'Invalid registered relative path: ' + name)
    return path


def _safe_file(root, name):
    relative = _relative_name(name)
    candidate = Path(root)
    for part in relative.parts:
        candidate = candidate / part
        _require(not candidate.is_symlink(), 'Registered path must not use a symlink: ' + name)
    _require(candidate.is_file(), 'Missing registered file: ' + name)
    return candidate


def inventory(directory):
    """All regular files below a directory; links are rejected, not followed."""
    directory = Path(directory)
    _require(directory.is_dir() and not directory.is_symlink(), 'Inventory root must be a regular directory')
    result = {}
    for path in sorted(directory.rglob('*')):
        _require(not path.is_symlink(), 'Inventory must not contain a symlink: ' + str(path))
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = digest_file(path)
    return result


def code_inventory(root):
    """Bind every P10 file except evidence, this receipt, and Python bytecode."""
    root = Path(root).resolve()
    directory = root / 'workflows/p10'
    _require(directory.is_dir() and not directory.is_symlink(), 'P10 source directory is missing or linked')
    _require(not (root / 'workflows').is_symlink(), 'P10 source ancestors must not be linked')
    result = {}
    for path in sorted(directory.rglob('*')):
        relative = path.relative_to(directory)
        # Reject links even in otherwise excluded directories. Their targets
        # must never become an implicit source/evidence substitution mechanism.
        _require(not path.is_symlink(), 'P10 inventory must not contain a symlink: ' + relative.as_posix())
        if (not path.is_file() or relative.parts[0] == 'evidence'
                or '__pycache__' in relative.parts or path.suffix == '.pyc'
                or path == root / REGISTRATION):
            continue
        result[path.relative_to(root).as_posix()] = digest_file(path)
    _require(bool(result), 'P10 code inventory is empty')
    return result


def parent_inventory(root):
    """Hash every immutable blob from the actual published BASE Git tree.

    Registration cannot omit an inconvenient file: verification reconstructs
    this same complete inventory from Git, independently of the worktree and
    the registration JSON. One persistent cat-file process streams the blobs.
    """
    root = Path(root).resolve()
    listed = subprocess.check_output(['git', '--no-replace-objects', '-C', str(root), 'ls-tree', '-r', '-z', BASE])
    records = []
    for entry in listed.split(b'\0'):
        if not entry:
            continue
        metadata, raw_name = entry.split(b'\t', 1)
        mode, kind, oid = metadata.decode('ascii').split()
        name = raw_name.decode('utf-8')
        _relative_name(name)
        _require(kind == 'blob' and mode in ('100644', '100755'),
                 'Published parent contains an unsupported linked or non-file entry: ' + name)
        if name not in MUTABLE_PARENT_PATHS:
            records.append((name, oid))
    _require(bool(records), 'Published parent Git inventory is empty')
    result = {}
    process = subprocess.Popen(['git', '--no-replace-objects', '-C', str(root), 'cat-file', '--batch'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for name, oid in records:
            process.stdin.write((oid + '\n').encode('ascii'))
            process.stdin.flush()
            header = process.stdout.readline().decode('ascii').strip().split()
            _require(len(header) == 3 and header[:2] == [oid, 'blob'],
                     'Published parent Git object is unavailable: ' + name)
            remaining = int(header[2])
            digest = hashlib.sha256()
            while remaining:
                chunk = process.stdout.read(min(1024 * 1024, remaining))
                _require(bool(chunk), 'Truncated published parent Git object: ' + name)
                digest.update(chunk)
                remaining -= len(chunk)
            _require(process.stdout.read(1) == b'\n', 'Invalid Git object stream terminator')
            result[name] = digest.hexdigest()
        process.stdin.close()
        _require(process.wait() == 0, 'Reading the published parent Git objects failed')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
    return dict(sorted(result.items()))


def _validate_record(record):
    _require(isinstance(record, dict), 'P10 registration must be an object')
    _require(record.get('schema_version') == 1 and type(record.get('schema_version')) is int
             and record.get('protocol_version') == PROTOCOL_VERSION and record.get('base_public_commit') == BASE,
             'P10 registration has an unsupported parent/version')
    _require(record.get('mutable_parent_paths') == sorted(MUTABLE_PARENT_PATHS),
             'P10 mutable parent allowlist differs')
    scope = record.get('scope')
    _require(isinstance(scope, dict) and set(scope) == set(SCOPE)
             and all(scope[name] is value for name, value in SCOPE.items()),
             'P10 registration overclaims or changes the integration scope')
    try:
        registered = datetime.fromisoformat(record['registered_at_utc'].replace('Z', '+00:00'))
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError('P10 registration time must be an aware actual execution time') from exc
    _require(registered.tzinfo is not None and registered <= datetime.now(timezone.utc),
             'P10 registration time must be aware and precede execution')
    for field in ('parent_sha256', 'code_sha256'):
        values = record.get(field)
        _require(isinstance(values, dict) and bool(values), 'Missing P10 inventory: ' + field)
        for name, expected in values.items():
            _relative_name(name)
            _require(isinstance(expected, str) and _HEX.fullmatch(expected),
                     'Invalid SHA256 inventory entry: ' + name)
    _require(isinstance(record.get('p09_registration_sha256'), str)
             and _HEX.fullmatch(record['p09_registration_sha256']), 'Invalid P09 registration digest')


def _verify_p09(root):
    path = _safe_file(root, 'workflows/p09/registry.py')
    spec = importlib.util.spec_from_file_location('ds_m01_p10_frozen_p09_registry', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify_registration(root)


def verify_registration(root):
    root = Path(root).resolve()
    _require(Path(__file__).resolve() == root / 'workflows/p10/registry.py',
             'Executed P10 registry belongs to another checkout')
    path = _safe_file(root, REGISTRATION)
    record = json.loads(path.read_text())
    _validate_record(record)
    expected_parent = parent_inventory(root)
    _require(record['parent_sha256'] == expected_parent,
             'P10 parent inventory omits, adds, or changes a published P09 file')
    for name, expected in expected_parent.items():
        _require(digest_file(_safe_file(root, name)) == expected, 'Frozen published P09 parent changed: ' + name)
    _require(code_inventory(root) == record['code_sha256'], 'P10 code/settings/test inventory differs from registration')
    p09_path = _safe_file(root, 'workflows/p09/registration.json')
    _require(digest_file(p09_path) == record['p09_registration_sha256'], 'Bound P09 registration differs')
    p09 = _verify_p09(root)
    return {
        'status': 'passed_p10_registration_and_frozen_p09_parent', 'base_public_commit': BASE,
        'parent_file_count': len(expected_parent), 'workflow_file_count': len(record['code_sha256']),
        'registration_sha256': digest_file(path), 'p09_registration': p09,
        'quality_accepted': False, 'operational_approval': False,
    }
