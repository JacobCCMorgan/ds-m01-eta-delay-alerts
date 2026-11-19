"""Independent G11 identity, clock, lineage and actual storage regressions."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
from workflows.p11 import revision as rev


def one_snapshot(tables, at="2026-06-23T10:00:00Z"):
    tables = copy.deepcopy(tables)
    snap, binding = rev.snapshot_for_trip("T01", "TRIP-1", at, tables)
    tables["provenance_record"].append(rev._provenance(snap, table="eta_snapshot",
        key=[snap["tenant_id"], snap["snapshot_id"]], version=rev.FEATURE_VERSION,
        parents=rev._snapshot_parents(snap, binding, tables), available=snap["snapshot_time"]))
    return tables["telemetry_event"], [snap], [] if binding is None else [binding], tables["provenance_record"]


class RevisionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tables = rev.fixture(ROOT)
        self.rows = self.tables["telemetry_event"]

    def test_exact_replay_preserves_every_revision_and_tenant(self):
        before = copy.deepcopy(self.rows)
        rows, duplicates = rev.canonicalize(self.rows + self.rows)
        self.assertEqual(duplicates, 4)
        self.assertEqual(len(rows), 4)
        self.assertEqual([r["record_revision"] for r in rows if r["tenant_id"] == "T01"], ["1", "2", "10"])
        self.assertEqual(before, self.rows)

    def test_conflicting_full_identity_does_not_mutate_input(self):
        before = copy.deepcopy(self.rows)
        changed = dict(self.rows[0], latitude_deg=9.0)
        with self.assertRaisesRegex(ValueError, "Conflicting payload"):
            rev.canonicalize(self.rows + [changed])
        self.assertEqual(self.rows, before)

    def test_noncanonical_revision_values_are_rejected(self):
        for value in (True, 1, "0", "01", "-1", "+2", "2.0", "two", "", "1" * 21):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "identity|positive integer"):
                rev.canonicalize([dict(self.rows[0], record_revision=value)])

    def test_same_canonical_event_cannot_change_source_identity(self):
        for field in ("source_system_id", "source_event_id"):
            changed = dict(self.rows[1], **{field: "different-source"})
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "changed source identity"):
                rev.canonicalize([self.rows[0], changed])

    def test_one_source_identity_cannot_alias_multiple_canonical_events(self):
        row = dict(self.rows[1], canonical_event_id="OTHER")
        with self.assertRaisesRegex(ValueError, "multiple canonical"):
            rev.canonicalize([self.rows[0], row])

    def test_revisions_cannot_share_provenance_identity(self):
        row = dict(self.rows[1], provenance_id=self.rows[0]["provenance_id"])
        with self.assertRaisesRegex(ValueError, "reuse one provenance"):
            rev.canonicalize([self.rows[0], row])

    def test_unknown_naive_overprecision_or_impossible_clock_rejected(self):
        for value in (None, "unknown", "2026-06-23T09:56:00", "2026-06-23T09:56:00.1234567Z", "2026-02-30T09:56:00Z"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                rev.canonicalize([dict(self.rows[0], available_time=value)])
        with self.assertRaisesRegex(ValueError, "precedes event"):
            rev.canonicalize([dict(self.rows[0], available_time="2026-06-23T09:54:59Z")])

    def test_nonfinite_and_boolean_numeric_payload_rejected(self):
        for field, value in (("latitude_deg", float("nan")), ("engine_rpm", float("inf")), ("gnss_speed_kmh", True)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                rev.canonicalize([dict(self.rows[0], **{field: value})])


class RevisionSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tables = rev.fixture(ROOT)

    def test_before_boundary_and_at_boundary_use_exact_known_revision(self):
        expectations = [("10:03:59.999999", "1"), ("10:04:00", "2"), ("10:04:59.999999", "2"), ("10:05:00", "10")]
        for time, expected in expectations:
            with self.subTest(time=time):
                snap, binding = rev.snapshot_for_trip("T01", "TRIP-1", f"2026-06-23T{time}Z", self.tables)
                self.assertEqual(binding["record_revision"], expected)
                self.assertEqual(binding["source_provenance_id"], "P11-GPS-T01-R" + expected)
                self.assertEqual(snap["latitude_deg"], {"1": 34.1, "2": 34.2, "10": 34.5}[expected])

    def test_numeric_revision_tie_does_not_sort_ten_below_two(self):
        self.tables["telemetry_event"][1]["available_time"] = "2026-06-23T10:05:00Z"
        _, binding = rev.snapshot_for_trip("T01", "TRIP-1", "2026-06-23T10:05:00Z", self.tables)
        self.assertEqual(binding["record_revision"], "10")

    def test_later_arriving_older_revision_cannot_downgrade_known_version(self):
        self.tables["telemetry_event"][1]["available_time"] = "2026-06-23T10:07:00Z"
        _, binding = rev.snapshot_for_trip("T01", "TRIP-1", "2026-06-23T10:08:00Z", self.tables)
        self.assertEqual(binding["record_revision"], "10")

    def test_late_revision_does_not_change_earlier_snapshot_or_binding(self):
        earlier = copy.deepcopy(self.tables)
        earlier["telemetry_event"] = [r for r in earlier["telemetry_event"] if r["record_revision"] == "1"]
        at = "2026-06-23T10:00:00Z"
        self.assertEqual(rev.snapshot_for_trip("T01", "TRIP-1", at, earlier), rev.snapshot_for_trip("T01", "TRIP-1", at, self.tables))

    def test_same_ids_in_other_tenant_never_substitute_position(self):
        a = rev.snapshot_for_trip("T01", "TRIP-1", "2026-06-23T10:06:00Z", self.tables)
        b = rev.snapshot_for_trip("T02", "TRIP-1", "2026-06-23T10:06:00Z", self.tables)
        self.assertEqual(a[1]["record_revision"], "10")
        self.assertEqual(b[1]["record_revision"], "1")
        self.assertEqual(b[0]["latitude_deg"], 35.2)
        self.assertNotEqual(a[0]["snapshot_id"], b[0]["snapshot_id"])

    def test_correction_invalidates_fix_without_resurrecting_older_revision(self):
        for updates in ({"gnss_valid": "no_fix"}, {"latitude_deg": 99.0}, {"vehicle_id": "V-OTHER"}, {"device_id": "D-OTHER"}):
            tables = copy.deepcopy(self.tables)
            tables["telemetry_event"][2].update(updates)
            with self.subTest(updates=updates):
                snap, binding = rev.snapshot_for_trip("T01", "TRIP-1", "2026-06-23T10:05:00Z", tables)
                self.assertEqual(snap["position_validity"], "no_position")
                self.assertIsNone(binding)
                self.assertIsNone(snap["latitude_deg"])

    def test_timezone_and_microsecond_boundary_preserved(self):
        row = self.tables["telemetry_event"][2]
        row["available_time"] = "2026-06-23T03:05:00.000001-07:00"
        before = rev.snapshot_for_trip("T01", "TRIP-1", "2026-06-23T10:05:00Z", self.tables)
        at = rev.snapshot_for_trip("T01", "TRIP-1", "2026-06-23T10:05:00.000001Z", self.tables)
        self.assertEqual(before[1]["record_revision"], "2")
        self.assertEqual(at[1]["record_revision"], "10")
        self.assertEqual(at[0]["position_available_at"], row["available_time"])


class RevisionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tables = rev.fixture(ROOT)
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / "store"

    def tearDown(self):
        self.temp.cleanup()

    def store(self, data=None):
        return rev.write_store(ROOT, self.output, *(data or one_snapshot(self.tables)))

    def test_actual_http_boundary_lineage_store_and_replay_end_to_end(self):
        report = rev.verify_revision_contract(ROOT, Path(self.temp.name) / "run")
        self.assertEqual(report["status"], "passed_revision_contract")
        self.assertEqual(report["retained_revisions"], 4)
        self.assertEqual(len(report["snapshot_witnesses"]), 10)
        self.assertTrue(report["http_server_closed"])
        self.assertEqual(report["http"]["retries"], 1)
        self.assertEqual(report["http"]["pages"], 3)
        self.assertEqual(report["http"]["http_requests"], 5)
        self.assertEqual(len(report["store"]["actual_constraint_rejections"]), 4)
        self.assertEqual(report["store"]["retained_revision_replay"]["new_rows"], 0)
        self.assertTrue(report["store"]["fresh_connection_exact"])
        self.assertTrue(report["store"]["all_column_parquet_exact"])

    def test_typed_source_decimal_rounding_is_rejected(self):
        self.tables["telemetry_event"][1]["gnss_speed_kmh"] = 62.0004
        with self.assertRaisesRegex(ValueError, "Typed source readback changed"):
            self.store()

    def test_source_provenance_must_bind_exact_revision(self):
        data = one_snapshot(self.tables)
        next(r for r in data[3] if r["provenance_id"] == "P11-GPS-T01-R1")["record_version"] = "2"
        with self.assertRaisesRegex(ValueError, "Telemetry provenance"):
            self.store(data)

    def test_selected_revision_fk_cannot_point_to_future_or_wrong_source(self):
        for updates in ({"record_revision": "10", "source_provenance_id": "P11-GPS-T01-R10"}, {"source_provenance_id": "P11-GPS-T02-R1"}):
            data = one_snapshot(self.tables)
            data[2][0].update(updates)
            with self.subTest(updates=updates), self.assertRaisesRegex(ValueError, "wrong or future revision"):
                self.store(data)

    def test_omitted_position_binding_is_rejected(self):
        events, snapshots, _, provenance = one_snapshot(self.tables)
        with self.assertRaisesRegex(ValueError, "bindings omitted"):
            self.store((events, snapshots, [], provenance))

    def test_snapshot_payload_must_match_the_version_in_its_lineage(self):
        data = one_snapshot(self.tables)
        data[1][0]["latitude_deg"] = 8.0
        with self.assertRaisesRegex(ValueError, "position payload differs"):
            self.store(data)

    def test_snapshot_provenance_identity_is_checked_for_every_snapshot(self):
        for field, value in (("tenant_id", "T02"), ("canonical_table", "wrong"), ("canonical_record_key", ["wrong"]), ("record_version", "wrong"), ("available_time", "2026-06-23T10:01:00Z")):
            data = one_snapshot(self.tables)
            data[3][-1][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Snapshot provenance"):
                self.store(data)

    def test_no_position_snapshot_cannot_keep_old_revision_lineage(self):
        for row in self.tables["telemetry_event"]:
            row["gnss_valid"] = "no_fix"
        data = one_snapshot(self.tables)
        self.assertEqual(data[2], [])
        data[3][-1]["parent_provenance_ids"].append("P11-GPS-T01-R1")
        with self.assertRaisesRegex(ValueError, "No-position snapshot"):
            self.store(data)

    def test_cross_tenant_or_missing_parent_provenance_is_rejected(self):
        for parent in ("HAND-2", "MISSING"):
            data = one_snapshot(self.tables)
            data[3][-1]["parent_provenance_ids"].append(parent)
            with self.subTest(parent=parent), self.assertRaisesRegex(ValueError, "cross-tenant or future"):
                self.store(data)

    def test_changed_retained_replay_fails_before_database_mutation(self):
        data = one_snapshot(self.tables)
        self.store(data)
        fields = [f for f in json.loads((ROOT / "contracts/fields.json").read_text()) if f["table"] == "telemetry_event"]
        with rev.duckdb.connect(str(self.output / "revision.duckdb")) as con:
            before = rev._table_rows(con, "telemetry_event")
            changed = [dict(data[0][0], latitude_deg=20.0)]
            with self.assertRaisesRegex(ValueError, "Conflicting or unregistered"):
                rev.replay_retained_events(con, changed, fields)
            self.assertEqual(before, rev._table_rows(con, "telemetry_event"))
            extra = [dict(data[0][0], unexpected_field="changed")]
            with self.assertRaisesRegex(ValueError, "replay columns differ"):
                rev.replay_retained_events(con, extra, fields)
            self.assertEqual(before, rev._table_rows(con, "telemetry_event"))

    def test_valid_no_position_is_persisted_with_no_false_binding(self):
        for row in self.tables["telemetry_event"]:
            row["gnss_valid"] = "no_fix"
        report = self.store()
        self.assertEqual(report["tables"]["p11_snapshot_position"]["rows"], 0)
        self.assertEqual(report["tables"]["eta_snapshot"]["rows"], 1)
        self.assertTrue(report["all_column_parquet_exact"])

    def test_output_path_cannot_overwrite_prior_evidence(self):
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("retained")
        with self.assertRaisesRegex(ValueError, "destination must be new"):
            self.store()
        self.assertEqual(marker.read_text(), "retained")


if __name__ == "__main__":
    unittest.main()
