"""Positive-list, point-in-time model features for the frozen M2 snapshots.

Labels, realized arrival events and simulator truth are deliberately not inputs.
Position metadata is checked before stale signals are masked. Snapshot production
and source-hash checks remain separate controls; this boundary does not recreate
the upstream device-assignment or status-event joins.
"""

from collections.abc import Mapping
from decimal import Decimal
import math
from numbers import Real

from .common import parse_ts, utc


FEATURE_DEFINITION_VERSION = 'm3b1-features-v1'
SOURCE_FEATURE_DEFINITION_VERSION = 'm1-features-v1'
NUMERIC_FEATURES = (
    'elapsed_trip_seconds', 'route_distance_km', 'remaining_distance_km',
    'gnss_speed_kmh', 'position_age_seconds',
)
CATEGORICAL_FEATURES = ('route_id', 'departure_time_bucket', 'position_validity')


def _identifier(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field} must be a nonempty string')
    return value


def _timestamp(value, field):
    try:
        return parse_ts(value)
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f'{field} must be a timezone-aware timestamp') from exc


def _number(value, field, *, optional=False, positive=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
        raise ValueError(f'{field} must be a finite numeric value')
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f'{field} must be a finite numeric value') from exc
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError(f'{field} must be finite and {"positive" if positive else "nonnegative"}')
    return result


def build_feature_row(snapshot, route, lineage, *, position_stale_seconds=900):
    """Project one snapshot to an explicit numeric/categorical model interface.

    The caller supplies the route version and lineage for this tenant/snapshot.
    Essential time/group/distance fields are mandatory. Missing optional speed,
    distance or departure bucket remains missing for fold-local preprocessing.
    Stale position speed and remaining distance are always masked; position age
    and quality remain available to describe the observation process.

    Extra source fields, including outcome/hidden-truth fields, are ignored and
    never copied to the result. Identifiers and clocks are audit metadata only.
    Inputs are not mutated. Malformed or inconsistent source contracts fail.
    """
    if not all(isinstance(row, Mapping) for row in (snapshot, route, lineage)):
        raise ValueError('snapshot, route and lineage must be mappings')
    stale_seconds = _number(position_stale_seconds, 'position_stale_seconds')
    tenant = _identifier(snapshot.get('tenant_id'), 'tenant_id')
    trip = _identifier(snapshot.get('trip_id'), 'trip_id')
    identifier = _identifier(snapshot.get('snapshot_id'), 'snapshot_id')
    version = _identifier(snapshot.get('route_version_id'), 'route_version_id')
    at = _timestamp(snapshot.get('snapshot_time'), 'snapshot_time')
    if snapshot.get('feature_definition_version') != SOURCE_FEATURE_DEFINITION_VERSION:
        raise ValueError('Unsupported source feature_definition_version')
    if (route.get('tenant_id') != tenant or route.get('route_version_id') != version):
        raise ValueError('Route tenant/version does not match snapshot')
    route_id = _identifier(route.get('route_id'), 'route.route_id')
    known = _timestamp(route.get('known_at'), 'route.known_at')
    valid_from = _timestamp(route.get('valid_from'), 'route.valid_from')
    valid_to = (_timestamp(route['valid_to'], 'route.valid_to')
                if route.get('valid_to') is not None else None)
    if (known > at or valid_from > at or (valid_to is not None and at >= valid_to)):
        raise ValueError('Route version was not known and valid at snapshot time')
    if valid_to is not None and valid_to <= valid_from:
        raise ValueError('Route validity interval must have positive duration')
    if lineage.get('tenant_id') != tenant or lineage.get('snapshot_id') != identifier:
        raise ValueError('Lineage tenant/snapshot does not match snapshot')
    max_available = _timestamp(lineage.get('max_source_available_time'),
                               'lineage.max_source_available_time')
    if max_available > at or max_available < known:
        raise ValueError('Feature lineage contains future or understated source availability')

    elapsed = _number(snapshot.get('elapsed_trip_seconds'), 'elapsed_trip_seconds')
    distance = _number(route.get('planned_distance_km'), 'route.planned_distance_km', positive=True)
    remaining = _number(snapshot.get('remaining_distance_km'), 'remaining_distance_km', optional=True)
    speed = _number(snapshot.get('gnss_speed_kmh'), 'gnss_speed_kmh', optional=True)
    age = _number(snapshot.get('position_age_seconds'), 'position_age_seconds', optional=True)
    quality = snapshot.get('position_validity')
    if quality not in ('valid', 'low_confidence', 'no_position'):
        raise ValueError('Unknown position_validity')
    if remaining is not None and remaining > distance + 0.001:
        raise ValueError('Remaining distance exceeds planned route distance')

    if quality == 'no_position':
        source_fields = ('selected_position_event_id', 'last_position_time',
                         'position_available_at', 'position_age_seconds',
                         'gnss_speed_kmh', 'remaining_distance_km')
        if any(snapshot.get(field) is not None for field in source_fields):
            raise ValueError('no_position requires absent position values and clocks')
    else:
        _identifier(snapshot.get('selected_position_event_id'), 'selected_position_event_id')
        event_at = _timestamp(snapshot.get('last_position_time'), 'last_position_time')
        available_at = _timestamp(snapshot.get('position_available_at'), 'position_available_at')
        if event_at > at or available_at > at or available_at < event_at:
            raise ValueError('Position event/availability clocks are inconsistent or future')
        if max_available < available_at:
            raise ValueError('Feature lineage understates position availability')
        actual_age = (at - event_at).total_seconds()
        if age is None or not math.isclose(age, actual_age, rel_tol=0, abs_tol=1e-6):
            raise ValueError('Position age must match snapshot minus position event time')
        if age > elapsed:
            raise ValueError('Position event precedes this trip departure')
        expected_quality = 'low_confidence' if age > stale_seconds else 'valid'
        if quality != expected_quality:
            raise ValueError('Position quality conflicts with configured stale threshold')
        if quality == 'low_confidence':
            remaining = speed = None

    bucket = snapshot.get('departure_time_bucket')
    if bucket is not None:
        bucket = _identifier(bucket, 'departure_time_bucket')
    return {
        'tenant_id': tenant, 'trip_id': trip, 'snapshot_id': identifier,
        'snapshot_time': utc(at), 'route_version_id': version,
        'feature_definition_version': FEATURE_DEFINITION_VERSION,
        'numeric': dict(zip(NUMERIC_FEATURES, (elapsed, distance, remaining, speed, age))),
        'categorical': dict(zip(CATEGORICAL_FEATURES, (route_id, bucket, quality))),
    }
