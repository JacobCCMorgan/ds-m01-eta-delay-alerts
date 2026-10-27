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


def write_source_manifest(root,counts):
    """Publish source-byte evidence only after persisted counts agree."""
    root=Path(root);files=[]
    for table,expected in sorted(counts.items()):
        path=root/'data/raw'/f'{table}.jsonl'
        actual=len(read_jsonl(path))
        if actual!=expected:raise IOError(f'Persisted count mismatch: {table}: {actual} != {expected}')
        files.append({'path':f'data/raw/{table}.jsonl','rows':actual,'sha256':digest_file(path)})
    manifest={'scope':'source bytes only; no scenario truth or feature values','files':files}
    path=root/'data/source_manifest.json';temporary=path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(manifest,indent=2)+'\n');temporary.replace(path)
    return manifest


def verify_source_manifest(root):
    root=Path(root);manifest=json.loads((root/'data/source_manifest.json').read_text())
    for record in manifest['files']:
        if not record['path'].startswith('data/raw/') or '..' in Path(record['path']).parts:raise ValueError('Invalid manifest path')
        if digest_file(root/record['path'])!=record['sha256']:raise IOError(f"Source changed after generation: {record['path']}")
    return manifest
