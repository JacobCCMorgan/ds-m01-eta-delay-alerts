"""P10 bounded source reconciliation over a real loopback HTTP transport.

The production candidate's frozen source modules are imported, never rewritten.
Full M2 checks cover all source payloads and every snapshot/label column. They do
not rebuild the much larger historical pool, derived lineage, or warehouse.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile

from ds_m01 import event_api, pipeline
from ds_m01.common import digest_file, parse_ts, read_jsonl, utc, verify_source_manifest, write_jsonl

OPERATIONAL_TABLES = tuple(t for t in pipeline.RAW_TABLES if t not in ('telemetry_event', 'provenance_record'))
PROJECTION_TABLES = ('snapshots', 'labels', 'split_assignments', 'memberships')
_CLOCK = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$')


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _digest_rows(rows):
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_json(row) + b'\n')
    return digest.hexdigest()


def logical(value):
    """Equivalent UTC spellings/numeric SQL representations; no rounding.

    A missing key remains distinct from explicit null. Boolean values are never
    coerced to numbers; timestamp fractions retain Python's microsecond precision.
    All other strings, including state and identity fields, remain exact.
    """
    if isinstance(value, dict):
        return {k: logical(v) for k, v in value.items()}
    if isinstance(value, list):
        return [logical(v) for v in value]
    if isinstance(value, str) and _CLOCK.fullmatch(value):
        # Reject fractions beyond the source/schema precision instead of silently
        # letting datetime truncate them while comparing PostgreSQL readback.
        fraction = re.search(r'\.(\d+)', value)
        _require(fraction is None or len(fraction.group(1)) <= 6, 'Clock precision exceeds the microsecond source contract')
        return utc(parse_ts(value))
    if isinstance(value, float):
        _require(math.isfinite(value), 'Non-finite numeric source value')
        return int(value) if value.is_integer() else value
    return value


def _row_inventory(rows):
    canonical = sorted((_json(logical(row)) for row in rows))
    digest = hashlib.sha256()
    for row in canonical:
        digest.update(row + b'\n')
    return {'rows': len(canonical), 'sha256': digest.hexdigest()}


def _event_identity(row):
    _require(isinstance(row, dict), 'Telemetry event must be an object')
    fields = ('tenant_id', 'canonical_event_id', 'record_revision')
    _require(all(row.get(k) is not None and not isinstance(row[k], bool) and str(row[k]) for k in fields),
             'Telemetry requires full tenant/event/revision identity')
    return tuple(str(row[k]) for k in fields)


def _event_order(row):
    return (parse_ts(row['available_time']), str(row['canonical_event_id']),
            str(row['tenant_id']), str(row['record_revision']))


def _expected_events(rows):
    _require(bool(rows), 'Telemetry source is empty')
    payloads = {}
    clocks = {}
    for row in rows:
        key = _event_identity(row)
        # Both clocks are independently checked, including their source precision.
        logical(row)
        clock = {name: utc(parse_ts(row[name])) for name in ('event_time', 'available_time')}
        payload = _json(row)
        _require(key not in payloads or payloads[key] == payload,
                 'Conflicting source payload for the same tenant/event/revision')
        payloads[key], clocks[key] = payload, clock
    return payloads, clocks


def reject_unsupported_revisions(rows):
    revisions = defaultdict(set)
    for row in rows:
        tenant, event, revision = _event_identity(row)
        revisions[tenant, event].add(revision)
    _require(all(len(values) == 1 for values in revisions.values()),
             'G11: whole-pipeline reconciliation does not support multiple revisions sharing a canonical_event_id')


def reconcile_endpoint(expected_rows, endpoint, *, page_size=event_api.PAGE_SIZE):
    """Fetch real HTTP pages and independently require the complete source set.

    Per-page hashes alone cannot detect a validly hashed, prematurely terminated
    page sequence. The registered full source payload map and snapshot digest do.
    """
    expected, expected_clocks = _expected_events(expected_rows)
    store, receipt = event_api.fetch_paginated(endpoint, page_size=page_size)
    observed = {_event_identity(row): _json(row) for row in store.ordered()}
    _require(observed == expected, 'HTTP full source payload set differs (missing, extra, changed, tenant or revision)')
    _, observed_clocks = _expected_events(store.ordered())
    _require(observed_clocks == expected_clocks, 'HTTP event/availability clocks differ')
    ordered_source = sorted(expected_rows, key=_event_order)
    source_snapshot = _digest_rows(ordered_source)
    _require(receipt['snapshot'] == source_snapshot, 'HTTP whole-source snapshot digest differs')
    expected_pages = [ordered_source[offset:offset + page_size]
                      for offset in range(0, len(ordered_source), page_size)]
    _require(len(receipt['page_records']) == len(expected_pages), 'HTTP deterministic page count differs')
    for record, expected_page in zip(receipt['page_records'], expected_pages):
        _require(record['rows'] == len(expected_page) and record['rows_sha256'] == _digest_rows(expected_page),
                 'HTTP deterministic page content or source duplicate multiplicity differs')
    _require(receipt['duplicates_during_pagination'] == len(expected_rows) - len(expected),
             'HTTP source duplicate accounting differs')
    _require(receipt['replay']['new_canonical_rows'] == 0, 'First-page replay was not idempotent')
    canonical = store.ordered()
    clock_rows = [{'identity': list(key), **expected_clocks[key]} for key in sorted(expected_clocks)]
    result = dict(receipt, source_rows=len(expected_rows), canonical_rows=len(canonical),
                  source_duplicate_rows=len(expected_rows) - len(expected),
                  canonical_rows_sha256=_digest_rows(canonical),
                  canonical_clock_sha256=_digest_rows(clock_rows),
                  full_payload_set_equal=True, event_and_available_clocks_equal=True,
                  deterministic_pages_equal=True, source_duplicate_multiplicity_equal=True,
                  tenant_event_revision_identity_preserved=True)
    return canonical, result


def transport(source_root, output_file):
    """Serve the frozen telemetry file through an actual temporary loopback server."""
    source_file = Path(source_root).resolve() / 'data/raw/telemetry_event.jsonl'
    output_file = Path(output_file).resolve()
    _require(not output_file.exists(), 'Refusing to replace an existing HTTP output')
    _require(output_file != source_file, 'HTTP output must not replace the source')
    before = digest_file(source_file)
    rows = read_jsonl(source_file)
    _expected_events(rows)
    with event_api._running_server(rows, inject_503=True) as (endpoint, state):
        canonical, report = reconcile_endpoint(rows, endpoint)
        _require(state['requests'] == report['http_requests'], 'HTTP client/server request accounting differs')
        _require(state['injected_503'] == report['retries'] == 1, 'Expected one actual bounded 503 retry')
    _require(state['closed'], 'HTTP server did not close')
    _require(digest_file(source_file) == before, 'Telemetry source changed during HTTP reconciliation')
    output_file.parent.mkdir(parents=True, exist_ok=True)
    # All source/transport checks precede publication of this input artifact.
    write_jsonl(output_file, canonical)
    _require(digest_file(output_file) == report['canonical_rows_sha256'], 'Persisted HTTP source digest differs')
    return dict(report, source_file_sha256=before, output_file_sha256=digest_file(output_file),
                server_closed=True, injected_503=state['injected_503'], transport='actual_loopback_http',
                network_scope='127.0.0.1 only; no external service or authentication claim')


def _load_tables(source_root, operational_root=None, telemetry_file=None):
    source_root = Path(source_root).resolve()
    operational_root = source_root if operational_root is None else Path(operational_root).resolve()
    tables, inputs = {}, {}
    for table in OPERATIONAL_TABLES:
        baseline = read_jsonl(source_root / 'data/raw' / (table + '.jsonl'))
        path = operational_root / 'data/raw' / (table + '.jsonl')
        rows = baseline if operational_root == source_root else read_jsonl(path)
        expected, actual = _row_inventory(baseline), _row_inventory(rows)
        _require(expected == actual, 'Operational full-row readback differs: ' + table)
        tables[table], inputs[table] = rows, actual
    telemetry_file = source_root / 'data/raw/telemetry_event.jsonl' if telemetry_file is None else Path(telemetry_file)
    tables['telemetry_event'] = read_jsonl(telemetry_file)
    actual_payloads, _ = _expected_events(tables['telemetry_event'])
    source_payloads, _ = _expected_events(read_jsonl(source_root / 'data/raw/telemetry_event.jsonl'))
    _require(actual_payloads == source_payloads, 'HTTP-fed telemetry full source payload set differs')
    reject_unsupported_revisions(tables['telemetry_event'])
    inputs['telemetry_event'] = _row_inventory(tables['telemetry_event'])
    return tables, inputs


def project(source_root, config, *, telemetry_file=None, operational_root=None, verify_manifest=True):
    """Evaluate all frozen-build snapshot/label opportunities without the history pool.

    Scheduling and input scoping mirror pipeline.build; the frozen point-in-time,
    split and label functions themselves are called for every trip/opportunity.
    Every returned visible column participates in the order-independent digest.
    """
    if verify_manifest:
        verify_source_manifest(Path(source_root))
    tables, input_evidence = _load_tables(source_root, operational_root, telemetry_file)
    interval = config['settings']['snapshot_interval_minutes']
    _require(isinstance(interval, (int, float)) and not isinstance(interval, bool) and interval > 0,
             'Snapshot interval must be positive')
    _require(config.get('max_snapshot_hours', 12) >= 0, 'Snapshot horizon must be nonnegative')
    tables['_settings'] = config
    telemetry = defaultdict(list)
    plans, events, promises, outcomes = (defaultdict(list) for _ in range(4))
    for row in tables['telemetry_event']:
        telemetry[row['tenant_id'], row['vehicle_id']].append(row)
    for name, index in (('eta_trip_plan_version', plans), ('eta_trip_event', events),
                        ('eta_promise_version', promises), ('eta_trip_outcome', outcomes)):
        for row in tables[name]:
            index[row['tenant_id'], row['trip_id']].append(row)
    trip_indexes = {'eta_trip_plan_version': plans, 'eta_promise_version': promises, 'eta_trip_event': events}
    snapshots, labels, assignments, memberships = [], [], [], []
    split_by_trip = {}
    opportunities = 0
    for trip in tables['eta_trip']:
        key = (trip['tenant_id'], trip['trip_id'])
        _require(key not in split_by_trip, 'Duplicate trip identity in source')
        plan = min(plans[key], key=lambda row: parse_ts(row['plan_known_at']))
        departures = [row for row in events[key] if row['event_type'] in ('departure', 'departed')]
        _require(bool(departures), 'Actual departure evidence is required: ' + repr(key))
        departure = min(departures, key=lambda row: parse_ts(row['occurred_at']))['occurred_at']
        route = next(row for row in tables['eta_route_version']
                     if row['tenant_id'] == key[0] and row['route_version_id'] == plan['route_version_id'])
        split = pipeline.assign_split(*key, departure, plan['vehicle_id'], route['route_id'], config)
        assignments.append(split)
        split_by_trip[key] = split
        unseen_v = plan['vehicle_id'] in config['heldout']['vehicle_ids']
        unseen_r = route['route_id'] in config['heldout']['route_ids']
        memberships.append(dict(tenant_id=key[0], trip_id=key[1], split_role=split['split_role'],
            vehicle_id=plan['vehicle_id'], route_id=route['route_id'], unseen_vehicle=unseen_v, unseen_route=unseen_r,
            exclusion_reason=('heldout_before_test' if unseen_v or unseen_r else 'label_buffer_or_outside_main')
            if split['split_role'] == 'excluded' else None))
        scoped = {name: (trip_indexes[name][key] if name in trip_indexes else rows)
                  for name, rows in tables.items()
                  if name not in ('telemetry_event', 'eta_trip_outcome', 'provenance_record', '_settings')}
        start = parse_ts(departure)
        end = min(start + timedelta(hours=config.get('max_snapshot_hours', 12)), parse_ts(config['extract_as_of']))
        vehicles = {row['vehicle_id'] for row in plans[key]}
        scoped['telemetry_event'] = [row for vehicle in vehicles for row in telemetry[key[0], vehicle]
                                     if start <= parse_ts(row['event_time']) <= end]
        scoped['core_vehicle_device_assignment'] = [row for row in tables['core_vehicle_device_assignment']
                                                    if row['tenant_id'] == key[0] and row['vehicle_id'] in vehicles]
        scoped['_settings'] = config
        at = max(start, min(parse_ts(row['available_at']) for row in departures))
        while at <= end:
            opportunities += 1
            snap = pipeline.snapshot_for_trip(*key, utc(at), scoped)
            if snap is not None:
                snapshots.append(snap)
            at += timedelta(minutes=interval)
    # Outcomes cannot decide which feature snapshots exist.
    for snapshot in snapshots:
        key = (snapshot['tenant_id'], snapshot['trip_id'])
        label = pipeline.label_for_snapshot(snapshot, outcomes[key], promises[key], split_by_trip[key]['label_cutoff_at'])
        _require(label['outcome_version_id'] is not None, 'Missing pending outcome before label cutoff: ' + repr(key))
        labels.append(label)
    result = dict(snapshots=snapshots, labels=labels, split_assignments=assignments, memberships=memberships)
    result['evidence'] = {
        'tables': {name: dict(_row_inventory(result[name]), serialized_jsonl_sha256=_digest_rows(result[name]))
                   for name in PROJECTION_TABLES},
        'inputs': input_evidence,
        'trip_count': len(tables['eta_trip']), 'scheduled_opportunities': opportunities,
        'snapshot_count': len(snapshots), 'label_count': len(labels),
        'position_quality': dict(sorted(Counter(row['position_validity'] for row in snapshots).items())),
        'label_eligibility': dict(sorted(Counter(row['label_eligibility'] for row in labels).items())),
        'config_sha256': hashlib.sha256(_json(config)).hexdigest(),
        'comparison': 'all columns; exact logical values; UTC-equivalent clocks; no float tolerance',
        'scope': 'snapshot/label/split/membership projection; no full-scale history, lineage or warehouse rebuild',
    }
    return result


def compare_projections(reference, observed):
    for table in PROJECTION_TABLES:
        _require(_row_inventory(reference[table]) == _row_inventory(observed[table]),
                 'Source input projection differs: ' + table)
    _require(reference['evidence']['scheduled_opportunities'] == observed['evidence']['scheduled_opportunities'],
             'Snapshot opportunity schedule differs')
    return {name: _row_inventory(observed[name]) for name in PROJECTION_TABLES}


def check_http_snapshot(repo_root, source_root, output, config=None, *, operational_root=None):
    """Publish a success proof only after all source modes and immutable inputs agree."""
    repo_root, source_root, output = (Path(value).resolve() for value in (repo_root, source_root, output))
    _require(not output.exists(), 'Output directory must be new')
    _require(not output.is_relative_to(repo_root) and not output.is_relative_to(source_root),
             'Output directory must be outside the checkout and source root')
    _require(not repo_root.is_relative_to(output) and not source_root.is_relative_to(output),
             'Output directory must not contain the checkout or source root')
    if config is None:
        config = json.loads((repo_root / 'config/m2.json').read_text())
    started = datetime.now(timezone.utc).isoformat()
    manifest = verify_source_manifest(source_root)
    manifest_sha256 = digest_file(source_root / 'data/source_manifest.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.p10-http-', dir=output.parent) as temporary:
        stage = Path(temporary)
        ingested = stage / 'telemetry_event.jsonl'
        transport_report = transport(source_root, ingested)
        direct = project(source_root, config, verify_manifest=False)
        fetched = project(source_root, config, telemetry_file=ingested, verify_manifest=False)
        comparisons = {'snapshot_vs_http': compare_projections(direct, fetched)}
        modes = {'snapshot': direct['evidence'], 'http': fetched['evidence']}
        if operational_root is not None:
            combined = project(source_root, config, telemetry_file=ingested,
                               operational_root=operational_root, verify_manifest=False)
            comparisons['snapshot_vs_postgres_http'] = compare_projections(direct, combined)
            modes['postgres_http'] = combined['evidence']
        _require(verify_source_manifest(source_root) == manifest and
                 digest_file(source_root / 'data/source_manifest.json') == manifest_sha256,
                 'Source inventory changed during reconciliation')
        report = {
            'status': 'passed', 'started_at': started, 'finished_at': datetime.now(timezone.utc).isoformat(),
            'source_manifest_sha256': manifest_sha256, 'source_files': manifest['files'],
            'transport': transport_report, 'projections': modes, 'comparisons': comparisons,
            'source_unchanged': True, 'postgres_http_projection_checked': operational_root is not None,
            'full_scale_history_lineage_warehouse_rebuilt': False,
            'multiple_revisions_whole_pipeline_supported': False,
            'model_refit': False, 'model_quality_accepted': False,
            'scope': 'synthetic source adapter and bounded projection parity; not external service, CI, deployment or M5.2 acceptance',
        }
        # Keep the verified HTTP input for the independent combined-source checks.
        (stage / 'receipt.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        stage.rename(output)
    return report
