"""Hand-authored runner cases for frozen history and protected-period isolation."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from ds_m01.baseline_run import (
    INPUT_FILES, evaluate, freeze_history, load_training_inputs, verify_frozen_inputs,
)
from ds_m01.common import digest_file, write_jsonl


PROJECT = Path(__file__).resolve().parents[1]


def source_config():
    return json.loads((PROJECT / "config/m2.json").read_text())


def settings():
    result = json.loads((PROJECT / "config/m3a.json").read_text())
    result.update(folds=result["folds"][:1], min_support=1, bootstrap_samples=0)
    return result


def hand_inputs():
    """One two-hour fit trip and four different validation outcome states."""
    specs = [
        ("FIT", "2026-01-10", "-08:00", "eligible"),
        ("V-ELIGIBLE", "2026-03-10", "-07:00", "eligible"),
        ("V-UNKNOWN", "2026-03-11", "-07:00", "not_mature"),
        ("V-ARRIVED", "2026-03-12", "-07:00", "already_arrived"),
        ("V-CANCELLED", "2026-03-13", "-07:00", "cancelled"),
    ]
    result = dict(trips=[], snapshots=[], first_snapshot={}, outcomes={}, promises={},
                  original_role_trip_counts={"train": len(specs)})
    for identifier, day, offset, state in specs:
        key = ("T1", identifier)
        departure = f"{day}T08:00:00{offset}"
        trip = dict(tenant_id="T1", trip_id=identifier, departure_time=departure,
                    split_role="train", vehicle_id="T1-V1", route_id="R1",
                    unseen_vehicle=False, unseen_route=False)
        snapshot = dict(
            tenant_id="T1", trip_id=identifier, snapshot_id="S-"+identifier,
            snapshot_time=f"{day}T08:30:00{offset}",
            route_id="R1", route_version_id="R1-v1", route_distance_km=100,
            departure_time_bucket="morning", elapsed_trip_seconds=1800,
            promise_version_id="P-"+identifier, position_validity="no_position",
        )
        outcome = dict(
            tenant_id="T1", trip_id=identifier, outcome_version_id="O-"+identifier+"-v1",
            label_available_time=f"{day}T12:00:00{offset}", label_status="verified",
            actual_arrival_time=f"{day}T10:00:00{offset}", arrival_time_precision="second",
        )
        if state == "not_mature":
            outcome.update(label_status="pending", actual_arrival_time=None)
        elif state == "already_arrived":
            outcome["actual_arrival_time"] = f"{day}T08:20:00{offset}"
        elif state == "cancelled":
            outcome.update(label_status="cancelled", actual_arrival_time=None)
        promise = dict(tenant_id="T1", trip_id=identifier,
                       promise_version_id=snapshot["promise_version_id"],
                       window_end_at=f"{day}T10:30:00{offset}")
        result["trips"].append(trip)
        result["snapshots"].append(snapshot)
        result["first_snapshot"][key] = snapshot
        result["outcomes"][key] = [outcome]
        result["promises"][key] = [promise]
    return result


def write_scoped_fixture(root, inputs, poison_protected=False):
    """Minimal M2 interface with extra groups whose payload must be discarded."""
    membership = [{k: v for k, v in row.items() if k != "departure_time"}
                  for row in inputs["trips"]]
    protected = [("CAL", "calibration"), ("TEST", "test"), ("EXCLUDED", "excluded")]
    membership += [dict(tenant_id="T1", trip_id=identifier, split_role=role)
                   for identifier, role in protected]
    assignments = [dict(tenant_id=row["tenant_id"], trip_id=row["trip_id"],
                        split_role=row["split_role"]) for row in membership]
    events = [dict(tenant_id=row["tenant_id"], trip_id=row["trip_id"],
                   event_type="departure", occurred_at=row["departure_time"],
                   available_at=row["departure_time"])
              for row in inputs["trips"]]
    snapshots = copy.deepcopy(inputs["snapshots"])
    outcomes = [copy.deepcopy(row) for values in inputs["outcomes"].values() for row in values]
    promises = [copy.deepcopy(row) for values in inputs["promises"].values() for row in values]
    lineage = [dict(tenant_id=row["tenant_id"], snapshot_id=row["snapshot_id"],
                    max_source_available_time=row["snapshot_time"])
               for row in snapshots]
    if poison_protected:
        for identifier, _ in protected:
            key = dict(tenant_id="T1", trip_id=identifier)
            events.append(dict(key, event_type="departure", occurred_at="bad-time"))
            snapshots.append(dict(key, snapshot_id="S-"+identifier, snapshot_time="bad-time"))
            outcomes.append(dict(key, label_available_time="bad-time", label_status="unexpected"))
            promises.append(dict(key, window_end_at="bad-time"))
            lineage.append(dict(tenant_id="T1", snapshot_id="S-"+identifier,
                                max_source_available_time="bad-time"))
    routes = [dict(tenant_id="T1", route_version_id="R1-v1", route_id="R1",
                   planned_distance_km=100, known_at="2025-12-01T00:00:00Z",
                   valid_from="2025-12-01T00:00:00Z", valid_to=None)]
    data = {
        "data/processed/eta_split_assignment.jsonl": assignments,
        "data/processed/trip_membership.jsonl": membership,
        "data/raw/eta_trip_event.jsonl": events,
        "data/raw/eta_trip_outcome.jsonl": outcomes,
        "data/raw/eta_promise_version.jsonl": promises,
        "data/raw/eta_route_version.jsonl": routes,
        "data/processed/eta_snapshot.jsonl": snapshots,
        "data/processed/feature_lineage.jsonl": lineage,
    }
    for relative, rows in data.items():
        write_jsonl(root / relative, rows)


class BaselineRunnerTests(unittest.TestCase):
    def test_fit_retains_old_known_revision_and_excludes_exact_cutoff(self):
        inputs = hand_inputs()
        original = inputs["outcomes"][("T1", "FIT")][0]
        late = dict(original, outcome_version_id="late-v2",
                    label_available_time="2026-03-01T08:00:00Z",
                    actual_arrival_time="2026-01-10T16:00:00-08:00")
        inputs["outcomes"][("T1", "FIT")].append(late)
        history, exclusions = freeze_history(inputs, {("T1", "FIT")}, "2026-03-01T08:00:00Z")
        self.assertEqual(exclusions, {})
        self.assertEqual(history[0]["historical_outcome_version_id"], original["outcome_version_id"])
        self.assertEqual(history[0]["duration_seconds"], 7200)
        later_history, _ = freeze_history(inputs, {("T1", "FIT")}, "2026-04-01T07:00:00Z")
        self.assertEqual(later_history[0]["historical_outcome_version_id"], "late-v2")

    def test_latest_conflict_does_not_resurrect_older_verified_outcome(self):
        inputs = hand_inputs()
        original = inputs["outcomes"][("T1", "FIT")][0]
        inputs["outcomes"][("T1", "FIT")].append(dict(
            original, outcome_version_id="conflict-v2", label_status="conflict",
            label_available_time="2026-02-01T08:00:00Z"))
        history, exclusions = freeze_history(inputs, {("T1", "FIT")}, "2026-03-01T08:00:00Z")
        self.assertEqual(history, [])
        self.assertEqual(exclusions, {"latest_outcome_not_verified": 1})

    def test_validation_targets_cannot_change_predictions(self):
        original = hand_inputs()
        changed = copy.deepcopy(original)
        changed["outcomes"][("T1", "V-ELIGIBLE")][0]["actual_arrival_time"] = "2026-03-10T20:00:00-07:00"
        before = evaluate(original, source_config(), settings())
        after = evaluate(changed, source_config(), settings())
        self.assertEqual(before["predictions"], after["predictions"])
        self.assertNotEqual(before["evaluation_rows"], after["evaluation_rows"])
        self.assertEqual(before["predictions"][0]["p50_remaining_seconds"], 5400)

    def test_fold_relabels_at_own_cutoff_not_global_m2_cutoff(self):
        inputs = hand_inputs()
        old = inputs["outcomes"][("T1", "V-ELIGIBLE")][0]
        inputs["outcomes"][("T1", "V-ELIGIBLE")].append(dict(
            old, outcome_version_id="at-april-cutoff", actual_arrival_time="2026-03-10T20:00:00-07:00",
            label_available_time="2026-04-01T07:00:00Z"))
        results = evaluate(inputs, source_config(), settings())
        row = next(row for row in results["evaluation_rows"] if row["trip_id"] == "V-ELIGIBLE")
        self.assertEqual(row["outcome_version_id"], old["outcome_version_id"])
        self.assertEqual(row["remaining_duration_seconds"], 5400)

    def test_all_opportunities_retained_before_target_eligibility(self):
        results = evaluate(hand_inputs(), source_config(), settings())
        self.assertEqual(len(results["predictions"]), 4)
        self.assertEqual({row["label_eligibility"] for row in results["evaluation_rows"]},
                         {"eligible", "not_mature", "already_arrived", "cancelled"})
        self.assertEqual({row["status"] for row in results["predictions"]}, {"available"})
        self.assertEqual(results["metrics"]["overall"]["counts"]["snapshots"], 4)
        self.assertEqual(results["metrics"]["overall"]["counts"]["supported_eligible_snapshots"], 1)

    def test_unsupported_predictions_also_remain_in_evaluation(self):
        controls = settings()
        controls["min_support"] = 2
        results = evaluate(hand_inputs(), source_config(), controls)
        self.assertEqual(len(results["predictions"]), 4)
        self.assertEqual({row["status"] for row in results["predictions"]}, {"unsupported"})
        self.assertTrue(all(row["eta_median"] is None for row in results["predictions"]))
        self.assertIsNone(results["metrics"]["overall"]["metrics"]["MAE_minutes"])

    def test_protected_payload_poison_is_discarded_by_loader(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_scoped_fixture(root, hand_inputs())
            clean = load_training_inputs(root, source_config())
            write_scoped_fixture(root, hand_inputs(), poison_protected=True)
            poisoned = load_training_inputs(root, source_config())
            self.assertEqual(clean, poisoned)
            self.assertEqual(clean["original_role_trip_counts"]["test"], 1)
            self.assertEqual(evaluate(clean, source_config(), settings())["predictions"],
                             evaluate(poisoned, source_config(), settings())["predictions"])

    def test_missing_future_or_duplicate_training_lineage_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_scoped_fixture(root, hand_inputs())
            path = root / "data/processed/feature_lineage.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            for bad_rows in (rows[1:], rows + [rows[0]],
                             [dict(row, max_source_available_time="2030-01-01T00:00:00Z") for row in rows]):
                write_jsonl(path, bad_rows)
                with self.subTest(bad_rows=bad_rows), self.assertRaises(ValueError):
                    load_training_inputs(root, source_config())

    def test_input_hash_gate_detects_drift_and_missing_manifest_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_scoped_fixture(root, hand_inputs())
            report = dict(status="passed", files=[dict(path=path, original_sha256=digest_file(root/path))
                                                  for path in INPUT_FILES])
            (root / "reports").mkdir()
            receipt = root / "reports/rebuild_report.json"
            receipt.write_text(json.dumps(report))
            self.assertEqual(set(verify_frozen_inputs(root)), set(INPUT_FILES))
            last = report["files"].pop()
            receipt.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, "differs"):
                verify_frozen_inputs(root)
            report["files"].append(last)
            receipt.write_text(json.dumps(report))
            with (root / INPUT_FILES[0]).open("a") as stream:
                stream.write("\n")
            with self.assertRaisesRegex(ValueError, "differs"):
                verify_frozen_inputs(root)


if __name__ == "__main__":
    unittest.main()
