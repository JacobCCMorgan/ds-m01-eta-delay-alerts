"""Publish one immutable DuckDB/Parquet generation through a single reference.

This is a single-filesystem protocol for cooperating readers and writers. It is
not a distributed transaction with PostgreSQL. Readers must resolve current.json
once and retain that generation; independently opening legacy output paths does
not acquire this guarantee. Completed generations and interrupted stages are
preserved for inspection, never silently promoted or removed during recovery.
"""
from contextlib import contextmanager
from datetime import date, datetime, time, timezone
from decimal import Decimal
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import uuid

import duckdb


PROTOCOL = 'p11-duckdb-parquet-generation-v1'
VALIDATION_MEMORY_LIMIT = '2GB'
VALIDATION_THREADS = 1
_MISSING = object()
_GENERATION = re.compile(r'g-[0-9a-f]{32}')
_VALIDATION_TEMP = re.compile(r'\.validation-[0-9a-f]{32}')
_TABLE = re.compile(r'[a-z][a-z0-9_]*')


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8') + b'\n'


def _sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path, value):
    with Path(path).open('xb') as stream:
        stream.write(_canonical(value))
        stream.flush()
        os.fsync(stream.fileno())


def _root(path):
    requested = Path(path).expanduser().absolute()
    _require(not requested.is_symlink(), 'Publication root must not be a symlink')
    return requested.resolve()


def _plain(path, *, directory=False):
    _require(not path.is_symlink(), 'Publication artifacts must not be symlinks: ' + path.name)
    _require(path.is_dir() if directory else path.is_file(), 'Missing publication artifact: ' + path.name)


def _marker(root):
    path = root / 'publication.json'
    _plain(path)
    _require(json.loads(path.read_bytes()) == {'protocol': PROTOCOL}, 'Publication root belongs to another protocol')


@contextmanager
def _writer(root):
    """Reject concurrent publication rather than queue an obsolete decision."""
    root.mkdir(parents=True, exist_ok=True)
    lock = root / '.writer.lock'
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Another writer owns this publication store') from exc
        marker = root / 'publication.json'
        if not marker.exists():
            _require(set(p.name for p in root.iterdir()) == {'.writer.lock'},
                     'Refusing to adopt a nonempty unregistered publication directory')
            _write_new(marker, {'protocol': PROTOCOL})
            _fsync_directory(root)
        _marker(root)
        for name in ('staging', 'generations'):
            path = root / name
            _require(not path.is_symlink(), 'Publication directory must not be a symlink')
            path.mkdir(exist_ok=True)
            _plain(path, directory=True)
        _fsync_directory(root)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _table_names(expected_tables):
    names = list(expected_tables)
    _require(names and len(names) == len(set(names)), 'Expected table catalog must be nonempty and unique')
    _require(all(isinstance(n, str) and _TABLE.fullmatch(n) for n in names), 'Invalid table name')
    return sorted(names)


def _column(name):
    return '"' + name.replace('"', '""') + '"'


def _typed(value):
    """Stable, type-tagged logical values; preserve float sign and exact decimal."""
    if value is None:
        return ['null']
    if isinstance(value, bool):
        return ['bool', value]
    if isinstance(value, int):
        return ['int', str(value)]
    if isinstance(value, float):
        _require(math.isfinite(value), 'Non-finite values cannot establish exact publication parity')
        return ['float', value.hex()]
    if isinstance(value, Decimal):
        _require(value.is_finite(), 'Non-finite decimal in publication')
        return ['decimal', str(value)]
    if isinstance(value, datetime):
        return ['datetime', (value.astimezone(timezone.utc) if value.tzinfo else value).isoformat()]
    if isinstance(value, (date, time)):
        return [type(value).__name__, value.isoformat()]
    if isinstance(value, bytes):
        return ['bytes', value.hex()]
    if isinstance(value, str):
        return ['str', value]
    if isinstance(value, (list, tuple)):
        return ['list', [_typed(v) for v in value]]
    if isinstance(value, dict):
        return ['dict', [[str(k), _typed(v)] for k, v in sorted(value.items())]]
    raise ValueError('Unsupported value type in publication: ' + type(value).__name__)


def _rows(cursor):
    while True:
        batch = cursor.fetchmany(256)
        if not batch:
            return
        for row in batch:
            yield _canonical([_typed(value) for value in row])


def validate_generation(directory, expected_tables):
    """Compare every typed row and schema, then hash closed artifacts.

    Ordering occurs inside DuckDB with a bounded memory limit and spill directory;
    Python holds at most two batches. Logical hashes aid repeat-run comparison,
    but acceptance compares row bytes directly rather than hashes alone.
    """
    directory = Path(directory).resolve()
    tables = _table_names(expected_tables)
    db = directory / 'warehouse.duckdb'
    parquet = directory / 'parquet'
    _plain(db)
    _plain(parquet, directory=True)
    actual = {p.name for p in parquet.iterdir()}
    _require(actual == {name + '.parquet' for name in tables}, 'Parquet catalog differs from expected tables')
    for name in tables:
        _plain(parquet / (name + '.parquet'))
    _require(not (directory / 'warehouse.duckdb.wal').exists(), 'Database must be checkpointed and closed before publication')
    allowed = {'warehouse.duckdb', 'parquet', 'manifest.json'}
    _require(set(p.name for p in directory.iterdir()).issubset(allowed), 'Unexpected files in generation')
    temp = directory.parent / ('.validation-' + uuid.uuid4().hex)
    temp.mkdir()
    con = None
    result = {}
    try:
        con = duckdb.connect(str(db), read_only=True)
        con.execute("SET TimeZone='UTC'")
        con.execute('SET threads=?', [VALIDATION_THREADS])
        con.execute('SET memory_limit=?', [VALIDATION_MEMORY_LIMIT])
        con.execute('SET temp_directory=?', [str(temp)])
        observed = con.execute("SELECT table_name, table_type FROM information_schema.tables WHERE table_schema='main' ORDER BY table_name").fetchall()
        _require(observed == [(name, 'BASE TABLE') for name in tables], 'Database catalog differs from expected tables')
        for name in tables:
            print('P11 comparison starting: ' + name, file=sys.stderr, flush=True)
            schema = con.execute(f'DESCRIBE "{name}"').fetchall()
            parquet_schema = con.execute('DESCRIBE SELECT * FROM read_parquet(?)', [str(parquet / (name + '.parquet'))]).fetchall()
            _require([(r[0], r[1]) for r in schema] == [(r[0], r[1]) for r in parquet_schema],
                     'Database/Parquet schema differs: ' + name)
            primary = con.execute("SELECT constraint_column_names FROM duckdb_constraints() WHERE schema_name='main' AND table_name=? AND constraint_type='PRIMARY KEY'", [name]).fetchall()
            _require(len(primary) <= 1, 'Unexpected primary-key catalog: ' + name)
            order_columns = list(primary[0][0]) if primary else [r[0] for r in schema]
            order = ','.join(_column(column) for column in order_columns)
            left, right = con.cursor(), con.cursor()
            try:
                # Normalize time-zone-aware timestamps to their exact UTC
                # microsecond value before Python conversion. This avoids an
                # optional pytz import in DuckDB's Python TIMESTAMPTZ adapter;
                # the original typed schema remains part of the manifest.
                columns = ','.join(
                    f'timezone(\'UTC\',{_column(r[0])}) AS {_column(r[0])}' if r[1] == 'TIMESTAMP WITH TIME ZONE'
                    else _column(r[0]) for r in schema)
                # A declared primary key is unique in the typed database, so
                # ordering on it aligns every complete row without sorting wide
                # JSON payloads as extra keys. Duplicate or changed Parquet rows
                # still fail the exact typed full-row comparison below.
                left.execute(f'SELECT {columns} FROM "{name}" ORDER BY {order}')
                right.execute('SELECT '+columns+' FROM read_parquet(?) ORDER BY '+order, [str(parquet / (name + '.parquet'))])
                a, b = iter(_rows(left)), iter(_rows(right))
                digest, count = hashlib.sha256(), 0
                while True:
                    va, vb = next(a, _MISSING), next(b, _MISSING)
                    if va is _MISSING and vb is _MISSING:
                        break
                    _require(va is not _MISSING and vb is not _MISSING and va == vb,
                             'Database/Parquet rows differ: ' + name)
                    digest.update(va)
                    count += 1
                result[name] = {'row_count': count, 'logical_sha256': digest.hexdigest(),
                                'columns': [{'name': r[0], 'type': r[1], 'nullable': r[2] == 'YES'} for r in schema],
                                'comparison_order': order_columns}
                print(f'P11 comparison passed: {name}; {count} rows', file=sys.stderr, flush=True)
            finally:
                left.close(); right.close()
    finally:
        if con is not None:
            con.close()
        shutil.rmtree(temp)
    artifacts = {'warehouse.duckdb': {'sha256': _sha(db), 'size_bytes': db.stat().st_size}}
    for name in tables:
        path = parquet / (name + '.parquet')
        artifacts['parquet/' + path.name] = {'sha256': _sha(path), 'size_bytes': path.stat().st_size}
    return {'tables': result, 'artifacts': artifacts, 'all_rows_compared_exactly': True,
            'comparison_runtime': {'engine': 'DuckDB', 'version': duckdb.__version__,
                                   'memory_limit': VALIDATION_MEMORY_LIMIT, 'threads': VALIDATION_THREADS,
                                   'preserve_insertion_order': True, 'python_batch_rows': 256}}


def _verify_manifest(directory, manifest, expected_tables=None, verify_logical=False):
    _require(manifest.get('protocol') == PROTOCOL, 'Generation protocol differs')
    _require(manifest.get('all_rows_compared_exactly') is True, 'Generation lacks exact row comparison')
    names = _table_names(manifest['tables'])
    if expected_tables is not None:
        _require(names == _table_names(expected_tables), 'Generation table catalog differs from requested catalog')
    expected_files = {'warehouse.duckdb'} | {'parquet/' + name + '.parquet' for name in names}
    _require(set(manifest['artifacts']) == expected_files, 'Generation artifact catalog differs')
    _require(set(p.name for p in directory.iterdir()) == {'warehouse.duckdb', 'parquet', 'manifest.json'},
             'Unexpected or missing generation files')
    _plain(directory / 'parquet', directory=True)
    _require(set(p.name for p in (directory / 'parquet').iterdir()) == {n + '.parquet' for n in names},
             'Generation Parquet catalog differs')
    for relative, record in manifest['artifacts'].items():
        path = directory / relative
        _plain(path)
        _require(path.stat().st_size == record['size_bytes'] and _sha(path) == record['sha256'],
                 'Generation artifact changed: ' + relative)
    if verify_logical:
        actual = validate_generation(directory, names)
        _require(actual['tables'] == manifest['tables'] and actual['artifacts'] == manifest['artifacts'],
                 'Generation logical data differs from its manifest')


def read_current(root, *, expected_tables=None, verify_logical=False):
    """Pin one atomic reference and verify its complete immutable generation."""
    root = _root(root)
    if not root.exists():
        return None
    _marker(root)
    pointer = root / 'current.json'
    if not pointer.exists():
        _require(not pointer.is_symlink(), 'Current reference must not be a symlink')
        return None
    _plain(pointer)
    reference = json.loads(pointer.read_bytes())
    _require(set(reference) == {'protocol', 'generation_id', 'manifest_sha256'} and reference['protocol'] == PROTOCOL,
             'Invalid current generation reference')
    identity = reference['generation_id']
    _require(isinstance(identity, str) and _GENERATION.fullmatch(identity), 'Invalid generation identity')
    _plain(root / 'generations', directory=True)
    directory = root / 'generations' / identity
    _plain(directory, directory=True)
    path = directory / 'manifest.json'
    _plain(path)
    data = path.read_bytes()
    _require(hashlib.sha256(data).hexdigest() == reference['manifest_sha256'], 'Current manifest hash differs')
    manifest = json.loads(data)
    _require(manifest.get('generation_id') == identity, 'Current manifest identity differs')
    _verify_manifest(directory, manifest, expected_tables, verify_logical)
    return manifest


def _fault(callback, name):
    if callback is not None:
        callback(name)


def publish(root, builder, *, expected_tables, expected_current=None, fault=None):
    """Build, validate and publish; ``None`` expects an empty current reference.

    ``builder(stage)`` must close ``warehouse.duckdb`` and write the exact
    ``parquet/<table>.parquet`` set. It may return JSON-serializable metadata.
    An exception or abrupt process exit leaves the old generation intact. A
    failure after replacing the reference can leave the complete new generation
    active; recovery always reads the reference instead of guessing from an exit
    code. Callers may inject builder faults after database creation/first export.
    """
    root = _root(root)
    tables = _table_names(expected_tables)
    with _writer(root):
        previous = read_current(root)
        observed = previous['generation_id'] if previous else None
        _require(observed == expected_current, 'Current generation changed; stale publication request rejected')
        identity = 'g-' + uuid.uuid4().hex
        stage = root / 'staging' / identity
        stage.mkdir(mode=0o700)
        _fsync_directory(stage.parent)
        metadata = builder(stage)
        validation = validate_generation(stage, tables)
        manifest = {'protocol': PROTOCOL, 'generation_id': identity, 'previous_generation_id': observed,
                    **validation, 'metadata': metadata,
                    'scope': 'Single-filesystem DuckDB and Parquet generation; readers pin current.json once. No distributed PostgreSQL transaction.'}
        _write_new(stage / 'manifest.json', manifest)
        for relative in validation['artifacts']:
            with (stage / relative).open('rb') as stream:
                os.fsync(stream.fileno())
        _fsync_directory(stage / 'parquet')
        _fsync_directory(stage)
        _fault(fault, 'after_validation')
        final = root / 'generations' / identity
        os.rename(stage, final)
        _fsync_directory(root / 'staging')
        _fsync_directory(root / 'generations')
        _fault(fault, 'after_generation_rename')
        reference = {'protocol': PROTOCOL, 'generation_id': identity,
                     'manifest_sha256': _sha(final / 'manifest.json')}
        temporary = root / ('.current-' + uuid.uuid4().hex + '.json')
        _write_new(temporary, reference)
        os.replace(temporary, root / 'current.json')
        _fsync_directory(root)
        _fault(fault, 'after_pointer_replace')
        _require(read_current(root, expected_tables=tables) == manifest, 'Published generation failed readback')
        return manifest


def recover(root):
    """Inspect a stopped writer without choosing, deleting or promoting outputs.

    The verified reference is authoritative. A later explicit ``publish`` with
    that reference as expected_current creates a fresh complete replacement.
    Corrupt active data fail closed; unreferenced files cannot repair it silently.
    """
    root = _root(root)
    with _writer(root):
        current = read_current(root)
        active = current['generation_id'] if current else None
        entries = []
        for category in ('staging', 'generations'):
            for directory in sorted((root / category).iterdir()):
                _require(not directory.is_symlink(), 'Recovery refuses symlink generations')
                if directory.is_dir() and _VALIDATION_TEMP.fullmatch(directory.name):
                    entries.append({'location': category + '/' + directory.name,
                                    'state': 'interrupted_validation_temporary', 'error_type': None})
                    continue
                _require(directory.is_dir() and _GENERATION.fullmatch(directory.name), 'Unknown recovery directory')
                if category == 'generations' and directory.name == active:
                    state, error = 'active_complete', None
                else:
                    try:
                        _plain(directory / 'manifest.json')
                        manifest = json.loads((directory / 'manifest.json').read_bytes())
                        _require(manifest['generation_id'] == directory.name, 'Generation identity differs')
                        _verify_manifest(directory, manifest)
                        state, error = 'complete_unreferenced', None
                    except (ValueError, OSError, KeyError, TypeError) as exc:
                        state, error = 'incomplete_or_invalid', type(exc).__name__
                entries.append({'location': category + '/' + directory.name, 'state': state, 'error_type': error})
        return {'protocol': PROTOCOL, 'status': 'passed_recovery_inspection', 'current_generation_id': active,
                'entries': entries, 'automatic_promotion': False, 'files_deleted': 0,
                'scope': 'Authoritative complete reference preserved; interrupted and orphan generations retained.'}
