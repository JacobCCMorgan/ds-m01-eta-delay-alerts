"""Trip-weighted comparison of frozen, training-period ETA predictions."""

from collections import Counter, defaultdict
import math
import random

from .baseline_metrics import summarize


BASELINE = 'historical_baseline'
COHORTS = ('native', 'common_native', 'delivered', 'baseline_anchor')
SUPPLEMENTAL_STRATA = ('route_id', 'departure_period_local', 'elapsed_slice')
SOURCES = ('native', 'baseline_fallback', 'unavailable')


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _check_rows(rows, methods):
    snapshots, trip_folds = set(), {}
    for row in rows:
        for field in ('fold_id', 'tenant_id', 'trip_id', 'snapshot_id', 'label_status'):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f'Missing comparison field: {field}')
        snapshot = (row['tenant_id'], row['snapshot_id'])
        trip = (row['tenant_id'], row['trip_id'])
        if snapshot in snapshots or trip_folds.setdefault(trip, row['fold_id']) != row['fold_id']:
            raise ValueError('Duplicate snapshot or validation trip in multiple folds')
        snapshots.add(snapshot)
        if type(row.get('eligible')) is not bool or row['eligible'] != (row['label_status'] == 'eligible'):
            raise ValueError('Inconsistent target eligibility')
        target = row.get('target_seconds')
        if (row['eligible'] and not _positive(target)) or (not row['eligible'] and target is not None):
            raise ValueError('Target must be positive only on eligible opportunities')
        forecasts = row['predictions']
        if set(forecasts) != set(methods):
            raise ValueError('Every method must account for every opportunity')
        for name, forecast in forecasts.items():
            source, values = forecast['source'], forecast['quantiles']
            if source not in SOURCES:
                raise ValueError('Unknown prediction source')
            if source == 'unavailable':
                if values is not None:
                    raise ValueError('Unavailable prediction has quantiles')
                continue
            if not isinstance(values, (tuple, list)) or len(values) != 3 or not all(_positive(v) for v in values):
                raise ValueError('Available quantiles must be three positive finite numbers')
            if not values[0] <= values[1] <= values[2]:
                raise ValueError('Quantiles must be ordered')
            if source == 'baseline_fallback' and (name == BASELINE or forecasts[BASELINE]['source'] != 'native'
                                                   or tuple(values) != tuple(forecasts[BASELINE]['quantiles'])):
                raise ValueError('Fallback must reproduce the available historical baseline')


def _in_cohort(row, method, cohort):
    if not row['eligible']:
        return False
    forecasts = row['predictions']
    if cohort == 'common_native':
        return all(f['source'] == 'native' for f in forecasts.values())
    if cohort == 'baseline_anchor':
        return forecasts[BASELINE]['source'] == 'native'
    if cohort == 'native':
        return forecasts[method]['source'] == 'native'
    return forecasts[method]['source'] != 'unavailable'


def _score(rows, method, cohort):
    # Reuse the independently tested baseline metric implementation. Its input
    # target accepts fractional seconds; the previous integer cast was upstream.
    scored = []
    requested = 0
    for row in rows:
        if not _in_cohort(row, method, cohort):
            continue
        requested += 1
        values = row['predictions'][method]['quantiles']
        if values is None:
            continue
        scored.append({**{k: row[k] for k in ('tenant_id', 'trip_id', 'snapshot_id', 'fold_id')},
                       'label_eligibility': 'eligible', 'remaining_duration_seconds': row['target_seconds'],
                       'status': 'available', **dict(zip(('p10_remaining_seconds', 'p50_remaining_seconds',
                                                          'p90_remaining_seconds'), values))})
    # A partially delivered fixed anchor cannot improve its scores by dropping rows.
    report = summarize(scored if len(scored) == requested else [], bootstrap_samples=0)['metrics']
    def seconds(value):
        return None if value is None else value * 60.0
    losses = [seconds(report['quantile_pinball_minutes'][p]) for p in ('0.1', '0.5', '0.9')]
    return {
        'cohort_snapshots': requested, 'scored_snapshots': len(scored),
        'scored_trips': len({(r['tenant_id'], r['trip_id']) for r in scored}),
        'median_mae_seconds': seconds(report['MAE_minutes']), 'median_rmse_seconds': seconds(report['RMSE_minutes']),
        **dict(zip(('pinball_p10_seconds', 'pinball_p50_seconds', 'pinball_p90_seconds'), losses)),
        'mean_pinball_seconds': None if losses[0] is None else math.fsum(losses) / len(losses),
        'p10_p90_coverage': report['p10_p90_empirical_coverage'],
        'p10_p90_mean_width_seconds': seconds(report['mean_width_minutes']),
    }


def _availability(rows, method):
    counts = Counter(row['predictions'][method]['source'] for row in rows)
    eligible = [r for r in rows if r['eligible']]
    eligible_counts = Counter(r['predictions'][method]['source'] for r in eligible)
    return {
        'opportunities': len(rows), 'trips': len({(r['tenant_id'], r['trip_id']) for r in rows}),
        'eligible_opportunities': len(eligible),
        **{f'{source}_opportunities': counts[source] for source in SOURCES},
        **{f'{source}_eligible_opportunities': eligible_counts[source] for source in SOURCES},
        'delivered_fraction': (1 - counts['unavailable'] / len(rows)) if rows else None,
        'native_fraction': counts['native'] / len(rows) if rows else None,
    }


def _percentile(sorted_values, probability):
    offset = (len(sorted_values) - 1) * probability
    low, high = math.floor(offset), math.ceil(offset)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (offset - low)


def _paired_interval(anchor, candidate, settings):
    # Each trip has a single validation fold. Resampling paired differences
    # carries all of its snapshots and keeps the methods on identical draws.
    errors = defaultdict(list)
    for row in anchor:
        proposed = row['predictions'][candidate]['quantiles']
        if proposed is None:
            return None
        target = row['target_seconds']
        baseline = row['predictions'][BASELINE]['quantiles'][1]
        key = (row['fold_id'], row['tenant_id'], row['trip_id'])
        errors[key].append(abs(proposed[1] - target) - abs(baseline - target))
    if len(errors) < 2:
        return None
    by_fold = defaultdict(list)
    for key, values in sorted(errors.items()):
        by_fold[key[0]].append(math.fsum(values) / len(values))
    rng = random.Random(settings['seed'])
    samples = [math.fsum(v for group in by_fold.values() for v in rng.choices(group, k=len(group))) / len(errors)
               for _ in range(settings['replicates'])]
    samples.sort()
    low, high = (_percentile(samples, p) for p in settings['percentiles'])
    return {'lower_seconds': low, 'upper_seconds': high, 'trips': len(errors),
            'fold_trip_counts': {fold: len(group) for fold, group in by_fold.items()}}


def select_method(rows, overall, protocol):
    rules = protocol['selection']
    all_anchor = [r for r in rows if r['predictions'][BASELINE]['source'] == 'native']
    eligible_anchor = [r for r in all_anchor if r['eligible']]
    reference = overall[BASELINE]['cohorts']['baseline_anchor']
    baseline_mae = reference['median_mae_seconds']
    baseline_pinball = reference['mean_pinball_seconds']
    candidates = {}
    for method in (m for m in protocol['methods'] if m != BASELINE):
        estimate = overall[method]['cohorts']['baseline_anchor']
        mae, pinball = estimate['median_mae_seconds'], estimate['mean_pinball_seconds']
        sources = Counter(r['predictions'][method]['source'] for r in all_anchor)
        native_share = sources['native'] / len(all_anchor) if all_anchor else None
        gain = 1 - mae / baseline_mae if mae is not None and baseline_mae else None
        ratio = pinball / baseline_pinball if pinball is not None and baseline_pinball else None
        interval = _paired_interval(eligible_anchor, method, protocol['bootstrap'])
        gates = {
            'preserves_baseline_availability': bool(all_anchor) and sources['unavailable'] <= rules['max_missing_baseline_supported_opportunities'],
            'sufficient_native_coverage': native_share is not None and native_share >= rules['minimum_native_fraction_of_baseline_supported_opportunities'],
            'mae_reduction': gain is not None and gain >= rules['minimum_relative_anchor_mae_reduction'],
            'quantile_loss': ratio is not None and ratio <= rules['maximum_anchor_mean_pinball_ratio'],
            'paired_uncertainty': interval is not None and interval['upper_seconds'] < 0,
        }
        candidates[method] = {'eligible_for_promotion': all(gates.values()), 'gates': gates,
                              'baseline_supported_opportunities_lost': sources['unavailable'],
                              'native_fraction_on_all_baseline_supported': native_share,
                              'relative_anchor_mae_reduction': gain, 'anchor_pinball_ratio': ratio,
                              'candidate_minus_baseline_mae_interval': interval}
    qualifying = [m for m, result in candidates.items() if result['eligible_for_promotion']]
    chosen = BASELINE
    if qualifying:
        best = min(overall[m]['cohorts']['baseline_anchor']['median_mae_seconds'] for m in qualifying)
        threshold = best * (1 + rules['tie_relative_to_best_mae'])
        chosen = next(m for m in rules['simplicity_order'] if m in qualifying and
                      overall[m]['cohorts']['baseline_anchor']['median_mae_seconds'] <= threshold)
    return {'carry_forward_method': chosen, 'fallback_method': BASELINE, 'candidates': candidates,
            'baseline_supported_opportunities': len(all_anchor), 'baseline_supported_eligible_snapshots': len(eligible_anchor),
            'decision_scope': 'Training-period synthetic validation; carry forward to a separate calibration stage.'}


def _slice_value(row, field):
    if field == 'departure_period_local':
        bucket = row.get('departure_time_bucket')
        if not bucket:
            return 'unknown'
        try:
            hour = int(bucket.split('@')[0].split('-')[1])
            if not 0 <= hour <= 23:
                raise ValueError
        except (AttributeError, IndexError, ValueError) as exc:
            raise ValueError('Invalid local departure bucket') from exc
        start = (hour // 6) * 6
        return f'{start:02d}:00–{start + 5:02d}:59'
    return row.get(field) or 'unknown'


def compare(rows, protocol):
    methods = protocol['methods']
    _check_rows(rows, methods)
    rows = sorted(rows, key=lambda r: (r['fold_id'], r['tenant_id'], r['trip_id'], r['snapshot_id']))
    overall = {method: {'availability': _availability(rows, method),
                        'cohorts': {c: _score(rows, method, c) for c in COHORTS}} for method in methods}
    slices = []
    for field in (*protocol['strata'], *SUPPLEMENTAL_STRATA):
        groups = defaultdict(list)
        for row in rows:
            groups[_slice_value(row, field)].append(row)
        for group, members in sorted(groups.items()):
            for method in methods:
                availability = _availability(members, method)
                for cohort in COHORTS:
                    score = _score(members, method, cohort)
                    slices.append({'stratum': field, 'value': group, 'method': method, 'cohort': cohort,
                                   'sparse': score['scored_trips'] < protocol['sparse_below_scored_trips'],
                                   **availability, **score})
    metrics = {'protocol_id': protocol['protocol_id'], 'opportunities_per_method': len(rows),
               'label_status_counts': dict(sorted(Counter(row['label_status'] for row in rows).items())),
               'methods': overall}
    return metrics, slices, select_method(rows, overall, protocol)
