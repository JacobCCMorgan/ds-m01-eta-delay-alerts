"""Small independent integration cases: actual duration and available lineage."""

import copy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from ds_m01.pipeline import build


FIXTURE = Path(__file__).parent / "fixtures" / "point_in_time_case.json"
RAW_TABLES = (
    "core_tenant", "core_vehicle", "core_device", "core_vehicle_device_assignment",
    "eta_trip", "eta_facility_version", "eta_route_version", "eta_trip_plan_version",
    "eta_promise_version", "eta_trip_event", "eta_trip_outcome", "telemetry_event",
    "provenance_record",
)


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class BuildInvariantTests(unittest.TestCase):
    def setUp(self):
        source = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.tables = copy.deepcopy(source["tables"])
        self.config = source["split_config"]
        self.config.update(extract_as_of="2026-10-15T07:00:00Z", max_snapshot_hours=0,
                           settings={"snapshot_interval_minutes": 30})
        self.tables.update(
            core_tenant=[{"tenant_id": "T01", "report_timezone": "America/Los_Angeles"}],
            core_vehicle=[{"tenant_id": "T01", "vehicle_id": "V-1"}],
            core_device=[{"tenant_id": "T01", "device_id": "D-1"}],
            telemetry_event=[], eta_trip_outcome=[],
        )
        # Only the historical trip's actual departure differs from its plan.
        # A second trip departs at noon, after the first label became available.
        for table in ("eta_trip", "eta_trip_plan_version", "eta_promise_version", "eta_trip_event"):
            prototype = self.tables[table][0]
            self.tables[table] = [dict(prototype, trip_id="HISTORY"), dict(prototype, trip_id="FOCAL")]
        plans = self.tables["eta_trip_plan_version"]
        plans[0].update(trip_version_id="PLAN-HISTORY", planned_departure_at="2026-06-23T09:00:00Z")
        plans[1].update(trip_version_id="PLAN-FOCAL", planned_departure_at="2026-06-23T12:00:00Z")
        promises = self.tables["eta_promise_version"]
        promises[0]["promise_version_id"] = "PROMISE-HISTORY"
        promises[1].update(promise_version_id="PROMISE-FOCAL", window_start_at="2026-06-23T13:00:00Z",
                           window_end_at="2026-06-23T14:00:00Z")
        departures = self.tables["eta_trip_event"]
        departures[0].update(trip_event_id="DEPART-HISTORY", occurred_at="2026-06-23T09:30:00Z",
                             available_at="2026-06-23T09:30:00Z")
        departures[1].update(trip_event_id="DEPART-FOCAL", occurred_at="2026-06-23T12:00:00Z",
                             available_at="2026-06-23T12:00:00Z")
        historical = dict(source["outcomes"][0], trip_id="HISTORY", outcome_version_id="OUTCOME-HISTORY",
                          service_start_time="2026-06-23T11:10:00Z", delivery_completed_time="2026-06-23T11:20:00Z",
                          label_available_time="2026-06-23T11:30:00Z", followup_through="2026-06-23T11:30:00Z")
        focal = dict(source["outcomes"][0], trip_id="FOCAL", outcome_version_id="OUTCOME-FOCAL",
                     actual_arrival_time="2026-06-23T13:00:00Z", service_start_time="2026-06-23T13:05:00Z",
                     delivery_completed_time="2026-06-23T13:10:00Z", label_available_time="2026-06-23T13:15:00Z",
                     followup_through="2026-06-23T13:15:00Z")
        self.tables["eta_trip_outcome"] = [historical, focal]
        # Distinct source provenance makes a missing dependency observable.
        provenance = []
        for table, rows in self.tables.items():
            if table == "provenance_record":
                continue
            for index, row in enumerate(rows):
                pid = f"HAND-{table}-{index}"
                row["provenance_id"] = pid
                available = next((row[key] for key in ("available_at", "available_time", "label_available_time",
                                                       "plan_known_at", "known_at", "known_from", "registered_at")
                                  if row.get(key)), "2026-01-01T00:00:00Z")
                provenance.append({"tenant_id": "T01", "provenance_id": pid, "available_time": available})
        self.tables["provenance_record"] = provenance

    def run_build(self):
        with tempfile.TemporaryDirectory(prefix="ds-m01-hand-case-") as temporary:
            root = Path(temporary)
            (root / "data/raw").mkdir(parents=True)
            for name in RAW_TABLES:
                rows = self.tables.get(name, [])
                (root / "data/raw" / f"{name}.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            summary = build(root, self.config)
            outputs = {}
            for path in (root / "data/processed").glob("*.jsonl"):
                outputs[path.stem] = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            return summary, outputs

    def test_history_uses_actual_departure_and_complete_available_lineage(self):
        summary, outputs = self.run_build()
        self.assertEqual(summary["snapshot_count"], 2)
        history = outputs["eta_history_reference"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["historical_trip_id"], "HISTORY")
        self.assertEqual(history[0]["historical_duration_seconds"], 5400)
        self.assertNotEqual(history[0]["historical_duration_seconds"], 7200)
        snapshots = {row["snapshot_id"]: row for row in outputs["eta_snapshot"]}
        provenance = {row["provenance_id"]: row for row in outputs["provenance_record"]}
        source_departure = self.tables["eta_trip_event"][0]["provenance_id"]
        source_outcome = self.tables["eta_trip_outcome"][0]["provenance_id"]
        focal_snapshot = snapshots[history[0]["snapshot_id"]]
        hist_prov = provenance[history[0]["provenance_id"]]
        self.assertTrue({source_departure, source_outcome, focal_snapshot["provenance_id"]}.issubset(
            hist_prov["parent_provenance_ids"]))
        self.assertEqual(timestamp(hist_prov["available_time"]), timestamp("2026-06-23T12:00:00Z"))
        for record in provenance.values():
            for parent_id in record.get("parent_provenance_ids", []):
                self.assertIn(parent_id, provenance)
                self.assertGreaterEqual(timestamp(record["available_time"]),
                                        timestamp(provenance[parent_id]["available_time"]))
        for snapshot in snapshots.values():
            self.assertEqual(timestamp(provenance[snapshot["provenance_id"]]["available_time"]),
                             timestamp(snapshot["snapshot_time"]))

    def test_history_rejects_imprecise_or_not_yet_known_outcomes(self):
        original = copy.deepcopy(self.tables["eta_trip_outcome"][0])
        cases = ({"arrival_time_precision": "date_only"}, {"arrival_time_precision": "interval"},
                 {"label_available_time": "2026-06-23T12:00:00Z"})
        for changes in cases:
            with self.subTest(changes=changes):
                self.tables["eta_trip_outcome"][0] = dict(original, **changes)
                _, outputs = self.run_build()
                self.assertEqual(outputs["eta_history_reference"], [])

    def test_short_cancelled_trip_keeps_first_departure_known_snapshot(self):
        # Keep one trip so a missing short-trip snapshot cannot hide in totals.
        for table in ("eta_trip", "eta_trip_plan_version", "eta_promise_version",
                      "eta_trip_event", "eta_trip_outcome"):
            self.tables[table] = [r for r in self.tables[table] if r["trip_id"] == "FOCAL"]
        self.config["max_snapshot_hours"] = 1
        departure = self.tables["eta_trip_event"][0]
        departure["available_at"] = "2026-06-23T12:00:20Z"
        cancellation = dict(departure, trip_event_id="CANCEL-FOCAL", event_type="cancellation",
                            trip_status="cancelled", occurred_at="2026-06-23T12:25:00Z",
                            available_at="2026-06-23T12:25:10Z", provenance_id="HAND-CANCEL-FOCAL")
        self.tables["eta_trip_event"].append(cancellation)
        outcome = self.tables["eta_trip_outcome"][0]
        outcome.update(label_status="cancelled", actual_arrival_time=None, service_start_time=None,
                       delivery_completed_time=None, cancellation_time="2026-06-23T12:25:00Z",
                       label_available_time="2026-06-23T12:25:10Z", cancellation_reason="customer",
                       followup_through="2026-06-23T12:25:10Z", followup_end_reason="cancelled")
        for row in self.tables["provenance_record"]:
            if row["provenance_id"] == departure["provenance_id"]:
                row["available_time"] = departure["available_at"]
            if row["provenance_id"] == outcome["provenance_id"]:
                row["available_time"] = outcome["label_available_time"]
        self.tables["provenance_record"].append({"tenant_id": "T01", "provenance_id": "HAND-CANCEL-FOCAL",
                                                "available_time": cancellation["available_at"]})
        summary, outputs = self.run_build()
        self.assertEqual(summary["snapshot_count"], 1)
        snapshot = outputs["eta_snapshot"][0]
        self.assertEqual(timestamp(snapshot["snapshot_time"]), timestamp("2026-06-23T12:00:20Z"))
        self.assertEqual(snapshot["elapsed_trip_seconds"], 20)
        self.assertEqual(snapshot["trip_status_as_of"], "in_transit")
        label = outputs["eta_snapshot_label"][0]
        self.assertEqual(label["label_eligibility"], "cancelled")
        self.assertIsNone(label["remaining_duration_seconds"])
        self.assertIsNone(label["delay_outcome"])

    def test_selected_status_is_a_snapshot_parent_but_future_status_is_not(self):
        self.config["max_snapshot_hours"] = 0.5
        status = {
            "tenant_id": "T01", "trip_id": "FOCAL", "trip_event_id": "STATUS-FOCAL",
            "event_type": "status_update", "trip_status": "lost",
            "occurred_at": "2026-06-23T12:10:00Z", "available_at": "2026-06-23T12:20:00Z",
            "provenance_id": "HAND-STATUS-FOCAL",
        }
        future = dict(status, trip_event_id="STATUS-FUTURE", trip_status="incomplete",
                      occurred_at="2026-06-23T12:25:00Z", available_at="2026-06-23T12:31:00Z",
                      provenance_id="HAND-STATUS-FUTURE")
        self.tables["eta_trip_event"].extend([status, future])
        self.tables["provenance_record"].extend([
            {"tenant_id": "T01", "provenance_id": row["provenance_id"], "available_time": row["available_at"]}
            for row in (status, future)
        ])
        _, outputs = self.run_build()
        snapshot = next(row for row in outputs["eta_snapshot"]
                        if row["trip_id"] == "FOCAL" and
                        timestamp(row["snapshot_time"]) == timestamp("2026-06-23T12:30:00Z"))
        self.assertEqual(snapshot["trip_status_as_of"], "lost")
        provenance = next(row for row in outputs["provenance_record"]
                          if row["provenance_id"] == snapshot["provenance_id"])
        self.assertIn("HAND-STATUS-FOCAL", provenance["parent_provenance_ids"])
        self.assertNotIn("HAND-STATUS-FUTURE", provenance["parent_provenance_ids"])
        lineage = next(row for row in outputs["feature_lineage"] if row["snapshot_id"] == snapshot["snapshot_id"])
        self.assertEqual(timestamp(lineage["max_source_available_time"]), timestamp("2026-06-23T12:20:00Z"))


if __name__ == "__main__":
    unittest.main()
