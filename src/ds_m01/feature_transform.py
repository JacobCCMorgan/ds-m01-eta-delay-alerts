"""Tenant-local preprocessing fitted on strictly earlier fold-training rows.

This module consumes the positive feature contract, never outcome labels. Each
numeric column is median-imputed, then centered and population-scaled using the
fit snapshots only. A missing indicator remains present even when fit data have
no missing values. Categories have separate missing and unseen slots. No target
encoding, global vocabulary, cross-tenant pooling or automatic refitting occurs.
"""

from collections.abc import Mapping
from decimal import Decimal
import math
from numbers import Real
from statistics import fmean, median, pstdev

from .common import parse_ts, utc
from .features import (
    CATEGORICAL_FEATURES, FEATURE_DEFINITION_VERSION, NUMERIC_FEATURES,
)


TRANSFORMER_DEFINITION_VERSION = "m3b1-transform-v1"
_METADATA = ("tenant_id", "trip_id", "snapshot_id", "snapshot_time",
             "feature_definition_version")


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _timestamp(value, name):
    try:
        return parse_ts(value)
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be timezone-aware") from exc


def _finite(value, name, *, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
        raise ValueError(f"{name} must be a finite number")
    try:
        answer = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(answer) or (nonnegative and answer < 0):
        raise ValueError(f"{name} must be finite" + (" and nonnegative" if nonnegative else ""))
    return answer


def _count(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < int(positive):
        raise ValueError(f"{name} must be a {'positive' if positive else 'nonnegative'} integer")
    return value


def _pairs(values, tenant, name, *, require_nonempty=True):
    if isinstance(values, (str, bytes, Mapping)):
        raise ValueError(f"{name} must contain tenant/identifier pairs")
    try:
        materialized = list(values)
    except TypeError as exc:
        raise ValueError(f"{name} must contain tenant/identifier pairs") from exc
    pairs = []
    for value in materialized:
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise ValueError(f"{name} must contain tenant/identifier pairs")
        pair = tuple(_identifier(item, name) for item in value)
        if pair[0] != tenant:
            raise ValueError(f"{name} cannot contain another tenant")
        pairs.append(pair)
    if len(pairs) != len(set(pairs)) or (require_nonempty and not pairs):
        raise ValueError(f"{name} must be nonempty and unique")
    return sorted(pairs)


def _rows(rows):
    """Validate contract-shaped values without inspecting extra payload fields."""
    if isinstance(rows, (str, bytes, Mapping)):
        raise ValueError("rows must be an iterable of feature mappings")
    try:
        iterator = iter(rows)
    except TypeError as exc:
        raise ValueError("rows must be an iterable of feature mappings") from exc
    result, seen = [], set()
    for row in iterator:
        if not isinstance(row, Mapping):
            raise ValueError("Each feature row must be a mapping")
        meta = {name: _identifier(row.get(name), name) for name in _METADATA}
        at = _timestamp(meta["snapshot_time"], "snapshot_time")
        meta["snapshot_time"] = utc(at)
        if meta["feature_definition_version"] != FEATURE_DEFINITION_VERSION:
            raise ValueError("Unsupported feature definition")
        key = meta["tenant_id"], meta["snapshot_id"]
        if key in seen:
            raise ValueError("Duplicate tenant/snapshot feature key")
        seen.add(key)
        numeric, categorical = row.get("numeric"), row.get("categorical")
        if not isinstance(numeric, Mapping) or set(numeric) != set(NUMERIC_FEATURES):
            raise ValueError("Numeric columns must exactly match the feature allowlist")
        if not isinstance(categorical, Mapping) or set(categorical) != set(CATEGORICAL_FEATURES):
            raise ValueError("Categorical columns must exactly match the feature allowlist")
        numbers = {}
        for name in NUMERIC_FEATURES:
            value = numeric[name]
            if value is None and name in ("elapsed_trip_seconds", "route_distance_km"):
                raise ValueError(f"Essential feature {name} cannot be missing")
            number = None if value is None else _finite(value, name, nonnegative=True)
            if name == "route_distance_km" and number == 0:
                raise ValueError("route_distance_km must be positive")
            numbers[name] = number
        categories = {name: None if categorical[name] is None else
                      _identifier(categorical[name], name) for name in CATEGORICAL_FEATURES}
        if categories["position_validity"] not in ("valid", "low_confidence", "no_position"):
            raise ValueError("Unsupported position_validity")
        if categories["route_id"] is None:
            raise ValueError("route_id cannot be missing")
        if categories["position_validity"] != "valid" and any(
                numbers[name] is not None for name in ("remaining_distance_km", "gnss_speed_kmh")):
            raise ValueError("Stale or absent position must not supply current motion features")
        if categories["position_validity"] == "no_position" and numbers["position_age_seconds"] is not None:
            raise ValueError("Absent position must not have a position age")
        result.append(dict(meta, numeric=numbers, categorical=categories))
    return sorted(result, key=lambda row: (row["tenant_id"], row["trip_id"],
                                          row["snapshot_time"], row["snapshot_id"]))


def _feature_names(categorical):
    names = []
    for field in NUMERIC_FEATURES:
        names.extend((f"numeric:{field}", f"missing:{field}"))
    for field in CATEGORICAL_FEATURES:
        names.extend((f"category:{field}:missing", f"category:{field}:unknown"))
        names.extend(f"category:{field}:value:{index}" for index in
                     range(len(categorical[field]["vocabulary"])))
    return names


def validate_transformer(state):
    """Reject malformed persisted state before transforming any observation.

    Schema validation detects accidental corruption, not malicious modification.
    Published state must additionally be bound to its recorded content hash.
    """
    if not isinstance(state, Mapping):
        raise ValueError("Transformer state must be a mapping")
    if state.get("transformer_definition_version") != TRANSFORMER_DEFINITION_VERSION:
        raise ValueError("Unsupported transformer definition")
    if state.get("feature_definition_version") != FEATURE_DEFINITION_VERSION:
        raise ValueError("Unsupported transformer feature definition")
    tenant = _identifier(state.get("tenant_id"), "tenant_id")
    _identifier(state.get("fold_id"), "fold_id")
    _timestamp(state.get("training_cutoff_at"), "training_cutoff_at")
    fit_keys = _pairs(state.get("fit_trip_keys"), tenant, "fit_trip_keys")
    fitted_keys = _pairs(state.get("fitted_trip_keys"), tenant, "fitted_trip_keys")
    if not set(fitted_keys) <= set(fit_keys):
        raise ValueError("Fitted trips must be declared fit trips")
    snapshots = _pairs(state.get("fitted_snapshot_keys"), tenant, "fitted_snapshot_keys")
    size = _count(state.get("fitted_rows"), "fitted_rows", positive=True)
    if size != len(snapshots) or len(fitted_keys) > size:
        raise ValueError("Fitted row and key counts disagree")
    numeric, categorical = state.get("numeric"), state.get("categorical")
    if not isinstance(numeric, Mapping) or set(numeric) != set(NUMERIC_FEATURES):
        raise ValueError("Transformer numeric columns disagree with the feature contract")
    if not isinstance(categorical, Mapping) or set(categorical) != set(CATEGORICAL_FEATURES):
        raise ValueError("Transformer categorical columns disagree with the feature contract")
    for field in NUMERIC_FEATURES:
        item = numeric[field]
        if not isinstance(item, Mapping):
            raise ValueError("Numeric transformer parameters must be mappings")
        _finite(item.get("median"), field+".median", nonnegative=True)
        _finite(item.get("center"), field+".center", nonnegative=True)
        if _finite(item.get("scale"), field+".scale") <= 0:
            raise ValueError("Numeric scale must be positive")
        observed = _count(item.get("observed_count"), field+".observed_count")
        missing = _count(item.get("missing_count"), field+".missing_count")
        if observed + missing != size:
            raise ValueError("Numeric fitted counts disagree")
        if field in ("elapsed_trip_seconds", "route_distance_km") and missing:
            raise ValueError("Essential features cannot be missing in fitted state")
        if observed == 0 and (item["median"] != 0 or item["center"] != 0 or item["scale"] != 1):
            raise ValueError("An all-missing fit feature requires the declared zero fallback")
    for field in CATEGORICAL_FEATURES:
        item = categorical[field]
        vocabulary = item.get("vocabulary") if isinstance(item, Mapping) else None
        if not isinstance(vocabulary, list):
            raise ValueError("Categorical vocabulary must be a list")
        for value in vocabulary:
            _identifier(value, field+".vocabulary")
        if vocabulary != sorted(set(vocabulary)):
            raise ValueError("Categorical vocabulary must be unique and sorted")
        if len(vocabulary) > size:
            raise ValueError("Categorical vocabulary exceeds fitted row count")
    if state.get("feature_names") != _feature_names(categorical):
        raise ValueError("Transformer output layout disagrees with its vocabulary")
    return state


def fit_transformer(rows, *, tenant_id, fold_id, training_cutoff_at, fit_trip_keys):
    """Fit one tenant/fold state; no rows outside the supplied fit groups enter.

    All historical fit opportunities may inform this unsupervised transform,
    independent of target maturity. Model fitting/target eligibility is a later
    stage. Fit snapshots have equal weight for preprocessing statistics.
    """
    tenant = _identifier(tenant_id, "tenant_id")
    fold = _identifier(fold_id, "fold_id")
    cutoff = _timestamp(training_cutoff_at, "training_cutoff_at")
    fit_keys = _pairs(fit_trip_keys, tenant, "fit_trip_keys")
    allowed = set(fit_keys)
    clean = _rows(rows)
    if not clean:
        raise ValueError("At least one fit feature row is required")
    for row in clean:
        if row["tenant_id"] != tenant or (tenant, row["trip_id"]) not in allowed:
            raise ValueError("Fit row is outside the declared tenant/trip groups")
        if _timestamp(row["snapshot_time"], "snapshot_time") >= cutoff:
            raise ValueError("Fit snapshot must be strictly before its training cutoff")
    numeric, categorical = {}, {}
    for field in NUMERIC_FEATURES:
        observed = sorted(row["numeric"][field] for row in clean if row["numeric"][field] is not None)
        fill = float(median(observed)) if observed else 0.0
        filled = [fill if row["numeric"][field] is None else row["numeric"][field] for row in clean]
        try:
            center, deviation = fmean(filled), pstdev(filled)
        except (OverflowError, ValueError) as exc:
            raise ValueError(f"Numeric fit statistics overflow for {field}") from exc
        scale = deviation if deviation > 0 else 1.0
        if not all(math.isfinite(value) for value in (fill, center, scale)):
            raise ValueError(f"Numeric fit statistics are not finite for {field}")
        numeric[field] = dict(median=fill, center=center, scale=scale,
                              observed_count=len(observed), missing_count=len(clean)-len(observed))
    for field in CATEGORICAL_FEATURES:
        categorical[field] = {"vocabulary": sorted({row["categorical"][field] for row in clean
                                                   if row["categorical"][field] is not None})}
    state = dict(
        transformer_definition_version=TRANSFORMER_DEFINITION_VERSION,
        feature_definition_version=FEATURE_DEFINITION_VERSION,
        tenant_id=tenant, fold_id=fold, training_cutoff_at=utc(cutoff),
        fit_trip_keys=[list(key) for key in fit_keys],
        fitted_trip_keys=[list(key) for key in sorted({(tenant, row["trip_id"]) for row in clean})],
        fitted_snapshot_keys=[list(key) for key in sorted((tenant, row["snapshot_id"]) for row in clean)],
        fitted_rows=len(clean), numeric=numeric, categorical=categorical,
        feature_names=_feature_names(categorical),
    )
    validate_transformer(state)
    return state


def transform_rows(rows, state, *, role):
    """Apply a frozen state without fitting or dropping prediction opportunities.

    Validation groups cannot overlap even a declared fit trip with no fitted
    snapshot. Validation clocks are at/after the frozen fit cutoff; fit clocks
    are strictly earlier. Missing positions remain rows, not exclusions.
    """
    validate_transformer(state)
    if role not in ("fit", "validation"):
        raise ValueError("role must be fit or validation")
    tenant = state["tenant_id"]
    fit_keys = {tuple(key) for key in state["fit_trip_keys"]}
    cutoff = _timestamp(state["training_cutoff_at"], "training_cutoff_at")
    clean = _rows(rows)
    result = []
    for row in clean:
        key = row["tenant_id"], row["trip_id"]
        at = _timestamp(row["snapshot_time"], "snapshot_time")
        if row["tenant_id"] != tenant:
            raise ValueError("Transformer cannot cross tenant boundaries")
        if role == "fit" and (key not in fit_keys or at >= cutoff):
            raise ValueError("Fit transform row is outside fit groups or cutoff")
        if role == "validation" and (key in fit_keys or at < cutoff):
            raise ValueError("Validation transform row overlaps fit groups or precedes cutoff")
        vector, missing, unknown = [], [], []
        for field in NUMERIC_FEATURES:
            parameters = state["numeric"][field]
            value = row["numeric"][field]
            is_missing = value is None
            if is_missing:
                value = parameters["median"]
                missing.append(field)
            transformed = (value-parameters["center"])/parameters["scale"]
            if not math.isfinite(transformed):
                raise ValueError(f"Transformed numeric value is not finite: {field}")
            vector.extend((transformed, float(is_missing)))
        for field in CATEGORICAL_FEATURES:
            vocabulary = state["categorical"][field]["vocabulary"]
            value = row["categorical"][field]
            encoded = [0.0]*(len(vocabulary)+2)
            if value is None:
                encoded[0] = 1.0
            elif value not in vocabulary:
                encoded[1] = 1.0
                unknown.append(field)
            else:
                encoded[vocabulary.index(value)+2] = 1.0
            vector.extend(encoded)
        result.append(dict({field: row[field] for field in _METADATA},
                           fold_id=state["fold_id"], fold_role=role, vector=vector,
                           missing_numeric_features=missing,
                           unknown_categorical_features=unknown))
    return result
