"""Trip-balanced assessment of uncalibrated binary delay probabilities.

Targets are mature, eligible snapshot-specific promise outcomes. Unknown targets
remain null and visible in availability. Ranking metrics use weighted, grouped
ties: ROC AUC is the concordant-pair probability and AP is noninterpolated average
precision. Reliability bins are fixed before scoring and do not fit a calibrator.
"""
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Mapping
import math
from numbers import Real

METHODS = ('fold_prevalence', 'logistic_delay')
COHORTS = ('native', 'delivered', 'common_supported', 'baseline_anchor')
SOURCES = ('model', 'prevalence_fallback', 'unavailable')
METRICS = ('brier_score', 'log_loss', 'roc_auc', 'average_precision',
           'weighted_event_rate', 'mean_probability', 'fixed_bin_ece')
STRATA = ('fold_id', 'tenant_id', 'route_id', 'departure_time_bucket', 'position_validity', 'early_trip', 'deadline_advance')
DEFAULT_EDGES = tuple(i / 10 for i in range(11))


def _number(value, field):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(f'{field} must be a finite number')
    return float(value)


def _identifier(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field} must be a nonempty string')
    return value


def _settings(settings):
    settings = {} if settings is None else settings
    edges = tuple(_number(x, 'reliability bin edge') for x in settings.get('reliability_bin_edges', DEFAULT_EDGES))
    if len(edges) < 2 or edges[0] != 0 or edges[-1] != 1 or any(a >= b for a, b in zip(edges, edges[1:])):
        raise ValueError('Reliability edges must increase strictly from zero to one')
    epsilon = _number(settings.get('log_loss_epsilon', 1e-15), 'log loss epsilon')
    if not 0 < epsilon < .5:
        raise ValueError('Log loss epsilon must be between zero and one half')
    sparse = settings.get('sparse_below_scored_trips', 20)
    if isinstance(sparse, bool) or not isinstance(sparse, int) or sparse < 1:
        raise ValueError('Sparse threshold must be a positive integer')
    early = _number(settings.get('early_trip_cutoff_seconds', 3600), 'early trip cutoff')
    if early <= 0:
        raise ValueError('Early trip cutoff must be positive')
    advance = tuple(_number(x, 'deadline advance edge') for x in settings.get('advance_bin_edges_seconds', (0, 1800, 7200)))
    if advance != (0., 1800., 7200.):
        raise ValueError('The declared deadline bands require edges [0,1800,7200]')
    if tuple(settings.get('method_ids', METHODS)) != METHODS or tuple(settings.get('cohorts', COHORTS)) != COHORTS:
        raise ValueError('Method/cohort configuration differs from the frozen metric contract')
    strata = tuple(settings.get('strata', STRATA))
    if len(strata) != len(set(strata)) or not set(strata).issubset(STRATA):
        raise ValueError('Unknown or duplicate metric stratum')
    return edges, epsilon, sparse, early


def _validate(rows):
    seen, trip_folds = set(), {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError('Each probability row must be a mapping')
        for field in ('tenant_id', 'trip_id', 'snapshot_id', 'fold_id'):
            _identifier(row.get(field), field)
        key = row['tenant_id'], row['snapshot_id']
        trip = row['tenant_id'], row['trip_id']
        if key in seen:
            raise ValueError('Duplicate probability snapshot')
        seen.add(key)
        if trip_folds.setdefault(trip, row['fold_id']) != row['fold_id']:
            raise ValueError('A validation trip cannot belong to multiple folds')
        eligibility = row.get('eligibility')
        if eligibility not in ('eligible', 'excluded'):
            raise ValueError('Target eligibility must be eligible or excluded')
        label = row.get('delay_label')
        if (eligibility == 'eligible' and type(label) is not bool) or (eligibility == 'excluded' and label is not None):
            raise ValueError('Eligible delay_label must be bool; excluded delay_label must be null')
        if eligibility == 'eligible' and row.get('label_status') != 'verified':
            raise ValueError('Eligible delay labels must have verified outcome status')
        status, source = row.get('status'), row.get('prediction_source')
        if status not in ('available', 'unavailable') or source not in SOURCES:
            raise ValueError('Unknown probability status or source')
        probability = row.get('probability')
        if status == 'unavailable':
            if probability is not None or source != 'unavailable':
                raise ValueError('Unavailable forecasts require null probability and unavailable source')
        else:
            p = _number(probability, 'probability')
            if not 0 <= p <= 1 or source == 'unavailable':
                raise ValueError('Available probabilities must be in [0,1] with a delivered source')
        for field in ('elapsed_trip_seconds', 'deadline_remaining_seconds'):
            if row.get(field) is not None:
                value = _number(row[field], field)
                if field == 'elapsed_trip_seconds' and value < 0:
                    raise ValueError('Elapsed trip time cannot be negative')


def _class_support(rows):
    positive = [r for r in rows if r['delay_label']]
    negative = [r for r in rows if not r['delay_label']]
    p = {(r['tenant_id'], r['trip_id']) for r in positive}
    n = {(r['tenant_id'], r['trip_id']) for r in negative}
    return {'positive_snapshots': len(positive), 'negative_snapshots': len(negative),
            'positive_trips': len(p), 'negative_trips': len(n), 'mixed_label_trips': len(p & n)}


def _weighted(rows):
    counts = Counter((r['tenant_id'], r['trip_id']) for r in rows)
    # A trip may cross reliability bins. Its global cohort weight is shared by
    # its snapshots; weights are not recomputed separately inside a bin.
    return [(float(r['probability']), int(r['delay_label']), 1 / counts[(r['tenant_id'], r['trip_id'])], r)
            for r in sorted(rows, key=lambda r: (r['tenant_id'], r['trip_id'], r['snapshot_id']))]


def _ranking(observations, include_curves):
    by_probability = defaultdict(list)
    for p, y, weight, _ in observations:
        by_probability[p].append((y, weight))
    groups = [(p, math.fsum(w for y, w in values if y), math.fsum(w for y, w in values if not y))
              for p, values in sorted(by_probability.items())]
    positive = math.fsum(x[1] for x in groups)
    negative = math.fsum(x[2] for x in groups)
    if not positive or not negative:
        reason = 'no_scored_snapshots' if not observations else 'single_class_scored_labels'
        return None, None, reason, None
    below_negative, concordance = 0., []
    for _, pweight, nweight in groups:
        concordance.append(pweight * (below_negative + .5 * nweight))
        below_negative += nweight
    auc = math.fsum(concordance) / (positive * negative)
    tp, fp, ap_terms = 0., 0., []
    curve = [{'threshold': None, 'recall': 0., 'precision': 1., 'true_positive_rate': 0., 'false_positive_rate': 0.}]
    for probability, pweight, nweight in reversed(groups):
        tp += pweight
        fp += nweight
        precision = tp / (tp + fp)
        ap_terms.append((pweight / positive) * precision)
        if include_curves:
            curve.append({'threshold': probability, 'recall': tp / positive, 'precision': precision,
                          'true_positive_rate': tp / positive, 'false_positive_rate': fp / negative})
    return auc, math.fsum(ap_terms), None, curve if include_curves else None


def _reliability(observations, edges, sparse):
    total = math.fsum(w for _, _, w, _ in observations)
    bins = [[] for _ in range(len(edges) - 1)]
    for item in observations:
        bins[min(bisect_right(edges, item[0]) - 1, len(bins) - 1)].append(item)
    results = []
    for i, values in enumerate(bins):
        weight = math.fsum(w for _, _, w, _ in values)
        raw = [r for _, _, _, r in values]
        trips = len({(r['tenant_id'], r['trip_id']) for r in raw})
        results.append({'bin_index': i, 'lower': edges[i], 'upper': edges[i + 1],
                        'upper_inclusive': i == len(bins) - 1, 'snapshots': len(raw), 'trips': trips,
                        **_class_support(raw), 'cohort_weight': weight,
                        'cohort_weight_fraction': weight / total if total else 0.,
                        'mean_probability': math.fsum(p*w for p, _, w, _ in values)/weight if weight else None,
                        'observed_event_rate': math.fsum(y*w for _, y, w, _ in values)/weight if weight else None,
                        'sparse': trips < sparse})
    return results


def summarize(rows, settings=None, *, include_curves=False):
    """Assess one method on all supplied opportunities, keeping exclusions visible.

    Eligible labels are bool; excluded labels are None. Each scored tenant/trip
    has total weight one, split equally among its scored snapshots. Both ranking
    statistics are intentionally null for one-class groups; probability losses
    remain defined. Log loss clips only its own calculation, never the stored
    predictions, Brier score, ranking or reliability bins.
    """
    rows = list(rows)
    edges, epsilon, sparse, _ = _settings(settings)
    _validate(rows)
    eligible = [r for r in rows if r['eligibility'] == 'eligible']
    scored = [r for r in eligible if r['status'] == 'available']
    observations = _weighted(scored)
    total = math.fsum(w for _, _, w, _ in observations)
    clipped = [(min(1-epsilon, max(epsilon, p)), y, w) for p, y, w, _ in observations]
    auc, ap, reason, curve = _ranking(observations, include_curves)
    reliability = _reliability(observations, edges, sparse)
    trips = {(r['tenant_id'], r['trip_id']) for r in scored}
    values = {name: None for name in METRICS}
    if total:
        values.update(brier_score=math.fsum(w*(p-y)**2 for p, y, w, _ in observations)/total,
                      log_loss=math.fsum(w*(-math.log(p) if y else -math.log1p(-p)) for p, y, w in clipped)/total,
                      roc_auc=auc, average_precision=ap,
                      weighted_event_rate=math.fsum(y*w for _, y, w, _ in observations)/total,
                      mean_probability=math.fsum(p*w for p, _, w, _ in observations)/total,
                      fixed_bin_ece=math.fsum(b['cohort_weight_fraction']*abs(b['mean_probability']-b['observed_event_rate'])
                                             for b in reliability if b['snapshots']))
    result = {
        'counts': {'opportunities': len(rows), 'trips': len({(r['tenant_id'], r['trip_id']) for r in rows}),
                   'eligible_snapshots': len(eligible), 'excluded_snapshots': len(rows)-len(eligible),
                   'available_snapshots': sum(r['status'] == 'available' for r in rows),
                   'scored_snapshots': len(scored), 'scored_trips': len(trips),
                   'unavailable_eligible_snapshots': len(eligible)-len(scored),
                   'log_loss_clipped_snapshots': sum(p < epsilon or p > 1-epsilon for p, _, _, _ in observations)},
        'eligible_class_support': _class_support(eligible), 'scored_class_support': _class_support(scored),
        'label_status_counts': dict(sorted(Counter(str(r.get('label_status') or 'unknown') for r in rows).items())),
        'exclusion_reason_counts': dict(sorted(Counter(str(r.get('reason') or 'unspecified') for r in rows
                                                     if r['eligibility'] != 'eligible').items())),
        'metrics': values, 'ranking_undefined_reason': reason, 'sparse': len(trips) < sparse,
        'weighting': 'equal_scored_tenant_trip_then_equal_scored_snapshot_within_trip',
        'reliability_weighting': 'fixed_cohort_trip_weights_conditioned_on_probability_bin',
        'probability_scope': 'raw_uncalibrated_probabilities', 'log_loss_epsilon': epsilon,
        'reliability': reliability,
    }
    if include_curves:
        result['ranking_curve'] = curve
    return result


def _slice_value(row, field, early):
    if field == 'early_trip':
        elapsed = row.get('elapsed_trip_seconds')
        return 'unknown' if elapsed is None else ('early' if elapsed < early else 'later')
    if field == 'deadline_advance':
        seconds = row.get('deadline_remaining_seconds')
        if seconds is None:
            return 'unknown'
        return 'overdue' if seconds < 0 else '0-30m' if seconds <= 1800 else '30-120m' if seconds <= 7200 else 'over-2h'
    return str(row.get(field) or 'unknown')


def _availability(rows):
    counts = Counter(r['prediction_source'] for r in rows)
    eligible = Counter(r['prediction_source'] for r in rows if r['eligibility'] == 'eligible')
    return {'opportunities': len(rows), 'eligible_opportunities': sum(eligible.values()),
            **{f'{s}_opportunities': counts[s] for s in SOURCES},
            **{f'{s}_eligible_opportunities': eligible[s] for s in SOURCES},
            'delivered_fraction': (len(rows)-counts['unavailable'])/len(rows) if rows else None}


def _cohort(rows, method, cohort, by_method, settings):
    selected = []
    for row in rows:
        if row['eligibility'] != 'eligible':
            continue
        key = row['tenant_id'], row['snapshot_id']
        if cohort == 'native' and row['prediction_source'] != 'model':
            continue
        if cohort == 'delivered' and row['status'] != 'available':
            continue
        if cohort == 'common_supported' and not all(other[key]['status'] == 'available' for other in by_method.values()):
            continue
        if cohort == 'baseline_anchor' and by_method['fold_prevalence'][key]['status'] != 'available':
            continue
        selected.append(row)
    result = summarize(selected, settings)
    result['cohort_snapshots'] = len(selected)
    result['cohort_complete'] = result['counts']['scored_snapshots'] == len(selected)
    if not result['cohort_complete']:
        # An incomplete fixed anchor cannot look better by dropping hard cases.
        result['metrics'] = {name: None for name in METRICS}
        result['ranking_undefined_reason'] = 'incomplete_fixed_cohort'
        result['reliability'] = []
    return result


def evaluate_methods(rows, settings=None):
    """Return (metrics, reliability rows, slice rows) for the two frozen methods.

    Every method must contain identical opportunities and target identities.
    Performance on each method's delivered subset is distinguished from the
    common supported subset and the fixed prevalence-baseline anchor.
    """
    settings = {} if settings is None else settings
    _, _, _, early = _settings(settings)
    rows = list(rows)
    if any(r.get('method_id') not in METHODS for r in rows):
        raise ValueError('Only the declared fold_prevalence and logistic_delay methods are accepted')
    by_method = {}
    for method in METHODS:
        method_rows = [r for r in rows if r['method_id'] == method]
        _validate(method_rows)
        by_method[method] = {(r['tenant_id'], r['snapshot_id']): r for r in method_rows}
    baseline = by_method['fold_prevalence']
    fields = ('trip_id', 'fold_id', 'snapshot_time', 'eligibility', 'delay_label', 'label_status', 'reason',
              'elapsed_trip_seconds', 'deadline_remaining_seconds', 'position_validity', 'route_id', 'departure_time_bucket')
    for method in METHODS:
        if by_method[method].keys() != baseline.keys():
            raise ValueError('Methods must preserve the same complete opportunity keys')
        for key, row in by_method[method].items():
            ref = baseline[key]
            if any(row.get(field) != ref.get(field) for field in fields):
                raise ValueError('Methods disagree on target identity or snapshot-time context')
            if row['prediction_source'] == 'prevalence_fallback':
                if method == 'fold_prevalence' or ref['status'] != 'available' or row['probability'] != ref['probability']:
                    raise ValueError('Prevalence fallback must match the available baseline exactly')
    overall, reliability_rows, slice_rows = {}, [], []
    for method in METHODS:
        method_rows = list(by_method[method].values())
        full = summarize(method_rows, settings)
        cohorts = {}
        for cohort in COHORTS:
            report = _cohort(method_rows, method, cohort, by_method, settings)
            reliability_rows.extend(dict(method_id=method, cohort=cohort, **b) for b in report.pop('reliability'))
            cohorts[cohort] = report
        full.pop('reliability')
        overall[method] = {'availability': _availability(method_rows), 'all_opportunities': full, 'cohorts': cohorts}
        for field in settings.get('strata', STRATA):
            groups = defaultdict(list)
            for row in method_rows:
                groups[_slice_value(row, field, early)].append(row)
            for value, members in sorted(groups.items()):
                for cohort in COHORTS:
                    report = _cohort(members, method, cohort, by_method, settings)
                    slice_rows.append({'stratum': field, 'value': value, 'method_id': method, 'cohort': cohort,
                                       **_availability(members), **{k: v for k, v in report['counts'].items() if k not in ('opportunities', 'trips')},
                                       'opportunity_trips': len({(r['tenant_id'], r['trip_id']) for r in members}),
                                       'cohort_trips': report['counts']['trips'], **report['scored_class_support'],
                                       **report['metrics'], 'cohort_snapshots': report['cohort_snapshots'],
                                       'cohort_complete': report['cohort_complete'], 'sparse': report['sparse'],
                                       'ranking_undefined_reason': report['ranking_undefined_reason']})
    summary = {'methods': overall, 'opportunities_per_method': len(baseline),
               'class_support_scope': 'validation labels; a trip can appear in both class counts when promises change',
               'evaluation_scope': 'training-period forward validation; raw probabilities, no calibration or alert threshold',
               'fixed_reliability_bin_edges': list(_settings(settings)[0]),
               'ranking_policy': 'weighted tied-score ROC AUC and noninterpolated AP; both null for one-class groups',
               'early_trip_seconds': early,
               'deadline_advance_bands': 'overdue<0; 0-30m=[0,1800]; 30-120m=(1800,7200]; over-2h>7200'}
    return summary, reliability_rows, slice_rows
