"""Display population, temporal disclosure and hostile-text regression checks."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from workflows.p09.viewer import build_data, build_viewer, write_viewer


def fixture():
    inputs = {
        "opportunities": [
            dict(tenant_id="T1", trip_id="TRIP-1", snapshot_id="S1", snapshot_time="2026-08-01T10:00:00Z",
                 route_id="R1", vehicle_id="V1", deadline_at="2026-08-01T11:00:00Z", promise_version_id="P1",
                 prediction_version="model-1", eta_status="available", probability_status="available",
                 lower_seconds=1200, median_seconds=1800, upper_seconds=2400, calibrated_probability=.6),
            dict(tenant_id="T2", trip_id="TRIP-1", snapshot_id="S1", snapshot_time="2026-08-01T10:00:00Z",
                 route_id="R2", vehicle_id="V2", prediction_version="model-2"),
        ],
        "memberships": [dict(tenant_id="T3", trip_id="EMPTY-TRIP", route_id="R3")],
        "alerts": [dict(tenant_id="T1", trip_id="TRIP-1", alert_id="A1", snapshot_id="S1",
                        created_at="2026-08-01T10:00:00Z", first_delivered_at="2026-08-01T10:02:00Z",
                        first_delivery_id="D1", attempt_count=1, alert_status="delivered", status_reason="confirmed")],
        "attempts": [dict(tenant_id="T1", trip_id="TRIP-1", delivery_id="D1", alert_id="A1", attempt_index=1,
                          sent_at="2026-08-01T10:00:00Z", delivered_at="2026-08-01T10:02:00Z",
                          receipt_known_at="2026-08-01T10:03:00Z", receipt_observed=True,
                          delivery_status="delivered")],
        "promises": [],
        "outcomes": [dict(tenant_id="T1", trip_id="TRIP-1", outcome_version_id="O1",
                          actual_arrival_time="2026-08-01T11:10:00Z", label_available_time="2026-08-02T01:00:00Z",
                          label_status="confirmed")],
    }
    sim = {
        "eta_adoption": [dict(tenant_id="T1", adoption_id="AD1", delivery_id="D1", adoption_status="accepted",
                              adoption_reason=None, decided_at="2026-08-01T10:05:00Z")],
        "eta_action": [dict(tenant_id="T1", action_id="AC1", adoption_id="AD1", alert_id="A1",
                            action_type="contact_driver", action_time="2026-08-01T10:07:00Z")],
        "eta_action_followup": [],
        "opening_events": [dict(tenant_id="T1", trip_id="TRIP-1", delivery_id="D1", opening_id="OP1",
                                opened_at="2026-08-01T10:04:00Z", known_at="2026-08-01T10:04:01Z")],
        "decision_audit": [dict(tenant_id="T1", trip_id="TRIP-1", adoption_id="AD1",
                                decision_known_at="2026-08-01T10:05:01Z")],
        "followup_audit": [dict(tenant_id="T1", trip_id="TRIP-1", action_id="AC1", followup_status="no_known_outcome",
                                canonical_row_emitted=False, outcome_version_id=None,
                                original_promise_version_id="P1", followup_promise_version_id="P1",
                                outcome_observation_end="2026-08-01T12:00:00Z", observed_delay_seconds=None,
                                original_promise_delay_seconds=None)],
        "promise_versions": [], "summary": {},
    }
    return inputs, sim, {}, {"protocol_version": "p09-workflow-v1"}


class ViewerTests(unittest.TestCase):
    def test_full_population_includes_no_alert_and_no_snapshot_membership(self):
        data = build_data(*fixture())
        self.assertEqual((data["trip_count"], data["opportunity_count"]), (3, 2))
        self.assertEqual(data["trips"][1]["alerts"], [])
        self.assertEqual(data["trips"][2]["opportunities"], [])

    def test_same_trip_snapshot_identifiers_do_not_cross_tenants(self):
        data = build_data(*fixture())
        first, second = data["trips"][:2]
        self.assertEqual(len(first["actions"]), 1)
        self.assertEqual(second["adoptions"], [])
        self.assertEqual(second["actions"], [])
        self.assertEqual(second["outcomes"], [])

    def test_unscored_auxiliary_followup_is_not_silently_dropped(self):
        trip = build_data(*fixture())["trips"][0]
        events = [e for e in trip["events"] if e["kind"] == "followup"]
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]["record"]["canonical"])
        self.assertEqual(events[0]["status"], "no_known_outcome")
        self.assertIsNone(events[0]["record"]["audit"]["observed_delay_seconds"])

    def test_send_and_candidate_records_do_not_reveal_future_receipts(self):
        events = build_data(*fixture())["trips"][0]["events"]
        candidate = next(e for e in events if e["kind"] == "alert")
        sent = next(e for e in events if e["kind"] == "delivery" and e["status"] == "sent")
        self.assertNotIn("first_delivered_at", candidate["record"])
        self.assertNotIn("first_delivery_id", candidate["record"])
        self.assertNotIn("attempt_count", candidate["record"])
        self.assertNotIn("delivery_status", sent["record"])
        self.assertNotIn("delivered_at", sent["record"])
        self.assertNotIn("receipt_known_at", sent["record"])
        receipt = next(e for e in events if e["kind"] == "delivery" and e["status"] == "delivered")
        self.assertEqual(receipt["known_at"], "2026-08-01T10:03:00Z")

    def test_closing_script_and_hostile_markup_remain_inert_json(self):
        args = fixture()
        hostile = '</script><img src=x onerror="alert(1)"> & \u2028'
        args[0]["opportunities"][0]["route_id"] = hostile
        html = build_viewer(*args)
        self.assertNotIn(hostile, html)
        self.assertNotIn('<img src=x', html)
        self.assertEqual(html.count('<script'), 2)
        payload = html.split('<script id="p09-data" type="application/json">')[1].split('</script>')[0]
        self.assertEqual(json.loads(payload)["trips"][0]["route_id"], hostile)
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("fetch(", html)

    def test_pure_deterministic_render_does_not_mutate_retained_inputs(self):
        args = fixture()
        before = deepcopy(args)
        first = build_viewer(*args)
        self.assertEqual(first, build_viewer(*args))
        self.assertEqual(args, before)
        self.assertIn("No deployment approval", first)
        self.assertIn("No external services", first)

    def test_unknown_trip_and_naive_clock_are_rejected(self):
        args = fixture()
        args[0]["alerts"][0]["trip_id"] = "UNKNOWN"
        with self.assertRaisesRegex(ValueError, "unknown trip"):
            build_data(*args)
        args = fixture()
        args[0]["opportunities"][0]["snapshot_time"] = "2026-08-01T10:00:00"
        with self.assertRaisesRegex(ValueError, "timezone"):
            build_data(*args)

    def test_duplicate_snapshot_is_rejected_and_empty_population_supported(self):
        args = fixture()
        args[0]["opportunities"].append(deepcopy(args[0]["opportunities"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            build_data(*args)
        self.assertEqual(build_data({}, {}, {}, {})["trip_count"], 0)

    def test_fresh_write_receipt_has_no_machine_path_and_existing_file_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "view.html"
            receipt = write_viewer(path, *fixture())
            before = path.read_bytes()
            self.assertEqual(receipt["bytes"], len(before))
            self.assertNotIn(folder, json.dumps(receipt))
            with self.assertRaisesRegex(ValueError, "exists"):
                write_viewer(path, *fixture())
            self.assertEqual(before, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
