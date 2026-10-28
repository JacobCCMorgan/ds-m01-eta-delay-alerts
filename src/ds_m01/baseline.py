"""Point-in-time empirical remaining-duration baseline.

The caller supplies one verified, precision-eligible outcome per training trip,
selected at its frozen fit cutoff. This module additionally enforces tenant,
own-trip and availability guards. It never reads target labels, GPS fields or
synthetic scenario truth. P10/P90 are raw empirical quantiles, not calibrated
coverage guarantees or a delay probability.

The five-trip minimum and 50-km distance bands are declared engineering defaults,
not thresholds selected on the calibration or final-test periods.
"""

from collections.abc import Mapping
from decimal import Decimal
import math
from numbers import Real

from .common import parse_ts


def _number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
        raise ValueError(f'{name} must be a finite numeric value')
    result = float(value)
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        bound = 'positive' if positive else 'non-negative'
        raise ValueError(f'{name} must be finite and {bound}')
    return result


def _timestamp(value, name):
    try:
        return parse_ts(value)
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f'{name} must be a timezone-aware timestamp') from exc


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be a non-empty string')
    return value


def _optional_identifier(row, name):
    value = row.get(name)
    return None if value is None else _identifier(value, name)


def _quantile(sorted_values, probability):
    """Linear interpolation at (n - 1) * p; input is nonempty and sorted."""
    offset = (len(sorted_values) - 1) * probability
    lower = math.floor(offset)
    upper = math.ceil(offset)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (offset - lower)


def _result(reason, *, pool=None, candidates=0, survivors=0, values=None,
            diagnostics=None, missing=None):
    return {
        'status': 'available' if values is not None else 'unsupported',
        'pool': pool,
        'candidate_count': candidates,
        'surviving_count': survivors,
        'p10_remaining_seconds': _quantile(values, 0.1) if values is not None else None,
        'p50_remaining_seconds': _quantile(values, 0.5) if values is not None else None,
        'p90_remaining_seconds': _quantile(values, 0.9) if values is not None else None,
        'reason': reason,
        'pool_diagnostics': diagnostics or [],
        'missing_snapshot_fields': missing or [],
    }


def predict(snapshot, history, training_cutoff_at, min_support=5, distance_bin_km=50):
    """Predict remaining seconds from strictly prior, tenant-local trip durations.

    ``snapshot`` requires tenant_id, trip_id, snapshot_time and
    elapsed_trip_seconds. Optional route_id, departure_time_bucket and
    route_distance_km enable narrower pools. ``history`` records require
    tenant_id, trip_id, duration_seconds, label_known_at and actual_arrival_time,
    with the same optional grouping fields. A caller may add snapshot_id and
    other audit fields; this function does not inspect them.

    Pool order is exact route plus departure bucket, 50-km planned-route-distance
    band, then tenant-global. Each pool must contain ``min_support`` distinct
    historical trips with total duration strictly greater than elapsed time.
    Their remaining durations are total duration minus elapsed time. Missing or
    stale positioning alone does not block this route-and-elapsed baseline.

    Missing essential snapshot inputs produce an explicit unsupported result.
    Malformed numeric values, timestamps, identifiers and duplicate eligible
    historical trip versions raise ValueError instead of silently becoming data.
    Inputs are never mutated.
    """
    if not isinstance(snapshot, Mapping):
        raise ValueError('snapshot must be a mapping')
    if isinstance(min_support, bool) or not isinstance(min_support, int) or min_support < 1:
        raise ValueError('min_support must be a positive integer')
    width = _number(distance_bin_km, 'distance_bin_km', positive=True)
    cutoff = _timestamp(training_cutoff_at, 'training_cutoff_at')
    required = ('tenant_id', 'trip_id', 'snapshot_time', 'elapsed_trip_seconds')
    missing = [field for field in required if snapshot.get(field) is None]
    if missing:
        return _result('missing_snapshot_inputs', missing=missing)
    tenant = _identifier(snapshot['tenant_id'], 'tenant_id')
    trip = _identifier(snapshot['trip_id'], 'trip_id')
    at = _timestamp(snapshot['snapshot_time'], 'snapshot_time')
    elapsed = _number(snapshot['elapsed_trip_seconds'], 'elapsed_trip_seconds')
    limit = min(cutoff, at)
    route = _optional_identifier(snapshot, 'route_id')
    bucket = _optional_identifier(snapshot, 'departure_time_bucket')
    distance = snapshot.get('route_distance_km')
    band = None if distance is None else math.floor(_number(distance, 'route_distance_km') / width)

    eligible = []
    seen = set()
    for row in history:
        if not isinstance(row, Mapping):
            raise ValueError('history records must be mappings')
        other_tenant = _identifier(row.get('tenant_id'), 'history.tenant_id')
        other_trip = _identifier(row.get('trip_id'), 'history.trip_id')
        if other_tenant != tenant or other_trip == trip:
            continue
        known = _timestamp(row.get('label_known_at'), 'history.label_known_at')
        arrival = _timestamp(row.get('actual_arrival_time'), 'history.actual_arrival_time')
        if known >= limit or arrival >= limit:
            continue
        key = (other_tenant, other_trip)
        if key in seen:
            raise ValueError(f'Duplicate eligible historical trip: {other_tenant}/{other_trip}')
        seen.add(key)
        duration = _number(row.get('duration_seconds'), 'history.duration_seconds', positive=True)
        other_route = _optional_identifier(row, 'route_id')
        other_bucket = _optional_identifier(row, 'departure_time_bucket')
        other_distance = row.get('route_distance_km')
        other_band = None if other_distance is None else math.floor(
            _number(other_distance, 'history.route_distance_km') / width)
        eligible.append((duration, other_route, other_bucket, other_band))

    pools = [
        ('route_departure', route is not None and bucket is not None,
         [r[0] for r in eligible if r[1] == route and r[2] == bucket]),
        ('distance_band', band is not None,
         [r[0] for r in eligible if r[3] == band]),
        ('tenant_global', True, [r[0] for r in eligible]),
    ]
    diagnostics = []
    selected = None
    for name, applicable, durations in pools:
        surviving = sorted(duration - elapsed for duration in durations if duration > elapsed) if applicable else []
        diagnostics.append({
            'pool': name, 'applicable': applicable,
            'candidate_count': len(durations) if applicable else 0,
            'surviving_count': len(surviving),
        })
        if selected is None and applicable and len(surviving) >= min_support:
            selected = (name, len(durations), surviving)
    if selected is not None:
        name, count, remaining = selected
        reason = 'route_departure_supported' if name == 'route_departure' else f'fallback_{name}'
        return _result(reason, pool=name, candidates=count, survivors=len(remaining),
                       values=remaining, diagnostics=diagnostics)

    last = diagnostics[-1]
    reason = ('no_eligible_history' if last['candidate_count'] == 0 else
              'no_surviving_history' if last['surviving_count'] == 0 else
              'insufficient_surviving_history')
    return _result(reason, candidates=last['candidate_count'], survivors=last['surviving_count'],
                   diagnostics=diagnostics)
