"""Replay integrity checks distinguish numeric tolerance from discrete drift."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ds_m01.candidate_model import canonical_digest
from ds_m01.candidate_replay import (
    _inference, bind_predictions, check_inference, compare_numerical,
    compare_predictions, verify_receipt,
)
from ds_m01.candidate_run import CODE_MODULES, OUTPUT_FILES, OUTCOME_SOURCE, train_and_predict, verify_inputs
from ds_m01.candidate_targets import select_fit_outcomes
from ds_m01.common import digest_file, write_jsonl
from test_candidate_publication import fixture_root
from test_candidate_run import fixture


PROJECT = Path(__file__).resolve().parents[1]
VERSIONS = {line.split("==")[0]: line.split("==")[1]
            for line in (PROJECT / "requirements-models.lock").read_text().splitlines()
            if "==" in line and not line.startswith("#")}


def receipt_fixture(root):
    settings = fixture_root(root)
    for name in CODE_MODULES:
        target = root / "src/ds_m01" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((PROJECT / "src/ds_m01" / name).read_bytes())
    settings["expected_retained_sha256"] = {name: digest_file(root / name)
                                          for name in settings["expected_retained_sha256"]}
    (root / "config/m3b2.json").write_text(json.dumps(settings))
    folder = root / "reports/m3b2"
    folder.mkdir(parents=True)
    for name in OUTPUT_FILES:
        (folder / name).write_text("[]\n" if name == "models.json" else "{}\n" if name == "summary.json" else "")
    receipt = dict(batch_id=settings["batch_id"], settings_sha256=digest_file(root / "config/m3b2.json"),
                   output_sha256={name: digest_file(folder / name) for name in OUTPUT_FILES},
                   code_sha256={name: digest_file(root / "src/ds_m01" / name) for name in CODE_MODULES},
                   input_sha256=verify_inputs(root, settings), package_versions=dict(VERSIONS))
    (folder / "run_receipt.json").write_text(json.dumps(receipt))
    return receipt, settings, folder


class CandidateReplayComparisonTests(unittest.TestCase):
    def test_float_tolerance_is_bounded_and_reports_actual_difference(self):
        result = compare_numerical({"seconds": 1.0}, {"seconds": 1.0000005})
        self.assertGreater(result["max_absolute_difference"], 0)
        self.assertEqual(result["compared_float_values"], 1)
        with self.assertRaisesRegex(ValueError, "tolerance"):
            compare_numerical({"seconds": 1.0}, {"seconds": 1.000002})

    def test_relative_tolerance_does_not_become_an_unbounded_absolute_tolerance(self):
        compare_numerical([1e9], [1e9 + 0.5])
        with self.assertRaisesRegex(ValueError, "tolerance"):
            compare_numerical([1e9], [1e9 + 2.0])

    def test_boolean_integer_float_and_none_types_are_distinct(self):
        for first, second in ((True, 1), (1, 1.0), (False, 0.0), (None, 0.0)):
            with self.subTest(first=first, second=second), self.assertRaisesRegex(ValueError, "type"):
                compare_numerical([first], [second])

    def test_nonfinite_values_fail_even_when_both_artifacts_contain_them(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "nonfinite"):
                compare_numerical([value], [value])

    def test_schema_counts_identifiers_and_decisions_are_exact(self):
        cases = [({"rows": 10}, {"rows": 11}),
                 ({"status": "available"}, {"status": "unavailable"}),
                 ({"tenant_id": "T1"}, {"tenant_id": "T2"}),
                 ({"quantile_crossing": False}, {"quantile_crossing": True}),
                 ({"x": 1}, {"x": 1, "extra": None}), ([1], [1, 2]),
                 ({"x": ["A", "B"]}, {"x": ["B", "A"]})]
        for first, second in cases:
            with self.subTest(first=first), self.assertRaises(ValueError):
                compare_numerical(first, second)

    def test_eta_tolerance_is_fifty_microseconds_and_does_not_apply_to_snapshot_clock(self):
        first = {"eta_median": "2026-03-01T08:00:00Z"}
        compare_numerical(first, {"eta_median": "2026-03-01T08:00:00.000050Z"}, eta_fields=True)
        with self.assertRaisesRegex(ValueError, "ETA"):
            compare_numerical(first, {"eta_median": "2026-03-01T08:00:00.000051Z"}, eta_fields=True)
        with self.assertRaisesRegex(ValueError, "discrete"):
            compare_numerical({"snapshot_time": first["eta_median"]},
                              {"snapshot_time": "2026-03-01T08:00:00.000001Z"}, eta_fields=True)


class CandidateReplayManifestTests(unittest.TestCase):
    def verify(self, root, folder, **kwargs):
        with patch("ds_m01.candidate_replay.verify_runtime", return_value=VERSIONS):
            return verify_receipt(root, folder, **kwargs)

    def test_complete_receipt_is_validated_before_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            receipt, settings, folder = receipt_fixture(root)
            self.assertEqual(self.verify(root, folder), (receipt, settings))

    def test_omitted_output_input_or_code_entry_cannot_hide_a_change(self):
        for field, key in (("output_sha256", "models.json"),
                           ("input_sha256", OUTCOME_SOURCE),
                           ("code_sha256", "candidate_model.py")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                receipt, _, folder = receipt_fixture(root)
                receipt[field].pop(key)
                (folder / "run_receipt.json").write_text(json.dumps(receipt))
                with self.assertRaisesRegex(ValueError, "complete canonical"):
                    self.verify(root, folder)

    def test_extra_manifest_path_is_rejected_without_opening_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            receipt, _, folder = receipt_fixture(root)
            receipt["output_sha256"]["../../unexpected"] = "0" * 64
            (folder / "run_receipt.json").write_text(json.dumps(receipt))
            with self.assertRaisesRegex(ValueError, "complete canonical"):
                self.verify(root, folder)

    def test_changed_settings_code_retained_input_and_output_fail(self):
        for relative in ("config/m3b2.json", "src/ds_m01/candidate_model.py",
                         "reports/m3b1/matrices.jsonl", "requirements-models.lock",
                         "reports/m3b2/predictions.jsonl"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                _, _, folder = receipt_fixture(root)
                path = root / relative
                path.write_text(path.read_text() + "\n")
                with self.assertRaises(ValueError):
                    self.verify(root, folder)

    def test_dependency_version_mismatch_is_not_numerical_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            receipt, _, folder = receipt_fixture(root)
            receipt["package_versions"]["numpy"] = "0.0.0"
            (folder / "run_receipt.json").write_text(json.dumps(receipt))
            with patch("ds_m01.candidate_replay.verify_runtime", return_value=dict(VERSIONS, numpy="2.3.5")), \
                    self.assertRaisesRegex(ValueError, "dependencies"):
                verify_receipt(root, folder)

    def test_inference_only_does_not_open_outcome_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _, folder = receipt_fixture(root)
            (root / OUTCOME_SOURCE).unlink()
            self.verify(root, folder, require_target_source=False)
            with self.assertRaises(FileNotFoundError):
                self.verify(root, folder, require_target_source=True)

    def test_invalid_manifest_hashes_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            receipt, _, folder = receipt_fixture(root)
            receipt["code_sha256"]["candidate_model.py"] = True
            (folder / "run_receipt.json").write_text(json.dumps(receipt))
            with self.assertRaisesRegex(ValueError, "invalid SHA256"):
                self.verify(root, folder)


class CandidateReplayBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs, cls.settings, cls.source, outcomes = fixture()
        cls.result = train_and_predict(cls.inputs, cls.settings, cls.source,
                                       lambda keys, cutoff: select_fit_outcomes(outcomes, keys, cutoff))
        cls.versions = dict(VERSIONS)
        cls.versions.update(cls.result["models"][0]["library_versions"])

    def bind(self, models=None, predictions=None):
        return bind_predictions(self.result["models"] if models is None else models,
                                self.result["predictions"] if predictions is None else predictions,
                                self.inputs, self.settings, self.versions)

    def test_every_prediction_is_independently_bound_to_its_exact_model(self):
        self.bind()
        changed = copy.deepcopy(self.result["predictions"])
        changed[0]["model_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "model content hash"):
            self.bind(predictions=changed)

    def test_swapping_model_hashes_across_tenants_cannot_pass(self):
        changed = copy.deepcopy(self.result["predictions"])
        other = next(row for row in changed if row["tenant_id"] != changed[0]["tenant_id"])
        changed[0]["model_sha256"], other["model_sha256"] = other["model_sha256"], changed[0]["model_sha256"]
        with self.assertRaisesRegex(ValueError, "model content hash"):
            self.bind(predictions=changed)

    def test_omitted_duplicated_or_identity_changed_opportunities_fail(self):
        for mutation in ("omit", "duplicate", "trip", "transformer"):
            predictions = copy.deepcopy(self.result["predictions"])
            if mutation == "omit": predictions.pop()
            elif mutation == "duplicate": predictions.append(predictions[0])
            elif mutation == "trip": predictions[0]["trip_id"] = "WRONG"
            else: predictions[0]["transformer_sha256"] = "0" * 64
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.bind(predictions=predictions)

    def test_models_cannot_be_omitted_duplicated_or_reassigned(self):
        for mutation in ("omit", "duplicate", "tenant"):
            models = copy.deepcopy(self.result["models"])
            if mutation == "omit": models.pop()
            elif mutation == "duplicate": models.append(models[0])
            else: models[0]["tenant_id"] = "WRONG"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.bind(models=models)

    def test_tolerated_model_difference_still_requires_recomputed_hash_binding(self):
        models = copy.deepcopy(self.result["models"])
        predictions = copy.deepcopy(self.result["predictions"])
        index = next(index for index, model in enumerate(models) if model["candidate_id"] == "linear_quantile")
        models[index]["estimators"][0]["intercept"] += 5e-7
        compare_numerical(self.result["models"], models)
        with self.assertRaisesRegex(ValueError, "model content hash"):
            compare_predictions(self.result["predictions"], predictions, self.result["models"], models,
                                self.inputs, self.settings, self.versions)
        model = models[index]
        for row in predictions:
            if (row["candidate_id"], row["fold_id"], row["tenant_id"]) == (model["candidate_id"], model["fold_id"], model["tenant_id"]):
                row["model_sha256"] = canonical_digest(model)
        compare_predictions(self.result["predictions"], predictions, self.result["models"], models,
                            self.inputs, self.settings, self.versions)

    def test_reordered_rows_or_changed_decisions_cannot_pair_by_position(self):
        for predictions in (list(reversed(self.result["predictions"])), copy.deepcopy(self.result["predictions"])):
            if predictions[0] == self.result["predictions"][0]:
                predictions[0]["prediction_source"] = "baseline_fallback"
            with self.assertRaises(ValueError):
                compare_predictions(self.result["predictions"], predictions, self.result["models"], self.result["models"],
                                    self.inputs, self.settings, self.versions)

    def test_retained_json_inference_uses_model_objects_without_target_loading(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "reports/m3b2"
            folder.mkdir(parents=True)
            (root / "config").mkdir()
            (root / "config/m2.json").write_text(json.dumps(self.source))
            (folder / "models.json").write_text(json.dumps(self.result["models"]))
            write_jsonl(folder / "predictions.jsonl", self.result["predictions"])
            with patch("ds_m01.candidate_replay.load_retained_inputs", return_value=self.inputs), \
                    patch("ds_m01.candidate_targets.load_fit_outcomes", side_effect=AssertionError("Targets must not be loaded")), \
                    patch("ds_m01.candidate_run.fit_candidate", side_effect=AssertionError("Training must not run")):
                result = _inference(root, self.settings, {"package_versions": self.versions})
            self.assertTrue(result["retained_inference_equal"])
            self.assertEqual(result["prediction_rows"], 8)
            self.assertEqual(result["numerical_differences"]["max_absolute_difference"], 0)

    def test_inference_wrapper_preserves_target_free_mode(self):
        with patch("ds_m01.candidate_replay.verify_receipt", return_value=({}, {})) as verified, \
                patch("ds_m01.candidate_replay._inference", return_value={"retained_inference_equal": True}), \
                patch("ds_m01.candidate_replay.run", side_effect=AssertionError("Training must not run")):
            result = check_inference(PROJECT)
        self.assertFalse(result["training_executed"])
        self.assertFalse(result["outcome_source_opened"])
        self.assertEqual(verified.call_count, 2)
        self.assertTrue(all(call.kwargs["require_target_source"] is False for call in verified.call_args_list))


if __name__ == "__main__":
    unittest.main()
