"""Verify retained candidate inference and numerically bounded training replay.

Exact source/artifact hashes protect retained evidence. Numeric replay tolerates
only the declared floating differences; identifiers, decisions, counts, schema
and dependency versions remain exact. A failed comparison must not be converted
to a pass by increasing tolerances after seeing the result.
"""

from collections.abc import Mapping
import json
import math
from pathlib import Path
import re

from .candidate_model import (
    CANDIDATE_IDS, CANDIDATE_PARAMETERS, canonical_digest,
    predict_candidate, transformer_digest, validate_model,
)
from .candidate_run import (
    CANDIDATE_RUN_VERSION, CODE_MODULES, OUTPUT_FILES, OUTCOME_SOURCE,
    REQUIRED_PINNED_FILES, _prediction, load_retained_inputs, run,
    validate_retained_inputs, verify_inputs, verify_runtime,
)
from .common import digest_file, parse_ts, read_jsonl, utc
from .model_splits import validate_folds


ABSOLUTE_TOLERANCE = 1e-6
RELATIVE_TOLERANCE = 1e-9
ETA_TOLERANCE_MICROSECONDS = 50
EXACT_OUTPUT_FILES = ("training_rows.jsonl", "summary.json")
_ETA_FIELDS = {"eta_lower", "eta_median", "eta_upper"}
_MODEL_KEY = ("candidate_id", "fold_id", "tenant_id")
_PREDICTION_KEY = (*_MODEL_KEY, "snapshot_id")
_MODEL_LIBRARIES = {"numpy", "scipy", "scikit-learn"}


def _json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def _hash_map(value, expected_keys, label):
    if not isinstance(value, dict) or set(value) != set(expected_keys):
        raise ValueError(f"{label} must cover its complete canonical file set")
    if any(not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{64}", item) is None
           for item in value.values()):
        raise ValueError(f"{label} contains an invalid SHA256")
    return value


def verify_receipt(root, folder, *, require_target_source=True):
    """Validate complete manifests, settings, dependencies and retained bytes.

    Inference-only verification does not open the outcome source. Its expected
    hash still has to agree across configuration and receipt. Full replay also
    reads and checks that source before any training begins.
    """
    root, folder = Path(root), Path(folder)
    receipt = _json(folder / "run_receipt.json")
    if not isinstance(receipt, dict):
        raise ValueError("Candidate receipt must be an object")
    settings_path = root / "config/m3b2.json"
    settings = _json(settings_path)
    if receipt.get("settings_sha256") != digest_file(settings_path):
        raise ValueError("Candidate settings changed since the retained run")
    if (settings.get("candidate_definition_version") != CANDIDATE_RUN_VERSION
            or settings.get("candidate_ids") != list(CANDIDATE_IDS)
            or settings.get("candidate_parameters") != CANDIDATE_PARAMETERS
            or settings.get("min_fit_trips") != 5):
        raise ValueError("Candidate settings differ from the fixed replay protocol")
    if receipt.get("batch_id") != settings.get("batch_id"):
        raise ValueError("Candidate receipt and configuration batch identifiers disagree")
    outputs = _hash_map(receipt.get("output_sha256"), OUTPUT_FILES, "Output manifest")
    code = _hash_map(receipt.get("code_sha256"), CODE_MODULES, "Code manifest")
    inputs = _hash_map(receipt.get("input_sha256"), (*REQUIRED_PINNED_FILES, OUTCOME_SOURCE), "Input manifest")
    pins = _hash_map(settings.get("expected_retained_sha256"), REQUIRED_PINNED_FILES, "Retained input pins")
    for name, expected in outputs.items():
        if digest_file(folder / name) != expected:
            raise ValueError(f"Retained candidate output changed: {name}")
    for name, expected in code.items():
        if (digest_file(root / "src/ds_m01" / name) != expected
                or digest_file(Path(__file__).parent / name) != expected):
            raise ValueError(f"Candidate implementation changed: {name}")
    for relative, expected in pins.items():
        if inputs[relative] != expected or digest_file(root / relative) != expected:
            raise ValueError(f"Retained candidate input changed: {relative}")
    if inputs[OUTCOME_SOURCE] != settings.get("expected_outcome_sha256"):
        raise ValueError("Outcome hash differs between receipt and configuration")
    if require_target_source and verify_inputs(root, settings) != inputs:
        raise ValueError("Candidate source verification differs from the receipt")
    frozen = _json(root / "config/m3a.json")
    if settings.get("folds") != frozen.get("folds"):
        raise ValueError("Candidate folds must remain frozen")
    validate_folds(settings["folds"], _json(root / "config/m2.json"))
    versions = verify_runtime(root)
    if receipt.get("package_versions") != versions:
        raise ValueError("Runtime dependencies differ from the retained run")
    return receipt, settings


def compare_numerical(expected, actual, *, eta_fields=False):
    """Compare equal JSON schemas; only floats and optional ETA clocks vary.

    Booleans cannot equal integers, integers are exact, lists retain their order,
    and all finite numeric values preserve their JSON types. This is a bounded
    numerical comparison, not a binary-identical artifact claim.
    """
    stats = {"max_absolute_difference": 0.0, "max_relative_difference": 0.0,
             "max_eta_difference_microseconds": 0.0, "compared_float_values": 0}

    def visit(left, right, path, field=None):
        if type(left) is not type(right):
            raise ValueError(f"Replay JSON type changed at {path}")
        if isinstance(left, dict):
            if set(left) != set(right):
                raise ValueError(f"Replay object fields changed at {path}")
            for key in sorted(left):
                visit(left[key], right[key], f"{path}.{key}", key)
        elif isinstance(left, list):
            if len(left) != len(right):
                raise ValueError(f"Replay list length changed at {path}")
            for index, (first, second) in enumerate(zip(left, right)):
                visit(first, second, f"{path}[{index}]")
        elif isinstance(left, float):
            if not math.isfinite(left) or not math.isfinite(right):
                raise ValueError(f"Replay contains a nonfinite value at {path}")
            difference = abs(left - right)
            denominator = max(abs(left), abs(right))
            relative = difference / denominator if denominator else 0.0
            stats["max_absolute_difference"] = max(stats["max_absolute_difference"], difference)
            stats["max_relative_difference"] = max(stats["max_relative_difference"], relative)
            stats["compared_float_values"] += 1
            if not math.isclose(left, right, rel_tol=RELATIVE_TOLERANCE, abs_tol=ABSOLUTE_TOLERANCE):
                raise ValueError(f"Replay float exceeds fixed tolerance at {path}: {difference}")
        elif eta_fields and field in _ETA_FIELDS and isinstance(left, str):
            difference = abs((parse_ts(left) - parse_ts(right)).total_seconds()) * 1_000_000
            stats["max_eta_difference_microseconds"] = max(stats["max_eta_difference_microseconds"], difference)
            if difference > ETA_TOLERANCE_MICROSECONDS:
                raise ValueError(f"Replay ETA exceeds fixed tolerance at {path}: {difference} microseconds")
        elif left != right:
            raise ValueError(f"Replay discrete value changed at {path}")

    visit(expected, actual, "root")
    return stats


def _index(rows, fields, label):
    if not isinstance(rows, list):
        raise ValueError(f"{label} must be a list")
    result = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError(f"{label} row must be a mapping")
        key = tuple(row.get(field) for field in fields)
        if any(not isinstance(item, str) or not item.strip() for item in key) or key in result:
            raise ValueError(f"Invalid or duplicate {label} key")
        result[key] = row
    return result


def bind_predictions(models, predictions, inputs, settings, package_versions):
    """Independently bind each prediction to its exact model and opportunity."""
    _, transformers = validate_retained_inputs(inputs, settings["folds"])
    model_index = _index(models, _MODEL_KEY, "model")
    expected_models = {(candidate, row["fold_id"], row["tenant_id"])
                       for candidate in settings["candidate_ids"] for row in inputs["memberships"]}
    if set(model_index) != expected_models:
        raise ValueError("Models must cover every candidate/fold/tenant exactly once")
    digests, transformer_digests = {}, {}
    fold_index = {row["fold_id"]: dict(row, training_cutoff_at=utc(row["training_cutoff_at"]))
                  for row in settings["folds"]}
    for key, model in model_index.items():
        candidate, fold_id, tenant = key
        transformer = transformers.get((fold_id, tenant))
        if transformer is None:
            expected = dict(candidate_id=candidate, fold_id=fold_id, tenant_id=tenant,
                            training_cutoff_at=fold_index[fold_id]["training_cutoff_at"],
                            status="unsupported", reason="no_tenant_transformer", transformer_sha256=None)
            if model != expected:
                raise ValueError("Missing-transformer model state differs from the declared unavailable branch")
        else:
            validate_model(model, transformer)
            libraries = {name: package_versions[name] for name in _MODEL_LIBRARIES} if model["status"] == "available" else {}
            if model.get("library_versions") != libraries:
                raise ValueError("Model dependency versions differ from its run receipt")
        digests[key] = canonical_digest(model)
        transformer_digests[fold_id, tenant] = transformer_digest(transformer) if transformer is not None else None
    validation = _index([row for row in inputs["matrices"] if row["fold_role"] == "validation"],
                        ("fold_id", "tenant_id", "snapshot_id"), "validation matrix")
    prediction_index = _index(predictions, _PREDICTION_KEY, "prediction")
    expected_predictions = {(candidate, *key) for candidate in settings["candidate_ids"] for key in validation}
    if set(prediction_index) != expected_predictions:
        raise ValueError("Predictions must preserve every validation opportunity for every candidate")
    for key, prediction in prediction_index.items():
        candidate, fold_id, tenant, sid = key
        row = validation[fold_id, tenant, sid]
        transformer = transformers.get((fold_id, tenant))
        if (prediction.get("trip_id") != row["trip_id"]
                or prediction.get("snapshot_time") != row["snapshot_time"]
                or prediction.get("training_cutoff_at") != fold_index[fold_id]["training_cutoff_at"]):
            raise ValueError("Prediction identity or cutoff differs from its validation opportunity")
        expected_model_hash = digests[candidate, fold_id, tenant]
        if prediction.get("model_sha256") != expected_model_hash:
            raise ValueError("Prediction model content hash is not independently bound")
        expected_transformer_hash = transformer_digests[fold_id, tenant]
        if prediction.get("transformer_sha256") != expected_transformer_hash:
            raise ValueError("Prediction transformer content hash is not independently bound")
    return model_index, prediction_index


def compare_predictions(expected, actual, expected_models, actual_models, inputs, settings, package_versions):
    # A numerically equivalent retrained model can have a different content hash.
    # Validate both hash references before excluding their literal text below.
    bind_predictions(expected_models, expected, inputs, settings, package_versions)
    bind_predictions(actual_models, actual, inputs, settings, package_versions)
    first = [{key: value for key, value in row.items() if key != "model_sha256"} for row in expected]
    second = [{key: value for key, value in row.items() if key != "model_sha256"} for row in actual]
    return compare_numerical(first, second, eta_fields=True)


def _inference(root, settings, receipt):
    """Reuse retained model JSON and frozen inputs without joining any targets."""
    root = Path(root)
    inputs = load_retained_inputs(root)
    features, transformers = validate_retained_inputs(inputs, settings["folds"])
    models = _json(root / "reports/m3b2/models.json")
    retained = read_jsonl(root / "reports/m3b2/predictions.jsonl")
    model_index, _ = bind_predictions(models, retained, inputs, settings, receipt["package_versions"])
    predictions = []
    for fold in validate_folds(settings["folds"], _json(root / "config/m2.json")):
        fold_id = fold["fold_id"]
        history = [row for row in inputs["fallback_history"] if row["fold_id"] == fold_id]
        tenants = sorted({row["tenant_id"] for row in inputs["memberships"] if row["fold_id"] == fold_id})
        for tenant in tenants:
            transformer = transformers.get((fold_id, tenant))
            rows = [row for row in inputs["matrices"] if row["fold_id"] == fold_id
                    and row["tenant_id"] == tenant and row["fold_role"] == "validation"]
            for candidate in settings["candidate_ids"]:
                model = model_index[candidate, fold_id, tenant]
                model_hash = canonical_digest(model)
                transformer_hash = transformer_digest(transformer) if transformer is not None else None
                if transformer is None:
                    answers = [dict(status="unsupported", reason="no_tenant_transformer",
                                    raw_quantiles=None, quantile_crossing=False) for _ in rows]
                else:
                    available = [row for row in rows if row["status"] == "available"]
                    answers_by_key = _index(predict_candidate(model, available, transformer),
                                            ("tenant_id", "snapshot_id"), "inference answer")
                    if set(answers_by_key) != {(row["tenant_id"], row["snapshot_id"]) for row in available}:
                        raise ValueError("JSON inference changed prediction opportunity coverage")
                    answers = [answers_by_key[row["tenant_id"], row["snapshot_id"]] if row["status"] == "available"
                               else dict(status="unsupported", reason="unsupported_feature_vector",
                                         raw_quantiles=None, quantile_crossing=False) for row in rows]
                for row, answer in zip(rows, answers):
                    answer = dict(answer, model_sha256=model_hash, transformer_sha256=transformer_hash)
                    predictions.append(_prediction(row, answer, features[row["tenant_id"], row["snapshot_id"]],
                                                   history, fold, candidate, transformer))
    predictions.sort(key=lambda row: (row["candidate_id"], row["fold_id"], row["tenant_id"],
                                     row["trip_id"], row["snapshot_time"], row["snapshot_id"]))
    differences = compare_predictions(retained, predictions, models, models, inputs, settings, receipt["package_versions"])
    return dict(retained_inference_equal=True, prediction_rows=len(predictions), numerical_differences=differences)


def _tolerances():
    return dict(absolute_float=ABSOLUTE_TOLERANCE, relative_float=RELATIVE_TOLERANCE,
                eta_microseconds=ETA_TOLERANCE_MICROSECONDS,
                discrete_fields="Exact types, identifiers, schema, decisions, counts and list order.")


def check_inference(root):
    root = Path(root).resolve()
    receipt, settings = verify_receipt(root, root / "reports/m3b2", require_target_source=False)
    result = _inference(root, settings, receipt)
    verify_receipt(root, root / "reports/m3b2", require_target_source=False)
    return dict(status="passed", **result, tolerances=_tolerances(),
                training_executed=False, outcome_source_opened=False,
                note="Retained JSON inference and fallback replay only; no accuracy or coverage evaluation.")


def check(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    reference_folder = root / "reports/m3b2"
    receipt, settings = verify_receipt(root, reference_folder)
    inference = _inference(root, settings, receipt)
    summary = run(root, root / "config/m3b2.json", output)
    replay_receipt, _ = verify_receipt(root, output)
    for field in ("input_sha256", "settings_sha256", "code_sha256", "package_versions"):
        if replay_receipt[field] != receipt[field]:
            raise ValueError(f"Replay execution context changed: {field}")
    exact = {name: digest_file(output / name) == receipt["output_sha256"][name] for name in EXACT_OUTPUT_FILES}
    if not all(exact.values()):
        raise ValueError(f"Exact training/summary replay differs: {exact}")
    expected_models = _json(reference_folder / "models.json")
    actual_models = _json(output / "models.json")
    inputs = load_retained_inputs(root)
    expected_predictions = read_jsonl(reference_folder / "predictions.jsonl")
    actual_predictions = read_jsonl(output / "predictions.jsonl")
    prediction_differences = compare_predictions(expected_predictions, actual_predictions, expected_models,
                                                actual_models, inputs, settings, receipt["package_versions"])
    model_differences = compare_numerical(expected_models, actual_models)
    verify_receipt(root, reference_folder)
    return dict(
        status="passed", exact_core_artifacts=exact,
        numerical_models_equal=True, numerical_predictions_equal=True,
        retained_inference_equal=inference["retained_inference_equal"],
        model_artifact_byte_identical=digest_file(output / "models.json") == receipt["output_sha256"]["models.json"],
        prediction_artifact_byte_identical=digest_file(output / "predictions.jsonl") == receipt["output_sha256"]["predictions.jsonl"],
        numerical_differences=dict(models=model_differences, predictions=prediction_differences,
                                   retained_inference=inference["numerical_differences"]),
        tolerances=_tolerances(), package_versions=receipt["package_versions"],
        validation_opportunities=summary["validation_opportunities"], prediction_rows=summary["prediction_rows"],
        output=str(output),
        note="Execution timestamps are run-specific. Numerical equivalence is distinct from byte equality. No model accuracy, calibrated coverage or heldout scoring is asserted.",
    )
