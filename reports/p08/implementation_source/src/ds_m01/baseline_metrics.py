"""Trip-balanced validation metrics for empirical remaining-time predictions.

Unknown targets and unsupported predictions remain visible in the counts. Error
metrics use only eligible, supported snapshots: snapshots are averaged within a
tenant/trip first, then each scored trip receives equal weight. Empirical P10/P90
coverage describes the raw interval and is not a calibration guarantee.
"""

from collections import Counter, defaultdict
from collections.abc import Mapping
from decimal import Decimal
import math
from numbers import Real
import random


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be a non-empty string')
    return value


def _positive_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
        raise ValueError(f'{name} must be a finite positive number')
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f'{name} must be a finite positive number')
    return number


def _mean(values):
    return math.fsum(values) / len(values)


def _quantile(sorted_values, probability):
    offset = (len(sorted_values) - 1) * probability
    lower, upper = math.floor(offset), math.ceil(offset)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (offset - lower)


def _empty_metrics():
    return {
        'MAE_minutes': None,
        'RMSE_minutes': None,
        'bias_minutes': None,
        'p10_p90_empirical_coverage': None,
        'mean_width_minutes': None,
        'quantile_pinball_minutes': {'0.1': None, '0.5': None, '0.9': None},
    }


def summarize(rows, bootstrap_samples=500, bootstrap_seed=20261028):
    """Summarize all opportunities and score supported, eligible targets only.

    Input rows require tenant_id, trip_id, snapshot_id, fold_id,
    label_eligibility, remaining_duration_seconds, and prediction status.
    Scored rows also require ordered positive P10/P50/P90 remaining seconds.
    Unsupported values are never substituted with zero. Ineligible targets must
    be null, so an unknown outcome cannot masquerade as an observed zero.

    RMSE is the square root of the mean of per-trip mean squared errors, not the
    average of per-trip RMSE values. MAE uncertainty is a percentile bootstrap
    of complete trips (their within-trip mean absolute errors), conditional on
    the fitted folds and observed supported/eligible sample. It does not include
    uncertainty from refitting the historical pools or generator misspecification.
    bootstrap_samples=0 explicitly disables the interval. Fewer than two scored
    trips always yields null interval bounds.
    """
    if isinstance(bootstrap_samples, bool) or not isinstance(bootstrap_samples, int) or bootstrap_samples < 0:
        raise ValueError('bootstrap_samples must be a non-negative integer')
    if isinstance(bootstrap_seed, bool) or not isinstance(bootstrap_seed, int):
        raise ValueError('bootstrap_seed must be an integer')

    label_counts, status_counts = Counter(), Counter()
    all_trips, eligible_trips = set(), set()
    trip_folds = {}
    snapshot_keys = set()
    scored = defaultdict(list)
    eligible_count = 0
    total_count = 0
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError('Each metric row must be a mapping')
        tenant = _identifier(row.get('tenant_id'), 'tenant_id')
        trip = _identifier(row.get('trip_id'), 'trip_id')
        snapshot = _identifier(row.get('snapshot_id'), 'snapshot_id')
        fold = _identifier(row.get('fold_id'), 'fold_id')
        label_status = _identifier(row.get('label_eligibility'), 'label_eligibility')
        prediction_status = row.get('status')
        if prediction_status not in ('available', 'unsupported'):
            raise ValueError('status must be available or unsupported')
        key = (tenant, trip)
        snapshot_key = (tenant, snapshot)
        if snapshot_key in snapshot_keys:
            raise ValueError(f'Duplicate validation snapshot: {tenant}/{snapshot}')
        snapshot_keys.add(snapshot_key)
        if key in trip_folds and trip_folds[key] != fold:
            raise ValueError(f'Validation trip appears in multiple folds: {tenant}/{trip}')
        trip_folds[key] = fold
        total_count += 1
        all_trips.add(key)
        label_counts[label_status] += 1
        status_counts[prediction_status] += 1
        if label_status != 'eligible':
            if row.get('remaining_duration_seconds') is not None:
                raise ValueError('Ineligible remaining_duration_seconds must be null')
            continue

        target = _positive_number(row.get('remaining_duration_seconds'), 'remaining_duration_seconds')
        eligible_count += 1
        eligible_trips.add(key)
        if prediction_status == 'unsupported':
            continue
        quantiles = [_positive_number(row.get(f'p{p}_remaining_seconds'), f'p{p}_remaining_seconds')
                     for p in (10, 50, 90)]
        if not quantiles[0] <= quantiles[1] <= quantiles[2]:
            raise ValueError('Prediction quantiles must satisfy P10 <= P50 <= P90')
        error = (quantiles[1] - target) / 60
        pinball = []
        for probability, prediction in zip((0.1, 0.5, 0.9), quantiles):
            residual = (target - prediction) / 60
            pinball.append(max(probability * residual, (probability - 1) * residual))
        scored[key].append((snapshot, {
            'absolute_error': abs(error),
            'squared_error': error * error,
            'bias': error,
            'coverage': float(quantiles[0] <= target <= quantiles[2]),
            'width': (quantiles[2] - quantiles[0]) / 60,
            'pinball_10': pinball[0], 'pinball_50': pinball[1], 'pinball_90': pinball[2],
        }))

    metrics = _empty_metrics()
    trip_metrics = []
    for key in sorted(scored):
        observations = [value for _, value in sorted(scored[key], key=lambda item: item[0])]
        trip_metrics.append({metric: _mean([observation[metric] for observation in observations])
                             for metric in observations[0]})
    if trip_metrics:
        average = {metric: _mean([trip[metric] for trip in trip_metrics]) for metric in trip_metrics[0]}
        metrics = {
            'MAE_minutes': average['absolute_error'],
            'RMSE_minutes': math.sqrt(average['squared_error']),
            'bias_minutes': average['bias'],
            'p10_p90_empirical_coverage': average['coverage'],
            'mean_width_minutes': average['width'],
            'quantile_pinball_minutes': {
                '0.1': average['pinball_10'], '0.5': average['pinball_50'], '0.9': average['pinball_90'],
            },
        }

    interval = {
        'lower_minutes': None, 'upper_minutes': None,
        'confidence_level': 0.95, 'method': 'trip_cluster_percentile',
        'trip_count': len(trip_metrics), 'bootstrap_samples': bootstrap_samples,
        'bootstrap_seed': bootstrap_seed, 'reason': None,
    }
    if len(trip_metrics) < 2:
        interval['reason'] = 'fewer_than_two_scored_trips'
    elif bootstrap_samples == 0:
        interval['reason'] = 'bootstrap_disabled'
    else:
        rng = random.Random(bootstrap_seed)
        trip_maes = [trip['absolute_error'] for trip in trip_metrics]
        draws = sorted(_mean(rng.choices(trip_maes, k=len(trip_maes))) for _ in range(bootstrap_samples))
        interval['lower_minutes'] = _quantile(draws, 0.025)
        interval['upper_minutes'] = _quantile(draws, 0.975)

    return {
        'counts': {
            'snapshots': total_count, 'trips': len(all_trips),
            'eligible_snapshots': eligible_count, 'eligible_trips': len(eligible_trips),
            'supported_eligible_snapshots': sum(len(values) for values in scored.values()),
            'supported_eligible_trips': len(scored),
        },
        'eligible_support_rate': (sum(len(values) for values in scored.values()) / eligible_count
                                  if eligible_count else None),
        'label_eligibility_counts': dict(sorted(label_counts.items())),
        'prediction_status_counts': dict(sorted(status_counts.items())),
        'metric_scope': 'eligible_and_supported_snapshots_only',
        'weighting': 'equal_scored_trip_weight_then_equal_snapshot_weight_within_trip',
        'bias_direction': 'positive_means_predicted_remaining_time_is_too_long',
        'metrics': metrics,
        'mae_bootstrap_95pct': interval,
    }
