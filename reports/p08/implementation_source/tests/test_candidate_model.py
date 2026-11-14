"""Independent model parity, weighting, binding and invalid-output checks."""

import copy
import json
import math
import unittest

from ds_m01.candidate_model import (
    CANDIDATE_IDS, CANDIDATE_PARAMETERS, QUANTILES, canonical_digest,
    enforce_quantile_policy, fit_candidate, predict_candidate, transformer_digest,
    validate_model,
)
from ds_m01.features import FEATURE_DEFINITION_VERSION
from ds_m01.feature_transform import fit_transformer, transform_rows


CUTOFF = "2026-03-01T08:00:00Z"


def feature(identifier, trip, *, validation=False, remaining=50, route="R1", quality="valid"):
    return dict(
        tenant_id="T1", trip_id=trip, snapshot_id=identifier,
        snapshot_time=CUTOFF if validation else "2026-02-01T08:00:00Z",
        feature_definition_version=FEATURE_DEFINITION_VERSION,
        numeric=dict(elapsed_trip_seconds=100, route_distance_km=100,
                     remaining_distance_km=remaining if quality == "valid" else None,
                     gnss_speed_kmh=40 if quality == "valid" else None,
                     position_age_seconds=30 if quality == "valid" else None),
        categorical=dict(route_id=route, departure_time_bucket="1-08", position_validity=quality),
    )


def fixture():
    rows = [feature(f"S{trip:02d}-{snapshot}", f"T{trip:02d}", remaining=20 + trip * 5 + snapshot)
            for trip in range(8) for snapshot in range(8)]
    transformer = fit_transformer(rows, tenant_id="T1", fold_id="F01", training_cutoff_at=CUTOFF,
                                  fit_trip_keys=[("T1", f"T{trip:02d}") for trip in range(8)])
    fit = transform_rows(rows, transformer, role="fit")
    for index, row in enumerate(fit):
        row.update(remaining_duration_seconds=1200.0 + (index // 8) * 120 + (index % 3) * 150,
                   sample_weight=1 / 8)
    validation = transform_rows(
        [feature(f"V{index}", f"VT{index}", validation=True, remaining=25 + index * 6)
         for index in range(7)], transformer, role="validation")
    return transformer, fit, validation


class CandidateModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.transformer, cls.fit, cls.validation = fixture()
        cls.models = {candidate: fit_candidate(cls.fit, cls.transformer, candidate_id=candidate)
                      for candidate in CANDIDATE_IDS}

    def test_exact_transformer_hash_binding_and_json_roundtrip(self):
        for model in self.models.values():
            restored = json.loads(json.dumps(model, allow_nan=False))
            self.assertEqual(model["transformer_sha256"], transformer_digest(self.transformer))
            self.assertEqual(predict_candidate(model, self.validation, self.transformer),
                             predict_candidate(restored, self.validation, self.transformer))

    def test_json_prediction_matches_independently_fitted_sklearn(self):
        from sklearn.ensemble import GradientBoostingRegressor
        from sklearn.linear_model import QuantileRegressor
        x = [row["vector"] for row in self.fit]
        y = [row["remaining_duration_seconds"] for row in self.fit]
        weights = [row["sample_weight"] for row in self.fit]
        validation = [row["vector"] for row in self.validation]
        for candidate, model in self.models.items():
            answers = predict_candidate(model, self.validation, self.transformer)
            for index, quantile in enumerate(QUANTILES):
                estimator = (QuantileRegressor(quantile=quantile, **CANDIDATE_PARAMETERS[candidate])
                             if candidate == "linear_quantile" else
                             GradientBoostingRegressor(alpha=quantile, **CANDIDATE_PARAMETERS[candidate]))
                estimator.fit(x, y, sample_weight=weights)
                for expected, answer in zip(estimator.predict(validation), answers):
                    self.assertAlmostEqual(answer["raw_quantiles"][index], float(expected), delta=1e-8)

    def test_each_quantile_is_independently_fitted(self):
        for model in self.models.values():
            self.assertEqual(model["quantiles"], [0.1, 0.5, 0.9])
            self.assertEqual(len(model["estimators"]), 3)
            self.assertGreater(len({canonical_digest(item) for item in model["estimators"]}), 1)

    def test_reordering_fit_rows_does_not_change_model(self):
        for candidate, model in self.models.items():
            shuffled = fit_candidate(list(reversed(self.fit)), self.transformer, candidate_id=candidate)
            self.assertEqual(model, shuffled)

    def test_trip_weights_are_a_checked_contract(self):
        changed = copy.deepcopy(self.fit)
        changed[0]["sample_weight"] = 1
        with self.assertRaisesRegex(ValueError, "weight one per trip"):
            fit_candidate(changed, self.transformer, candidate_id="linear_quantile")

    def test_each_trips_duplicated_observations_preserve_linear_objective(self):
        # All rows are duplicated with half weight and distinct snapshot keys.
        # This checks the declared weighting against a different row frequency.
        original = self.models["linear_quantile"]
        transformed = copy.deepcopy(self.transformer)
        doubled = []
        for row in self.fit:
            for suffix in ("a", "b"):
                item = dict(row, snapshot_id=row["snapshot_id"] + suffix,
                            sample_weight=row["sample_weight"] / 2)
                doubled.append(item)
        transformed["fitted_snapshot_keys"] = [["T1", row["snapshot_id"]] for row in doubled]
        transformed["fitted_rows"] *= 2
        for parameters in transformed["numeric"].values():
            parameters["observed_count"] *= 2
            parameters["missing_count"] *= 2
        repeated = fit_candidate(doubled, transformed, candidate_id="linear_quantile")
        expected = predict_candidate(original, self.validation, self.transformer)
        actual = predict_candidate(repeated, self.validation, transformed)
        for left, right in zip(expected, actual):
            for value, other in zip(left["raw_quantiles"], right["raw_quantiles"]):
                self.assertAlmostEqual(value, other, delta=1e-7)

    def test_low_support_and_empty_fit_emit_explicit_unsupported_state(self):
        for rows in ([], self.fit[:8]):
            model = fit_candidate(rows, self.transformer, candidate_id="linear_quantile")
            self.assertEqual(model["status"], "unsupported")
            answers = predict_candidate(model, self.validation, self.transformer)
            self.assertEqual(len(answers), len(self.validation))
            self.assertTrue(all(row["reason"] == "insufficient_fit_trips" for row in answers))
            self.assertTrue(all(row["p50_remaining_seconds"] is None for row in answers))

    def test_validation_data_and_model_inputs_are_not_mutated(self):
        before = copy.deepcopy((self.fit, self.validation, self.transformer, self.models))
        for model in self.models.values():
            predict_candidate(model, self.validation, self.transformer)
        self.assertEqual(before, (self.fit, self.validation, self.transformer, self.models))

    def test_unknown_categories_and_missing_position_still_receive_predictions(self):
        rows = transform_rows([feature("VUNK", "VUNK", validation=True, route="UNSEEN", quality="no_position")],
                              self.transformer, role="validation")
        self.assertIn("route_id", rows[0]["unknown_categorical_features"])
        for model in self.models.values():
            answer = predict_candidate(model, rows, self.transformer)
            self.assertEqual(len(answer), 1)
            self.assertEqual(answer[0]["snapshot_id"], "VUNK")
            self.assertEqual(answer[0]["status"], "available")

    def test_transformer_statistics_change_is_rejected_even_with_same_layout(self):
        changed = copy.deepcopy(self.transformer)
        changed["numeric"]["gnss_speed_kmh"]["center"] += 1
        with self.assertRaisesRegex(ValueError, "content hash"):
            predict_candidate(self.models["linear_quantile"], self.validation, changed)

    def test_model_metadata_mismatch_is_rejected(self):
        for field, value in (("tenant_id", "T2"), ("fold_id", "F02"),
                             ("training_cutoff_at", "2026-04-01T07:00:00Z"),
                             ("feature_names", list(reversed(self.transformer["feature_names"])))):
            model = copy.deepcopy(self.models["linear_quantile"])
            model[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                predict_candidate(model, self.validation, self.transformer)

    def test_fit_groups_clocks_and_snapshot_membership_are_checked(self):
        for field, value in (("tenant_id", "OTHER"), ("trip_id", "VALIDATION"),
                             ("snapshot_id", "MISSING"), ("snapshot_time", CUTOFF),
                             ("fold_id", "F02"), ("fold_role", "validation")):
            rows = copy.deepcopy(self.fit)
            rows[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                fit_candidate(rows, self.transformer, candidate_id="linear_quantile")

    def test_inference_rejects_wrong_tenant_fold_fit_trip_and_before_cutoff(self):
        for field, value in (("tenant_id", "OTHER"), ("trip_id", "T00"),
                             ("snapshot_time", "2026-03-01T07:59:59Z"),
                             ("fold_id", "F02"), ("fold_role", "fit")):
            rows = copy.deepcopy(self.validation)
            rows[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                predict_candidate(self.models["linear_quantile"], rows, self.transformer)

    def test_duplicate_fit_and_inference_snapshots_fail(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            fit_candidate(self.fit + self.fit[:1], self.transformer, candidate_id="linear_quantile")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            predict_candidate(self.models["linear_quantile"], self.validation * 2, self.transformer)

    def test_missing_nonpositive_nonfinite_targets_and_weights_fail(self):
        for field in ("remaining_duration_seconds", "sample_weight"):
            for value in (None, True, 0, -1, float("inf"), float("nan"), "100"):
                rows = copy.deepcopy(self.fit)
                rows[0][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    fit_candidate(rows, self.transformer, candidate_id="linear_quantile")

    def test_vector_width_and_nonfinite_elements_fail(self):
        for vector in (None, [], self.validation[0]["vector"] + [0],
                       [float("nan")] * len(self.transformer["feature_names"]),
                       [True] * len(self.transformer["feature_names"])):
            rows = copy.deepcopy(self.validation)
            rows[0]["vector"] = vector
            with self.subTest(vector=vector), self.assertRaises(ValueError):
                predict_candidate(self.models["linear_quantile"], rows, self.transformer)

    def test_crossing_quantiles_are_sorted_and_originals_are_retained(self):
        answer = enforce_quantile_policy([30, 10, 20])
        self.assertEqual(answer["raw_quantiles"], [30, 10, 20])
        self.assertTrue(answer["quantile_crossing"])
        self.assertEqual(answer["quantile_postprocessing"], "monotone_sort")
        self.assertEqual([answer[field] for field in ("p10_remaining_seconds", "p50_remaining_seconds", "p90_remaining_seconds")], [10, 20, 30])

    def test_nonpositive_quantiles_do_not_get_silently_clipped(self):
        for raw in ([0, 10, 20], [10, -1, 20], [20, 30, -1]):
            answer = enforce_quantile_policy(raw)
            self.assertEqual(answer["reason"], "nonpositive_quantile")
            self.assertEqual(answer["raw_quantiles"], raw)
            self.assertIsNone(answer["p50_remaining_seconds"])

    def test_nonfinite_quantiles_are_json_safe_and_explicit(self):
        answer = enforce_quantile_policy([10, float("nan"), float("inf")])
        self.assertEqual(answer["reason"], "nonfinite_quantile")
        self.assertEqual(answer["raw_quantiles"], [10, None, None])
        self.assertEqual(answer["invalid_quantile_indices"], [1, 2])
        json.dumps(answer, allow_nan=False)

    def test_equal_quantiles_are_allowed_without_a_coverage_claim(self):
        answer = enforce_quantile_policy([10, 10, 10])
        self.assertEqual(answer["status"], "available")
        self.assertFalse(answer["quantile_crossing"])

    def test_linear_numeric_overflow_produces_explicit_unsupported_answer(self):
        model = copy.deepcopy(self.models["linear_quantile"])
        for estimator in model["estimators"]:
            estimator["coefficients"] = [1e308] + [0.0] * (len(model["feature_names"]) - 1)
        row = copy.deepcopy(self.validation[0])
        row["vector"][0] = 2.0
        answer = predict_candidate(model, [row], self.transformer)[0]
        self.assertEqual(answer["reason"], "nonfinite_quantile")
        json.dumps(answer, allow_nan=False)

    def test_json_tree_traversal_uses_float32_boundary_semantics(self):
        model = copy.deepcopy(self.models["gradient_boosting_quantile"])
        leaf = {"children_left": [-1], "children_right": [-1], "feature": [-2], "threshold": [-2.0], "value": [0.0]}
        split = {"children_left": [1, -1, -1], "children_right": [2, -1, -1],
                 "feature": [0, -2, -2], "threshold": [1.00000003, -2.0, -2.0],
                 "value": [0.0, 10.0, 20.0]}
        for estimator in model["estimators"]:
            estimator["initial_prediction"] = 100.0
            estimator["trees"] = [copy.deepcopy(split)] + [copy.deepcopy(leaf) for _ in range(63)]
        row = copy.deepcopy(self.validation[0])
        row["vector"][0] = 1.00000004  # Above threshold in float64, rounds to 1 in float32.
        answer = predict_candidate(model, [row], self.transformer)[0]
        self.assertEqual(answer["raw_quantiles"], [100.5] * 3)

    def test_out_of_range_float32_vector_is_rejected(self):
        row = copy.deepcopy(self.validation[0])
        row["vector"][0] = 1e308
        with self.assertRaisesRegex(ValueError, "float32"):
            predict_candidate(self.models["gradient_boosting_quantile"], [row], self.transformer)

    def test_corrupt_tree_cycles_children_indices_and_arrays_fail_closed(self):
        for mutation in ("cycle", "out_of_range", "feature", "array", "nonfinite", "duplicate"):
            model = copy.deepcopy(self.models["gradient_boosting_quantile"])
            tree = next(tree for tree in model["estimators"][0]["trees"] if tree["children_left"][0] != -1)
            if mutation == "cycle": tree["children_left"][0] = 0
            elif mutation == "out_of_range": tree["children_left"][0] = 1000
            elif mutation == "feature": tree["feature"][0] = len(model["feature_names"])
            elif mutation == "array": tree["value"].pop()
            elif mutation == "nonfinite": tree["value"][0] = float("nan")
            else: tree["children_left"][0] = tree["children_right"][0]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_model(model, self.transformer)

    def test_unknown_candidates_parameter_drift_and_incomplete_artifacts_fail(self):
        with self.assertRaisesRegex(ValueError, "Unknown"):
            fit_candidate(self.fit, self.transformer, candidate_id="other")
        for mutation in ("parameters", "estimators", "support", "version", "groups"):
            model = copy.deepcopy(self.models["linear_quantile"])
            if mutation == "parameters": model["parameters"]["alpha"] = 0
            elif mutation == "estimators": model["estimators"].pop()
            elif mutation == "support": model["fitted_rows"] += 1
            elif mutation == "version": model["model_definition_version"] = "other"
            else: model["fitted_trip_keys"][0][1] = "outside"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_model(model, self.transformer)

    def test_predictions_ignore_unrelated_target_like_payloads(self):
        rows = [dict(row, actual_arrival_time="bad", remaining_duration_seconds=-999,
                     target={"poison": True}) for row in self.validation]
        for model in self.models.values():
            self.assertEqual(predict_candidate(model, rows, self.transformer),
                             predict_candidate(model, self.validation, self.transformer))

    def test_empty_inference_and_timezone_equivalent_cutoff(self):
        for model in self.models.values():
            self.assertEqual(predict_candidate(model, [], self.transformer), [])
            row = dict(self.validation[0], snapshot_time="2026-03-01T00:00:00-08:00")
            self.assertEqual(predict_candidate(model, [row], self.transformer),
                             predict_candidate(model, [self.validation[0]], self.transformer))


if __name__ == "__main__":
    unittest.main()
