"""Independent, hand-calculated tests for time leakage and target semantics."""

import copy
import json
from pathlib import Path
import unittest

from ds_m01.pipeline import assign_split, label_for_snapshot, snapshot_for_trip


FIXTURE = Path(__file__).parent / "fixtures" / "point_in_time_case.json"


class PointInTimeTests(unittest.TestCase):
    def setUp(self):
        self.case = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.tables = self.case["tables"]
        self.when = self.case["snapshot_time"]

    def snapshot(self, when=None):
        return snapshot_for_trip("T01", "TRIP-1", when or self.when, self.tables)

    def label(self, snapshot=None, outcomes=None, cutoff="2026-07-01T07:00:00Z"):
        return label_for_snapshot(
            self.snapshot() if snapshot is None else snapshot,
            self.case["outcomes"] if outcomes is None else outcomes,
            self.tables["eta_promise_version"],
            cutoff,
        )

    def status_event(self, status, occurred, available):
        return {
            "tenant_id": "T01", "trip_id": "TRIP-1",
            "trip_event_id": "STATUS-" + status,
            "event_type": "cancellation" if status == "cancelled" else "status_update",
            "trip_status": status, "occurred_at": occurred,
            "available_at": available, "provenance_id": "HAND-1",
        }

    def test_hand_calculated_snapshot(self):
        snapshot = self.snapshot()
        self.assertIsNotNone(snapshot)
        for key in ("selected_position_event_id", "position_age_seconds", "elapsed_trip_seconds"):
            self.assertEqual(snapshot[key], self.case["expected"][key])
        self.assertEqual(snapshot["position_validity"], "valid")

    def test_future_promise_is_ignored_and_label_keeps_snapshot_promise(self):
        future = dict(self.tables["eta_promise_version"][0])
        future.update(promise_version_id="PROMISE-2", known_at="2026-06-23T10:05:00Z",
                      window_end_at="2026-06-23T12:00:00Z",
                      supersedes_promise_version_id="PROMISE-1")
        self.tables["eta_promise_version"].append(future)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["promise_version_id"], "PROMISE-1")
        label = self.label(snapshot)
        self.assertEqual(label["promise_version_id"], "PROMISE-1")
        self.assertIs(label["delay_outcome"], True)

    def test_late_gps_and_future_event_gps_do_not_leak(self):
        late = dict(self.tables["telemetry_event"][0])
        late.update(canonical_event_id="GPS-LATE", event_time="2026-06-23T09:59:00Z",
                    available_time="2026-06-23T10:01:00Z", latitude_deg=35.0,
                    source_event_id="GPS-RAW-LATE")
        future = dict(late)
        future.update(canonical_event_id="GPS-FUTURE", event_time="2026-06-23T10:02:00Z",
                      available_time="2026-06-23T09:59:00Z", source_event_id="GPS-RAW-FUTURE")
        self.tables["telemetry_event"].extend([late, future])
        snapshot = self.snapshot()
        self.assertEqual(snapshot["selected_position_event_id"], "GPS-1")
        self.assertEqual(snapshot["latitude_deg"], 34.3)
        self.assertEqual(snapshot["position_age_seconds"], 300)

    def test_known_arrival_stops_new_snapshot(self):
        self.tables["eta_trip_event"].append(self.status_event(
            "arrived", "2026-06-23T09:50:00Z", "2026-06-23T09:51:00Z"))
        self.assertIsNone(self.snapshot())

    def test_late_arrival_status_does_not_stop_historical_snapshot(self):
        self.tables["eta_trip_event"].append(self.status_event(
            "arrived", "2026-06-23T09:50:00Z", "2026-06-23T10:05:00Z"))
        snapshot = self.snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["trip_status_as_of"], "in_transit")

    def test_future_occurrence_cannot_stop_snapshot_even_if_received(self):
        self.tables["eta_trip_event"].append(self.status_event(
            "arrived", "2026-06-23T10:05:00Z", "2026-06-23T09:59:00Z"))
        self.assertIsNotNone(self.snapshot())

    def test_known_cancellation_stops_new_snapshot(self):
        self.tables["eta_trip_event"].append(self.status_event(
            "cancelled", "2026-06-23T09:50:00Z", "2026-06-23T09:51:00Z"))
        self.assertIsNone(self.snapshot())

    def test_later_known_status_correction_reopens_erroneous_arrival(self):
        wrong = self.status_event("arrived", "2026-06-23T09:45:00Z", "2026-06-23T09:46:00Z")
        corrected = self.status_event("in_transit", "2026-06-23T09:45:00Z", "2026-06-23T09:55:00Z")
        corrected["trip_event_id"] = "STATUS-CORRECTION"
        self.tables["eta_trip_event"].extend([wrong, corrected])
        self.assertIsNone(self.snapshot("2026-06-23T09:50:00Z"))
        snapshot = self.snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["trip_status_as_of"], "in_transit")

    def test_latest_lost_or_incomplete_status_is_preserved(self):
        for status in ("lost", "incomplete"):
            with self.subTest(status=status):
                self.tables["eta_trip_event"] = [self.tables["eta_trip_event"][0],
                    self.status_event(status, "2026-06-23T09:50:00Z", "2026-06-23T09:55:00Z")]
                snapshot = self.snapshot()
                self.assertIsNotNone(snapshot)
                self.assertEqual(snapshot["trip_status_as_of"], status)

    def test_assignment_revision_uses_known_time_and_half_open_boundary(self):
        old = self.tables["core_vehicle_device_assignment"][0]
        old.update(known_to="2026-06-23T11:00:00Z", known_to_state="known")
        corrected = dict(old)
        corrected.update(assignment_id="ASSIGN-2", device_id="D-2",
                         known_from="2026-06-23T11:00:00Z", known_to=None,
                         known_to_state="open")
        self.tables["core_vehicle_device_assignment"].append(corrected)
        position = dict(self.tables["telemetry_event"][0])
        position.update(canonical_event_id="GPS-D2", device_id="D-2",
                        event_time="2026-06-23T09:58:00Z", available_time="2026-06-23T09:59:00Z",
                        source_event_id="GPS-RAW-D2")
        self.tables["telemetry_event"].append(position)
        self.assertEqual(self.snapshot()["selected_position_event_id"], "GPS-1")
        at_revision = self.snapshot("2026-06-23T11:00:00Z")
        self.assertEqual(at_revision["selected_position_event_id"], "GPS-D2")
        self.assertEqual(at_revision["device_id"], "D-2")
        # Unknown ends are not open ends; withdrawn/unknown mappings are not
        # evidence that this vehicle owns a position, even with matching IDs.
        for changes in ({"resolution_status": "unknown"},
                        {"resolution_status": "withdrawn"},
                        {"valid_to": None, "valid_to_state": "unknown"},
                        {"known_to": None, "known_to_state": "unknown"}):
            with self.subTest(mapping=changes):
                self.tables = copy.deepcopy(self.case["tables"])
                self.tables["core_vehicle_device_assignment"] = [dict(old, **changes)]
                self.assertIsNone(self.snapshot()["selected_position_event_id"])

    def test_tenant_collisions_cannot_change_snapshot_or_label(self):
        for table_name, rows in list(self.tables.items()):
            colliding = copy.deepcopy(rows)
            for row in colliding:
                row["tenant_id"] = "T02"
                if table_name == "telemetry_event":
                    row.update(event_time="2026-06-23T09:59:00Z",
                               available_time="2026-06-23T09:59:00Z", latitude_deg=1.0)
                if table_name == "eta_promise_version":
                    row["window_end_at"] = "2026-06-23T15:00:00Z"
            rows.extend(colliding)
        self.tables["eta_trip_event"].append(dict(
            self.status_event("cancelled", "2026-06-23T09:58:00Z", "2026-06-23T09:59:00Z"),
            tenant_id="T02"))
        foreign_outcome = dict(self.case["outcomes"][0], tenant_id="T02",
                               actual_arrival_time="2026-06-23T10:01:00Z",
                               label_available_time="2026-06-23T14:00:00Z")
        self.case["outcomes"].append(foreign_outcome)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["latitude_deg"], 34.3)
        label = self.label(snapshot)
        self.assertEqual(label["remaining_duration_seconds"], 3600)
        self.assertIs(label["delay_outcome"], True)

    def test_invalid_latest_gps_falls_back_to_last_valid_position(self):
        for changes in ({"gnss_valid": "no_fix"}, {"latitude_deg": 95.0}):
            with self.subTest(changes=changes):
                bad = dict(self.tables["telemetry_event"][0])
                bad.update(canonical_event_id="GPS-BAD", event_time="2026-06-23T09:59:00Z",
                           available_time="2026-06-23T09:59:00Z", source_event_id="GPS-RAW-BAD", **changes)
                self.tables["telemetry_event"] = [self.tables["telemetry_event"][0], bad]
                self.assertEqual(self.snapshot()["selected_position_event_id"], "GPS-1")

    def test_no_valid_position_preserves_missing_values(self):
        self.tables["telemetry_event"][0]["gnss_valid"] = "no_fix"
        snapshot = self.snapshot()
        self.assertEqual(snapshot["position_validity"], "no_position")
        for key in ("selected_position_event_id", "latitude_deg", "longitude_deg", "position_age_seconds"):
            self.assertIsNone(snapshot[key], key)

    def test_arrival_service_and_delivery_are_distinct_targets(self):
        label = self.label()
        self.assertEqual(label["remaining_duration_seconds"], 3600)
        self.assertNotEqual(label["remaining_duration_seconds"], 7200)
        self.assertNotEqual(label["remaining_duration_seconds"], 10800)
        self.assertEqual(label["label_eligibility"], "eligible")

    def test_unknown_and_cancelled_outcomes_never_become_zero_targets(self):
        absent = self.label(outcomes=[])
        self.assertEqual(absent["label_eligibility"], "not_mature")
        self.assertIsNone(absent["remaining_duration_seconds"])
        self.assertIsNone(absent["delay_outcome"])
        for status, eligibility in (("cancelled", "cancelled"), ("lost", "lost"),
                                    ("conflict", "conflict"), ("pending", "not_mature")):
            with self.subTest(status=status):
                outcome = dict(self.case["outcomes"][0], label_status=status,
                               actual_arrival_time=None, service_start_time=None,
                               delivery_completed_time=None)
                label = self.label(outcomes=[outcome])
                self.assertEqual(label["label_eligibility"], eligibility)
                self.assertIsNone(label["remaining_duration_seconds"])
                self.assertIsNone(label["delay_outcome"])

    def test_late_label_revision_respects_exclusive_cutoff(self):
        revision = dict(self.case["outcomes"][0])
        revision.update(outcome_version_id="OUTCOME-2", actual_arrival_time="2026-06-23T11:30:00Z",
                        label_available_time="2026-07-01T07:00:00Z",
                        supersedes_outcome_version_id="OUTCOME-1", revision_reason="gate correction")
        self.case["outcomes"].append(revision)
        at_cutoff = self.label(cutoff="2026-07-01T07:00:00Z")
        self.assertEqual(at_cutoff["outcome_version_id"], "OUTCOME-1")
        self.assertEqual(at_cutoff["remaining_duration_seconds"], 3600)
        after_cutoff = self.label(cutoff="2026-07-01T07:00:01Z")
        self.assertEqual(after_cutoff["outcome_version_id"], "OUTCOME-2")
        self.assertEqual(after_cutoff["remaining_duration_seconds"], 5400)

    def test_post_arrival_not_known_snapshot_is_retained_but_not_regression_target(self):
        self.tables["eta_trip_event"].append(self.status_event(
            "arrived", "2026-06-23T09:50:00Z", "2026-06-23T10:05:00Z"))
        snapshot = self.snapshot()
        self.assertIsNotNone(snapshot)
        outcome = dict(self.case["outcomes"][0], actual_arrival_time="2026-06-23T09:50:00Z")
        label = self.label(snapshot=snapshot, outcomes=[outcome])
        self.assertEqual(label["label_eligibility"], "already_arrived")
        self.assertIsNone(label["remaining_duration_seconds"])

    def test_split_buffers_and_local_date_boundaries(self):
        cases = (
            ("2026-06-23T23:59:00-07:00", "train"),
            ("2026-06-24T00:00:00-07:00", "excluded"),
            ("2026-06-30T23:59:00-07:00", "excluded"),
            ("2026-07-01T00:00:00-07:00", "calibration"),
            ("2026-07-24T23:59:00-07:00", "calibration"),
            ("2026-07-25T00:00:00-07:00", "excluded"),
            ("2026-07-31T23:59:00-07:00", "excluded"),
            ("2026-08-01T00:00:00-07:00", "test"),
            ("2026-09-30T23:59:00-07:00", "test"),
            ("2026-10-01T00:00:00-07:00", "excluded"),
        )
        for departure, expected in cases:
            with self.subTest(departure=departure):
                split = assign_split("T01", "TRIP-1", departure, "V-1", "ROUTE-1", self.case["split_config"])
                self.assertEqual(split["split_role"], expected)

    def test_unseen_vehicles_and_routes_excluded_before_test(self):
        for vehicle, route in (("T01-V10", "ROUTE-1"), ("V-1", "ROUTE-12")):
            for departure in ("2025-12-10T12:00:00-08:00", "2026-02-10T12:00:00-08:00",
                              "2026-07-10T12:00:00-07:00"):
                with self.subTest(vehicle=vehicle, route=route, departure=departure):
                    split = assign_split("T01", "TRIP-1", departure, vehicle, route, self.case["split_config"])
                    self.assertEqual(split["split_role"], "excluded")
            split = assign_split("T01", "TRIP-1", "2026-08-10T12:00:00-07:00",
                                 vehicle, route, self.case["split_config"])
            self.assertEqual(split["split_role"], "test")

    def test_repeated_dst_hour_uses_offset_not_wall_clock_subtraction(self):
        departure = self.tables["eta_trip_event"][0]
        departure.update(occurred_at="2026-11-01T01:30:00-07:00",
                         available_at="2026-11-01T01:30:00-07:00")
        snapshot = self.snapshot("2026-11-01T01:30:00-08:00")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["elapsed_trip_seconds"], 3600)


if __name__ == "__main__":
    unittest.main()
