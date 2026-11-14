"""Independent expected-value and leakage-boundary tests for preprocessing."""

import copy
import json
import math
import unittest

from ds_m01.feature_transform import fit_transformer, transform_rows, validate_transformer
from ds_m01.features import FEATURE_DEFINITION_VERSION


CUTOFF = "2026-03-01T08:00:00Z"


def feature_row(identifier, *, trip=None, tenant="T1", at="2026-01-15T08:00:00Z",
                elapsed=10, distance=100, remaining=50, speed=40, age=100,
                route="R1", bucket="1-08@America/Los_Angeles", quality="valid"):
    return dict(tenant_id=tenant, trip_id=trip or identifier, snapshot_id=identifier,
                snapshot_time=at, feature_definition_version=FEATURE_DEFINITION_VERSION,
                numeric=dict(elapsed_trip_seconds=elapsed, route_distance_km=distance,
                             remaining_distance_km=remaining, gnss_speed_kmh=speed,
                             position_age_seconds=age),
                categorical=dict(route_id=route, departure_time_bucket=bucket,
                                 position_validity=quality))


def training_rows():
    return [feature_row("A", elapsed=10, speed=20),
            feature_row("B", elapsed=30, speed=None),
            feature_row("C", elapsed=50, speed=60)]


def fit(rows=None, **changes):
    rows = training_rows() if rows is None else rows
    options = dict(tenant_id="T1", fold_id="F01", training_cutoff_at=CUTOFF,
                   fit_trip_keys=[("T1", row["trip_id"]) for row in rows])
    options.update(changes)
    return fit_transformer(rows, **options)


def validation(identifier="V", **changes):
    return feature_row(identifier, at=CUTOFF, **changes)


class FeatureTransformerTests(unittest.TestCase):
    def test_statistics_match_independent_expected_values(self):
        state = fit()
        speed = state["numeric"]["gnss_speed_kmh"]
        self.assertEqual(speed["median"], 40)
        self.assertEqual(speed["center"], 40)
        self.assertAlmostEqual(speed["scale"], math.sqrt(800/3))
        self.assertEqual((speed["observed_count"], speed["missing_count"]), (2, 1))
        output = transform_rows(training_rows(), state, role="fit")
        index = state["feature_names"].index("numeric:gnss_speed_kmh")
        self.assertAlmostEqual(output[0]["vector"][index], -math.sqrt(1.5))
        self.assertEqual(output[1]["vector"][index:index+2], [0.0, 1.0])
        self.assertAlmostEqual(output[2]["vector"][index], math.sqrt(1.5))

    def test_constant_columns_have_unit_scale_and_zero_centered_values(self):
        state = fit()
        self.assertEqual(state["numeric"]["route_distance_km"]["scale"], 1)
        index = state["feature_names"].index("numeric:route_distance_km")
        self.assertEqual(transform_rows(training_rows(), state, role="fit")[0]["vector"][index], 0)

    def test_all_missing_column_has_declared_zero_fallback_and_indicator(self):
        rows = [feature_row("A", speed=None), feature_row("B", speed=None)]
        state = fit(rows)
        parameters = state["numeric"]["gnss_speed_kmh"]
        self.assertEqual(parameters, dict(median=0.0, center=0.0, scale=1.0,
                                         observed_count=0, missing_count=2))
        index = state["feature_names"].index("numeric:gnss_speed_kmh")
        result = transform_rows([validation(speed=None)], state, role="validation")[0]
        self.assertEqual(result["vector"][index:index+2], [0.0, 1.0])

    def test_validation_extremes_and_categories_do_not_refit_state(self):
        state = fit()
        frozen = copy.deepcopy(state)
        result = transform_rows([validation(speed=1000000, route="UNSEEN")], state,
                                role="validation")[0]
        self.assertEqual(state, frozen)
        self.assertIn("route_id", result["unknown_categorical_features"])
        self.assertNotIn("UNSEEN", state["categorical"]["route_id"]["vocabulary"])

    def test_missing_and_unknown_categories_have_distinct_slots(self):
        state = fit()
        rows = [validation("V1", bucket=None), validation("V2", bucket="new-bucket")]
        result = transform_rows(rows, state, role="validation")
        missing = state["feature_names"].index("category:departure_time_bucket:missing")
        unknown = state["feature_names"].index("category:departure_time_bucket:unknown")
        self.assertEqual([result[0]["vector"][missing], result[0]["vector"][unknown]], [1, 0])
        self.assertEqual([result[1]["vector"][missing], result[1]["vector"][unknown]], [0, 1])

    def test_category_literals_cannot_collide_with_reserved_slots(self):
        rows = [feature_row("A", bucket="missing"), feature_row("B", bucket="unknown")]
        state = fit(rows)
        result = transform_rows(rows, state, role="fit")
        self.assertEqual(len(state["feature_names"]), len(set(state["feature_names"])))
        missing = state["feature_names"].index("category:departure_time_bucket:missing")
        unknown = state["feature_names"].index("category:departure_time_bucket:unknown")
        for row in result:
            self.assertEqual((row["vector"][missing], row["vector"][unknown]), (0, 0))

    def test_serialized_state_roundtrip_preserves_exact_transform(self):
        state = fit()
        restored = json.loads(json.dumps(state, allow_nan=False))
        self.assertEqual(transform_rows([validation()], state, role="validation"),
                         transform_rows([validation()], restored, role="validation"))

    def test_row_order_does_not_change_state_or_output(self):
        rows = training_rows()
        self.assertEqual(fit(rows), fit(list(reversed(rows))))
        state = fit(rows)
        self.assertEqual(transform_rows(rows, state, role="fit"),
                         transform_rows(list(reversed(rows)), state, role="fit"))

    def test_input_mappings_are_not_mutated(self):
        rows = training_rows()
        original = copy.deepcopy(rows)
        state = fit(rows)
        transform_rows(rows, state, role="fit")
        self.assertEqual(rows, original)

    def test_fit_cutoff_is_exclusive_and_validation_cutoff_is_inclusive(self):
        at_cutoff = [feature_row("A", at=CUTOFF)]
        with self.assertRaisesRegex(ValueError, "strictly before"):
            fit(at_cutoff)
        state = fit()
        self.assertEqual(len(transform_rows([validation()], state, role="validation")), 1)
        before = feature_row("V", at="2026-03-01T07:59:59Z")
        with self.assertRaisesRegex(ValueError, "precedes cutoff"):
            transform_rows([before], state, role="validation")

    def test_clock_comparison_uses_offsets(self):
        row = feature_row("A", at="2026-02-28T23:59:59-08:00")
        state = fit([row])
        self.assertEqual(state["training_cutoff_at"], CUTOFF)
        with self.assertRaises(ValueError):
            fit([feature_row("A", at="2026-03-01T00:00:00-08:00")])

    def test_validation_trip_overlap_rejected_even_for_unobserved_fit_trip(self):
        state = fit(fit_trip_keys=[("T1", "A"), ("T1", "B"), ("T1", "C"), ("T1", "RESERVED")])
        for trip in ("A", "RESERVED"):
            with self.subTest(trip=trip), self.assertRaisesRegex(ValueError, "overlaps fit"):
                transform_rows([validation(trip=trip)], state, role="validation")

    def test_foreign_tenant_cannot_fit_or_transform(self):
        rows = [feature_row("A", tenant="T2")]
        with self.assertRaises(ValueError):
            fit(rows, fit_trip_keys=[("T1", "A")])
        with self.assertRaises(ValueError):
            fit(training_rows(), fit_trip_keys=[("T2", "A")])
        with self.assertRaisesRegex(ValueError, "tenant boundaries"):
            transform_rows([validation(tenant="T2")], fit(), role="validation")

    def test_fit_rows_must_belong_to_explicit_fit_groups(self):
        with self.assertRaisesRegex(ValueError, "outside the declared"):
            fit(training_rows(), fit_trip_keys=[("T1", "A")])
        with self.assertRaises(ValueError):
            transform_rows([feature_row("UNDECLARED")], fit(), role="fit")

    def test_duplicate_snapshot_or_group_declarations_fail(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            fit([feature_row("A"), feature_row("A")], fit_trip_keys=[("T1", "A")])
        with self.assertRaises(ValueError):
            fit(fit_trip_keys=[("T1", "A"), ("T1", "A")])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            transform_rows([validation(), validation()], fit(), role="validation")

    def test_multiple_snapshots_same_fit_trip_are_allowed(self):
        rows = [feature_row("A1", trip="A"), feature_row("A2", trip="A", elapsed=20)]
        state = fit(rows, fit_trip_keys=[("T1", "A")])
        self.assertEqual(state["fitted_rows"], 2)
        self.assertEqual(state["fitted_trip_keys"], [["T1", "A"]])

    def test_missing_positions_are_retained_as_opportunities(self):
        state = fit()
        row = validation(quality="no_position", remaining=None, speed=None, age=None)
        output = transform_rows([row], state, role="validation")
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]["missing_numeric_features"],
                         ["remaining_distance_km", "gnss_speed_kmh", "position_age_seconds"])
        self.assertEqual(len(output[0]["vector"]), len(state["feature_names"]))

    def test_current_motion_values_cannot_bypass_stale_mask(self):
        with self.assertRaisesRegex(ValueError, "Stale or absent"):
            transform_rows([validation(quality="low_confidence")], fit(), role="validation")

    def test_labels_and_simulator_truth_are_ignored(self):
        clean = training_rows()
        poisoned = copy.deepcopy(clean)
        for row in poisoned:
            row.update(remaining_duration_seconds=-999, actual_arrival_time="future",
                       synthetic_scenario_id="SPOILER", delay_outcome=True)
        self.assertEqual(fit(clean), fit(poisoned))
        self.assertEqual(transform_rows(clean, fit(clean), role="fit"),
                         transform_rows(poisoned, fit(clean), role="fit"))

    def test_unlisted_columns_and_invalid_numeric_values_fail(self):
        bad = training_rows()
        bad[0]["numeric"]["target"] = 1
        with self.assertRaisesRegex(ValueError, "allowlist"):
            fit(bad)
        for value in (True, float("nan"), float("inf"), -1, "12"):
            rows = training_rows()
            rows[0]["numeric"]["gnss_speed_kmh"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                fit(rows)

    def test_essential_values_versions_and_timestamps_are_required(self):
        mutations = [("feature_definition_version", "future-version"),
                     ("snapshot_time", "2026-01-01T12:00:00"), ("tenant_id", "")]
        for field, value in mutations:
            rows = training_rows()
            rows[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                fit(rows)
        rows = training_rows()
        rows[0]["numeric"]["elapsed_trip_seconds"] = None
        with self.assertRaises(ValueError):
            fit(rows)

    def test_empty_fit_fails_but_empty_validation_is_valid(self):
        with self.assertRaises(ValueError):
            fit([], fit_trip_keys=[("T1", "A")])
        self.assertEqual(transform_rows([], fit(), role="validation"), [])

    def test_corrupt_persisted_state_fails_before_transform(self):
        changes = [lambda x: x.update(transformer_definition_version="unknown"),
                   lambda x: x["numeric"]["elapsed_trip_seconds"].update(scale=0),
                   lambda x: x["numeric"]["elapsed_trip_seconds"].update(center=float("inf")),
                   lambda x: x["numeric"]["elapsed_trip_seconds"].update(observed_count=99),
                   lambda x: x["categorical"]["route_id"].update(vocabulary=["R1", "R1"]),
                   lambda x: x["feature_names"].append("label"),
                   lambda x: x.update(fitted_rows=True),
                   lambda x: x.update(fit_trip_keys=[["T2", "A"]])]
        for index, change in enumerate(changes):
            state = fit()
            change(state)
            with self.subTest(index=index), self.assertRaises(ValueError):
                transform_rows([validation()], state, role="validation")

    def test_invalid_role_does_not_silently_select_training(self):
        with self.assertRaisesRegex(ValueError, "role"):
            transform_rows([validation()], fit(), role="test")

    def test_state_is_json_finite_and_validation_schema_is_public(self):
        state = fit()
        self.assertIs(validate_transformer(state), state)
        self.assertIsInstance(json.dumps(state, allow_nan=False), str)


if __name__ == "__main__":
    unittest.main()
