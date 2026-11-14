"""Paired July calibration diagnostics with visible opportunity denominators.

Expanded interval endpoints are not quantile estimates. Trip-wise simultaneous
coverage includes missing forecasts as misses; marginal error/width is conditional
on supported, eligible snapshots with one unit of weight per scored trip.
"""
from collections import Counter, defaultdict
import math

from .delay_metrics import summarize as probability_summary

VARIANTS = ('raw', 'calibrated')
ROLES = ('assessment',)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _key(row):
    return row['tenant_id'], row['snapshot_id']


def _trip(row):
    return row['tenant_id'], row['trip_id']


def summarize_intervals(rows):
    """Assess one variant, retaining unsupported forecasts and excluded labels."""
    rows = list(rows)
    seen = set()
    for r in rows:
        if _key(r) in seen:
            raise ValueError('Duplicate interval opportunity')
        seen.add(_key(r))
        if r['eligibility'] not in ('eligible', 'excluded'):
            raise ValueError('Unknown interval label eligibility')
        y = r.get('target_seconds')
        if (r['eligibility'] == 'eligible' and (not _finite(y) or y <= 0)) or (r['eligibility'] == 'excluded' and y is not None):
            raise ValueError('Interval target must be positive only when eligible')
        bounds = [r.get(k) for k in ('lower_seconds', 'median_seconds', 'upper_seconds')]
        if r['status'] == 'available':
            if not all(_finite(v) for v in bounds) or not 0 <= bounds[0] <= bounds[1] <= bounds[2]:
                raise ValueError('Available interval must be finite, nonnegative and ordered')
        elif r['status'] != 'unavailable' or any(v is not None for v in bounds):
            raise ValueError('Unavailable interval requires null values')
    eligible = [r for r in rows if r['eligibility'] == 'eligible']
    scored = [r for r in eligible if r['status'] == 'available']
    n = Counter(_trip(r) for r in scored)
    w = [(r, 1 / n[_trip(r)]) for r in scored]
    denominator = len(n)
    def average(fn):
        return math.fsum(weight * fn(r) for r, weight in w) / denominator if denominator else None
    eligible_trips = defaultdict(list)
    for r in eligible:
        eligible_trips[_trip(r)].append(r)
    fully_available = {k: v for k, v in eligible_trips.items() if all(r['status'] == 'available' for r in v)}
    covered_trips = sum(all(r['lower_seconds'] <= r['target_seconds'] <= r['upper_seconds'] for r in v) for v in fully_available.values())
    def score(r):
        lo, hi, y = r['lower_seconds'], r['upper_seconds'], r['target_seconds']
        return hi - lo + 10 * max(lo - y, 0) + 10 * max(y - hi, 0)
    return {
        'counts': {'opportunities': len(rows), 'trips': len({_trip(r) for r in rows}),
                   'eligible_snapshots': len(eligible), 'eligible_trips': len(eligible_trips),
                   'excluded_snapshots': len(rows) - len(eligible),
                   'available_snapshots': sum(r['status'] == 'available' for r in rows),
                   'scored_snapshots': len(scored), 'scored_trips': len(n),
                   'unavailable_eligible_snapshots': len(eligible) - len(scored),
                   'fully_available_eligible_trips': len(fully_available),
                   'all_eligible_snapshots_covered_trips': covered_trips,
                   'unsupported_calibration_opportunities': sum(r.get('calibration_status') == 'unsupported' for r in rows),
                   'calibration_applied_snapshots': sum(bool(r.get('calibration_applied')) for r in rows)},
        'metrics': {'trip_weighted_snapshot_coverage': average(lambda r: float(r['lower_seconds'] <= r['target_seconds'] <= r['upper_seconds'])),
                    'mean_width_seconds': average(lambda r: r['upper_seconds'] - r['lower_seconds']),
                    'median_mae_seconds': average(lambda r: abs(r['median_seconds'] - r['target_seconds'])),
                    'mean_interval_score_seconds': average(score),
                    'trip_all_eligible_snapshots_coverage': covered_trips / len(eligible_trips) if eligible_trips else None,
                    'trip_all_snapshots_coverage_given_full_availability': covered_trips / len(fully_available) if fully_available else None},
        'exclusion_reason_counts': dict(sorted(Counter(str(r.get('reason')) for r in rows if r['eligibility'] == 'excluded').items())),
        'prediction_source_counts': dict(sorted(Counter(r['prediction_source'] for r in rows).items())),
        'calibration_reason_counts': dict(sorted(Counter(str(r.get('calibration_reason')) for r in rows).items())),
        'nominal_coverage': 0.8, 'sparse': len(n) < 20,
        'weighting': 'equal_scored_trip_then_equal_scored_snapshot',
        'trip_coverage_denominator': 'all_trips_with_eligible_labels_missing_forecasts_count_as_misses',
        'interval_score_alpha': 0.2,
        'endpoint_semantics': 'prediction_interval_endpoints_not_reestimated_quantiles',
    }


def _slice(row, field):
    if field == 'early_trip':
        return 'early' if row['elapsed_trip_seconds'] < 3600 else 'later'
    if field == 'deadline_advance':
        x = row['deadline_remaining_seconds']
        return 'overdue' if x < 0 else '0-30m' if x <= 1800 else '30-120m' if x <= 7200 else 'over-2h'
    return str(row.get(field) or 'unknown')


def _paired(rows):
    methods = sorted({r['variant'] for r in rows})
    if methods != sorted(VARIANTS):
        raise ValueError('Both raw and calibrated variants are required')
    sets = {v: {_key(r): r for r in rows if r['variant'] == v} for v in VARIANTS}
    for v in VARIANTS:
        if len(sets[v]) != sum(r['variant'] == v for r in rows):
            raise ValueError('Duplicate paired opportunity')
    if sets['raw'].keys() != sets['calibrated'].keys():
        raise ValueError('Paired variants must retain identical opportunities')
    for key, before in sets['raw'].items():
        after = sets['calibrated'][key]
        for field in ('trip_id', 'snapshot_time', 'calibration_role', 'eligibility', 'reason', 'label_status', 'target_seconds', 'delay_label', 'status', 'prediction_source', 'median_seconds', 'route_id', 'position_validity', 'elapsed_trip_seconds', 'deadline_remaining_seconds'):
            if before.get(field) != after.get(field):
                raise ValueError(f'Paired labels, support or median changed: {field}')


def build_metrics(interval_rows, probability_rows):
    """Use identical raw/calibrated observations; no promotion gate or retuning."""
    output = {'scope': 'July_development_assessment_not_final_test',
              'assessment_used_to_select_calibrator': False, 'final_test_evaluated': False,
              'eta': {}, 'probability': {}}
    for family, rows in (('eta', list(interval_rows)), ('probability', list(probability_rows))):
        if any(r['calibration_role'] != 'assessment' for r in rows):
            raise ValueError('Assessment metrics reject fit or buffer opportunities')
        _paired(rows)
        for role in ROLES:
            data = [r for r in rows if r['calibration_role'] == role]
            role_result = {'use': 'in_sample_fit_diagnostic' if role == 'calibration_fit' else 'out_of_calibrator_fit_assessment_also_reserved_policy_development',
                           'overall': {}, 'slices': []}
            def summarize(group):
                if family == 'eta':
                    return summarize_intervals(group)
                result = probability_summary(group)
                result['probability_scope'] = 'raw_or_calibrated_per_variant'
                result['counts']['unsupported_calibration_opportunities'] = sum(r.get('calibration_status') == 'unsupported' for r in group)
                result['calibration_applied_snapshots'] = sum(bool(r.get('calibration_applied')) for r in group)
                result['calibration_reason_counts'] = dict(sorted(Counter(str(r.get('calibration_reason')) for r in group).items()))
                return result
            for variant in VARIANTS:
                selected = [r for r in data if r['variant'] == variant]
                role_result['overall'][variant] = summarize(selected)
                for field in ('tenant_id', 'position_validity', 'route_id', 'early_trip', 'deadline_advance', 'prediction_source'):
                    groups = defaultdict(list)
                    for r in selected:
                        groups[_slice(r, field)].append(r)
                    for value, group in sorted(groups.items()):
                        role_result['slices'].append({'variant': variant, 'field': field, 'value': value, **summarize(group)})
            output[family][role] = role_result
    return output
