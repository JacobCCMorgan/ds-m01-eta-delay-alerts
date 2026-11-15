"""Bind the P09 extension without changing the exposed P08 experiment."""
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone

BASE = '12b904696b87d90369d85dabeee01eadadc854cd'
REGISTRATION = 'workflows/p09/registration.json'


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def inventory(directory):
    directory = Path(directory)
    return {p.relative_to(directory).as_posix(): digest_file(p)
            for p in sorted(directory.rglob('*')) if p.is_file()}


def code_inventory(root):
    root = Path(root).resolve()
    directory = root / 'workflows/p09'
    result = {}
    for p in sorted(directory.rglob('*')):
        relative = p.relative_to(directory)
        if not p.is_file() or relative.parts[0] == 'evidence' or '__pycache__' in relative.parts or p.suffix == '.pyc':
            continue
        if p == root / REGISTRATION:
            continue
        if p.is_symlink():
            raise ValueError('P09 code must not be a symlink: ' + relative.as_posix())
        result[p.relative_to(root).as_posix()] = digest_file(p)
    return result


def verify_registration(root):
    root = Path(root).resolve()
    if Path(__file__).resolve() != root / 'workflows/p09/registry.py':
        raise ValueError('Executed P09 registry belongs to another checkout')
    path = root / REGISTRATION
    record = json.loads(path.read_text())
    if record.get('schema_version') != 1 or record.get('base_public_commit') != BASE:
        raise ValueError('P09 registration has an unsupported parent/version')
    claims = record.get('scope', {})
    if (claims.get('post_p08_outcome_exposure') is not True
            or claims.get('new_untouched_final_test') is not False
            or claims.get('model_refitted') is not False
            or claims.get('quality_accepted') is not False
            or claims.get('operational_approval') is not False):
        raise ValueError('P09 registration overclaims experimental or operational scope')
    registered = datetime.fromisoformat(record['registered_at_utc'].replace('Z', '+00:00'))
    if registered.tzinfo is None or registered > datetime.now(timezone.utc):
        raise ValueError('P09 registration time must be aware and precede execution')
    parent = record.get('parent_sha256')
    if not isinstance(parent, dict) or not parent:
        raise ValueError('Missing frozen parent inventory')
    for name, expected in parent.items():
        p = Path(name)
        if p.is_absolute() or '..' in p.parts or not isinstance(expected, str) or len(expected) != 64:
            raise ValueError('Invalid frozen parent inventory entry')
        candidate = root / p
        if candidate.is_symlink() or digest_file(candidate) != expected:
            raise ValueError('Frozen P08 parent changed: ' + name)
    if code_inventory(root) != record.get('code_sha256'):
        raise ValueError('P09 code/settings/test inventory differs from registration')
    from ds_m01.final_gate import source_inventory, historical_inventory
    if source_inventory(root) != record.get('p08_source_sha256'):
        raise ValueError('Original P08 source inventory changed')
    if historical_inventory(root) != record.get('p08_historical_report_sha256'):
        raise ValueError('Original P08 historical report inventory changed')
    return {'status': 'passed_parent_and_workflow_registration',
            'base_public_commit': BASE, 'parent_file_count': len(parent),
            'workflow_file_count': len(record['code_sha256']),
            'registration_sha256': digest_file(path), 'quality_accepted': False,
            'operational_approval': False}
