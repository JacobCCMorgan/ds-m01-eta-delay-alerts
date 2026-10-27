#!/usr/bin/env python3
"""Reproduce generator checks without retaining the full synthetic dataset.

Run from the project root:
    PYTHONPATH=src python scripts/check_generator_stability.py

Only Python's standard library and this project's generator are required.
The report is written to reports/generator_validation.json; temporary raw
datasets are removed when the check exits, including when an assertion fails.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import tempfile
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from ds_m01.common import parse_ts
from ds_m01.generate import ACTIVE_TABLES, PRIMARY_KEYS, VERSION, generate


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_rows(path):
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if line.strip():
                yield line_number, json.loads(line)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024*1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def bool_value(value):
    return str(value).lower() == "true"


def check_schema(directory, fields_by_table):
    """Inspect every row, including the provenance table, by the contract."""
    counts = {}
    source_keys = {}
    for table in ACTIVE_TABLES:
        fields = fields_by_table[table]
        seen = set()
        keys = set()
        count = 0
        for line, row in read_rows(directory / "data" / "raw" / f"{table}.jsonl"):
            count += 1
            location = f"{directory.name}:{table}:{line}"
            require(set(row) == set(fields), f"{location}: canonical field mismatch {set(row)^set(fields)}")
            for name, value in row.items():
                field = fields[name]
                if value is None:
                    require(bool_value(field["nullable"]), f"{location}.{name}: forbidden NULL")
                    continue
                kind = field["type"]
                if kind == "VARCHAR":
                    require(isinstance(value, str), f"{location}.{name}: expected string")
                    require(value != "", f"{location}.{name}: empty string is not a meaningful value")
                elif kind == "TIMESTAMPTZ":
                    parse_ts(value)
                elif kind in ("DOUBLE", "FLOAT") or kind.startswith("DECIMAL"):
                    require(isinstance(value, (int, float)) and not isinstance(value, bool)
                            and math.isfinite(value), f"{location}.{name}: expected finite numeric value")
                elif kind in ("INTEGER", "BIGINT", "SMALLINT"):
                    require(isinstance(value, int) and not isinstance(value, bool),
                            f"{location}.{name}: expected integer")
                elif kind == "JSON":
                    require(isinstance(value, (dict, list)), f"{location}.{name}: expected JSON object/array")
                elif kind == "BOOLEAN":
                    require(isinstance(value, bool), f"{location}.{name}: expected boolean")
                else:
                    raise AssertionError(f"Unimplemented canonical type check: {table}.{name} {kind}")
            if table == "provenance_record":
                record_key = row["provenance_id"]
            else:
                record_key = (row["tenant_id"], row[PRIMARY_KEYS[table]])
                keys.add(record_key)
            require(record_key not in seen, f"{location}: duplicate primary key {record_key}")
            seen.add(record_key)
        counts[table] = count
        if table != "provenance_record":
            source_keys[table] = keys
    # Join provenance back to actual canonical keys, rather than trusting that
    # merely having a non-null provenance ID proves traceability.
    represented = defaultdict(set)
    provenance_ids = set()
    for _, row in read_rows(directory / "data" / "raw" / "provenance_record.jsonl"):
        table = row["canonical_table"]
        require(table in source_keys, f"Provenance points to unexpected table {table}")
        key_fields = row["canonical_record_key"]
        key = (key_fields["tenant_id"], key_fields[PRIMARY_KEYS[table]])
        require(key in source_keys[table], f"Provenance target does not exist: {table}:{key}")
        require(row["tenant_id"] == key[0], f"Provenance tenant mismatch: {table}:{key}")
        require(key not in represented[table], f"Duplicate provenance for raw row {table}:{key}")
        represented[table].add(key)
        provenance_ids.add(row["provenance_id"])
        require(row["origin_type"] == "synthetic", "A generated source row is not marked synthetic")
        require(row["availability_basis"] == "simulated", "Synthetic availability is not marked simulated")
    for table, keys in source_keys.items():
        require(represented[table] == keys, f"Incomplete provenance coverage for {table}")
        for _, row in read_rows(directory / "data" / "raw" / f"{table}.jsonl"):
            if "provenance_id" in row:
                require(row["provenance_id"] in provenance_ids, f"Missing linked provenance: {table}")
    require(counts["provenance_record"] == sum(v for t, v in counts.items() if t != "provenance_record"),
            "Provenance count does not equal the raw source record count")
    return counts


def check_containment(trial, full):
    matched_counts = {}
    for table in ACTIVE_TABLES:
        if table == "provenance_record":
            # Provenance records locate actual file line numbers, which change
            # when non-trial trips are included. The business provenance IDs
            # themselves are included in the exact row equality below.
            continue
        key = PRIMARY_KEYS[table]
        expected = {(row["tenant_id"], row[key]): row
                    for _, row in read_rows(trial / "data" / "raw" / f"{table}.jsonl")}
        found = set()
        for _, row in read_rows(full / "data" / "raw" / f"{table}.jsonl"):
            row_key = (row["tenant_id"], row[key])
            if row_key in expected:
                require(row == expected[row_key], f"Trial source row changed on expansion: {table}:{row_key}")
                found.add(row_key)
        require(found == set(expected), f"Trial records missing from full design: {table}")
        matched_counts[table] = len(found)
    return matched_counts


def check_existing_directory_regeneration(trial, config, first_counts):
    """Regression: replacing existing files must preserve every final buffer."""
    relatives = [Path("data") / "raw" / f"{table}.jsonl" for table in ACTIVE_TABLES]
    relatives += [Path("validation") / "latent_truth.jsonl", Path("validation") / "scenario_registry.json"]
    first_hashes = {str(relative): sha256(trial / relative) for relative in relatives}
    first_persisted = {table: sum(1 for _ in read_rows(trial / "data" / "raw" / f"{table}.jsonl"))
                       for table in ACTIVE_TABLES}
    require(first_persisted == first_counts, "Fresh-directory counts differ from persisted rows")
    returned_counts = generate(trial, config)
    second_persisted = {table: sum(1 for _ in read_rows(trial / "data" / "raw" / f"{table}.jsonl"))
                        for table in ACTIVE_TABLES}
    require(second_persisted == returned_counts == first_persisted,
            "Existing-directory regeneration lost or added persisted rows")
    second_hashes = {str(relative): sha256(trial / relative) for relative in relatives}
    require(second_hashes == first_hashes, "Same-config existing-directory generation changed persisted bytes")
    registry = json.loads((trial / "validation" / "scenario_registry.json").read_text(encoding="utf-8"))
    publication = registry["publication"]
    require(publication["persisted_counts_verified_before_publish"] is True,
            "Missing staged row-count verification evidence")
    require(publication["persisted_counts_and_hashes_verified_after_publish"] is True,
            "Missing post-publication row-count/hash verification evidence")
    for relative, evidence in publication["files"].items():
        require(sha256(trial / relative) == evidence["sha256"], f"Registry hash mismatch: {relative}")
        require(sum(1 for _ in read_rows(trial / relative)) == evidence["rows"], f"Registry row-count mismatch: {relative}")
    return {"status": "passed", "fresh_counts": first_persisted, "overwrite_counts": second_persisted,
            "all_generated_file_hashes_unchanged": True, "generated_file_sha256": second_hashes,
            "committed_registry_hashes_and_counts_match_disk": True,
            "same_config_generated_at": config["generated_at"]}


def check_trial_coverage(trial):
    plans = [row for _, row in read_rows(trial / "data" / "raw" / "eta_trip_plan_version.jsonl")]
    truths = [row for _, row in read_rows(trial / "validation" / "latent_truth.jsonl")]
    tenants = sorted({row["tenant_id"] for row in plans})
    vehicles = sorted({row["vehicle_id"] for row in plans})
    routes = sorted({row["route_version_id"].removesuffix("-V1") for row in plans})
    scenarios = Counter(row["scenario"] for row in truths)
    require(tenants == ["T01", "T02", "T03"], "Trial does not cover all three tenants")
    require(set(vehicles) == {f"T{t:02d}-V{v:02d}" for t in range(1, 4) for v in range(1, 11)},
            "Trial does not cover all thirty vehicles")
    require(set(routes) == {f"ROUTE-{r:02d}" for r in range(1, 13)}, "Trial does not cover all twelve routes")
    expected_scenarios = {"normal", "late_arrival", "cancelled", "gps_dropout", "late_gps",
                          "promise_revision", "device_swap", "arrival_report_late", "outcome_revision",
                          "unfinished", "invalid_position"}
    require(set(scenarios) == expected_scenarios, "Trial does not cover all eleven fixture families")
    require(sorted(row["global_ordinal"] for row in truths) == list(range(0, 1800, 15)),
            "Trial is not the preregistered every-fifteenth-ordinal subset")
    return {"tenants": tenants, "vehicle_count": len(vehicles), "routes": routes,
            "scenario_counts": dict(sorted(scenarios.items())), "trial_trip_count": len(truths)}


def no_overlaps(groups, name):
    checked = 0
    for key, values in groups.items():
        values.sort(key=lambda item: item[0])
        for previous, following in zip(values, values[1:]):
            # Half-open intervals may share a boundary but may not overlap.
            require(previous[1] is not None and previous[1] <= following[0],
                    f"{name} overlap for {key}: {previous[2]} -> {following[2]}")
            checked += 1
    return checked


def check_schedule_and_assignments(full, trial, cutoff):
    trips = [row for _, row in read_rows(full / "validation" / "latent_truth.jsonl")]
    vehicle_trips = defaultdict(list)
    spare_windows = defaultdict(list)
    for row in trips:
        start = parse_ts(row["true_departure_at"])
        end_value = row["true_gate_arrival_at"] or row["cancellation_at"]
        require(end_value is not None, f"Missing physical end for validation trip {row['trip_id']}")
        end = parse_ts(end_value)
        require(start < end, f"Nonpositive physical trip interval: {row['trip_id']}")
        vehicle_trips[(row["tenant_id"], row["vehicle_id"])].append((start, end, row["trip_id"]))
        if row["swap_at"]:
            # The independent fixture records actual physical swap windows;
            # compare the source assignment history separately below.
            spare_windows[row["tenant_id"]].append((parse_ts(row["swap_at"]),
                                                    parse_ts(row["swap_back_at"]), row["trip_id"]))
    trip_pairs = no_overlaps(vehicle_trips, "Physical vehicle-trip")
    spare_pairs = no_overlaps(spare_windows, "Physical tenant-spare")

    assignments = []
    for _, original in read_rows(full / "data" / "raw" / "core_vehicle_device_assignment.jsonl"):
        row = dict(original)
        for name in ("valid_from", "valid_to", "known_from", "known_to"):
            row[name] = parse_ts(row[name]) if row[name] else None
        require(row["valid_to"] is None or row["valid_from"] < row["valid_to"], "Invalid assignment valid interval")
        require(row["known_to"] is None or row["known_from"] < row["known_to"], "Invalid assignment known interval")
        assignments.append(row)
    final_history = [row for row in assignments
                     if row["known_from"] < cutoff and (row["known_to"] is None or cutoff <= row["known_to"])]
    by_vehicle = defaultdict(list)
    by_device = defaultdict(list)
    for row in final_history:
        interval = (row["valid_from"], row["valid_to"], row["assignment_id"])
        by_vehicle[(row["tenant_id"], row["vehicle_id"])].append(interval)
        by_device[(row["tenant_id"], row["device_id"])].append(interval)
    vehicle_assignment_pairs = no_overlaps(by_vehicle, "Final known vehicle-assignment")
    device_assignment_pairs = no_overlaps(by_device, "Final known device-assignment")

    index = defaultdict(list)
    for row in assignments:
        index[(row["tenant_id"], row["device_id"])].append(row)
    matched = unknown = 0
    unknown_examples = []
    for _, event in read_rows(trial / "data" / "raw" / "telemetry_event.jsonl"):
        event_time = parse_ts(event["event_time"])
        available = parse_ts(event["available_time"])
        require(available >= event_time, f"Telemetry became available before occurrence: {event['canonical_event_id']}")
        candidates = [row for row in index[(event["tenant_id"], event["device_id"])]
                      if row["valid_from"] <= event_time
                      and (row["valid_to"] is None or event_time < row["valid_to"])]
        known = [row for row in candidates if row["known_from"] <= available
                 and (row["known_to"] is None or available < row["known_to"])]
        require(len(known) <= 1, f"Ambiguous source event assignment: {event['canonical_event_id']}")
        if known:
            require(known[0]["vehicle_id"] == event["vehicle_id"],
                    f"Wrong vehicle from valid+known lookup: {event['canonical_event_id']}")
            matched += 1
        else:
            # Legitimate unknowns must have a later matching-vehicle source
            # relationship that was unavailable at this observation cutoff.
            same_vehicle = [row for row in candidates if row["vehicle_id"] == event["vehicle_id"]
                            and (row["known_to"] is None or available < row["known_to"])]
            require(same_vehicle and min(row["known_from"] for row in same_vehicle) > available,
                    f"Unexplained missing relationship: {event['canonical_event_id']}")
            unknown += 1
            if len(unknown_examples) < 5:
                unknown_examples.append({"event_id": event["canonical_event_id"],
                                         "available_time": event["available_time"],
                                         "reason": "Relationship had not yet been reported at the event availability cutoff."})
    return {"physical_vehicle_trip_pairs_checked": trip_pairs,
            "physical_spare_window_pairs_checked": spare_pairs,
            "final_known_vehicle_assignment_pairs_checked": vehicle_assignment_pairs,
            "final_known_device_assignment_pairs_checked": device_assignment_pairs,
            "physical_vehicle_overlap_count": 0, "physical_spare_overlap_count": 0,
            "wrong_or_ambiguous_trial_assignments": 0, "matched_trial_telemetry": matched,
            "unknown_before_assignment_report": unknown, "unknown_examples": unknown_examples,
            "unknown_policy": "Retain as unknown at the historical cutoff; do not backfill future relationship knowledge."}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="Project root containing config/m1.json and contracts/fields.json (default: script's project root)")
    args = parser.parse_args()
    root = args.root.resolve()
    report_path = root / "reports" / "generator_validation.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    report = {"status": "running", "started_at": utc_now(), "generator_version": VERSION,
              "command": "PYTHONPATH=src python scripts/check_generator_stability.py",
              "dataset_kind": "synthetic", "temporary_full_data_retained": False,
              "stage_claim": "M1 generator validation, not M2 full-project delivery or model validation", "checks": {}}
    exit_code = 0
    try:
        config_path = root / "config" / "m1.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        fields = json.loads((root / "contracts" / "fields.json").read_text(encoding="utf-8"))
        fields_by_table = defaultdict(dict)
        for field in fields:
            fields_by_table[field["table"]][field["name"]] = field
        config["generated_at"] = report["started_at"]
        report["config"] = {"seed": config.get("seed", 20261021), "namespace": config.get("namespace", "ds-m01-2026-v1"),
                            "generated_at_used": config["generated_at"],
                            "extract_as_of": config.get("extract_as_of", "2026-10-15T07:00:00Z")}
        report["input_sha256"] = {
            str(path.relative_to(root)): sha256(path) for path in (
                config_path, root / "contracts" / "fields.json", root / "src" / "ds_m01" / "generate.py",
                root / "src" / "ds_m01" / "common.py", root / "scripts" / "check_generator_stability.py")}
        with tempfile.TemporaryDirectory(prefix="ds_m01_generator_validation_") as temporary:
            trial, full = Path(temporary) / "trial", Path(temporary) / "full"
            trial_config, full_config = dict(config, sample_size=120), dict(config, sample_size=1800)
            generation_started = time.perf_counter()
            trial_counts = generate(trial, trial_config)
            report["checks"]["same_directory_regeneration"] = check_existing_directory_regeneration(
                trial, trial_config, trial_counts)
            full_counts = generate(full, full_config)
            report["generation_seconds"] = round(time.perf_counter()-generation_started, 3)
            report["checks"]["canonical_schema_trial"] = check_schema(trial, fields_by_table)
            report["checks"]["canonical_schema_full"] = check_schema(full, fields_by_table)
            require(report["checks"]["canonical_schema_trial"] == trial_counts, "Trial emitted/observed counts differ")
            require(report["checks"]["canonical_schema_full"] == full_counts, "Full emitted/observed counts differ")
            report["checks"]["trial_business_row_containment"] = check_containment(trial, full)
            report["checks"]["trial_coverage"] = check_trial_coverage(trial)
            report["checks"]["temporal_consistency"] = check_schedule_and_assignments(
                full, trial, parse_ts(report["config"]["extract_as_of"]))
            report["status"] = "passed"
    except Exception as exc:
        exit_code = 1
        report["status"] = "failed"
        report["failure"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
    finally:
        report["finished_at"] = utc_now()
        report["elapsed_seconds"] = round(time.perf_counter()-started, 3)
        report["exit_code"] = exit_code
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(report_path),
                      "elapsed_seconds": report["elapsed_seconds"], "exit_code": exit_code}, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
