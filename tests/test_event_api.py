"""Independent local HTTP fixtures for pagination, replay and failure bounds."""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

from ds_m01.event_api import (EventStore, TransportError, _cursor, _offset,
                             _request_page, _running_server, check, fetch_paginated)


def event(n, tenant='T01', revision='1'):
    return {'tenant_id': tenant, 'canonical_event_id': f'E{n:04}',
            'record_revision': revision, 'available_time': '2026-01-01T00:00:00Z',
            'latitude_deg': 35 + n / 10000}


def canonical(rows):
    return ''.join(json.dumps(row, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':')) + '\n' for row in rows).encode()


@contextmanager
def faulty_server(mode):
    calls = []
    row = event(1)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            calls.append(self.path)
            if mode == 'redirect':
                self.send_response(302)
                self.send_header('Location', 'https://example.invalid/events')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            body = ({'error': 'persistent outage'} if mode == '503' else
                    {'source_kind': 'local_synthetic_telemetry', 'snapshot': 'a' * 64,
                     'rows': [row], 'rows_sha256': hashlib.sha256(canonical([row])).hexdigest(),
                     'next_cursor': 'same-cursor'})
            payload = json.dumps(body).encode()
            self.send_response(503 if mode == '503' else 200)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/events', calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class HTTPTransportTests(unittest.TestCase):
    def test_actual_check_paginates_retries_replays_and_writes_canonical_bytes(self):
        # Reversed order and a real duplicate prevent a file-copy implementation
        # from accidentally satisfying canonical ordering/idempotency assertions.
        unique = [event(n) for n in range(140)]
        raw = list(reversed(unique)) + [event(2)]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'data/raw/telemetry_event.jsonl'
            source.parent.mkdir(parents=True)
            source.write_bytes(canonical(raw))
            result = check(root)
            self.assertEqual(result['status'], 'passed')
            self.assertEqual(result['pages'], 2)
            self.assertEqual(result['retries'], 1)
            self.assertEqual(result['injected_503'], 1)
            self.assertEqual(result['http_requests'], 4)
            self.assertEqual(result['source_duplicate_rows'], 1)
            self.assertEqual(result['duplicates_during_pagination'], 1)
            self.assertEqual(result['duplicate_rows_replayed'], 137)
            self.assertEqual(result['replay']['new_canonical_rows'], 0)
            self.assertTrue(result['server_closed'])
            self.assertTrue(result['mismatch_duplicate_payload_rejected'])
            self.assertFalse(result['raw_file_bytes_equal_output'])
            self.assertEqual((root / result['output_path']).read_bytes(), canonical(unique))

    def test_full_identity_preserves_other_tenant_and_new_revision(self):
        store = EventStore()
        store.ingest([event(1), event(1, tenant='T02'), event(1, revision='2'), event(1)])
        self.assertEqual(len(store.rows), 3)
        self.assertEqual(store.duplicates, 1)
        conflicting = dict(event(1), latitude_deg=99)
        with self.assertRaisesRegex(TransportError, 'Conflicting payload'):
            store.ingest([conflicting])
        self.assertEqual(len(store.rows), 3)

    def test_bad_and_cross_snapshot_cursors_rejected_by_actual_endpoint(self):
        rows = [event(n) for n in range(3)]
        opener = build_opener(ProxyHandler({}))
        with _running_server(rows, page_size=2, inject_503=False) as (endpoint, _state):
            for token in ['invalid', _cursor('wrong-snapshot', 2)]:
                with self.assertRaises(HTTPError) as context:
                    opener.open(endpoint + '?cursor=' + token, timeout=3)
                self.assertEqual(context.exception.code, 400)
                context.exception.close()
        for offset in [True, -1, 1, 4]:
            with self.assertRaises(TransportError):
                _offset(_cursor('snapshot', offset), 'snapshot', 3, 2)

    def test_repeated_cursor_stops_real_http_after_two_pages(self):
        with faulty_server('cycle') as (endpoint, calls):
            with self.assertRaisesRegex(TransportError, 'Repeated cursor'):
                fetch_paginated(endpoint)
            self.assertEqual(len(calls), 2)

    def test_persistent_503_has_bounded_actual_http_attempts(self):
        with faulty_server('503') as (endpoint, calls):
            with self.assertRaisesRegex(TransportError, 'bounded attempts exhausted'):
                _request_page(endpoint, max_retries=2)
            self.assertEqual(len(calls), 3)

    def test_external_endpoint_is_rejected_before_any_network_call(self):
        with self.assertRaisesRegex(TransportError, 'Only the local'):
            _request_page('https://example.invalid/events')

    def test_redirect_cannot_send_client_outside_loopback(self):
        with faulty_server('redirect') as (endpoint, calls):
            with self.assertRaisesRegex(TransportError, 'Redirects are disabled'):
                _request_page(endpoint)
            self.assertEqual(len(calls), 1)

    def test_bind_failure_reports_blocked_without_a_fake_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'data/raw/telemetry_event.jsonl'
            source.parent.mkdir(parents=True)
            source.write_bytes(canonical([event(1)]))
            with patch('ds_m01.event_api.ThreadingHTTPServer', side_effect=PermissionError('bind denied')):
                result = check(root)
            self.assertEqual(result['status'], 'blocked')
            self.assertFalse(result['server_started'])
            self.assertFalse(result['output_written'])
            self.assertFalse((root / 'data/ingested/telemetry_event.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
