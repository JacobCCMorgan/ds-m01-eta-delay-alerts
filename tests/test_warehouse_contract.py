"""Integration checks for the M2 physical schema and deferred-output boundary."""
from collections import defaultdict
import json
from pathlib import Path
import tempfile
import unittest

import duckdb

from ds_m01.pipeline import RAW_TABLES, DERIVED_TABLES
from ds_m01.warehouse import materialize_tables, provenance_violations


PROJECT = Path(__file__).resolve().parents[1]


class WarehouseContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ds-m01-schema-case-")
        self.root = Path(self.temp.name)
        self.con = duckdb.connect()
        self.contract = defaultdict(list)
        for field in json.loads((PROJECT / "contracts/fields.json").read_text()):
            self.contract[field["table"]].append(field)
        self.required = json.loads((PROJECT / "config/m1.json").read_text())["required_tables"]
        self.active = list(RAW_TABLES) + list(DERIVED_TABLES)
        for table in self.active:
            folder = "processed" if table in DERIVED_TABLES or table == "provenance_record" else "raw"
            path = self.root / "data" / folder / f"{table}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def test_full_schema_has_typed_empty_future_outputs_and_keeps_source_constraints(self):
        # One independently specified source record verifies that active tables still load.
        source = {"tenant_id": "T01", "tenant_name": "Fixture Tenant",
                  "report_timezone": "America/Los_Angeles", "base_currency": "USD"}
        (self.root / "data/raw/core_tenant.jsonl").write_text(json.dumps(source) + "\n")
        _, counts, status, rows = materialize_tables(
            self.con, self.root, self.contract, self.active, self.required, True)
        tables = {r[0] for r in self.con.execute("SHOW TABLES").fetchall()}
        self.assertEqual(tables, set(self.required))
        self.assertEqual(len(tables), 28)
        self.assertEqual(rows, 1)
        self.assertEqual(counts["core_tenant"], 1)
        self.assertEqual(len(list((self.root / "data/parquet").glob("*.parquet"))), 28)
        reserved = set(self.required) - set(self.active)
        self.assertEqual(len(reserved), 11)
        for table in reserved:
            self.assertEqual(counts[table], 0)
            self.assertEqual(status[table]["population_status"], "reserved_empty_pending_later_stage")
        fields = self.con.execute("DESCRIBE eta_prediction").fetchall()
        names = [r[0] for r in fields]
        self.assertEqual(names, ["tenant_id", "snapshot_id", "prediction_version", "eta_median",
                                 "eta_lower", "eta_upper", "delay_probability", "fallback_reason",
                                 "remaining_eta_seconds", "generated_at", "value_states"])
        self.assertEqual(fields[names.index("eta_median")][1], "TIMESTAMP WITH TIME ZONE")
        self.assertEqual(fields[names.index("delay_probability")][1], "DECIMAL(7,6)")
        # Empty exports retain a usable schema rather than being placeholder text files.
        exported = self.con.read_parquet(str(self.root / "data/parquet/eta_prediction.parquet"))
        self.assertEqual(exported.columns, names)
        self.assertEqual(exported.count("*").fetchone()[0], 0)
        with self.assertRaises(duckdb.ConstraintException):
            self.con.execute("INSERT INTO core_tenant VALUES ('T01','Duplicate','UTC','USD')")

    def test_reserved_prediction_input_cannot_be_silently_accepted_or_discarded(self):
        (self.root / "data/processed/eta_prediction.jsonl").write_text('{"snapshot_id":"UNEXPECTED"}\n')
        with self.assertRaisesRegex(ValueError, "Reserved M2 table has unexpected populated input"):
            materialize_tables(self.con, self.root, self.contract, self.active, self.required, True)
        self.assertEqual(self.con.execute("SHOW TABLES").fetchall(), [])

    def test_m1_mode_retains_only_data_stage_tables(self):
        _, counts, status, _ = materialize_tables(
            self.con, self.root, self.contract, self.active, self.required, False)
        self.assertEqual(set(counts), set(self.active))
        self.assertEqual(len(counts), 17)
        self.assertNotIn("eta_prediction", status)

    def test_sql_lineage_finds_missing_parent_and_future_parent_clock(self):
        self.con.execute("CREATE TABLE provenance_record(provenance_id VARCHAR, available_time TIMESTAMPTZ, parent_provenance_ids JSON)")
        self.con.execute("""
            INSERT INTO provenance_record VALUES
              ('available', '2026-01-01T00:00:00Z', '[]'),
              ('future', '2026-01-03T00:00:00Z', '[]'),
              ('child', '2026-01-02T00:00:00Z', '["available","future","missing"]'),
              ('null-clock', NULL, '["available"]')
        """)
        self.assertEqual(provenance_violations(self.con), (1, 1))


if __name__ == "__main__":
    unittest.main()
