"""Exercise false-positive and false-negative cases for the M6.1 evidence audit."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from workflows.p12 import check_reconciliation as audit


class ReconciliationTests(unittest.TestCase):
    def audit_change(self, catalog, key, **changes):
        read = audit.csv_rows

        def edited_rows(root, name, column):
            records = read(root, name, column)
            if name == f"p12_{catalog}_reconciliation.csv":
                records[key].update(changes)
            return records

        with patch.object(audit, "csv_rows", side_effect=edited_rows):
            return audit.audit(ROOT)

    def test_field_identity_cannot_be_transferred_between_requirements(self):
        with self.assertRaisesRegex(ValueError, "column"):
            self.audit_change("requirement", "DS-M01-F001", column="vehicle_id")

    def test_duplicate_rows_cannot_cover_an_absent_item(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            audit.keyed([{"id": "F001"}, {"id": "F001"}], "id")

    def test_profile_cannot_cite_a_different_worker_receipt(self):
        profile = audit.json_file(ROOT / audit.PROFILE)
        profile["worker_result_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "receipt digest"):
            audit.check_generation(ROOT, profile, audit.json_file(ROOT / audit.REBUILD))

    def test_profile_cannot_swap_a_physical_file_digest(self):
        profile = audit.json_file(ROOT / audit.PROFILE)
        profile["generation_artifacts_sha256"]["warehouse.duckdb"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "physical artifact inventory"):
            audit.check_generation(ROOT, profile, audit.json_file(ROOT / audit.REBUILD))

    def test_m2_field_count_cannot_be_inferred_from_another_column(self):
        with self.assertRaisesRegex(ValueError, "M2 observed value count"):
            self.audit_change("requirement", "DS-M01-F001", m2_nonnull_rows="999")

    def test_typed_table_cannot_stand_in_for_a_measured_field(self):
        with self.assertRaisesRegex(ValueError, "M2 observed value count|measured M2 field output"):
            self.audit_change("requirement", "DS-M01-F001", output_path=audit.REBUILD,
                              output_locator="/runs/0/tables/core_tenant")

    def test_offline_receipt_requires_real_nonnull_counts(self):
        with self.assertRaisesRegex(ValueError, "P09 denominator"):
            self.audit_change("requirement", "DS-M01-F041", p09_nonnull_rows="59")

    def test_existing_unrelated_file_is_not_validation_evidence(self):
        with self.assertRaisesRegex(ValueError, "prior evidence"):
            self.audit_change("requirement", "DS-M01-F001", validation_evidence="LICENSE")

    def test_required_gap_remains_a_successful_but_blocking_audit(self):
        result = self.audit_change("requirement", "DS-M01-F001", disposition="required_gap",
                                   remaining_boundary="Manual review identified an unresolved tenant-key discrepancy.")
        self.assertEqual(result["status"], "passed_p12_reconciliation_audit")
        self.assertFalse(result["eligible_for_p13"])
        self.assertEqual([r["item"] for r in result["required_gaps"]], ["DS-M01-F001"])

    def test_disabled_stage_cannot_be_reported_as_a_positive_execution(self):
        with self.assertRaisesRegex(ValueError, "cannot replace"):
            self.audit_change("validation", "ETA-V13", disposition="demonstrated")

    def test_schema_rows_must_be_bound_to_the_right_store(self):
        with self.assertRaisesRegex(ValueError, "table counts"):
            self.audit_change("table", "eta_alert", m2_rows="63")

    def test_named_validation_must_have_a_successful_execution_log(self):
        with self.assertRaisesRegex(ValueError, "execution record"):
            self.audit_change("validation", "ETA-V12", executed_test_names="test_typo_even_if_it_looks_plausible")

    def test_another_scenario_cannot_supply_its_output(self):
        source, anchor = audit.SCENARIO_RESULTS["ETA-V03"]
        with self.assertRaisesRegex(ValueError, "another scenario"):
            self.audit_change("validation", "ETA-V06", output_path=source, output_locator=anchor)

    def test_missing_anchors_do_not_count_as_outputs(self):
        with self.assertRaises((KeyError, ValueError)):
            self.audit_change("validation", "ETA-V06", output_locator="/unknown_result")


if __name__ == "__main__":
    unittest.main()
