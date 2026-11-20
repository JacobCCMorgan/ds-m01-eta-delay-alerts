"""Check the P12 contract audit against the published catalogs and retained outputs."""
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

BASE = "2757d817b76a14f2d91da915302979a10560f09f"
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from workflows.p12.acquisition import verify_receipt

REBUILD = "workflows/p11/evidence/rebuild.json"
STORE = "workflows/p09/evidence/store"
ACQUISITION = "workflows/p12/evidence/acquisition_receipt.json"
REVISION = "workflows/p11/evidence/revision.json"
PROFILE = "workflows/p12/evidence/m2_field_profile.json"
WITNESSES = "workflows/p12/evidence/scenario_witnesses.txt"
SCENARIOS = {
    "ETA-V05": "test_v05_delayed_position_and_missing_position_fallback",
    "ETA-V08": "test_v08_literal_undercoverage_and_width",
    "ETA-V09": "test_v09_trip_and_snapshot_weighting",
    "ETA-V10": "test_v10_same_episode_retry_and_partial_adoption",
    "ETA-V12": "test_v12_repeated_hour_and_late_correction",
}


# A passing test transcript is not a substitute for the output of its scenario.
# Register the separate retained result families used for the seven prior cases.
SCENARIO_RESULTS = {
    "ETA-V02": (f"{STORE}/jsonl/eta_action_followup.jsonl", "/original_promise_version_id"),
    "ETA-V03": ("reports/p08/eta_targets.jsonl", "/eligibility"),
    "ETA-V04": ("reports/p08/outcome_source.jsonl", "/actual_arrival_time"),
    "ETA-V06": ("reports/p08/inventory.json", "/trip_keys"),
    "ETA-V07": ("reports/p08/scenarios/S04_natural_entity_holdouts/forecast_metrics.json", "/eta/slices"),
    "ETA-V11": ("reports/p08/scenarios/S00_unmodified/forecast_metrics.json", "/probability/methods"),
    "ETA-V14": ("reports/p08/scenarios/S00_unmodified/model_registry.jsonl", "/source_bindings"),
}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def parent_integrity(root):
    git = ["git", "--no-replace-objects", "-C", str(root)]
    subprocess.run([*git, "merge-base", "--is-ancestor", BASE, "HEAD"], check=True)
    changed = subprocess.check_output([*git, "diff", "--no-ext-diff", "--no-renames",
                                       "--name-only", "-z", "--diff-filter=DMT", BASE, "--", "."])
    allowed = {"README.md", "docs/change_log.md", "docs/evidence_index.md"}
    changed_paths = {item.decode() for item in changed.split(b"\0") if item}
    require(not changed_paths - allowed, "Published parent files changed: " + str(sorted(changed_paths - allowed)))


def json_file(path):
    return json.loads(path.read_text(encoding="utf-8"))


def keyed(rows, key):
    result = {}
    for row in rows:
        value = row[key]
        require(value and value not in result, f"Missing or duplicate {key}: {value}")
        result[value] = row
    return result


def csv_rows(root, name, key):
    folder = "workflows/p12/contracts" if name.startswith("p12_") else "contracts"
    with (root / folder / name).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        require(reader.fieldnames and len(set(reader.fieldnames)) == len(reader.fieldnames),
                f"Invalid CSV header: {name}")
        rows = list(reader)
    require(all(None not in row and None not in row.values() for row in rows), f"Malformed CSV: {name}")
    return keyed(rows, key)


def same_ids(rows, reference, expected_count, kind):
    require(len(reference) == expected_count and rows.keys() == reference.keys(),
            f"{kind} inventory differs from the {expected_count} registered items")


def parts(text):
    return [s.strip() for s in text.split(";") if s.strip()]


def paths(root, row, column, key):
    names = parts(row[column])
    require(names, f"{key}: missing {column}")
    for name in names:
        path = root / name
        require(not Path(name).is_absolute() and "\\" not in name and ".." not in Path(name).parts
                and name == Path(name).as_posix() and path.resolve() == path and path.is_file(),
                f"{key}: invalid {column} reference {name}")
    return names


def linked_evidence(root, row, prior, key):
    names = paths(root, row, "validation_evidence", key)
    lineage = {name for field in ("evidence_paths", "integration_evidence_at_P10",
                                   "reconstruction_evidence_at_P11") for name in parts(prior[field])}
    require(bool(lineage.intersection(names)), f"{key}: no reference to this item's prior evidence")
    return names


def disposition(row, expected, key):
    require(row["disposition"] in (expected, "required_gap"),
            f"{key}: {row['disposition']} cannot replace {expected}")
    for field in ("remaining_boundary", "evidence_note"):
        value = row[field].strip()
        require(value and value.lower() not in {"planned", "tbd", "none", "n/a", "-"},
                f"{key}: missing substantive {field}")


def pointer(doc, location):
    require(location.startswith("/"), f"Invalid JSON pointer: {location}")
    for fragment in location[1:].split("/"):
        name = fragment.replace("~1", "/").replace("~0", "~")
        if isinstance(doc, list):
            require(name.isdecimal() and str(int(name)) == name, f"Invalid list index: {location}")
            doc = doc[int(name)]
        else:
            doc = doc[name]
    return doc


def read_materializations(root):
    rebuild = json_file(root / REBUILD)
    runs = rebuild["runs"]
    require(len(runs) == 2 and rebuild["repeatability"]["schema_count_and_all_row_digests_equal"]
            and runs[0]["tables"] == runs[1]["tables"], "P11 repeatability evidence incomplete")
    store = json_file(root / "workflows/p09/evidence/store_verification.json")
    require(store["status"] == "passed" and store["release_mode"] == "offline_replay"
            and store["quality_accepted"] is False and store["operational_approval"] is False
            and store["real_recipient_behaviour_observed"] is False, "Invalid P09 store scope")
    offline = {}
    for name in store["canonical_tables"]:
        raw = (root / STORE / "jsonl" / (name + ".jsonl")).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == store["output_sha256"][f"jsonl/{name}.jsonl"],
                f"{name}: P09 output hash mismatch")
        rows = [json.loads(line) for line in raw.splitlines()]
        require(len(rows) == store["counts"][name] and len(rows) > 0, f"{name}: invalid P09 count")
        offline[name] = rows
    return rebuild, offline


def bound_json(path, digest):
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == digest, f"Retained receipt digest differs: {path.name}")
    return json.loads(data)


def check_generation(root, profile, rebuild):
    evidence = root / "workflows/p12/evidence"
    worker = bound_json(evidence / "m2_worker_result.json", profile["worker_result_sha256"])
    manifest = bound_json(evidence / "m2_generation_manifest.json", profile["generation_manifest_sha256"])
    require(worker["status"] == "passed_full_frozen_m2_rebuild" and manifest["all_rows_compared_exactly"] is True
            and worker["generation_id"] == manifest["generation_id"] == profile["generation_id"],
            "Profile and completed M2 generation differ")
    for reference in rebuild["runs"]:
        require(worker["historical_oracle"]["byte_exact_sha256"] == reference["historical_oracle"]["byte_exact_sha256"]
                and all(worker[key] == reference[key] for key in ("generated_counts", "warehouse_counts", "tables")),
                "Fresh worker populations, schemas or logical digests differ from P11")
    require(set(manifest["tables"]) == set(worker["tables"])
            and all(manifest["tables"][name][key] == value for name, table in worker["tables"].items()
                    for key, value in table.items()), "M2 generation manifest differs from worker tables")
    artifacts = manifest["artifacts"]
    require({name: r["sha256"] for name, r in artifacts.items()} == profile["generation_artifacts_sha256"]
            and all(type(r["size_bytes"]) is int and r["size_bytes"] > 0 for r in artifacts.values()),
            "Profile physical artifact inventory differs from generation manifest")


def m2_profiles(root, prior, rebuild):
    tables = rebuild["runs"][0]["tables"]
    profile = json_file(root / PROFILE)
    required_ids = {key for key, row in prior.items()
                    if row["evidence_status"] in {"synthetic_data", "partial_semantics"}}
    fields = profile["fields"]
    require(profile["status"] == "measured_m2_field_presence" and profile["protocol_version"] == 1
            and profile["all_28_tables_matched_both_published_rebuilds"] is True
            and profile["baseline_rebuild_sha256"] == hashlib.sha256((root / REBUILD).read_bytes()).hexdigest()
            and len(required_ids) == profile["profiled_requirements"] == 62 and required_ids == fields.keys(),
            "M2 column profile has different baseline or inventory")
    artifacts = profile["generation_artifacts_sha256"]
    expected_artifacts = {"warehouse.duckdb"} | {f"parquet/{name}.parquet" for name in tables}
    require(re.fullmatch(r"g-[0-9a-f]{32}", profile["generation_id"])
            and profile["profiled_relations"] == len({prior[k]["table"] for k in required_ids})
            and artifacts.keys() == expected_artifacts
            and all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in
                    [profile["generation_manifest_sha256"], profile["worker_result_sha256"], *artifacts.values()]),
            "Missing M2 generation, artifact or worker receipt binding")
    check_generation(root, profile, rebuild)
    for key, field in fields.items():
        table, column = prior[key]["table"], prior[key]["column"]
        nonnull, null = field["non_null_count"], field["null_count"]
        require(field["table"] == table and field["column"] == column
                and field["table_logical_sha256"] == tables[table]["logical_sha256"]
                and type(field["row_count"]) is int and field["row_count"] == tables[table]["row_count"]
                and type(null) is int and type(nonnull) is int and min(nonnull, null) >= 0
                and nonnull + null == field["row_count"], f"{key}: M2 measured column identity/counts differ")
    return fields


def check_requirements(root, rebuild, offline):
    prior = csv_rows(root, "current_requirement_evidence.csv", "requirement_id")
    plan = csv_rows(root, "requirement_traceability.csv", "requirement_id")
    rows = csv_rows(root, "p12_requirement_reconciliation.csv", "requirement_id")
    same_ids(rows, plan, 110, "Requirement")
    same_ids(prior, plan, 110, "P11 requirement")
    fields = json_file(root / "contracts/fields.json")
    tables = rebuild["runs"][0]["tables"]
    profiles = m2_profiles(root, prior, rebuild)
    revision = json_file(root / REVISION)
    require(revision["status"] == "passed_revision_contract" and revision["earlier_snapshot_unchanged"]
            and revision["retained_revisions"] == 4
            and revision["store"]["retained_revision_replay"]["all_typed_payloads_unchanged"],
            "P11 revision evidence incomplete")
    acquisition = json_file(root / ACQUISITION)
    verify_receipt(root, acquisition)
    for key, row in rows.items():
        frozen = plan[key]
        for field in ("name", "table", "column", "field_id", "condition"):
            require(row[field] == frozen[field], f"{key}: changed registered {field}")
        require(row["outputs_in_plan"] == frozen["outputs"], f"{key}: changed planned outputs")
        previous = prior[key]["evidence_status"]
        require(row["implementation_status"] == previous, f"{key}: historical evidence classification changed")
        table, column = row["table"], row["column"]
        outputs = paths(root, row, "output_path", key)
        evidence = linked_evidence(root, row, prior[key], key)
        if key in profiles:
            profile = profiles[key]
            require(row["m2_rows"] == str(profile["row_count"])
                    and row["m2_nonnull_rows"] == str(profile["non_null_count"])
                    and PROFILE in [*outputs, *evidence], f"{key}: M2 observed value count differs")
        else:
            require(not row["m2_rows"] and not row["m2_nonnull_rows"], f"{key}: unprofiled M2 denominator")
        expected = "demonstrated"
        locator = row["output_locator"]
        if table in tables:
            require(column in {c["name"] for c in tables[table]["columns"]}, f"{key}: missing canonical column")
        if table not in offline:
            require(not row["p09_rows"] and not row["p09_nonnull_rows"], f"{key}: spurious P09 data")
        if table in offline:
            data = offline[table]
            require(all(column in record for record in data), f"{key}: column missing from P09 JSONL")
            count = sum(record[column] is not None for record in data)
            require(row["p09_rows"] == str(len(data)) and row["p09_nonnull_rows"] == str(count),
                    f"{key}: P09 denominator or observed value count differs")
            require(f"{STORE}/jsonl/{table}.jsonl" in outputs and locator == "/" + column,
                    f"{key}: incorrect P09 row reference")
            if key == "DS-M01-F041":
                require(count == 0 and "after release" in row["condition"], f"{key}: conditional opening scope changed")
                expected = "conditional_untriggered"
            elif not count:
                expected = "required_gap"
        elif previous == "optional_disabled":
            expected = "optional_disabled"
            require(table not in tables and "contracts/fields.json" in outputs, f"{key}: disabled reference incorrect")
            f = pointer(fields, locator)
            require((f["field_id"], f["name"], f["table"]) == (row["field_id"], column, table),
                    f"{key}: wrong field catalog entry")
        elif key == "DS-M01-F006":
            expected = "revision_witness"
            require(REVISION in outputs and pointer(revision, locator) == revision["store"]["tables"]["provenance_record"],
                    f"{key}: revision witness missing")
        elif key == "DS-M01-P009":
            expected = "acquisition_witness"
            require(ACQUISITION in outputs and pointer(acquisition, locator) == acquisition["retrieved_at"],
                    f"{key}: acquisition witness missing")
        else:
            require(outputs == [PROFILE] and locator == "/fields/" + key and REBUILD in evidence,
                    f"{key}: missing measured M2 field output")
            if not profiles[key]["non_null_count"]:
                expected = "required_gap"
        disposition(row, expected, key)
    return rows


def check_tables(root, rebuild, offline):
    rows = csv_rows(root, "p12_table_reconciliation.csv", "table")
    plan = csv_rows(root, "table_delivery_register.csv", "table")
    prior = csv_rows(root, "current_table_evidence.csv", "table")
    same_ids(rows, plan, 30, "Table")
    same_ids(prior, plan, 30, "P11 table")
    typed = rebuild["runs"][0]["tables"]
    required_tables = {n for n in plan if plan[n]["scope"] == "required"}
    require(set(typed) == required_tables and len(typed) == 28, "Required schema inventory differs")
    for name, row in rows.items():
        scope = plan[name]["scope"]
        require(row["original_scope"] == scope and row["implementation_status"] == prior[name]["canonical_status"],
                f"{name}: table identity or coverage differs")
        require(row["planned_fields"] == plan[name]["field_count"]
                and row["mapped_requirement_ids"] == plan[name]["mapped_requirement_count"],
                f"{name}: planned field/requirement counts changed")
        outputs = paths(root, row, "output_path", name)
        linked_evidence(root, row, prior[name], name)
        m2 = str(typed[name]["row_count"]) if name in typed else ""
        p09 = str(len(offline[name])) if name in offline else ""
        require((row["m2_rows"], row["p09_rows"]) == (m2, p09), f"{name}: wrong table counts or store")
        locator = row["output_locator"]
        if name in offline:
            require(outputs == [f"{STORE}/jsonl/{name}.jsonl"] and locator == "each JSONL object",
                    f"{name}: not the registered separate store")
        elif scope == "required":
            require(int(m2) and outputs == [REBUILD] and locator == f"/runs/0/tables/{name}",
                    f"{name}: missing populated scoped output")
        else:
            require(outputs == ["config/m2.json"] and pointer(json_file(root / outputs[0]), locator) == name,
                    f"{name}: disabled-table configuration differs")
        disposition(row, "demonstrated" if scope == "required" else "optional_disabled", name)
    return rows


def successful_tests(log):
    return set(re.findall(r"^(test_\w+) \([^\n]+\) \.\.\. ok$", log, re.MULTILINE))


def check_scenarios(root):
    rows = csv_rows(root, "p12_validation_reconciliation.csv", "case_id")
    plan = keyed(json_file(root / "contracts/validation_cases_reference.json"), "id")
    prior = csv_rows(root, "current_validation_evidence.csv", "case_id")
    same_ids(rows, plan, 14, "Scenario")
    same_ids(prior, plan, 14, "P11 scenario")
    log = (root / WITNESSES).read_text()
    passed = successful_tests(log)
    logs = {WITNESSES: passed}
    require(passed == set(SCENARIOS.values()) and re.search(r"^Ran 5 tests in [\d.]+s\s+OK\s*\Z", log, re.M),
            "P12 scenario execution evidence incomplete")
    for key, row in rows.items():
        outputs = paths(root, row, "output_path", key)
        previous = prior[key]
        evidence = linked_evidence(root, row, previous, key)
        require(row["reference_scenario"] == plan[key]["case"] and row["implementation_status"] == previous["status"],
                f"{key}: scenario or its historical status changed")
        names = set(parts(row["executed_test_names"]))
        originals = set(parts(previous["executed_test_names"]))
        confirmed = set()
        for path in [*outputs, *evidence]:
            if path.endswith((".log", ".txt")):
                if path not in logs:
                    logs[path] = successful_tests((root / path).read_text())
                confirmed.update(logs[path])
        require(originals <= names <= confirmed, f"{key}: missing test or successful execution record")
        for path in outputs:
            if path.endswith(".json"):
                pointer(json_file(root / path), row["output_locator"])
            elif path.endswith(".jsonl"):
                with (root / path).open() as records:
                    pointer(json.loads(next(records, "")), row["output_locator"])
            else:
                require(path == WITNESSES and row["output_locator"] in passed, f"{key}: unknown output locator")
        if key in SCENARIOS:
            require(SCENARIOS[key] in names and WITNESSES in outputs and row["output_locator"] == SCENARIOS[key], f"{key}: incorrect scenario witness")
            expected = "synthetic_witness"
        elif previous["status"] == "optional_disabled":
            require(not names, f"{key}: disabled scenario reported as executed")
            require(outputs == ["config/m2.json"] and row["output_locator"].startswith("/optional_tables_disabled"),
                    f"{key}: missing disabled option configuration")
            expected = "optional_disabled"
        else:
            reference, anchor = SCENARIO_RESULTS[key]
            require(bool(names) and outputs == [reference] and row["output_locator"] == anchor,
                    f"{key}: retained output belongs to another scenario")
            expected = "demonstrated"
        disposition(row, expected, key)
    return rows


def audit(root):
    root = root.resolve()
    parent_integrity(root)
    rebuild, offline = read_materializations(root)
    groups = {"requirements": check_requirements(root, rebuild, offline),
              "tables": check_tables(root, rebuild, offline), "scenarios": check_scenarios(root)}
    blockers = [{"item": key, "reason": row["remaining_boundary"]} for rows in groups.values()
                for key, row in rows.items() if row["disposition"] == "required_gap"]
    return {"status": "passed_p12_reconciliation_audit", "parent_commit": BASE,
            "scope": "Synthetic engineering evidence for the P12 content gate. Publication, GitHub CI and final project acceptance are evaluated separately.",
            "item_counts": {kind: len(rows) for kind, rows in groups.items()},
            "required_table_count": len(rebuild["runs"][0]["tables"]),
            "dispositions": {kind: dict(sorted(Counter(r["disposition"] for r in rows.values()).items()))
                             for kind, rows in groups.items()},
            "required_gaps": blockers, "eligible_for_p13": not blockers,
            "open_model_quality_findings": ["G14", "G15", "G16"],
            "quality_accepted": False, "operational_approval": False, "p13_accepted": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--require-ready", action="store_true", help="exit 2 when the reconciliation records a required gap")
    args = parser.parse_args()
    try:
        result = audit(args.root)
    except (ValueError, OSError, KeyError, IndexError, TypeError, csv.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "failed_p12_reconciliation_audit", "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(result, sort_keys=True, indent=2))
    return 2 if args.require_ready and not result["eligible_for_p13"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
