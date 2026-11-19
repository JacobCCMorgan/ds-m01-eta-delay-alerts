"""P11 revision-aware telemetry, point-in-time positions and typed persistence.

This additive entry point leaves the scientific and historical P08/P09/P10 paths
unchanged. An event is identified by tenant/canonical event; its immutable source
identity is tenant/source system/source event. A version adds a canonical positive
integer revision string. At a snapshot, choose the highest known revision before
filtering vehicle assignment or GNSS validity. A correction may invalidate a
previous position, but a later-arriving lower revision cannot roll it back.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

import duckdb

from ds_m01 import event_api, pipeline
from ds_m01.common import digest_file, ident, parse_ts, read_jsonl, utc, write_jsonl
from workflows.p10.http_check import reconcile_endpoint

REVISION = re.compile(r"[1-9][0-9]{0,19}\Z")
CLOCK = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.[0-9]{1,6})?(?:Z|[+-]\d\d:\d\d)\Z")
FEATURE_VERSION = "p11-revision-aware-features-v1"
STORE_TABLES = ("provenance_record", "telemetry_event", "eta_snapshot", "p11_snapshot_position")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def clock(value):
    require(isinstance(value, str) and CLOCK.fullmatch(value), "Known timezone-aware microsecond source clock required")
    return parse_ts(value)


def identity(row):
    require(isinstance(row, dict), "Telemetry must be a JSON object")
    keys = ("tenant_id", "canonical_event_id", "record_revision", "source_system_id", "source_event_id", "provenance_id")
    require(all(isinstance(row.get(k), str) and row[k] and row[k].strip() == row[k] for k in keys),
            "Telemetry requires explicit string identity and provenance")
    require(REVISION.fullmatch(row["record_revision"]), "Revision must be a canonical positive integer string of at most 20 digits")
    require(clock(row.get("event_time")) <= clock(row.get("available_time")), "Source availability precedes event time")
    for field in ("latitude_deg", "longitude_deg", "gnss_speed_kmh", "vehicle_speed_kmh"):
        require(not isinstance(row.get(field), bool), "Boolean numeric telemetry payload")
    encoded(row)  # Reject every NaN/infinity, including fields not used by selection.
    return row["tenant_id"], row["canonical_event_id"], row["record_revision"]


def canonicalize(rows):
    """Validate an entire input collection without mutating or partially storing it."""
    payloads, values, sources, canonicals, provenance = {}, {}, {}, {}, {}
    duplicates = 0
    for row in rows:
        key = identity(row)
        source = (key[0], row["source_system_id"], row["source_event_id"])
        canonical = key[:2]
        require(source not in sources or sources[source] == canonical,
                "One source event maps to multiple canonical events")
        require(canonical not in canonicals or canonicals[canonical] == source,
                "Canonical event changed source identity across revisions")
        require(row["provenance_id"] not in provenance or provenance[row["provenance_id"]] == key,
                "Different telemetry versions reuse one provenance identity")
        payload = encoded(row)
        require(key not in payloads or payloads[key] == payload,
                "Conflicting payload for the same tenant/event/revision")
        if key in payloads:
            duplicates += 1
        else:
            values[key] = copy.deepcopy(row)
        payloads[key] = payload
        sources[source], canonicals[canonical], provenance[row["provenance_id"]] = canonical, source, key
    ordered = [values[k] for k in sorted(values, key=lambda k: (k[0], k[1], int(k[2])))]
    return ordered, duplicates


def latest_known(rows, at):
    cutoff = clock(at)
    selected = {}
    canonical, _ = canonicalize(rows)
    for row in canonical:
        if clock(row["available_time"]) > cutoff:
            continue
        key = row["tenant_id"], row["canonical_event_id"]
        if key not in selected or int(row["record_revision"]) > int(selected[key]["record_revision"]):
            selected[key] = row
    return [selected[key] for key in sorted(selected)]


def snapshot_for_trip(tenant, trip, at, tables):
    """Return a P11 snapshot and its exact selected-position version binding.

    Selection is global over known revisions before the frozen feature function
    applies vehicle, departure, assignment and coordinate filters. This prevents
    resurrection of an old revision after a corrected vehicle or invalid fix.
    """
    selected = latest_known(tables.get("telemetry_event", []), at)
    scoped = dict(tables, telemetry_event=selected)
    snap = pipeline.snapshot_for_trip(tenant, trip, at, scoped)
    if snap is None:
        return None, None
    snap = dict(snap)
    snap["feature_definition_version"] = FEATURE_VERSION
    snap["snapshot_id"] = "S-" + ident(tenant, trip, utc(clock(at)), FEATURE_VERSION)
    snap["provenance_id"] = "P-" + ident("eta_snapshot", snap["snapshot_id"])
    if snap["selected_position_event_id"] is None:
        return snap, None
    matches = [r for r in selected if r["tenant_id"] == tenant and r["canonical_event_id"] == snap["selected_position_event_id"]]
    require(len(matches) == 1, "Selected position version is ambiguous")
    row = matches[0]
    require(row["event_time"] == snap["last_position_time"] and row["available_time"] == snap["position_available_at"],
            "Snapshot clocks do not bind the selected revision")
    binding = {"tenant_id": tenant, "snapshot_id": snap["snapshot_id"],
               "canonical_event_id": row["canonical_event_id"], "record_revision": row["record_revision"],
               "source_provenance_id": row["provenance_id"]}
    return snap, binding


def _provenance(row, *, table, key, version, parents=(), available=None):
    """Complete original provenance contract for the independently authored fixture."""
    availability = available or row["available_time"]
    return {"provenance_id": row["provenance_id"], "source_system_id": row.get("source_system_id", "P11-FIXTURE"),
            "source_record_id": encoded(key).decode(), "record_version": str(version), "record_state": "active",
            "event_time": row.get("event_time", row.get("snapshot_time")), "available_time": availability,
            "retrieved_at": availability, "extract_as_of": availability, "raw_record_reference": "independent-p11-revision-fixture",
            "origin_type": "synthetic", "source_dataset_id": "p11-revision-fixture-v1", "synthetic_scenario_id": "revision-boundary",
            "value_states": {}, "source_timezone": "UTC", "source_utc_offset_minutes": 0, "tenant_id": row["tenant_id"],
            "field_origins": {}, "availability_basis": "independent fixture receipt clock", "availability_evidence": "literal fixture timestamps",
            "canonical_table": table, "canonical_record_key": key, "parent_provenance_ids": sorted(set(parents))}


def _snapshot_parents(snapshot, binding, tables):
    """Resolve the same feature inputs, but telemetry through the explicit version."""
    tenant, trip, at = snapshot["tenant_id"], snapshot["trip_id"], clock(snapshot["snapshot_time"])
    rows = []
    for table, field in (("eta_trip_plan_version", "trip_version_id"), ("eta_promise_version", "promise_version_id"), ("eta_route_version", "route_version_id")):
        matches = [r for r in tables[table] if r["tenant_id"] == tenant and r[field] == snapshot[field]]
        require(len(matches) == 1, "Ambiguous snapshot dependency")
        rows.extend(matches)
    route = rows[-1]
    events = [r for r in tables["eta_trip_event"] if r["tenant_id"] == tenant and r["trip_id"] == trip
              and clock(r["available_at"]) <= at and clock(r["occurred_at"]) <= at]
    rows.extend(r for r in events if r["event_type"] in ("departure", "departed"))
    if events:
        rows.append(max(events, key=lambda r: (clock(r["available_at"]), clock(r["occurred_at"]), r.get("trip_event_id", ""))))
    rows.extend(r for r in tables["eta_facility_version"] if r["tenant_id"] == tenant
                and r["facility_version_id"] in (route["origin_facility_version_id"], route["destination_facility_version_id"]))
    if binding:
        positions = [r for r in tables["telemetry_event"] if identity(r) == (tenant, binding["canonical_event_id"], binding["record_revision"])]
        position = positions[0]
        rows.append(position)
        rows.extend(r for r in tables["core_vehicle_device_assignment"] if r["tenant_id"] == tenant
                    and r["vehicle_id"] == snapshot["vehicle_id"] and r["device_id"] == position["device_id"]
                    and pipeline._within(clock(position["event_time"]), r.get("valid_from"), r.get("valid_to"))
                    and pipeline._within(at, r.get("known_from"), r.get("known_to")) and pipeline._usable_assignment(r))
    return sorted({r["provenance_id"] for r in rows})


def fixture(repo):
    """Literal correction timeline, using only the existing hand-authored context."""
    repo = Path(repo)
    source = json.loads((repo / "tests/fixtures/point_in_time_case.json").read_text())
    tables = copy.deepcopy(source["tables"])
    fields = {f["name"] for f in json.loads((repo / "contracts/fields.json").read_text()) if f["table"] == "telemetry_event"}
    prototype = {name: None for name in fields}
    prototype.update(tables["telemetry_event"][0])
    events = []
    for revision, available, latitude in (("1", "2026-06-23T09:56:00Z", 34.1),
                                          ("2", "2026-06-23T10:04:00Z", 34.2),
                                          ("10", "2026-06-23T10:05:00Z", 34.5)):
        events.append(dict(prototype, record_revision=revision, available_time=available,
                           latitude_deg=latitude, provenance_id="P11-GPS-T01-R" + revision))
    events.append(dict(prototype, tenant_id="T02", latitude_deg=35.2, source_system_id="hand_fixture_t02",
                       provenance_id="P11-GPS-T02-R1"))
    for name, rows in list(tables.items()):
        if name not in ("telemetry_event", "provenance_record"):
            tables[name] += [dict(row, tenant_id="T02", provenance_id="HAND-2") for row in copy.deepcopy(rows)]
    # The context fixture has no tenant rows; report timezone is explicit here.
    tables["core_tenant"] = [{"tenant_id": t, "report_timezone": "America/Los_Angeles"} for t in ("T01", "T02")]
    tables["telemetry_event"] = events
    contexts = [_provenance({"tenant_id": t, "provenance_id": "HAND-" + str(i),
                            "available_time": "2026-01-01T00:00:00Z"}, table="p11_fixture_context", key=[t], version="1")
                for i, t in enumerate(("T01", "T02"), 1)]
    tables["provenance_record"] = contexts + [_provenance(r, table="telemetry_event", key=list(identity(r)), version=r["record_revision"]) for r in events]
    return tables


def _typed(value):
    if isinstance(value, datetime):
        return utc(value)
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, list):
        return [_typed(x) for x in value]
    if isinstance(value, tuple):
        return tuple(_typed(x) for x in value)
    return value


def _table_rows(con, table, parquet=None):
    relation = f'"{table}"' if parquet is None else "read_parquet(?)"
    parameters = [] if parquet is None else [str(parquet)]
    columns = con.execute("DESCRIBE SELECT * FROM " + relation, parameters).fetchall()
    # Native DuckDB TIMESTAMPTZ conversion can require optional pytz. Compare
    # exact signed microseconds in SQL instead; no rounding or extra dependency.
    expressions = [f'epoch_us("{row[0]}")' if row[1] == "TIMESTAMP WITH TIME ZONE" else f'"{row[0]}"' for row in columns]
    rows = [_typed(row) for row in con.execute("SELECT " + ",".join(expressions) + " FROM " + relation, parameters).fetchall()]
    return sorted(rows, key=encoded)


def _expected_rows(rows, fields):
    result = []
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    for row in rows:
        values = []
        for field in fields:
            value = row[field["name"]]
            if value is not None:
                if field["type"] == "TIMESTAMPTZ":
                    delta = clock(value) - epoch
                    value = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
                elif field["type"].startswith("DECIMAL"):
                    value = str(Decimal(str(value)).normalize())
                elif field["type"] == "JSON":
                    value = encoded(value).decode()
            values.append(value)
        result.append(tuple(values))
    return sorted(result, key=encoded)


def _insert(con, name, rows, fields):
    columns = [f["name"] for f in fields]
    parameters = []
    for row in rows:
        require(set(row) == set(columns), "Typed fixture columns differ: " + name)
        values = []
        for field in fields:
            value = row[field["name"]]
            if value is not None and field["type"] == "JSON":
                value = encoded(value).decode()
            values.append(value)
        parameters.append(values)
    if parameters:
        con.executemany(f'INSERT INTO "{name}" VALUES (' + ",".join("?" for _ in columns) + ")", parameters)


def replay_retained_events(con, events, fields):
    """Replay all retained versions only after exact typed-payload conflict checks."""
    canonical, duplicate_count = canonicalize(events)
    columns = [f["name"] for f in fields]
    key_indices = [columns.index(f) for f in ("tenant_id", "canonical_event_id", "record_revision")]
    before = _table_rows(con, "telemetry_event")
    retained = {tuple(row[i] for i in key_indices): row for row in before}
    for row in canonical:
        require(set(row) == set(columns), "Retained replay columns differ from the registered contract")
        expected = _expected_rows([row], fields)[0]
        require(identity(row) in retained and encoded(retained[identity(row)]) == encoded(expected),
                "Conflicting or unregistered retained revision replay")
    # The database enforces full-version identity. Only exact existing versions
    # reach ON CONFLICT; changed payloads are never silently dropped.
    placeholders = ",".join("?" for _ in columns)
    for row in events:
        values = [encoded(row[f["name"]]).decode() if f["type"] == "JSON" and row[f["name"]] is not None else row[f["name"]] for f in fields]
        con.execute("INSERT INTO telemetry_event VALUES (" + placeholders + ") ON CONFLICT DO NOTHING", values)
    after = _table_rows(con, "telemetry_event")
    require(encoded(before) == encoded(after), "Retained revision replay changed persisted rows")
    return {"input_rows": len(events), "unique_versions": len(canonical), "duplicate_input_rows": duplicate_count,
            "new_rows": 0, "all_typed_payloads_unchanged": True}


def write_store(repo, output, events, snapshots, bindings, provenance):
    """Real typed canonical telemetry plus an explicit full-version snapshot FK."""
    output = Path(output)
    require(not output.exists(), "Revision store destination must be new")
    canonical, _ = canonicalize(events)
    require(len(canonical) == len(events), "Pass canonical unique rows to the store")
    prov_by_id = {r["provenance_id"]: r for r in provenance}
    require(len(prov_by_id) == len(provenance), "Duplicate provenance identity")
    for row in canonical:
        prov = prov_by_id.get(row["provenance_id"])
        require(prov is not None and prov["tenant_id"] == row["tenant_id"]
                and prov["record_version"] == row["record_revision"]
                and prov["canonical_table"] == "telemetry_event"
                and prov["canonical_record_key"] == list(identity(row))
                and clock(prov["available_time"]) == clock(row["available_time"]), "Telemetry provenance does not bind exact revision")
    snapshot_by_id = {(r["tenant_id"], r["snapshot_id"]): r for r in snapshots}
    require(len(snapshot_by_id) == len(snapshots), "Duplicate snapshot identity")
    telemetry_provenance = {r["provenance_id"] for r in canonical}
    for snap in snapshots:
        prov = prov_by_id.get(snap["provenance_id"])
        require(prov is not None and prov["tenant_id"] == snap["tenant_id"]
                and prov["canonical_table"] == "eta_snapshot"
                and prov["canonical_record_key"] == [snap["tenant_id"], snap["snapshot_id"]]
                and prov["record_version"] == FEATURE_VERSION
                and snap["feature_definition_version"] == FEATURE_VERSION
                and clock(prov["available_time"]) == clock(snap["snapshot_time"]),
                "Snapshot provenance does not bind exact snapshot")
        if snap["selected_position_event_id"] is None:
            require(not (set(prov["parent_provenance_ids"]) & telemetry_provenance)
                    and snap["position_validity"] == "no_position"
                    and all(snap[f] is None for f in ("device_id", "last_position_time", "position_available_at",
                        "latitude_deg", "longitude_deg", "gnss_speed_kmh", "position_age_seconds", "remaining_distance_km")),
                    "No-position snapshot retains position values or lineage")
    event_by_key = {identity(r): r for r in canonical}
    expected_binding_ids = {key for key, r in snapshot_by_id.items() if r["selected_position_event_id"] is not None}
    require({(r["tenant_id"], r["snapshot_id"]) for r in bindings} == expected_binding_ids and len(bindings) == len(expected_binding_ids),
            "Snapshot version bindings omitted or duplicated")
    for binding in bindings:
        snap = snapshot_by_id[binding["tenant_id"], binding["snapshot_id"]]
        event = event_by_key.get((binding["tenant_id"], binding["canonical_event_id"], binding["record_revision"]))
        require(event is not None and event["provenance_id"] == binding["source_provenance_id"]
                and snap["selected_position_event_id"] == event["canonical_event_id"]
                and clock(event["event_time"]) <= clock(snap["snapshot_time"])
                and clock(event["available_time"]) <= clock(snap["snapshot_time"])
                and clock(event["available_time"]) == clock(snap["position_available_at"])
                and clock(event["event_time"]) == clock(snap["last_position_time"]), "Snapshot bound the wrong or future revision")
        eligible = [r for r in latest_known(canonical, snap["snapshot_time"]) if r["tenant_id"] == binding["tenant_id"] and r["canonical_event_id"] == binding["canonical_event_id"]]
        require(len(eligible) == 1 and identity(eligible[0]) == identity(event), "Snapshot lineage binds a superseded known revision")
        require(encoded([snap[f] for f in ("vehicle_id", "device_id", "latitude_deg", "longitude_deg", "gnss_speed_kmh")])
                == encoded([event[f] for f in ("vehicle_id", "device_id", "latitude_deg", "longitude_deg", "gnss_speed_kmh")]),
                "Snapshot position payload differs from selected revision")
        snap_prov = prov_by_id[snap["provenance_id"]]
        require(set(snap_prov["parent_provenance_ids"]) & telemetry_provenance == {event["provenance_id"]},
                "Snapshot provenance omits or substitutes selected revision")
    for prov in provenance:
        for parent_id in prov["parent_provenance_ids"]:
            parent = prov_by_id.get(parent_id)
            require(parent is not None and parent["tenant_id"] == prov["tenant_id"]
                    and clock(parent["available_time"]) <= clock(prov["available_time"]), "Missing, cross-tenant or future provenance parent")
    fields = json.loads((Path(repo) / "contracts/fields.json").read_text())
    contracts = {name: [f for f in fields if f["table"] == name] for name in STORE_TABLES[:-1]}
    contracts["p11_snapshot_position"] = [{"name": n, "type": "VARCHAR", "nullable": False} for n in
        ("tenant_id", "snapshot_id", "canonical_event_id", "record_revision", "source_provenance_id")]
    source_rows = dict(zip(STORE_TABLES, (provenance, canonical, snapshots, bindings)))
    output.mkdir(parents=True)
    database = output / "revision.duckdb"
    con = duckdb.connect(str(database))
    constraints = {
        "provenance_record": 'PRIMARY KEY (provenance_id)',
        "telemetry_event": 'PRIMARY KEY (tenant_id,canonical_event_id,record_revision), UNIQUE (tenant_id,canonical_event_id,record_revision,provenance_id), FOREIGN KEY (provenance_id) REFERENCES provenance_record(provenance_id)',
        "eta_snapshot": 'PRIMARY KEY (tenant_id,snapshot_id), FOREIGN KEY (provenance_id) REFERENCES provenance_record(provenance_id)',
        "p11_snapshot_position": 'PRIMARY KEY (tenant_id,snapshot_id), FOREIGN KEY (tenant_id,snapshot_id) REFERENCES eta_snapshot(tenant_id,snapshot_id), FOREIGN KEY (tenant_id,canonical_event_id,record_revision,source_provenance_id) REFERENCES telemetry_event(tenant_id,canonical_event_id,record_revision,provenance_id)',
    }
    try:
        con.execute("BEGIN")
        for name in STORE_TABLES:
            columns = [f'"{f["name"]}" {f["type"]}' + ("" if f["nullable"] else " NOT NULL") for f in contracts[name]]
            con.execute(f'CREATE TABLE "{name}" (' + ",".join(columns) + "," + constraints[name] + ")")
            _insert(con, name, source_rows[name], contracts[name])
        con.execute("COMMIT")
        expected = {name: _expected_rows(source_rows[name], contracts[name]) for name in STORE_TABLES}
        for name in STORE_TABLES:
            require(encoded(_table_rows(con, name)) == encoded(expected[name]), "Typed source readback changed a value: " + name)
        replay = replay_retained_events(con, canonical + canonical, contracts["telemetry_event"])
        probes = []
        probe_sql = [("duplicate_full_revision", "INSERT INTO telemetry_event SELECT * FROM telemetry_event LIMIT 1")] if canonical else []
        if bindings:
            probe_sql.extend([
                ("wrong_revision_provenance_fk", "UPDATE p11_snapshot_position SET source_provenance_id='does-not-exist'"),
                ("missing_revision_fk", "UPDATE p11_snapshot_position SET record_revision='99999999999999999999'"),
            ])
            foreign = next(((b, r) for b in bindings for r in canonical if b["tenant_id"] != r["tenant_id"]), None)
            if foreign:
                binding, row = foreign
                # Existing foreign-tenant source provenance, with the snapshot's
                # original tenant/key kept intact: exercises the composite FK.
                escaped = lambda text: "'" + text.replace("'", "''") + "'"
                probe_sql.append(("cross_tenant_position_fk", "UPDATE p11_snapshot_position SET source_provenance_id=" + escaped(row["provenance_id"])
                                  + " WHERE tenant_id=" + escaped(binding["tenant_id"]) + " AND snapshot_id=" + escaped(binding["snapshot_id"])))
        for label, sql in probe_sql:
            try:
                con.execute(sql)
            except duckdb.ConstraintException:
                probes.append(label)
            else:
                raise ValueError("Revision constraint did not reject " + label)
        for name in STORE_TABLES:
            require(encoded(_table_rows(con, name)) == encoded(expected[name]), "Negative probe changed retained store")
            con.execute(f'COPY (SELECT * FROM "{name}" ORDER BY ALL) TO ? (FORMAT PARQUET, COMPRESSION ZSTD)', [str(output / (name + ".parquet"))])
    finally:
        con.close()
    with duckdb.connect(str(database), read_only=True) as con:
        for name in STORE_TABLES:
            require(encoded(_table_rows(con, name)) == encoded(expected[name]), "Fresh database connection changed retained rows")
            parquet = _table_rows(con, name, output / (name + ".parquet"))
            require(encoded(parquet) == encoded(expected[name]), "Parquet typed readback changed retained rows")
    return {"status": "passed_typed_revision_store", "database": "revision.duckdb", "tables": {name: {"rows": len(expected[name]), "typed_rows_sha256": hashlib.sha256(encoded(expected[name])).hexdigest(), "parquet_sha256": digest_file(output / (name + ".parquet"))} for name in STORE_TABLES},
            "actual_constraint_rejections": probes, "retained_revision_replay": replay, "fresh_connection_exact": True, "all_column_parquet_exact": True,
            "telemetry_primary_key": ["tenant_id", "canonical_event_id", "record_revision"], "snapshot_revision_foreign_key_enforced": True}


def verify_revision_contract(repo, output):
    """Run the complete bounded G11 witness; all artifacts stay outside the repo."""
    repo, output = Path(repo).resolve(), Path(output).resolve()
    require(not output.exists() and not output.is_relative_to(repo), "Use a new external revision output directory")
    output.mkdir(parents=True)
    tables = fixture(repo)
    source = tables["telemetry_event"] + [copy.deepcopy(tables["telemetry_event"][0])]
    expected, duplicates = canonicalize(source)
    with event_api._running_server(source, page_size=2, inject_503=True) as (endpoint, server):
        actual, transport = reconcile_endpoint(source, endpoint, page_size=2)
    actual, _ = canonicalize(actual)
    require(encoded(actual) == encoded(expected) and server["closed"], "HTTP source or lifecycle mismatch")
    tables["telemetry_event"] = actual
    times = ("2026-06-23T10:00:00Z", "2026-06-23T10:04:00Z", "2026-06-23T10:04:59.999999Z", "2026-06-23T10:05:00Z", "2026-06-23T10:06:00Z")
    snapshots, bindings, witnesses = [], [], []
    for tenant in ("T01", "T02"):
        for at in times:
            snap, binding = snapshot_for_trip(tenant, "TRIP-1", at, tables)
            require(snap is not None and binding is not None, "Fixture snapshot unexpectedly absent")
            expected_revision = ("1" if at < "2026-06-23T10:04:00Z" else "2" if at < "2026-06-23T10:05:00Z" else "10") if tenant == "T01" else "1"
            require(binding["record_revision"] == expected_revision, "Known-revision selection differs from literal timeline oracle")
            latitude = {"1": 34.1, "2": 34.2, "10": 34.5}[expected_revision] if tenant == "T01" else 35.2
            require(snap["latitude_deg"] == latitude, "Snapshot payload differs from literal revision oracle")
            snapshots.append(snap); bindings.append(binding)
            tables["provenance_record"].append(_provenance(snap, table="eta_snapshot", key=[tenant, snap["snapshot_id"]], version=FEATURE_VERSION, parents=_snapshot_parents(snap, binding, tables), available=snap["snapshot_time"]))
            witnesses.append({"tenant_id": tenant, "snapshot_time": at, "record_revision": expected_revision, "latitude_deg": latitude, "source_provenance_id": binding["source_provenance_id"]})
    first = snapshots[0]
    # Appending a correction known later must not rewrite the earlier snapshot.
    without_future = dict(tables, telemetry_event=[r for r in actual if r["record_revision"] == "1"])
    require(snapshot_for_trip("T01", "TRIP-1", times[0], without_future)[0] == first, "Late correction rewrote an earlier snapshot")
    store = write_store(repo, output / "store", actual, snapshots, bindings, tables["provenance_record"])
    for name, rows in (("source", source), ("http_events", actual), ("snapshots", snapshots), ("snapshot_positions", bindings), ("provenance", tables["provenance_record"])):
        write_jsonl(output / (name + ".jsonl"), rows)
    report = {"status": "passed_revision_contract", "scope": "Independent bounded P11 revision-aware entry point; frozen historical entry points remain unchanged",
              "canonical_events": len({(r["tenant_id"], r["canonical_event_id"]) for r in actual}), "retained_revisions": len(actual),
              "source_duplicate_rows": duplicates, "snapshot_witnesses": witnesses, "http": transport, "http_server_closed": True, "store": store,
              "earlier_snapshot_unchanged": True, "scientific_results_changed": False,
              "files": {p.relative_to(output).as_posix(): digest_file(p) for p in sorted(output.rglob("*")) if p.is_file()}}
    (output / "revision_receipt.json").write_text(json.dumps(report, indent=2) + "\n")
    return report
