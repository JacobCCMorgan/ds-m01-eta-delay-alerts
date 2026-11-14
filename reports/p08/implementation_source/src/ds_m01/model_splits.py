"""Training-only chronological validation, preserving the frozen M2 trip split.

This module assigns whole tenant/trip groups from trip metadata only. It never
reads labels or feature rows. Departure date ranges are inclusive in the
configured local timezone; all label cutoffs are exclusive UTC instants.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .common import parse_ts, utc


def default_folds():
    """Return four expanding folds fixed before observing model scores."""
    specs = [
        ("F01", "2026-02-21", "2026-03-01T00:00:00-08:00",
         "2026-03-01", "2026-03-24", "2026-04-01T00:00:00-07:00"),
        ("F02", "2026-03-24", "2026-04-01T00:00:00-07:00",
         "2026-04-01", "2026-04-23", "2026-05-01T00:00:00-07:00"),
        ("F03", "2026-04-23", "2026-05-01T00:00:00-07:00",
         "2026-05-01", "2026-05-24", "2026-06-01T00:00:00-07:00"),
        ("F04", "2026-05-24", "2026-06-01T00:00:00-07:00",
         "2026-06-01", "2026-06-23", "2026-07-01T00:00:00-07:00"),
    ]
    return [dict(fold_id=identifier,
                 train_departure_dates=["2026-01-01", train_end],
                 training_cutoff_at=fit_cutoff,
                 validation_departure_dates=[validation_start, validation_end],
                 validation_label_cutoff_at=evaluation_cutoff)
            for identifier, train_end, fit_cutoff, validation_start,
            validation_end, evaluation_cutoff in specs]


def _identifier(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _date_range(value, field):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{field} must contain two inclusive ISO dates")
    try:
        start, end = (date.fromisoformat(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid ISO date in {field}") from exc
    if [start.isoformat(), end.isoformat()] != list(value) or start > end:
        raise ValueError(f"{field} requires ordered YYYY-MM-DD dates")
    return start, end


def _timestamp(value, field):
    try:
        return parse_ts(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} requires a timezone-aware timestamp") from exc


def validate_folds(folds, global_config):
    """Validate expanding folds against M2 and return normalized copies.

    Evaluation windows cannot overlap one another. Later fit windows can include
    earlier validation trips, as expected for expanding chronological validation.
    Every fit and validation departure remains inside the original train period;
    their label cutoffs cannot exceed the original train label cutoff.
    """
    if not isinstance(folds, (list, tuple)) or not folds:
        raise ValueError("At least one validation fold is required")
    try:
        zone = ZoneInfo(global_config["timezone"])
        split = global_config["split"]
        global_start, global_end = _date_range(
            split["train_departure_dates"], "train_departure_dates")
        global_cutoff = _timestamp(
            split["train_label_cutoff_exclusive"], "train_label_cutoff_exclusive")
    except (KeyError, TypeError) as exc:
        raise ValueError("Complete M2 timezone and train split settings are required") from exc
    if split.get("group_by") != ["tenant_id", "trip_id"]:
        raise ValueError("M2 grouping must remain tenant_id, trip_id")
    next_midnight = lambda day: datetime.combine(day + timedelta(days=1), time(), zone)
    if global_cutoff < next_midnight(global_end):
        raise ValueError("M2 train label cutoff precedes the end of its departures")
    result, seen = [], set()
    previous_fit_end = previous_validation_end = previous_fit_cutoff = None
    for fold in folds:
        try:
            identifier = _identifier(fold["fold_id"], "fold_id")
            fit_start, fit_end = _date_range(fold["train_departure_dates"], "fold train_departure_dates")
            val_start, val_end = _date_range(fold["validation_departure_dates"], "validation_departure_dates")
            fit_cutoff = _timestamp(fold["training_cutoff_at"], "training_cutoff_at")
            val_cutoff = _timestamp(fold["validation_label_cutoff_at"], "validation_label_cutoff_at")
        except (KeyError, TypeError) as exc:
            raise ValueError("Each fold requires an ID, date windows and cutoffs") from exc
        if identifier in seen:
            raise ValueError("Duplicate fold_id")
        seen.add(identifier)
        if not (fit_start == global_start <= fit_end < val_start <= val_end <= global_end):
            raise ValueError("Fold windows must expand from M2 start and stay within M2 training dates")
        val_midnight = datetime.combine(val_start, time(), zone)
        if not (next_midnight(fit_end) <= fit_cutoff <= val_midnight):
            raise ValueError("Fit cutoff must follow fit departures and not exceed validation start")
        if not (next_midnight(val_end) <= val_cutoff <= global_cutoff):
            raise ValueError("Validation label cutoff must follow validation departures and remain within M2 cutoff")
        if previous_fit_end is not None:
            if fit_end <= previous_fit_end or fit_cutoff <= previous_fit_cutoff:
                raise ValueError("Fit windows and cutoffs must expand chronologically")
            if val_start <= previous_validation_end:
                raise ValueError("Validation windows must be chronological and disjoint")
        previous_fit_end, previous_validation_end = fit_end, val_end
        previous_fit_cutoff = fit_cutoff
        result.append(dict(
            fold_id=identifier,
            train_departure_dates=[fit_start.isoformat(), fit_end.isoformat()],
            training_cutoff_at=utc(fit_cutoff),
            validation_departure_dates=[val_start.isoformat(), val_end.isoformat()],
            validation_label_cutoff_at=utc(val_cutoff),
        ))
    return result


def build_fold_memberships(trips, folds, global_config):
    """Assign unique original-training trips to fit/validation roles per fold.

    Trip input requires tenant_id, trip_id, departure_time and split_role.
    Optional vehicle_id, route_id, unseen_vehicle and unseen_route provide
    additional assertions against accidental reassignment of frozen holdouts.
    Original calibration, test and excluded groups never receive a fold role.
    One output row is emitted per assigned fold and tenant/trip group, sorted
    deterministically. Unassigned dates (including buffers) are simply absent.
    """
    normalized = validate_folds(folds, global_config)
    zone = ZoneInfo(global_config["timezone"])
    train_start, train_end = global_config["split"]["train_departure_dates"]
    heldout = global_config.get("heldout", {})
    seen, training_trips = set(), []
    for trip in trips:
        try:
            tenant = _identifier(trip["tenant_id"], "tenant_id")
            identifier = _identifier(trip["trip_id"], "trip_id")
            departure = _timestamp(trip["departure_time"], "departure_time")
            role = trip["split_role"]
        except (KeyError, TypeError) as exc:
            raise ValueError("Trip metadata is incomplete") from exc
        key = (tenant, identifier)
        if key in seen:
            raise ValueError(f"Duplicate tenant/trip group: {key}")
        seen.add(key)
        if role not in ("train", "calibration", "test", "excluded"):
            raise ValueError(f"Unknown original split role: {role}")
        if role != "train":
            continue
        local_date = departure.astimezone(zone).date().isoformat()
        if not train_start <= local_date <= train_end:
            raise ValueError("Original-training trip is outside M2 training dates")
        if (trip.get("unseen_vehicle") or trip.get("unseen_route")
                or trip.get("vehicle_id") in heldout.get("vehicle_ids", [])
                or trip.get("route_id") in heldout.get("route_ids", [])):
            raise ValueError("Frozen heldout entities cannot enter training folds")
        training_trips.append((tenant, identifier, local_date))
    result = []
    for fold in normalized:
        for tenant, identifier, local_date in sorted(training_trips):
            fit_start, fit_end = fold["train_departure_dates"]
            val_start, val_end = fold["validation_departure_dates"]
            if fit_start <= local_date <= fit_end:
                role, cutoff = "fit", fold["training_cutoff_at"]
            elif val_start <= local_date <= val_end:
                role, cutoff = "validation", fold["validation_label_cutoff_at"]
            else:
                continue
            result.append(dict(
                fold_id=fold["fold_id"], tenant_id=tenant, trip_id=identifier,
                departure_date=local_date, original_split_role="train",
                fold_role=role, training_cutoff_at=fold["training_cutoff_at"],
                label_cutoff_at=cutoff,
            ))
    return result
