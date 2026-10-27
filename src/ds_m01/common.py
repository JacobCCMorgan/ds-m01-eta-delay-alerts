"""Stable serialization and time helpers. See docs/shared_runtime_audit.json."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def parse_ts(value):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timezone-aware timestamp required')
    return result.astimezone(timezone.utc)


def utc(value):
    return parse_ts(value).isoformat().replace('+00:00', 'Z')


def stable_seed(seed, *tokens):
    value = '\x1f'.join(map(str, (seed, *tokens)))
    return int.from_bytes(hashlib.sha256(value.encode('utf-8')).digest()[:16], 'big')


def ident(*parts):
    return hashlib.sha256('\x1f'.join(map(str, parts)).encode()).hexdigest()[:24]


def read_jsonl(path):
    with Path(path).open(encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n')


def digest_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


# All canonical raw inputs are mandatory for a published source snapshot.
# Kept independent of pipeline imports to avoid a common/pipeline cycle.
SOURCE_TABLES = (
    'core_tenant', 'core_vehicle', 'core_device',
    'core_vehicle_device_assignment', 'eta_trip', 'eta_facility_version',
    'eta_route_version', 'eta_trip_plan_version', 'eta_promise_version',
    'eta_trip_event', 'eta_trip_outcome', 'telemetry_event', 'provenance_record',
)


def _source_file_evidence(path):
    """Count and hash persisted JSONL in one streaming pass, without row retention."""
    count = 0
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.endswith(b'\n') or not line.strip():
                raise IOError(f'Incomplete or blank source record: {path}:{line_number}')
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError) as exc:
                raise IOError(f'Invalid source JSON: {path}:{line_number}') from exc
            if not isinstance(row, dict):
                raise IOError(f'Source record is not an object: {path}:{line_number}')
            count += 1
            digest.update(line)
    return {'rows': count, 'sha256': digest.hexdigest()}


def write_source_manifest(root, counts):
    """Publish evidence only for a complete, uniquely named canonical source set."""
    root = Path(root)
    if set(counts) != set(SOURCE_TABLES):
        raise ValueError('Source counts must contain exactly the 13 canonical raw tables')
    files = []
    for table, expected in sorted(counts.items()):
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise ValueError(f'Invalid expected source row count: {table}')
        relative = f'data/raw/{table}.jsonl'
        evidence = _source_file_evidence(root / relative)
        if evidence['rows'] != expected:
            raise IOError(f'Persisted count mismatch: {table}: {evidence["rows"]} != {expected}')
        files.append({'path': relative, **evidence})
    manifest = {'scope': 'source bytes only; no scenario truth or feature values', 'files': files}
    path = root / 'data/source_manifest.json'
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(manifest, indent=2) + '\n')
    temporary.replace(path)
    return manifest


def verify_source_manifest(root):
    """Fail closed on omitted/duplicate sources, invalid rows, count or hash drift."""
    root = Path(root)
    manifest = json.loads((root / 'data/source_manifest.json').read_text())
    files = manifest.get('files')
    if not isinstance(files, list) or any(not isinstance(record, dict) for record in files):
        raise ValueError('Invalid source manifest records')
    paths = [record.get('path') for record in files]
    expected = {f'data/raw/{table}.jsonl' for table in SOURCE_TABLES}
    if (len(paths) != len(expected) or any(not isinstance(path, str) for path in paths)
            or len(set(paths)) != len(paths) or set(paths) != expected):
        raise ValueError('Source manifest must cover all 13 canonical raw paths exactly once')
    for record in files:
        rows = record.get('rows')
        digest = record.get('sha256')
        if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
            raise ValueError(f'Invalid source manifest row count: {record["path"]}')
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError(f'Invalid source manifest SHA256: {record["path"]}')
        evidence = _source_file_evidence(root / record['path'])
        if evidence['rows'] != rows:
            raise IOError(f'Source row count changed after generation: {record["path"]}')
        if evidence['sha256'] != digest:
            raise IOError(f'Source changed after generation: {record["path"]}')
    return manifest
