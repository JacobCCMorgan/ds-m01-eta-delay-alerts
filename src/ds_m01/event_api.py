"""Local synthetic HTTP transport proof; no external service or authentication.

``check(root)`` really binds loopback, fetches bounded pages with urllib, retries
one injected 503, replays the first page, and writes verified canonical events.
This is an M1 transport check, not a production ingestion service.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from .common import digest_file, parse_ts, read_jsonl, write_jsonl

PAGE_SIZE = 137
MAX_RETRIES = 2


class TransportError(ValueError):
    """A page, cursor, snapshot or event violates the replay contract."""


class LocalBindError(OSError):
    """The runtime did not permit the actual loopback service to start."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TransportError('Redirects are disabled for the local synthetic endpoint')


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _key(row: dict) -> tuple[str, str, str]:
    values = (row.get('tenant_id'), row.get('canonical_event_id'), row.get('record_revision'))
    if any(value is None or isinstance(value, bool) or str(value) == '' for value in values):
        raise TransportError('Event requires tenant_id, canonical_event_id and record_revision')
    return tuple(str(value) for value in values)


def _order(row: dict):
    return (parse_ts(row['available_time']), str(row['canonical_event_id']),
            str(row['tenant_id']), str(row['record_revision']))


def _rows_hash(rows: list[dict]) -> str:
    h = hashlib.sha256()
    for row in rows:
        h.update(_json(row) + b'\n')
    return h.hexdigest()


class EventStore:
    """Idempotent on full tenant/event/revision keys; conflicts fail closed."""

    def __init__(self):
        self.rows: dict[tuple[str, str, str], dict] = {}
        self.payloads: dict[tuple[str, str, str], bytes] = {}
        self.duplicates = 0

    def ingest(self, rows: list[dict]) -> None:
        for row in rows:
            if not isinstance(row, dict):
                raise TransportError('Event must be an object')
            key, payload = _key(row), _json(row)
            _order(row)  # Require a timezone-aware availability timestamp.
            if key in self.payloads:
                if self.payloads[key] != payload:
                    raise TransportError('Conflicting payload for the same tenant/event/revision')
                self.duplicates += 1
            else:
                self.payloads[key] = payload
                self.rows[key] = row

    def ordered(self) -> list[dict]:
        return sorted(self.rows.values(), key=_order)


def _cursor(snapshot: str, offset: int) -> str:
    return base64.urlsafe_b64encode(_json([snapshot, offset])).decode('ascii')


def _offset(token: str | None, snapshot: str, count: int, page_size: int) -> int:
    if token is None:
        return 0
    try:
        if len(token) > 1024:
            raise ValueError('cursor too long')
        item = json.loads(base64.b64decode(token.encode('ascii'), altchars=b'-_', validate=True))
        if (not isinstance(item, list) or len(item) != 2 or item[0] != snapshot
                or type(item[1]) is not int or item[1] < 0
                or item[1] % page_size != 0 or item[1] >= count):
            raise ValueError('cursor does not belong to this snapshot/page boundary')
        return item[1]
    except (ValueError, TypeError, UnicodeError) as exc:
        raise TransportError('Invalid or cross-snapshot cursor') from exc


@contextmanager
def _running_server(rows: list[dict], *, page_size: int = PAGE_SIZE, inject_503: bool = True):
    if type(page_size) is not int or not 1 <= page_size <= PAGE_SIZE:
        raise ValueError('Page size must be between 1 and 137')
    ordered = sorted(rows, key=_order)
    snapshot = _rows_hash(ordered)
    state = {'requests': 0, 'injected_503': 0, 'successful_pages': 0,
             'snapshot': snapshot, 'closed': False}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, status, body):
            payload = _json(body)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path != '/events':
                self.reply(404, {'error': 'unknown endpoint'})
                return
            query = parse_qs(parsed.query, keep_blank_values=True)
            try:
                if set(query) - {'cursor'} or ('cursor' in query and len(query['cursor']) != 1):
                    raise TransportError('Invalid query parameters')
                offset = _offset(query.get('cursor', [None])[0], snapshot, len(ordered), page_size)
            except TransportError as exc:
                self.reply(400, {'error': str(exc)})
                return
            with lock:
                state['requests'] += 1
                fail = inject_503 and state['injected_503'] == 0
                if fail:
                    state['injected_503'] += 1
                else:
                    state['successful_pages'] += 1
            if fail:
                self.reply(503, {'error': 'deterministic synthetic retry fixture'})
                return
            page = ordered[offset:offset + page_size]
            end = offset + len(page)
            self.reply(200, {'source_kind': 'local_synthetic_telemetry', 'snapshot': snapshot,
                            'rows': page, 'rows_sha256': _rows_hash(page),
                            'next_cursor': _cursor(snapshot, end) if end < len(ordered) else None})

    try:
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    except OSError as exc:
        raise LocalBindError(exc.errno, 'Could not bind the local synthetic HTTP endpoint') from exc
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
    try:
        thread.start()
        yield f'http://127.0.0.1:{server.server_port}/events', state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        state['closed'] = not thread.is_alive()
        if not state['closed']:
            raise RuntimeError('Local HTTP server did not shut down')


def _request_page(url: str, *, max_retries: int = MAX_RETRIES) -> tuple[dict, list[int]]:
    address = urlsplit(url)
    if (address.scheme != 'http' or address.hostname != '127.0.0.1'
            or address.username or address.password or address.path != '/events'):
        raise TransportError('Only the local synthetic /events endpoint is allowed')
    # Explicitly ignore environment proxies; requests never leave loopback.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    statuses = []
    for attempt in range(max_retries + 1):
        try:
            with opener.open(url, timeout=5) as response:
                payload = response.read(5 * 1024 * 1024 + 1)
                if len(payload) > 5 * 1024 * 1024:
                    raise TransportError('Page exceeds the bounded response size')
                statuses.append(response.status)
                body = json.loads(payload)
                if not isinstance(body, dict):
                    raise TransportError('Page must be an object')
                return body, statuses
        except HTTPError as exc:
            status = exc.code
            exc.close()
            statuses.append(status)
            if status not in (429, 503) or attempt == max_retries:
                raise TransportError(f'HTTP {status}; bounded attempts exhausted or non-retryable') from exc
            time.sleep(0.01 * (attempt + 1))
    raise AssertionError('Unreachable retry state')


def _validate_page(page: dict, snapshot: str | None, page_size: int):
    if page.get('source_kind') != 'local_synthetic_telemetry':
        raise TransportError('Unexpected source kind')
    current = page.get('snapshot')
    if not isinstance(current, str) or len(current) != 64 or (snapshot and current != snapshot):
        raise TransportError('Source snapshot changed during pagination')
    rows = page.get('rows')
    if not isinstance(rows, list) or len(rows) > page_size:
        raise TransportError('Invalid or oversized page')
    if _rows_hash(rows) != page.get('rows_sha256'):
        raise TransportError('Page content hash mismatch')
    next_cursor = page.get('next_cursor')
    if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor or not rows):
        raise TransportError('Invalid continuation cursor')
    return current, rows, next_cursor


def fetch_paginated(endpoint: str, *, page_size: int = PAGE_SIZE, max_pages: int = 10000) -> tuple[EventStore, dict]:
    """Fetch and replay through real HTTP, rejecting cursor cycles and conflicts."""
    store, seen, records = EventStore(), set(), []
    cursor, snapshot = None, None
    for _ in range(max_pages):
        if cursor in seen:
            raise TransportError('Repeated cursor detected')
        seen.add(cursor)
        url = endpoint + ('?' + urlencode({'cursor': cursor}) if cursor is not None else '')
        page, statuses = _request_page(url)
        snapshot, rows, next_cursor = _validate_page(page, snapshot, page_size)
        store.ingest(rows)
        records.append({'cursor': cursor, 'next_cursor': next_cursor,
                        'rows': len(rows), 'rows_sha256': _rows_hash(rows),
                        'http_statuses': statuses})
        if next_cursor is None:
            break
        if next_cursor in seen:
            raise TransportError('Repeated cursor detected')
        cursor = next_cursor
    else:
        raise TransportError('Pagination exceeded the maximum page count')
    before_replay = len(store.rows)
    page, replay_statuses = _request_page(endpoint)
    _, replay_rows, _ = _validate_page(page, snapshot, page_size)
    if _rows_hash(replay_rows) != records[0]['rows_sha256']:
        raise TransportError('First page changed when replayed')
    duplicates_before_replay = store.duplicates
    store.ingest(replay_rows)
    if len(store.rows) != before_replay:
        raise TransportError('Replay created new canonical events')
    return store, {'snapshot': snapshot, 'pages': len(records), 'page_records': records,
                   'retries': sum(len(r['http_statuses']) - 1 for r in records) + len(replay_statuses) - 1,
                   'http_requests': sum(len(r['http_statuses']) for r in records) + len(replay_statuses),
                   'duplicates_during_pagination': duplicates_before_replay,
                   'duplicate_rows_replayed': store.duplicates - duplicates_before_replay,
                   'duplicate_rows_total': store.duplicates,
                   'replay': {'cursor': None, 'rows': len(replay_rows),
                              'rows_sha256': _rows_hash(replay_rows), 'http_statuses': replay_statuses,
                              'new_canonical_rows': len(store.rows) - before_replay}}


def check(root: Path) -> dict:
    """Roundtrip the actual raw telemetry file; a bind failure returns blocked."""
    root = Path(root)
    source = root / 'data/raw/telemetry_event.jsonl'
    raw = read_jsonl(source)
    if not raw:
        raise TransportError('No telemetry events available for the transport check')
    expected = EventStore()
    expected.ingest(raw)
    ordered_source = expected.ordered()
    report = {'status': 'running', 'scope': 'local synthetic HTTP pagination/replay ingestion proof',
              'started_at_utc': datetime.now(timezone.utc).isoformat(),
              'page_size': PAGE_SIZE, 'max_retries_per_request': MAX_RETRIES,
              'source_rows': len(raw), 'source_unique_events': len(expected.rows),
              'source_duplicate_rows': expected.duplicates,
              'source_file_sha256': digest_file(source),
              'canonical_source_sha256': _rows_hash(ordered_source),
              'network_scope': '127.0.0.1 ephemeral port; no external endpoint or credentials'}
    try:
        with _running_server(raw) as (endpoint, state):
            received, transport = fetch_paginated(endpoint)
            report['endpoint'] = endpoint
        report['server_closed'] = state['closed']
        report['injected_503'] = state['injected_503']
        report['server_http_requests'] = state['requests']
        report['server_successful_pages_including_replay'] = state['successful_pages']
    except LocalBindError as exc:
        report.update(status='blocked', reason=str(exc), server_started=False,
                      server_closed=True, output_written=False)
        return report
    report.update(transport)
    if report['server_http_requests'] != report['http_requests']:
        raise TransportError('Server/client request accounting mismatch')
    report['server_started'] = True
    # Compare independent source and received stores, including full payloads.
    if expected.payloads != received.payloads:
        raise TransportError('HTTP ingestion differs from the raw canonical event set or content')
    canonical_rows = received.ordered()
    if _rows_hash(canonical_rows) != report['canonical_source_sha256']:
        raise TransportError('HTTP ingestion content hash differs from canonical raw source')
    probe = EventStore()
    probe.ingest([canonical_rows[0]])
    conflict = dict(canonical_rows[0], _conflicting_payload_probe=True)
    try:
        probe.ingest([conflict])
    except TransportError:
        report['mismatch_duplicate_payload_rejected'] = True
    else:
        raise TransportError('Conflicting duplicate was accepted')
    destination = root / 'data/ingested/telemetry_event.jsonl'
    write_jsonl(destination, canonical_rows)
    output_hash = digest_file(destination)
    if output_hash != report['canonical_source_sha256']:
        raise TransportError('Written JSONL bytes differ from canonical sorted source bytes')
    report.update(status='passed', output_written=True,
                  output_path='data/ingested/telemetry_event.jsonl', output_rows=len(canonical_rows),
                  output_sha256=output_hash, canonical_sets_and_content_equal=True,
                  raw_file_bytes_equal_output=report['source_file_sha256'] == output_hash,
                  ordering='UTC available_time, canonical_event_id, tenant_id, record_revision',
                  completed_at_utc=datetime.now(timezone.utc).isoformat())
    return report
