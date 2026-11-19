"""Bind P11 checks to the complete published P10 tree and exact new code."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess

from workflows.p10.registry import (
    MUTABLE_PARENT_PATHS, _relative_name, _safe_file, digest_file,
    verify_registration as verify_p10,
)

BASE = '151ad00095fa84a3c9c975a8a74546a798a4a135'
PROTOCOL_VERSION = 'p11-reconstruction-revision-recovery-v1'
REGISTRATION = 'workflows/p11/registration.json'
CI_WORKFLOW = '.github/workflows/p11-integration.yml'
SCOPE = {
    'post_p08_outcome_exposure': True,
    'new_untouched_final_test': False,
    'model_refitted': False,
    'quality_accepted': False,
    'operational_approval': False,
    'production_integration_claimed': False,
    'historical_entrypoints_changed': False,
    'unified_populated_28_table_postgres_claimed': False,
    'distributed_postgres_parquet_atomicity_claimed': False,
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def code_inventory(root):
    root = Path(root).resolve()
    directory = root / 'workflows/p11'
    require(directory.is_dir() and not directory.is_symlink()
            and not (root / 'workflows').is_symlink(), 'P11 code root is absent or linked')
    result = {}
    for path in sorted(directory.rglob('*')):
        name = path.relative_to(directory)
        require(not path.is_symlink(), 'P11 inventory contains a symlink: ' + str(name))
        if (not path.is_file() or name.parts[0] == 'evidence'
                or '__pycache__' in name.parts or path.suffix == '.pyc'
                or path == root / REGISTRATION):
            continue
        result[path.relative_to(root).as_posix()] = digest_file(path)
    result[CI_WORKFLOW] = digest_file(_safe_file(root, CI_WORKFLOW))
    return dict(sorted(result.items()))


def parent_inventory(root):
    """Derive the entire immutable inventory from actual published Git blobs."""
    root = Path(root).resolve()
    command = ['git', '--no-replace-objects', '-C', str(root)]
    listing = subprocess.check_output([*command, 'ls-tree', '-r', '-z', BASE])
    records = []
    for entry in listing.split(b'\0'):
        if not entry:
            continue
        metadata, raw_name = entry.split(b'\t', 1)
        mode, kind, oid = metadata.decode('ascii').split()
        name = raw_name.decode('utf-8')
        _relative_name(name)
        require(kind == 'blob' and mode in ('100644', '100755'),
                'Published parent contains a non-regular file: ' + name)
        if name not in MUTABLE_PARENT_PATHS:
            records.append((name, oid))
    require(bool(records), 'Published P10 tree is unavailable')
    result = {}
    process = subprocess.Popen([*command, 'cat-file', '--batch'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for name, oid in records:
            process.stdin.write((oid + '\n').encode('ascii'))
            process.stdin.flush()
            header = process.stdout.readline().decode('ascii').strip().split()
            require(len(header) == 3 and header[:2] == [oid, 'blob'],
                    'Published blob unavailable: ' + name)
            remaining, digest = int(header[2]), hashlib.sha256()
            while remaining:
                chunk = process.stdout.read(min(1024 * 1024, remaining))
                require(bool(chunk), 'Truncated published blob: ' + name)
                digest.update(chunk)
                remaining -= len(chunk)
            require(process.stdout.read(1) == b'\n', 'Invalid Git object terminator')
            result[name] = digest.hexdigest()
        process.stdin.close()
        require(process.wait() == 0, 'Published Git object stream failed')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()
    return dict(sorted(result.items()))


def validate_record(record):
    require(isinstance(record, dict) and type(record.get('schema_version')) is int
            and record['schema_version'] == 1 and record.get('base_public_commit') == BASE
            and record.get('protocol_version') == PROTOCOL_VERSION,
            'Invalid P11 registration version or parent')
    require(record.get('mutable_parent_paths') == sorted(MUTABLE_PARENT_PATHS),
            'P11 mutable parent allowlist differs')
    scope = record.get('scope')
    require(isinstance(scope, dict) and set(scope) == set(SCOPE)
            and all(scope[k] is v for k, v in SCOPE.items()), 'P11 scope differs')
    try:
        timestamp = datetime.fromisoformat(record['registered_at_utc'].replace('Z', '+00:00'))
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError('Invalid P11 registration time') from exc
    require(timestamp.tzinfo is not None and timestamp <= datetime.now(timezone.utc),
            'P11 registration must use an aware actual prior time')
    for field in ('parent_sha256', 'code_sha256'):
        require(isinstance(record.get(field), dict) and bool(record[field]),
                'Missing P11 inventory: ' + field)
        for name, digest in record[field].items():
            _relative_name(name)
            require(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest),
                    'Invalid P11 SHA256 entry')
    require(isinstance(record.get('p10_registration_sha256'), str)
            and re.fullmatch('[0-9a-f]{64}', record['p10_registration_sha256']),
            'Missing P10 registration binding')


def verify_registration(root):
    root = Path(root).resolve()
    require(Path(__file__).resolve() == root / 'workflows/p11/registry.py',
            'P11 verifier belongs to another checkout')
    path = _safe_file(root, REGISTRATION)
    record = json.loads(path.read_text())
    validate_record(record)
    expected = parent_inventory(root)
    require(record['parent_sha256'] == expected,
            'P11 registration omits, adds or changes a published P10 file')
    for name, digest in expected.items():
        require(digest_file(_safe_file(root, name)) == digest,
                'Immutable P10 parent changed: ' + name)
    require(code_inventory(root) == record['code_sha256'],
            'P11 code, settings, tests or CI workflow changed')
    require(digest_file(_safe_file(root, 'workflows/p10/registration.json'))
            == record['p10_registration_sha256'], 'P10 registration binding changed')
    previous = verify_p10(root)
    return {'status': 'passed_p11_registration_and_published_p10_parent',
            'base_public_commit': BASE, 'parent_file_count': len(expected),
            'workflow_file_count': len(record['code_sha256']),
            'registration_sha256': digest_file(path), 'p10_registration': previous,
            'quality_accepted': False, 'operational_approval': False}
