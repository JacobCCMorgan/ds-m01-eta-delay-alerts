"""P10 real transport negatives and independent frozen-build projection oracles."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))
from ds_m01 import event_api, pipeline
from ds_m01.common import digest_file, read_jsonl, write_jsonl, write_source_manifest
from workflows.p10 import http_check as check


def event(n, tenant='T01', revision='1'):
    return {'tenant_id': tenant, 'canonical_event_id': f'E{n:04}', 'record_revision': revision,
            'event_time': '2026-01-01T00:00:00.000001Z',
            'available_time': '2025-12-31T16:00:00.000002-08:00', 'latitude_deg': 35 + n / 10000}


@contextmanager
def pages_server(mode, rows):
    """Independent server intentionally violates one wire contract at a time."""
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            calls.append(self.path)
            status = 503 if mode == 'outage' else 200
            selected = copy.deepcopy(rows)
            if mode == 'drop_duplicate':
                selected = list({check._json(row): row for row in selected}.values())
            elif mode == 'reorder':
                selected.reverse()
            elif mode == 'conflict':
                selected.append(dict(rows[0], latitude_deg=99))
            elif mode == 'replay_changed' and len(calls) > 1:
                selected[0]['latitude_deg'] = 99
            body = {'source_kind': 'local_synthetic_telemetry', 'snapshot': check._digest_rows(rows),
                    'rows': selected, 'rows_sha256': check._digest_rows(selected), 'next_cursor': None}
            if mode == 'page_digest':
                body['rows_sha256'] = 'a' * 64
            elif mode == 'snapshot':
                body['snapshot'] = ('a' if len(calls) == 1 else 'b') * 64
            elif mode == 'cycle':
                body['next_cursor'] = 'same'
            payload = json.dumps(body).encode()
            self.send_response(status)
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
        if thread.is_alive():
            raise AssertionError('Test server did not close')


def small_source(root):
    # The existing hand-calculated integration fixture supplies complete source
    # dependencies; frozen pipeline.build is a separate oracle, not our projector.
    spec = importlib.util.spec_from_file_location('p10_build_fixture', ROOT / 'tests/test_build_invariants.py')
    fixture_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture_module)
    case = fixture_module.BuildInvariantTests()
    case.setUp()
    config = copy.deepcopy(case.config)
    config['max_snapshot_hours'] = 1
    config['verify_source_manifest'] = True
    tables = copy.deepcopy(case.tables)
    prototype = json.loads((ROOT / 'tests/fixtures/point_in_time_case.json').read_text())['tables']['telemetry_event'][0]
    # Delayed departure reception, fractional clocks and stale/no-position rows
    # make an incorrect occurrence-time schedule visible.
    tables['eta_trip_event'][1]['available_at'] = '2026-06-23T12:00:00.500001Z'
    tables['telemetry_event'] = [dict(prototype, canonical_event_id='GPS-FOCAL', source_event_id='RAW-FOCAL',
        event_time='2026-06-23T12:00:00.000001Z', available_time='2026-06-23T12:00:00.500000Z',
        provenance_id='HAND-GPS-FOCAL')]
    tables['provenance_record'].append({'tenant_id': 'T01', 'provenance_id': 'HAND-GPS-FOCAL',
                                      'available_time': '2026-06-23T12:00:00.500000Z'})
    for row in tables['provenance_record']:
        if row['provenance_id'] == tables['eta_trip_event'][1]['provenance_id']:
            row['available_time'] = tables['eta_trip_event'][1]['available_at']
    for name in pipeline.RAW_TABLES:
        write_jsonl(root / 'data/raw' / (name + '.jsonl'), tables.get(name, []))
    write_source_manifest(root, {name: len(tables.get(name, [])) for name in pipeline.RAW_TABLES})
    return config, tables


class ActualHTTPReconciliationTests(unittest.TestCase):
    def test_real_pages_retry_duplicate_replay_and_fractional_clocks(self):
        rows = [event(n) for n in reversed(range(140))] + [event(1)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_jsonl(root / 'data/raw/telemetry_event.jsonl', rows)
            receipt = check.transport(root, root / 'fetched.jsonl')
            self.assertEqual(receipt['pages'], 2)
            self.assertEqual(receipt['http_requests'], 4)
            self.assertEqual(receipt['retries'], 1)
            self.assertEqual(receipt['canonical_rows'], 140)
            self.assertEqual(receipt['source_duplicate_rows'], 1)
            self.assertEqual(receipt['duplicate_rows_replayed'], 137)
            self.assertEqual(receipt['replay']['new_canonical_rows'], 0)
            self.assertTrue(receipt['server_closed'])
            self.assertEqual(receipt['output_file_sha256'], digest_file(root / 'fetched.jsonl'))
            self.assertEqual(read_jsonl(root / 'fetched.jsonl')[0]['available_time'], rows[0]['available_time'])

    def test_real_transport_preserves_tenants_and_revisions_but_projection_rejects_g11(self):
        rows = [event(1), event(1, tenant='T02'), event(1, revision='2')]
        with event_api._running_server(rows) as (endpoint, _):
            actual, receipt = check.reconcile_endpoint(rows, endpoint)
        self.assertEqual(len(actual), 3)
        self.assertTrue(receipt['tenant_event_revision_identity_preserved'])
        with self.assertRaisesRegex(ValueError, 'G11'):
            check.reject_unsupported_revisions(rows)
        check.reject_unsupported_revisions(rows[:2])

    def test_validly_hashed_incomplete_or_substituted_source_rejected(self):
        expected = [event(1), event(2)]
        for rows in ([event(1)], [event(1), event(2, tenant='T02')],
                     [event(1), event(2, revision='2')], [event(1), event(2), event(3)]):
            with self.subTest(rows=rows), event_api._running_server(rows) as (endpoint, _):
                with self.assertRaisesRegex(ValueError, 'full source payload set'):
                    check.reconcile_endpoint(expected, endpoint)

    def test_whole_source_digest_claim_cannot_hide_missing_duplicates_or_wrong_page_order(self):
        expected = [event(1), event(1), event(2)]
        for mode in ('drop_duplicate', 'reorder'):
            with self.subTest(mode=mode), pages_server(mode, expected) as (endpoint, _):
                with self.assertRaisesRegex(ValueError, 'deterministic page content'):
                    check.reconcile_endpoint(expected, endpoint)

    def test_real_faults_fail_and_retry_cycle_bounds_are_observed(self):
        expected = [event(1), event(2)]
        for mode in ('page_digest', 'conflict', 'snapshot', 'replay_changed', 'cycle', 'outage'):
            with self.subTest(mode=mode), pages_server(mode, expected) as (endpoint, calls):
                with self.assertRaises(ValueError):
                    check.reconcile_endpoint(expected, endpoint)
                if mode == 'outage':
                    self.assertEqual(len(calls), 3)
                elif mode == 'cycle':
                    self.assertEqual(len(calls), 2)

    def test_cross_snapshot_cursor_actual_endpoint_returns_400(self):
        rows = [event(1), event(2)]
        opener = build_opener(ProxyHandler({}))
        with event_api._running_server(rows, page_size=1, inject_503=False) as (endpoint, _):
            token = event_api._cursor('different-snapshot', 1)
            with self.assertRaises(HTTPError) as caught:
                opener.open(endpoint + '?cursor=' + token, timeout=3)
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()

    def test_conflicting_source_and_invalid_clocks_fail_before_output(self):
        invalid_sets = [[event(1), dict(event(1), latitude_deg=99)],
                        [dict(event(1), event_time='2026-01-01T00:00:00')],
                        [dict(event(1), available_time='2026-01-01T00:00:00.1234567Z')]]
        for rows in invalid_sets:
            with self.subTest(rows=rows), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_jsonl(root / 'data/raw/telemetry_event.jsonl', rows)
                with self.assertRaises(ValueError):
                    check.transport(root, root / 'fetched.jsonl')
                self.assertFalse((root / 'fetched.jsonl').exists())


class FrozenPipelineProjectionTests(unittest.TestCase):
    def test_projection_matches_frozen_pipeline_all_four_tables_and_seven_outputs_http_parity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, _ = small_source(root)
            projection = check.project(root, config)
            first_summary = pipeline.build(root, config)
            expected_files = {path.name: digest_file(path) for path in (root / 'data/processed').iterdir()}
            self.assertEqual(len(expected_files), 7)
            for projected, table in (('snapshots', 'eta_snapshot'), ('labels', 'eta_snapshot_label'),
                                     ('split_assignments', 'eta_split_assignment'), ('memberships', 'trip_membership')):
                self.assertEqual(projection[projected], read_jsonl(root / 'data/processed' / (table + '.jsonl')))
                self.assertEqual(projection['evidence']['tables'][projected]['serialized_jsonl_sha256'],
                                 expected_files[table + '.jsonl'])
            # Fractional first-known departure must not be rounded or delayed 30 min.
            focal = [row for row in projection['snapshots'] if row['trip_id'] == 'FOCAL']
            self.assertEqual(focal[0]['snapshot_time'], '2026-06-23T12:00:00.500001Z')
            self.assertEqual(focal[0]['position_age_seconds'], 0)
            self.assertEqual(focal[1]['position_validity'], 'low_confidence')
            self.assertTrue(any(row['position_validity'] == 'no_position' for row in projection['snapshots']))
            check.transport(root, root / 'data/ingested/telemetry_event.jsonl')
            second_summary = pipeline.build(root, dict(config, telemetry_mode='http_snapshot'))
            self.assertEqual(second_summary, first_summary)
            self.assertEqual(expected_files, {path.name: digest_file(path) for path in (root / 'data/processed').iterdir()})

    def test_changed_plan_vehicle_and_extract_boundary_follow_frozen_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, tables = small_source(root)
            config['extract_as_of'] = '2026-06-23T12:30:00.500001Z'
            changed = dict(tables['eta_trip_plan_version'][1], trip_version_id='FOCAL-V2', vehicle_id='V-2',
                           plan_known_at='2026-06-23T12:20:00Z', provenance_id='PLAN-V2')
            tables['eta_trip_plan_version'].append(changed)
            position = dict(tables['telemetry_event'][0], canonical_event_id='GPS-V2', source_event_id='RAW-V2',
                            vehicle_id='V-2', device_id='D-2', event_time='2026-06-23T12:29:59.999999Z',
                            available_time='2026-06-23T12:30:00Z', provenance_id='POSITION-V2')
            tables['telemetry_event'].append(position)
            tables['core_vehicle_device_assignment'].append(dict(tables['core_vehicle_device_assignment'][0],
                vehicle_id='V-2', device_id='D-2', assignment_id='ASSIGN-V2', provenance_id='ASSIGN-V2'))
            for name in pipeline.RAW_TABLES:
                write_jsonl(root / 'data/raw' / (name + '.jsonl'), tables.get(name, []))
            write_source_manifest(root, {name: len(tables.get(name, [])) for name in pipeline.RAW_TABLES})
            result = check.project(root, config)
            pipeline.build(root, config)
            self.assertEqual(result['snapshots'], read_jsonl(root / 'data/processed/eta_snapshot.jsonl'))
            focal = [row for row in result['snapshots'] if row['trip_id'] == 'FOCAL']
            self.assertEqual(len(focal), 2)
            self.assertEqual(focal[-1]['selected_position_event_id'], 'GPS-V2')
            self.assertEqual(focal[-1]['position_age_seconds'], 0)

    def test_selected_promises_and_exclusive_unknown_label_cutoff_match_frozen_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, tables = small_source(root)
            config['split']['train_label_cutoff_exclusive'] = '2026-06-23T12:30:00Z'
            revised = dict(tables['eta_promise_version'][1], promise_version_id='PROMISE-FOCAL-V2',
                           known_at='2026-06-23T12:15:00Z', window_end_at='2026-06-23T12:45:00Z',
                           provenance_id='PROMISE-V2')
            tables['eta_promise_version'].append(revised)
            tables['eta_trip_outcome'][1]['label_available_time'] = '2026-06-23T12:30:00Z'
            pending = dict(tables['eta_trip_outcome'][1], outcome_version_id='FOCAL-PENDING',
                           label_status='pending', label_available_time='2026-06-23T12:29:59.999999Z',
                           actual_arrival_time=None, provenance_id='FOCAL-PENDING')
            tables['eta_trip_outcome'].append(pending)
            for name in pipeline.RAW_TABLES:
                write_jsonl(root / 'data/raw' / (name + '.jsonl'), tables.get(name, []))
            write_source_manifest(root, {name: len(tables.get(name, [])) for name in pipeline.RAW_TABLES})
            result = check.project(root, config)
            pipeline.build(root, config)
            self.assertEqual(result['labels'], read_jsonl(root / 'data/processed/eta_snapshot_label.jsonl'))
            focal = [row for row in result['snapshots'] if row['trip_id'] == 'FOCAL']
            self.assertEqual([row['promise_version_id'] for row in focal], ['PROMISE-FOCAL', 'PROMISE-FOCAL-V2'])
            labels = [row for row in result['labels'] if row['snapshot_id'] in {r['snapshot_id'] for r in focal}]
            self.assertEqual({row['outcome_version_id'] for row in labels}, {'FOCAL-PENDING'})
            self.assertTrue(all(row['delay_outcome'] is None and row['remaining_duration_seconds'] is None
                                and row['label_eligibility'] == 'not_mature' for row in labels))

    def test_unselected_telemetry_payload_mutation_cannot_pass_as_http_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, tables = small_source(root)
            altered = [dict(tables['telemetry_event'][0], raw_record_id='changed-unused-field')]
            write_jsonl(root / 'fetched.jsonl', altered)
            with self.assertRaisesRegex(ValueError, 'HTTP-fed telemetry full source payload set differs'):
                check.project(root, config, telemetry_file=root / 'fetched.jsonl')

    def test_pg_equivalent_clock_spelling_and_order_pass_full_row_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, pg = root / 'source', root / 'pg'
            config, tables = small_source(source)
            for table in check.OPERATIONAL_TABLES:
                rows = copy.deepcopy(tables.get(table, []))
                for row in rows:
                    for key, value in row.items():
                        if isinstance(value, str) and value.endswith('Z'):
                            row[key] = value[:-1] + '+00:00'
                write_jsonl(pg / 'data/raw' / (table + '.jsonl'), list(reversed(rows)))
            direct = check.project(source, config)
            exported = check.project(source, config, operational_root=pg)
            self.assertEqual(check.compare_projections(direct, exported),
                             {name: check._row_inventory(exported[name]) for name in check.PROJECTION_TABLES})

    def test_unselected_operational_field_tampering_is_not_hidden_by_equal_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, pg = root / 'source', root / 'pg'
            config, tables = small_source(source)
            for table in check.OPERATIONAL_TABLES:
                rows = copy.deepcopy(tables.get(table, []))
                if table == 'core_vehicle':
                    rows[0]['invented_field'] = None
                write_jsonl(pg / 'data/raw' / (table + '.jsonl'), rows)
            with self.assertRaisesRegex(ValueError, 'Operational full-row readback differs: core_vehicle'):
                check.project(source, config, operational_root=pg)

    def test_manifest_failure_and_existing_output_cannot_publish_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / 'source', root / 'out'
            config, _ = small_source(source)
            path = source / 'data/raw/eta_trip.jsonl'
            path.write_bytes(path.read_bytes() + b'{}\n')
            with self.assertRaises((ValueError, OSError)):
                check.check_http_snapshot(ROOT, source, output, config)
            self.assertFalse(output.exists())
            output.mkdir()
            with self.assertRaisesRegex(ValueError, 'must be new'):
                check.check_http_snapshot(ROOT, source, output, config)
            self.assertEqual(list(output.iterdir()), [])

    def test_success_requires_source_unchanged_and_retains_only_bounded_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / 'source', root / 'out'
            config, _ = small_source(source)
            before = {path.name: digest_file(path) for path in (source / 'data/raw').iterdir()}
            receipt = check.check_http_snapshot(ROOT, source, output, config)
            self.assertEqual(receipt['status'], 'passed')
            self.assertTrue(receipt['source_unchanged'])
            self.assertFalse(receipt['full_scale_history_lineage_warehouse_rebuilt'])
            self.assertFalse(receipt['model_quality_accepted'])
            self.assertEqual({path.name for path in output.iterdir()}, {'receipt.json', 'telemetry_event.jsonl'})
            self.assertEqual(before, {path.name: digest_file(path) for path in (source / 'data/raw').iterdir()})


if __name__ == '__main__':
    unittest.main()
