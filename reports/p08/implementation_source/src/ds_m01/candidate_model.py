"""Fixed quantile candidates with explicit, inspectable JSON inference state.

The caller selects mature training targets at its fold cutoff. This module
checks fit membership, clocks and equal trip weights; it never joins labels.
Each tenant/fold model binds the exact M3b.1 transformer and vector layout.
P10/P50/P90 are raw conditional estimates, not calibrated coverage guarantees.
"""

from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
import hashlib
from importlib.metadata import version
import json
import math
from numbers import Real
import struct
import warnings

from .common import parse_ts, utc
from .feature_transform import validate_transformer


MODEL_DEFINITION_VERSION = "m3b2-quantile-candidate-v1"
QUANTILES = (0.1, 0.5, 0.9)
CANDIDATE_IDS = ("linear_quantile", "gradient_boosting_quantile")
CANDIDATE_PARAMETERS = {
    "linear_quantile": {"alpha": 0.01, "solver": "highs", "fit_intercept": True},
    "gradient_boosting_quantile": {
        "loss": "quantile", "n_estimators": 64, "learning_rate": 0.05,
        "max_depth": 2, "min_samples_leaf": 20, "random_state": 20261030,
        "subsample": 1.0, "n_iter_no_change": None,
    },
}
_META = ("tenant_id", "trip_id", "snapshot_id", "snapshot_time")
_OUTPUT_FIELDS = ("p10_remaining_seconds", "p50_remaining_seconds", "p90_remaining_seconds")


def canonical_digest(value):
    """Hash canonical JSON content, independent of a file's whitespace."""
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def transformer_digest(state):
    validate_transformer(state)
    return canonical_digest(state)


def _number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{name} must be finite" + (" and positive" if positive else ""))
    return result


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _integer(value, name, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _timestamp(value, name):
    try:
        return parse_ts(value)
    except (ValueError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError(f"{name} must be timezone-aware") from exc


def _vector(value, width):
    if not isinstance(value, (list, tuple)) or len(value) != width:
        raise ValueError("Vector width differs from the bound feature layout")
    return [_number(item, "vector element") for item in value]


def _rows(rows, transformer, *, fit):
    if isinstance(rows, (str, bytes, Mapping)):
        raise ValueError("Rows must be an iterable of mappings")
    try:
        materialized = list(rows)
    except TypeError as exc:
        raise ValueError("Rows must be an iterable of mappings") from exc
    declared = {tuple(key) for key in transformer["fit_trip_keys"]}
    fitted_snapshots = {tuple(key) for key in transformer["fitted_snapshot_keys"]}
    cutoff = _timestamp(transformer["training_cutoff_at"], "training cutoff")
    clean, seen, weights = [], set(), defaultdict(list)
    for row in materialized:
        if not isinstance(row, Mapping):
            raise ValueError("Each model row must be a mapping")
        meta = {name: _identifier(row.get(name), name) for name in _META}
        at = _timestamp(meta["snapshot_time"], "snapshot_time")
        meta["snapshot_time"] = utc(at)
        key = meta["tenant_id"], meta["trip_id"]
        snapshot_key = meta["tenant_id"], meta["snapshot_id"]
        if meta["tenant_id"] != transformer["tenant_id"]:
            raise ValueError("A model cannot cross tenant boundaries")
        if snapshot_key in seen:
            raise ValueError("Duplicate tenant/snapshot model key")
        seen.add(snapshot_key)
        if row.get("fold_id", transformer["fold_id"]) != transformer["fold_id"]:
            raise ValueError("Model row belongs to a different fold")
        expected_role = "fit" if fit else "validation"
        if row.get("fold_role", expected_role) != expected_role:
            raise ValueError("Model row has an incompatible fold role")
        if row.get("feature_definition_version", transformer["feature_definition_version"]) != transformer["feature_definition_version"]:
            raise ValueError("Model row uses an incompatible feature definition")
        if fit:
            if key not in declared or snapshot_key not in fitted_snapshots or at >= cutoff:
                raise ValueError("Model fit row is outside the transformer's fit groups or cutoff")
        elif key in declared or at < cutoff:
            raise ValueError("Inference row overlaps fit groups or precedes fit cutoff")
        item = dict(meta, vector=_vector(row.get("vector"), len(transformer["feature_names"])))
        if fit:
            item["remaining_duration_seconds"] = _number(
                row.get("remaining_duration_seconds"), "remaining_duration_seconds", positive=True)
            item["sample_weight"] = _number(row.get("sample_weight"), "sample_weight", positive=True)
            weights[key].append(item["sample_weight"])
        clean.append(item)
    if fit and any(not math.isclose(math.fsum(values), 1.0, rel_tol=0, abs_tol=1e-9)
                   for values in weights.values()):
        raise ValueError("Eligible snapshots must share total weight one per trip")
    return sorted(clean, key=lambda row: (row["tenant_id"], row["trip_id"],
                                          row["snapshot_time"], row["snapshot_id"]))


def _serialize_estimator(estimator, candidate_id):
    if candidate_id == "linear_quantile":
        return {"intercept": float(estimator.intercept_), "coefficients": estimator.coef_.tolist()}
    trees = []
    for stage in estimator.estimators_:
        tree = stage[0].tree_
        trees.append({"children_left": tree.children_left.tolist(),
                      "children_right": tree.children_right.tolist(),
                      "feature": tree.feature.tolist(), "threshold": tree.threshold.tolist(),
                      "value": tree.value[:, 0, 0].tolist()})
    return {"initial_prediction": float(estimator.init_.constant_[0, 0]),
            "learning_rate": float(estimator.learning_rate), "trees": trees}


def fit_candidate(rows, transformer, *, candidate_id, min_fit_trips=5):
    """Fit three fixed quantiles, with each eligible trip carrying weight one.

    Low support yields explicit unsupported state. Malformed rows, a failed
    optimizer or corrupted artifacts raise errors, not an apparent successful
    fallback model. Hyperparameters are fixed engineering defaults, not tuned
    against validation, calibration or final-test outcomes.
    """
    validate_transformer(transformer)
    if candidate_id not in CANDIDATE_IDS:
        raise ValueError("Unknown quantile candidate")
    minimum = _integer(min_fit_trips, "min_fit_trips", minimum=1)
    clean = _rows(rows, transformer, fit=True)
    trip_keys = sorted({(row["tenant_id"], row["trip_id"]) for row in clean})
    model = dict(
        model_definition_version=MODEL_DEFINITION_VERSION, candidate_id=candidate_id,
        tenant_id=transformer["tenant_id"], fold_id=transformer["fold_id"],
        training_cutoff_at=transformer["training_cutoff_at"],
        transformer_sha256=transformer_digest(transformer),
        feature_names=list(transformer["feature_names"]), quantiles=list(QUANTILES),
        parameters=deepcopy(CANDIDATE_PARAMETERS[candidate_id]), min_fit_trips=minimum,
        fitted_trip_keys=[list(key) for key in trip_keys],
        fitted_snapshot_keys=[[row["tenant_id"], row["snapshot_id"]] for row in clean],
        fitted_rows=len(clean), fitted_trips=len(trip_keys),
        weighting="Each eligible fit trip has total weight one across its eligible snapshots.",
        status="unsupported", reason="insufficient_fit_trips", estimators=[],
        library_versions={},
    )
    if len(trip_keys) < minimum:
        validate_model(model, transformer)
        return model
    # Numeric libraries are only required for training. JSON inference below
    # deliberately does not deserialize executable Python objects.
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.linear_model import QuantileRegressor

    vectors = [row["vector"] for row in clean]
    targets = [row["remaining_duration_seconds"] for row in clean]
    weights = [row["sample_weight"] for row in clean]
    for quantile in QUANTILES:
        parameters = CANDIDATE_PARAMETERS[candidate_id]
        estimator = (QuantileRegressor(quantile=quantile, **parameters)
                     if candidate_id == "linear_quantile" else
                     GradientBoostingRegressor(alpha=quantile, **parameters))
        with warnings.catch_warnings():
            warnings.simplefilter("error", ConvergenceWarning)
            estimator.fit(vectors, targets, sample_weight=weights)
        model["estimators"].append(_serialize_estimator(estimator, candidate_id))
    model.update(status="available", reason=None,
                 library_versions={name: version(name) for name in ("numpy", "scipy", "scikit-learn")})
    validate_model(model, transformer)
    return model


def _pairs(values, tenant, name):
    if not isinstance(values, list):
        raise ValueError(f"{name} must be a list")
    result = []
    for pair in values:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError(f"{name} requires tenant/key pairs")
        first, second = [_identifier(value, name) for value in pair]
        if first != tenant:
            raise ValueError(f"{name} cannot cross tenant boundaries")
        result.append((first, second))
    if len(result) != len(set(result)):
        raise ValueError(f"{name} contains duplicate keys")
    return set(result)


def _validate_tree(tree, width):
    fields = ("children_left", "children_right", "feature", "threshold", "value")
    if not isinstance(tree, Mapping) or set(tree) != set(fields):
        raise ValueError("Tree must contain the declared node arrays")
    if any(not isinstance(tree[field], list) for field in fields):
        raise ValueError("Tree nodes must be stored in arrays")
    size = len(tree["value"])
    if not 1 <= size <= 7 or any(len(tree[field]) != size for field in fields):
        raise ValueError("Tree arrays differ in length or exceed the fixed depth-two limit")
    reached, stack = set(), [(0, 0)]
    while stack:
        node, depth = stack.pop()
        if node in reached:
            raise ValueError("Tree contains repeated or cyclic nodes")
        reached.add(node)
        left, right, feature = (tree[name][node] for name in fields[:3])
        for value in (left, right, feature):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("Tree indices must be integers")
        _number(tree["threshold"][node], "tree threshold")
        _number(tree["value"][node], "tree value")
        if left == right == -1:
            if feature != -2:
                raise ValueError("Leaf feature marker is invalid")
            continue
        if (depth >= 2 or not node < left < size or not node < right < size
                or left == right or not 0 <= feature < width):
            raise ValueError("Tree split has invalid children, depth or feature index")
        stack.extend(((left, depth + 1), (right, depth + 1)))
    if reached != set(range(size)):
        raise ValueError("Tree contains unreachable nodes")


def validate_model(model, transformer):
    """Validate JSON state and exact transformer binding before inference."""
    validate_transformer(transformer)
    if not isinstance(model, Mapping) or model.get("model_definition_version") != MODEL_DEFINITION_VERSION:
        raise ValueError("Unsupported model definition")
    candidate = model.get("candidate_id")
    if candidate not in CANDIDATE_IDS:
        raise ValueError("Unknown quantile candidate")
    for field in ("tenant_id", "fold_id", "training_cutoff_at", "feature_names"):
        if model.get(field) != transformer[field]:
            raise ValueError(f"Model and transformer {field} disagree")
    if model.get("transformer_sha256") != transformer_digest(transformer):
        raise ValueError("Model transformer content hash does not match")
    if model.get("quantiles") != list(QUANTILES) or model.get("parameters") != CANDIDATE_PARAMETERS[candidate]:
        raise ValueError("Model parameters differ from the frozen candidate protocol")
    minimum = _integer(model.get("min_fit_trips"), "min_fit_trips", minimum=1)
    rows = _integer(model.get("fitted_rows"), "fitted_rows")
    trips = _integer(model.get("fitted_trips"), "fitted_trips")
    keys = _pairs(model.get("fitted_trip_keys"), transformer["tenant_id"], "fitted_trip_keys")
    snapshots = _pairs(model.get("fitted_snapshot_keys"), transformer["tenant_id"], "fitted_snapshot_keys")
    if (trips != len(keys) or rows != len(snapshots) or trips > rows
            or not keys <= {tuple(key) for key in transformer["fit_trip_keys"]}
            or not snapshots <= {tuple(key) for key in transformer["fitted_snapshot_keys"]}):
        raise ValueError("Model support or groups disagree with transformer fit evidence")
    estimators = model.get("estimators")
    if not isinstance(estimators, list):
        raise ValueError("Model estimators must be a list")
    if model.get("status") == "unsupported":
        if trips >= minimum or model.get("reason") != "insufficient_fit_trips" or estimators:
            raise ValueError("Unsupported model state disagrees with its support")
        return model
    if model.get("status") != "available" or model.get("reason") is not None or trips < minimum or len(estimators) != 3:
        raise ValueError("Available model state is incomplete")
    width = len(transformer["feature_names"])
    for estimator in estimators:
        if not isinstance(estimator, Mapping):
            raise ValueError("Estimator state must be a mapping")
        if candidate == "linear_quantile":
            if set(estimator) != {"intercept", "coefficients"}:
                raise ValueError("Invalid linear estimator fields")
            _number(estimator["intercept"], "intercept")
            _vector(estimator["coefficients"], width)
        else:
            if set(estimator) != {"initial_prediction", "learning_rate", "trees"}:
                raise ValueError("Invalid boosted estimator fields")
            _number(estimator["initial_prediction"], "initial_prediction")
            if estimator["learning_rate"] != CANDIDATE_PARAMETERS[candidate]["learning_rate"]:
                raise ValueError("Boosted learning rate differs from frozen parameters")
            if not isinstance(estimator["trees"], list) or len(estimator["trees"]) != CANDIDATE_PARAMETERS[candidate]["n_estimators"]:
                raise ValueError("Boosted estimator count differs from frozen parameters")
            for tree in estimator["trees"]:
                _validate_tree(tree, width)
    return model


def _float32(value):
    """Match scikit-learn tree traversal's float32 input conversion."""
    try:
        result = struct.unpack("f", struct.pack("f", value))[0]
    except (OverflowError, struct.error) as exc:
        raise ValueError("Vector cannot be represented as finite float32") from exc
    if not math.isfinite(result):
        raise ValueError("Vector cannot be represented as finite float32")
    return result


def _predict_estimator(estimator, vector, candidate_id):
    if candidate_id == "linear_quantile":
        try:
            return math.fsum([estimator["intercept"], *
                              (weight * value for weight, value in zip(estimator["coefficients"], vector))])
        except (OverflowError, ValueError):
            return float("nan")
    converted = [_float32(value) for value in vector]
    answer = estimator["initial_prediction"]
    for tree in estimator["trees"]:
        node = 0
        while tree["children_left"][node] != -1:
            node = (tree["children_left"][node] if converted[tree["feature"][node]] <= tree["threshold"][node]
                    else tree["children_right"][node])
        answer += estimator["learning_rate"] * tree["value"][node]
    return answer


def enforce_quantile_policy(raw_quantiles):
    """Sort finite positive quantiles; expose invalid estimates without clipping.

    Sorting removes crossing only. It does not calibrate interval coverage.
    Nonfinite values become JSON null with their indices retained for auditing.
    """
    if not isinstance(raw_quantiles, (list, tuple)) or len(raw_quantiles) != 3:
        raise ValueError("Exactly three raw quantile estimates are required")
    raw, invalid = [], []
    for index, value in enumerate(raw_quantiles):
        try:
            raw.append(_number(value, "raw quantile"))
        except ValueError:
            raw.append(None)
            invalid.append(index)
    reason = "nonfinite_quantile" if invalid else "nonpositive_quantile" if min(raw) <= 0 else None
    crossing = False if invalid else not raw[0] <= raw[1] <= raw[2]
    ordered = sorted(raw) if reason is None else [None, None, None]
    return dict(status="available" if reason is None else "unsupported", reason=reason,
                raw_quantiles=raw, invalid_quantile_indices=invalid,
                quantile_crossing=crossing,
                quantile_postprocessing="monotone_sort" if crossing and reason is None else "none",
                **dict(zip(_OUTPUT_FIELDS, ordered)))


def predict_candidate(model, rows, transformer):
    """Predict every supplied validation opportunity, without any outcome access.

    Invalid models, bindings or input vectors fail closed. A valid unsupported
    model or invalid numerical output is explicit for the caller's declared
    baseline fallback. Row order is canonical and no valid opportunity is lost.
    """
    validate_model(model, transformer)
    clean = _rows(rows, transformer, fit=False)
    model_hash = canonical_digest(model)
    results = []
    for row in clean:
        if model["status"] == "unsupported":
            answer = dict(status="unsupported", reason=model["reason"], raw_quantiles=[None] * 3,
                          invalid_quantile_indices=[], quantile_crossing=False,
                          quantile_postprocessing="none", **dict.fromkeys(_OUTPUT_FIELDS))
        else:
            raw = [_predict_estimator(estimator, row["vector"], model["candidate_id"])
                   for estimator in model["estimators"]]
            answer = enforce_quantile_policy(raw)
        results.append(dict({field: row[field] for field in _META},
                            candidate_id=model["candidate_id"], fold_id=model["fold_id"],
                            training_cutoff_at=model["training_cutoff_at"],
                            transformer_sha256=model["transformer_sha256"], model_sha256=model_hash,
                            **answer))
    return results
