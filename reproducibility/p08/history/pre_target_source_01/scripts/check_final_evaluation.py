"""Independent standard-library references for the frozen P08 evaluation.

No production evaluator is imported. In-memory functions support preregistered
synthetic conformance tests; retained targets are read only after artifact gates.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import random
import struct
import sys
from zoneinfo import ZoneInfo


ABS_TOLERANCE = 1e-10
REL_TOLERANCE = 1e-9
EPSILON = 1e-15
NOVELTY = ('seen_vehicle_seen_route', 'unseen_vehicle_seen_route',
           'seen_vehicle_unseen_route', 'unseen_vehicle_unseen_route')
SCENARIOS = ('S00_unmodified', 'S01_missing_position', 'S02_stale_position',
             'S03_unseen_route_slot', 'S04_natural_entity_holdouts',
             'S05_stationary_window', 'S06_distance_scale',
             'S07_transport_degraded', 'S08_missing_and_degraded')
OUTCOME_STATUSES = {'verified', 'pending', 'cancelled', 'incomplete', 'lost', 'conflict'}
DERIVED_EVIDENCE = {f'contracts/current_{name}_evidence.csv' for name in ('requirement', 'table', 'validation')}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def clock(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(result.tzinfo is not None, 'Timestamp must carry an offset')
    return result.astimezone(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def trip(row):
    return row['tenant_id'], row['trip_id']


def snapshot(row):
    return row['tenant_id'], row['snapshot_id']


def index(rows, key=snapshot):
    result = {}
    for row in rows:
        identity = key(row)
        require(identity not in result, 'Duplicate identity: ' + str(identity))
        result[identity] = row
    return result


def finite(value):
    require(not isinstance(value, bool) and isinstance(value, (int, float, Decimal)), 'Expected finite number')
    answer = float(value)
    require(math.isfinite(answer), 'Expected finite number')
    return answer


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def mean(values):
    values = list(values)
    return math.fsum(values) / len(values) if values else None


def quantile(values, probability):
    values = sorted(values)
    if not values:
        return None
    offset = (len(values) - 1) * probability
    left = math.floor(offset)
    right = math.ceil(offset)
    return values[left] + (values[right] - values[left]) * (offset - left)


def distribution(values):
    values = [finite(v) for v in values if v is not None]
    return {'n': len(values), **{name: quantile(values, p) for name, p in
            [('min', 0), ('p10', .1), ('p25', .25), ('median', .5),
             ('p75', .75), ('p90', .9), ('max', 1)]}}


def project_probability(value):
    require(0 <= finite(value) <= 1, 'Probability outside [0,1]')
    return format(Decimal(str(value)).quantize(Decimal('.000001'), rounding=ROUND_HALF_EVEN), '.6f')


def project_eta(lower, median, upper):
    values = [finite(v) for v in (lower, median, upper)]
    require(0 <= values[0] <= values[1] <= values[2], 'Invalid ordered ETA interval')
    lo, mid, hi = map(lambda v: Decimal(str(v)), (lower, median, upper))
    center = int(mid.to_integral_value(rounding=ROUND_HALF_EVEN))
    return (min(int(lo.to_integral_value(rounding=ROUND_FLOOR)), center), center,
            max(int(hi.to_integral_value(rounding=ROUND_CEILING)), center))


def trip_means(rows, expression):
    grouped = defaultdict(list)
    for row in rows:
        grouped[trip(row)].append(expression(row))
    return {key: mean(values) for key, values in sorted(grouped.items())}


def eligible(row):
    return row['eligibility'] == 'eligible'


def available(row):
    return row['status'] == 'available'


def support(rows, scored):
    accepted = [r for r in rows if eligible(r)]
    return {'opportunities': len(rows), 'monitored_trips': len({trip(r) for r in rows}),
            'eligible_snapshots': len(accepted), 'eligible_trips': len({trip(r) for r in accepted}),
            'scored_snapshots': len(scored), 'scored_trips': len({trip(r) for r in scored}),
            'available_snapshots': sum(available(r) for r in rows),
            'unavailable_snapshots': sum(not available(r) for r in rows),
            'prediction_source_counts': dict(sorted(Counter(r.get('prediction_source', 'unspecified') for r in rows).items())),
            'calibration_status_counts': dict(sorted(Counter(r.get('calibration_status', 'unspecified') for r in rows).items()))}


def reference_eta(rows):
    """Equal trip weights; missing intervals miss all-eligible-trip coverage."""
    rows = list(rows)
    index(rows)
    chosen = [r for r in rows if eligible(r) and available(r)]
    for row in chosen:
        target = finite(row['target_seconds'])
        lo, mid, hi = [finite(row[name]) for name in ('lower_seconds', 'median_seconds', 'upper_seconds')]
        require(target > 0 and 0 <= lo <= mid <= hi, 'Invalid ETA scoring values')
    covered = lambda r: r['lower_seconds'] <= r['target_seconds'] <= r['upper_seconds']
    pinball = lambda y, estimate, q: (q - int(y < estimate)) * (y - estimate)
    def interval_score(row):
        lo, hi, target = row['lower_seconds'], row['upper_seconds'], row['target_seconds']
        return hi-lo + 10 * max(lo-target, 0) + 10 * max(target-hi, 0)
    contributions = {
        'median_mae_seconds': trip_means(chosen, lambda r: abs(r['median_seconds']-r['target_seconds'])),
        'median_mse_seconds2': trip_means(chosen, lambda r: (r['median_seconds']-r['target_seconds'])**2),
        'snapshot_coverage': trip_means(chosen, lambda r: int(covered(r))),
        'mean_width_seconds': trip_means(chosen, lambda r: r['upper_seconds']-r['lower_seconds']),
        'interval_score': trip_means(chosen, interval_score)}
    for q, name in [(.1, 'lower_seconds'), (.5, 'median_seconds'), (.9, 'upper_seconds')]:
        contributions['pinball_' + str(q)] = trip_means(chosen, lambda r, q=q, name=name: pinball(r['target_seconds'], r[name], q))
    by_trip = defaultdict(list)
    for row in rows:
        if eligible(row):
            by_trip[trip(row)].append(row)
    all_coverage = {key: int(all(available(r) and covered(r) for r in group)) for key, group in by_trip.items()}
    full = {key: all_coverage[key] for key, group in by_trip.items() if all(available(r) for r in group)}
    contributions['whole_trip_coverage'] = all_coverage
    metrics = {name: mean(values.values()) for name, values in contributions.items()}
    mse = metrics.pop('median_mse_seconds2')
    metrics['median_rmse_seconds'] = math.sqrt(mse) if mse is not None else None
    metrics['conditional_full_availability_coverage'] = mean(full.values())
    counts = support(rows, chosen)
    counts.update(fully_available_eligible_trips=len(full), whole_trip_coverage_denominator=len(all_coverage),
                  whole_trip_covered=sum(all_coverage.values()))
    reasons = {name: ('no_eligible_targets' if not by_trip else 'no_available_eligible_forecasts')
               for name, value in metrics.items() if value is None}
    if not full:
        reasons['conditional_full_availability_coverage'] = 'no_fully_available_eligible_trips'
    return {'support': counts, 'metrics': metrics, 'null_reasons': reasons,
            'trip_contributions': contributions, 'sparse': counts['scored_trips'] < 20}


def reference_probability(rows):
    """Weighted ranking handles probability ties as groups, including AP ties."""
    rows = list(rows)
    index(rows)
    scored = [r for r in rows if eligible(r) and available(r)]
    for row in scored:
        require(type(row['delay_label']) is bool, 'Scored binary target must be bool')
        require(0 <= finite(row['probability']) <= 1, 'Probability outside [0,1]')
    trip_sizes = Counter(trip(r) for r in scored)
    records = [(r['probability'], int(r['delay_label']), 1/trip_sizes[trip(r)], r) for r in scored]
    total = len(trip_sizes)
    positive = math.fsum(weight*y for _, y, weight, _ in records)
    negative = math.fsum(weight*(1-y) for _, y, weight, _ in records)
    loss = lambda p, y: -(y*math.log(min(1-EPSILON, max(EPSILON, p))) +
                          (1-y)*math.log1p(-min(1-EPSILON, max(EPSILON, p))))
    contributions = {'brier': trip_means(scored, lambda r: (r['probability']-r['delay_label'])**2),
                     'log_loss': trip_means(scored, lambda r: loss(r['probability'], r['delay_label']))}
    groups = defaultdict(list)
    for p, y, weight, _ in records:
        groups[p].append((y, weight))
    auc = ap = None
    if positive and negative:
        below_negative = numerator = 0.0
        for p in sorted(groups):
            pos = math.fsum(y*w for y, w in groups[p])
            neg = math.fsum((1-y)*w for y, w in groups[p])
            numerator += pos*(below_negative + .5*neg)
            below_negative += neg
        auc = numerator/(positive*negative)
        cum_positive = cum_total = ap = 0.0
        for p in sorted(groups, reverse=True):
            pos = math.fsum(y*w for y, w in groups[p])
            weight = math.fsum(w for _, w in groups[p])
            cum_positive += pos
            cum_total += weight
            ap += pos/positive * cum_positive/cum_total
    bins = []
    for number in range(10):
        members = [record for record in records if (number/10 <= record[0] < (number+1)/10 or
                                                    number == 9 and record[0] == 1)]
        weight = math.fsum(r[2] for r in members)
        avg = ratio(math.fsum(p*w for p, _, w, _ in members), weight)
        rate = ratio(math.fsum(y*w for _, y, w, _ in members), weight)
        bins.append({'bin': number, 'lower': number/10, 'upper': (number+1)/10,
                     'upper_inclusive': number == 9, 'snapshots': len(members),
                     'trips': len({trip(r[3]) for r in members}), 'weight': weight,
                     'weight_fraction': ratio(weight, total), 'mean_probability': avg, 'observed_frequency': rate,
                     'positive_snapshots': sum(r[1] for r in members),
                     'negative_snapshots': sum(1-r[1] for r in members),
                     'positive_trips': len({trip(r[3]) for r in members if r[1]}),
                     'negative_trips': len({trip(r[3]) for r in members if not r[1]}),
                     'null_reason': None if weight else 'empty_bin'})
    metrics = {name: mean(values.values()) for name, values in contributions.items()}
    metrics.update(roc_auc=auc, average_precision=ap,
                   ece=math.fsum(b['weight']*abs(b['mean_probability']-b['observed_frequency'])
                                 for b in bins if b['weight'])/total if total else None)
    counts = support(rows, scored)
    counts.update(positive_snapshots=sum(r['delay_label'] for r in scored),
                  negative_snapshots=sum(not r['delay_label'] for r in scored),
                  positive_trips=len({trip(r) for r in scored if r['delay_label']}),
                  negative_trips=len({trip(r) for r in scored if not r['delay_label']}),
                  positive_weight=positive, negative_weight=negative,
                  log_loss_clipped_snapshots=sum(p < EPSILON or p > 1-EPSILON for p, *_ in records))
    reason = 'no_scored_targets' if not scored else 'single_class'
    return {'support': counts, 'metrics': metrics, 'reliability': bins,
            'trip_contributions': contributions, 'null_reasons': {name: reason for name, value in metrics.items() if value is None},
            'sparse': counts['scored_trips'] < 20}


def common_support(left, right):
    a, b = index(left), index(right)
    require(a.keys() == b.keys(), 'Paired opportunity inventory differs')
    sa = {k for k, row in a.items() if eligible(row) and available(row)}
    sb = {k for k, row in b.items() if eligible(row) and available(row)}
    keys = sorted(sa & sb)
    return ([a[k] for k in keys], [b[k] for k in keys],
            {'common_snapshots': len(keys), 'common_trips': len({trip(a[k]) for k in keys}),
             'left_lost_snapshots': len(sa-sb), 'right_lost_snapshots': len(sb-sa),
             'left_lost_trips': len({trip(a[k]) for k in sa}-{trip(a[k]) for k in keys}),
             'right_lost_trips': len({trip(b[k]) for k in sb}-{trip(b[k]) for k in keys})})


def bootstrap_draws(memberships, replicates=2000, seed=20261111):
    by_trip = index(memberships, trip)
    strata = defaultdict(list)
    for key, row in sorted(by_trip.items()):
        require(row['novelty_group'] in NOVELTY, 'Unknown novelty group')
        strata[(key[0], row['novelty_group'])].append(key)
    rng = random.Random(seed)
    return [tuple(rng.choice(strata[stratum]) for stratum in sorted(strata)
                  for _ in strata[stratum]) for _ in range(replicates)]


def bootstrap_interval(contributions, draws, *, transform=None, other=None, minimum=20):
    """Master inventory draws are shared; undefined replicates are never dropped."""
    if other is not None:
        require(contributions.keys() == other.keys(), 'Bootstrap paired trip support differs')
    calculated, undefined = [], 0
    for sample in draws:
        values = [contributions[key] for key in sample if key in contributions]
        if not values:
            undefined += 1
            continue
        current = mean(values)
        current = transform(current) if transform else current
        if other is not None:
            paired = mean(other[key] for key in sample if key in other)
            current -= transform(paired) if transform else paired
        calculated.append(current)
    reason = ('fewer_than_20_scored_trips' if len(contributions) < minimum else
              'undefined_replicate_denominator' if undefined else None)
    return {'lower': None if reason else quantile(calculated, .025),
            'upper': None if reason else quantile(calculated, .975), 'reason': reason,
            'original_scored_trips': len(contributions), 'requested_replicates': len(draws),
            'undefined_replicates': undefined}


def reference_stress(features, scenario, *, with_audit=False):
    """Perturb original feature copies; consecutive means the original row stream."""
    require(scenario in SCENARIOS, 'Unknown stress scenario')
    original = index(features)
    result = deepcopy(features)
    selected = set()
    if scenario == 'S05_stationary_window':
        groups = defaultdict(list)
        for row in features:
            groups[trip(row)].append(row)
        for group in groups.values():
            group.sort(key=lambda r: (clock(r['snapshot_time']), r['snapshot_id']))
            for offset in range(max(0, len(group)-2)):
                window = group[offset:offset+3]
                valid = all(r['numeric']['elapsed_trip_seconds'] >= 3600 and
                            r['categorical']['position_validity'] == 'valid' and
                            r['numeric']['remaining_distance_km'] is not None for r in window)
                if valid and (clock(window[-1]['snapshot_time'])-clock(window[0]['snapshot_time'])).total_seconds() >= 3600:
                    selected.update(snapshot(r) for r in window)
                    break
    first_distance = {}
    for row in sorted(features, key=lambda r: (clock(r['snapshot_time']), *snapshot(r))):
        if snapshot(row) in selected:
            first_distance.setdefault(trip(row), row['numeric']['remaining_distance_km'])
    for row in result:
        numeric, categorical = row['numeric'], row['categorical']
        if scenario in ('S01_missing_position', 'S08_missing_and_degraded'):
            categorical['position_validity'] = 'no_position'
            numeric.update(remaining_distance_km=None, gnss_speed_kmh=None, position_age_seconds=None)
        elif scenario == 'S02_stale_position' and numeric['elapsed_trip_seconds'] >= 1800 and categorical['position_validity'] != 'no_position':
            categorical['position_validity'] = 'low_confidence'
            numeric.update(remaining_distance_km=None, gnss_speed_kmh=None, position_age_seconds=1800.0)
        elif scenario == 'S03_unseen_route_slot':
            categorical['route_id'] = '__P08_UNSEEN_ROUTE__'
        elif snapshot(row) in selected:
            numeric.update(remaining_distance_km=first_distance[trip(row)], gnss_speed_kmh=0.0, position_age_seconds=0.0)
        elif scenario == 'S06_distance_scale':
            for name in ('route_distance_km', 'remaining_distance_km'):
                if numeric[name] is not None:
                    numeric[name] = float(Decimal(str(numeric[name]))*Decimal('1.25'))
        distance, remaining = numeric['route_distance_km'], numeric['remaining_distance_km']
        if scenario == 'S06_distance_scale' or snapshot(row) in selected:
            require(distance is not None and finite(distance) > 0, 'Stress route distance must remain positive')
            require(remaining is None or 0 <= finite(remaining) <= distance, 'Stress remaining distance outside route')
        require(snapshot(row) in original, 'Stress changed snapshot identity')
    if not with_audit:
        return result
    records = []
    for row in result:
        before = original[snapshot(row)]
        applicable, reason = True, 'fixed_transform'
        if scenario == 'S02_stale_position':
            applicable = before['numeric']['elapsed_trip_seconds'] >= 1800 and before['categorical']['position_validity'] != 'no_position'
            if not applicable:
                reason = 'no_original_position_or_elapsed_below_1800'
        elif scenario == 'S05_stationary_window':
            applicable = snapshot(row) in selected
            if not applicable:
                reason = 'outside_first_qualifying_consecutive_window'
        elif scenario in ('S00_unmodified', 'S04_natural_entity_holdouts', 'S07_transport_degraded'):
            reason = 'unmodified_features'
        previous = {name: before[name] for name in ('numeric', 'categorical')}
        changed = {name: row[name] for name in ('numeric', 'categorical')}
        records.append({**{name: row[name] for name in ('tenant_id', 'trip_id', 'snapshot_id', 'snapshot_time')},
                        'scenario_id': scenario, 'applicable': applicable, 'affected': previous != changed,
                        'reason': reason, 'before': previous, 'after': changed})
    return {'features': result, 'audit': records, 'summary': {
        'scenario_id': scenario, 'opportunities': len(result), 'trips': len({trip(r) for r in result}),
        'affected': sum(r['affected'] for r in records), 'applicable': sum(r['applicable'] for r in records),
        'not_applicable': sum(not r['applicable'] for r in records),
        'reason_counts': dict(sorted(Counter(r['reason'] for r in records).items())),
        'transport_changed': scenario in ('S07_transport_degraded', 'S08_missing_and_degraded'),
        'source_observations_rewritten': False, 'outcomes_used': False}}


def reference_targets(opportunities, outcomes, promises, cutoff):
    """Independent latest-version oracle; a later pending withdrawal wins."""
    opportunities = list(opportunities)
    index(opportunities)
    allowed = {trip(row) for row in opportunities}
    boundary = clock(cutoff)
    selected, known_times, versions = {}, set(), set()
    for outcome in outcomes:
        key = trip(outcome)
        if key not in allowed:
            continue
        known = clock(outcome['label_available_time'])
        if known >= boundary:
            continue
        identity = key[0], outcome['outcome_version_id']
        require((key, known) not in known_times and identity not in versions, 'Ambiguous outcome version')
        known_times.add((key, known)); versions.add(identity)
        if key not in selected or known > selected[key][0]:
            selected[key] = known, outcome
    promises_by_trip = defaultdict(list)
    for promise in promises:
        if trip(promise) in allowed:
            promises_by_trip[trip(promise)].append(promise)
    eta, binary = [], []
    for row in sorted(opportunities, key=lambda r: (*trip(r), clock(r['snapshot_time']), r['snapshot_id'])):
        now = clock(row['snapshot_time'])
        require(now < boundary, 'Snapshot must precede outcome cutoff')
        visible = [p for p in promises_by_trip[trip(row)] if clock(p['known_at']) <= now]
        require(visible, 'Missing snapshot-effective promise')
        times = [clock(p['known_at']) for p in visible]
        require(len(times) == len(set(times)), 'Ambiguous promise known time')
        promise = max(visible, key=lambda p: clock(p['known_at']))
        require(promise['promise_version_id'] == row['promise_version_id'], 'Promise identity differs')
        deadline = clock(promise['window_end_at'])
        require(clock(promise['window_start_at']) <= deadline, 'Reversed promise window')
        base = {name: row[name] for name in ('tenant_id', 'trip_id', 'snapshot_id', 'snapshot_time')}
        base.update(eligibility='excluded', reason='no_outcome_known_before_cutoff', label_status=None,
                    outcome_version_id=None, label_known_at=None, actual_arrival_time=None,
                    arrival_time_precision=None, arrival_evidence_ref=None, label_cutoff_at=stamp(boundary))
        erow = dict(base, target_seconds=None)
        brow = dict(base, delay_label=None, deadline_at=stamp(deadline), promise_version_id=promise['promise_version_id'],
                    promise_known_at=stamp(clock(promise['known_at'])), remaining_duration_seconds=None, delay_seconds=None)
        selected_outcome = selected.get(trip(row))
        if selected_outcome:
            known, outcome = selected_outcome
            state = outcome['label_status']
            require(state in OUTCOME_STATUSES, 'Unknown frozen outcome status')
            for current, precisions in [(erow, ('second', 'minute')), (brow, ('second',))]:
                current.update(label_status=state, outcome_version_id=outcome['outcome_version_id'],
                               label_known_at=stamp(known), arrival_time_precision=outcome.get('arrival_time_precision'))
                if state != 'verified':
                    current['reason'] = 'latest_outcome_' + state
                elif outcome.get('arrival_time_precision') not in precisions:
                    current['reason'] = 'insufficient_arrival_precision'
                elif outcome.get('actual_arrival_time') is None:
                    current['reason'] = 'missing_gate_arrival'
                elif not isinstance(outcome.get('arrival_evidence_ref'), str) or not outcome['arrival_evidence_ref'].strip():
                    current['reason'] = 'missing_arrival_evidence'
                elif outcome.get('cancellation_time') is not None:
                    current['reason'] = 'conflicting_cancellation_evidence'
                else:
                    arrival = clock(outcome['actual_arrival_time'])
                    require(arrival <= known, 'Arrival follows verification')
                    current.update(actual_arrival_time=stamp(arrival), arrival_evidence_ref=outcome['arrival_evidence_ref'])
                    if arrival <= now:
                        current['reason'] = 'already_arrived_at_snapshot'
                    else:
                        current['eligibility'] = 'eligible'
                        if current is erow:
                            current.update(target_seconds=(arrival-now).total_seconds(), reason='verified_positive_gate_remaining')
                        else:
                            current.update(delay_label=arrival > deadline, remaining_duration_seconds=(arrival-now).total_seconds(),
                                           delay_seconds=(arrival-deadline).total_seconds(), reason='verified_gate_arrival')
        eta.append(erow); binary.append(brow)
    return {'eta_targets': eta, 'probability_targets': binary}


class Audit:
    def __init__(self):
        self.comparisons = 0
        self.failures = []
        self.maximum_numeric_difference = 0.0

    def compare(self, actual, expected, path):
        if isinstance(expected, dict):
            self.compare(isinstance(actual, dict), True, path+'/mapping')
            if not isinstance(actual, dict):
                return
            self.compare(sorted(actual), sorted(expected), path+'/keys')
            for name in expected.keys() & actual.keys():
                self.compare(actual[name], expected[name], path+'/'+str(name))
        elif isinstance(expected, (list, tuple)):
            self.compare(isinstance(actual, (list, tuple)), True, path+'/sequence')
            if not isinstance(actual, (list, tuple)):
                return
            self.compare(len(actual), len(expected), path+'/length')
            for number, (a, e) in enumerate(zip(actual, expected)):
                self.compare(a, e, path+'/'+str(number))
        else:
            self.comparisons += 1
            equal = type(actual) is type(expected) and actual == expected
            if isinstance(expected, float) and type(actual) in (int, float):
                difference = abs(actual-expected)
                self.maximum_numeric_difference = max(self.maximum_numeric_difference, difference)
                equal = math.isfinite(actual) and math.isclose(actual, expected, rel_tol=REL_TOLERANCE, abs_tol=ABS_TOLERANCE)
            if not equal:
                self.failures.append({'path': path, 'actual': actual, 'expected': expected})

    def subset(self, actual, expected, path):
        for name, value in expected.items():
            self.compare(name in actual, True, path+'/'+name+'/present')
            if name in actual:
                self.compare(actual[name], value, path+'/'+name)


def digest_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def contained(root, relative):
    require(isinstance(relative, str) and relative and not Path(relative).is_absolute(), 'Expected relative artifact path')
    path = (root / relative).resolve()
    require(path.is_relative_to(root) and path != root, 'Artifact leaves its root')
    return path


def load_reference(root, name):
    spec = importlib.util.spec_from_file_location('_independent_' + name, root/'scripts'/(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    old = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = old
    return module


ETA_NAMES = {'median_mae_seconds': 'median_mae_seconds', 'median_rmse_seconds': 'median_rmse_seconds',
             'mean_width_seconds': 'mean_width_seconds', 'mean_interval_score_seconds': 'interval_score',
             'trip_weighted_snapshot_coverage': 'snapshot_coverage',
             'trip_all_eligible_snapshots_coverage': 'whole_trip_coverage',
             'trip_all_snapshots_coverage_given_full_availability': 'conditional_full_availability_coverage'}
PROBABILITY_NAMES = {'brier_score': 'brier', 'log_loss': 'log_loss', 'roc_auc': 'roc_auc',
                     'average_precision': 'average_precision', 'fixed_bin_ece': 'ece'}
SLICE_FIELDS = ('novelty_group', 'tenant_id', 'departure_month', 'route_id', 'position_validity',
                'early_trip', 'elapsed_7200', 'deadline_advance', 'prediction_source', 'calibration_status')


def slice_value(row, field):
    if field == 'departure_month':
        return clock(row['departure_time']).astimezone(ZoneInfo('America/Los_Angeles')).strftime('%Y-%m')
    if field in ('early_trip', 'elapsed_7200'):
        value = row.get('elapsed_trip_seconds')
        boundary = 3600 if field == 'early_trip' else 7200
        return 'unknown' if value is None else ('below_' if value < boundary else 'at_least_') + str(boundary)
    if field == 'deadline_advance':
        value = row.get('deadline_remaining_seconds')
        return ('unknown' if value is None else 'overdue' if value < 0 else '[0,1800]' if value <= 1800
                else '(1800,7200]' if value <= 7200 else '>7200')
    return str(row.get(field) or 'unknown')


def class_support(rows):
    pos = [r for r in rows if r['delay_label']]
    neg = [r for r in rows if not r['delay_label']]
    return {'positive_snapshots': len(pos), 'negative_snapshots': len(neg),
            'positive_trips': len({trip(r) for r in pos}), 'negative_trips': len({trip(r) for r in neg}),
            'mixed_label_trips': len({trip(r) for r in pos} & {trip(r) for r in neg})}


def metric_reference_record(rows, family, variant):
    reference = reference_eta(rows) if family == 'eta' else reference_probability(rows)
    counts = reference['support']
    accepted = [r for r in rows if eligible(r)]
    scored = [r for r in accepted if available(r)]
    counts_out = {name: counts[name] for name in ('opportunities', 'eligible_snapshots', 'eligible_trips',
                  'available_snapshots', 'scored_snapshots', 'scored_trips')}
    counts_out.update(trips=counts['monitored_trips'], excluded_snapshots=len(rows)-len(accepted),
                      available_trips=len({trip(r) for r in rows if available(r)}),
                      unavailable_eligible_snapshots=len(accepted)-len(scored))
    mapping = ETA_NAMES if family == 'eta' else PROBABILITY_NAMES
    metrics = {name: reference['metrics'][other] for name, other in mapping.items()}
    if family == 'eta':
        counts_out.update(fully_available_eligible_trips=counts['fully_available_eligible_trips'],
                          all_eligible_snapshots_covered_trips=counts['whole_trip_covered'])
        if variant == 'raw':
            for q in (.1, .5, .9):
                metrics[f'pinball_{q:.1f}_seconds'] = reference['metrics']['pinball_'+str(q)]
        reasons = {name: ('no_eligible_trips' if name == 'trip_all_eligible_snapshots_coverage' else
                         'no_fully_available_eligible_trips' if name == 'trip_all_snapshots_coverage_given_full_availability' else
                         'no_scored_snapshots') for name, value in metrics.items() if value is None}
    else:
        sizes = Counter(trip(r) for r in scored)
        metrics['weighted_event_rate'] = ratio(math.fsum(r['delay_label']/sizes[trip(r)] for r in scored), len(sizes))
        metrics['mean_probability'] = ratio(math.fsum(r['probability']/sizes[trip(r)] for r in scored), len(sizes))
        counts_out['log_loss_clipped_snapshots'] = counts['log_loss_clipped_snapshots']
        reasons = {name: ('single_class_scored_labels' if scored and name in ('roc_auc', 'average_precision') else
                         'no_scored_snapshots') for name, value in metrics.items() if value is None}
    result = {'counts': counts_out, 'metrics': metrics, 'undefined_reasons': reasons, 'sparse': len({trip(r) for r in scored}) < 20,
              'prediction_source_counts': dict(sorted(Counter(str(r.get('prediction_source') or 'unknown') for r in rows).items())),
              'calibration_status_counts': {name: sum(r.get('calibration_status') == name for r in rows)
                                           for name in ('not_applied', 'fitted', 'unsupported', 'unavailable')},
              'exclusion_reason_counts': dict(sorted(Counter(str(r.get('reason') or 'unknown') for r in rows if not eligible(r)).items())),
              'availability_fraction': ratio(sum(available(r) for r in rows), len(rows)),
              'availability_undefined_reason': None if rows else 'no_opportunities'}
    def source_bucket(row):
        return ('unavailable' if not available(row) else 'fallback'
                if row['prediction_source'] in ('baseline_fallback', 'prevalence_fallback') else 'native')
    result['native_fallback_unavailable_support'] = {}
    for bucket in ('native', 'fallback', 'unavailable'):
        selected = [r for r in rows if source_bucket(r) == bucket]
        result['native_fallback_unavailable_support'][bucket] = {
            'opportunities': len(selected), 'trips': len({trip(r) for r in selected}),
            'eligible_opportunities': sum(eligible(r) for r in selected),
            'scored_opportunities': sum(eligible(r) and available(r) for r in selected)}
    if family == 'eta':
        coverage = metrics['trip_all_eligible_snapshots_coverage']
        result['eta_whole_trip_point_target_met'] = coverage >= .8 if coverage is not None else None
    else:
        result.update(eligible_class_support=class_support(accepted), scored_class_support=class_support(scored))
        bins = []
        for b in reference['reliability']:
            original = [r for r in scored if b['lower'] <= r['probability'] < b['upper'] or
                        b['upper_inclusive'] and r['probability'] == 1]
            bins.append({'bin_index': b['bin'], 'lower': b['lower'], 'upper': b['upper'],
                         'upper_inclusive': b['upper_inclusive'], 'snapshots': b['snapshots'], 'trips': b['trips'],
                         **class_support(original), 'cohort_weight': b['weight'], 'cohort_weight_fraction': b['weight_fraction'],
                         'mean_probability': b['mean_probability'], 'observed_event_rate': b['observed_frequency'],
                         'sparse': b['trips'] < 20, 'undefined_reason': None if b['snapshots'] else 'empty_fixed_bin'})
        result['reliability'] = bins
    contribution_names = {name: ('median_mse_seconds2' if name == 'median_rmse_seconds' else other)
                          for name, other in mapping.items()}
    contributions = {name: reference['trip_contributions'][other] for name, other in contribution_names.items()
                     if other in reference['trip_contributions']}
    return result, contributions


class BootstrapReference:
    """Integer multiplicities built independently, with support-only caching."""
    def __init__(self, memberships):
        self.keys = sorted(trip(r) for r in memberships)
        self.draws = [Counter(sample) for sample in bootstrap_draws(memberships)]
        self.cache = {}
        self.interval_cache = {}
        encoded = b''.join(struct.pack('<i', sample[key]) for sample in self.draws for key in self.keys)
        self.draw_counts_sha256 = hashlib.sha256(encoded).hexdigest()

    def interval(self, values, name, right=None, scored_trips=None):
        keys = tuple(sorted(values))
        scored_trips = len(keys) if scored_trips is None else scored_trips
        identity = (name, tuple((key, values[key]) for key in keys), scored_trips,
                    tuple((key, right[key]) for key in keys) if right is not None else None)
        if identity in self.interval_cache:
            return self.interval_cache[identity]
        if right is not None:
            require(set(values) == set(right), 'Paired contribution trip sets differ')
        if keys not in self.cache:
            self.cache[keys] = [[(key, sample[key]) for key in keys if sample[key]] for sample in self.draws]
        samples = self.cache[keys]
        undefined = sum(not sample for sample in samples)
        reason = ('fewer_than_20_original_scored_trips' if scored_trips < 20 else
                  'undefined_replicate_denominator' if undefined else None)
        endpoints = []
        if reason is None:
            for sample in samples:
                denominator = sum(count for _, count in sample)
                current = math.fsum(values[key]*count for key, count in sample)/denominator
                if name == 'median_rmse_seconds':
                    current = math.sqrt(current)
                if right is not None:
                    paired = math.fsum(right[key]*count for key, count in sample)/denominator
                    if name == 'median_rmse_seconds':
                        paired = math.sqrt(paired)
                    current = paired-current
                endpoints.append(current)
        result = {'lower': quantile(endpoints, .025), 'upper': quantile(endpoints, .975),
                'confidence_level': .95, 'undefined_reason': reason,
                'original_contributing_trips': len(keys), 'original_scored_trips': scored_trips, 'replicates_requested': 2000,
                'defined_replicates': 2000-undefined, 'undefined_replicates': undefined,
                'quantile_interpolation': 'linear_r_(n-1)*p'}
        self.interval_cache[identity] = result
        return result


def verify_forecast_metrics(eta_rows, probability_rows, document, memberships, audit, path, bootstrap=None):
    bootstrap = bootstrap or BootstrapReference(memberships)
    audit.subset(document['bootstrap_design'], {'draw_counts_sha256': bootstrap.draw_counts_sha256,
                 'inventory_trips': len(memberships), 'seed': 20261111, 'replicates': 2000,
                 'auc_ap_bootstrapped': False}, path+'/bootstrap_design')
    for family, rows in [('eta', eta_rows), ('probability', probability_rows)]:
        groups = defaultdict(list)
        for row in rows:
            groups[row['method_id']+'/'+row['variant']].append(row)
        baseline = 'historical_baseline' if family == 'eta' else 'fold_prevalence'
        required = {'pipeline/raw', 'pipeline/calibrated', 'pipeline/canonical', baseline+'/raw'}
        audit.compare(sorted(groups), sorted(required), path+'/'+family+'/method_inventory')
        actual = document[family]
        audit.compare(sorted(actual['methods']), sorted(required), path+'/'+family+'/reported_methods')
        def verify_group(members, record, variant, label):
            reference, contributions = metric_reference_record(members, family, variant)
            audit.subset(record, reference, label)
            intervals = {name: bootstrap.interval(values, name, scored_trips=reference['counts']['scored_trips'])
                         for name, values in contributions.items()}
            audit.compare(record['bootstrap_intervals'], intervals, label+'/bootstrap_intervals')
        for method in sorted(groups):
            verify_group(groups[method], actual['methods'][method], method.split('/')[1], path+'/'+family+'/'+method)
        defaults = {'novelty_group': NOVELTY, 'departure_month': ('2026-08', '2026-09'),
                    'position_validity': ('valid', 'low_confidence', 'no_position'),
                    'early_trip': ('below_3600', 'at_least_3600'), 'elapsed_7200': ('below_7200', 'at_least_7200'),
                    'deadline_advance': ('overdue', '[0,1800]', '(1800,7200]', '>7200'),
                    'calibration_status': ('not_applied', 'fitted', 'unsupported', 'unavailable')}
        domains = {field: set(defaults.get(field, ())) | {slice_value(r, field) for r in rows} for field in SLICE_FIELDS}
        expected_slices = {(method, field, value) for method in groups for field, values in domains.items() for value in values}
        seen = set()
        for item in actual['slices']:
            method = item['method_id']+'/'+item['variant']
            identity = method, item['field'], item['value']
            require(identity not in seen, 'Duplicate forecast slice')
            seen.add(identity)
            members = [r for r in groups[method] if slice_value(r, item['field']) == item['value']]
            verify_group(members, item, item['variant'], path+'/'+family+'/slice/'+str(identity))
        audit.compare(sorted(seen), sorted(expected_slices), path+'/'+family+'/slice_inventory')
        expected_pairs = {('pipeline/raw', 'pipeline/calibrated'), ('pipeline/calibrated', 'pipeline/canonical'),
                          (baseline+'/raw', 'pipeline/raw'), (baseline+'/raw', 'pipeline/calibrated')}
        pair_scopes = {('overall', 'all')} | {(field, value) for field, values in domains.items()
                                              if field not in ('prediction_source', 'calibration_status') for value in values}
        seen = set()
        for pair in actual['paired_comparisons']:
            left, right, field, value = (pair[k] for k in ('left_method', 'right_method', 'field', 'value'))
            identity = left, right, field, value
            require(identity not in seen, 'Duplicate paired comparison')
            seen.add(identity)
            a = [r for r in groups[left] if field == 'overall' or slice_value(r, field) == value]
            b = [r for r in groups[right] if field == 'overall' or slice_value(r, field) == value]
            aa, bb, counts = common_support(a, b)
            _, ac = metric_reference_record(aa, family, left.split('/')[1])
            _, bc = metric_reference_record(bb, family, right.split('/')[1])
            if family == 'eta':
                name = 'trip_all_eligible_snapshots_coverage'
                ac[name] = reference_eta(a)['trip_contributions']['whole_trip_coverage']
                bc[name] = reference_eta(b)['trip_contributions']['whole_trip_coverage']
            metrics = {}
            for name, values in ac.items():
                first, second = mean(values.values()), mean(bc[name].values())
                if name == 'median_rmse_seconds' and first is not None:
                    first, second = math.sqrt(first), math.sqrt(second)
                metrics[name] = {'left': first, 'right': second, 'right_minus_left': second-first if first is not None else None,
                                 'undefined_reason': None if first is not None else 'no_fixed_paired_support',
                                 'support': 'all_original_eligible_trips_missing_forecasts_are_misses' if name == 'trip_all_eligible_snapshots_coverage' else
                                            'fixed_common_eligible_available_snapshots',
                                 'interval': bootstrap.interval(values, name, bc[name], scored_trips=counts['common_trips'])}
            audit.compare(pair['metrics'], metrics, path+'/'+family+'/pair/'+str(identity))
            audit.subset(pair['counts'], {'all_opportunities': len(a), 'eligible_snapshots': sum(eligible(r) for r in a),
                         'left_scored_snapshots': sum(eligible(r) and available(r) for r in a),
                         'right_scored_snapshots': sum(eligible(r) and available(r) for r in b),
                         'paired_scored_snapshots': counts['common_snapshots'], 'paired_scored_trips': counts['common_trips'],
                         'left_only_scored_snapshots': counts['left_lost_snapshots'],
                         'right_only_scored_snapshots': counts['right_lost_snapshots'],
                         'left_scored_trips': len({trip(r) for r in a if eligible(r) and available(r)}),
                         'right_scored_trips': len({trip(r) for r in b if eligible(r) and available(r)}),
                         'left_trips_lost_from_paired_support': counts['left_lost_trips'],
                         'right_trips_lost_from_paired_support': counts['right_lost_trips']}, path+'/'+family+'/pair_support/'+str(identity))
            audit.compare(pair['paired_support_sha256'], digest_json(sorted(snapshot(r) for r in aa)), path+'/paired_support_hash')
        expected = {(left, right, field, value) for left, right in expected_pairs for field, value in pair_scopes}
        audit.compare(sorted(seen), sorted(expected), path+'/'+family+'/pair_inventory')


def clock_distribution(values):
    values = list(values)
    result = distribution(values)
    result.update(N=result.pop('n'), missing=sum(v is None for v in values), unit='seconds',
                  null_reason=None if any(v is not None for v in values) else 'no_observed_values')
    return result


def arrival_advance(label, when):
    trusted = (eligible(label) or label['reason'] == 'already_arrived_at_snapshot')
    if not trusted or label.get('label_status') != 'verified' or label.get('arrival_time_precision') != 'second' or not label.get('actual_arrival_time'):
        return None
    return (clock(label['actual_arrival_time'])-clock(when)).total_seconds()


def policy_slice_value(row, dimension, scenario):
    if dimension == 'novelty_group':
        return ('unseen_vehicle' if row['unseen_vehicle'] else 'seen_vehicle')+'_'+('unseen_route' if row['unseen_route'] else 'seen_route')
    if dimension == 'original_position_validity':
        return row['position_validity']
    if dimension in ('early_trip', 'duration_shift'):
        boundary = 3600 if dimension == 'early_trip' else 7200
        return ('elapsed_lt_' if row['elapsed_trip_seconds'] < boundary else 'elapsed_ge_')+str(boundary)
    if dimension == 'deadline_advance':
        lead = (clock(row['deadline_at'])-clock(row['snapshot_time'])).total_seconds()
        return 'overdue' if lead < 0 else '0_to_1800' if lead <= 1800 else 'gt_1800_to_7200' if lead <= 7200 else 'gt_7200'
    if dimension == 'probability_calibration':
        return row['probability_calibration_status']
    if dimension == 'delivery_scenario':
        return scenario
    if dimension in ('eta_source', 'probability_source'):
        kind = dimension.removesuffix('_source')
        if row[kind+'_status'] == 'unavailable':
            return 'unavailable'
        return 'native' if row[dimension] in ('candidate', 'model', 'native', 'gradient_boosting_quantile', 'logistic_delay') else 'fallback'
    return slice_value(row, dimension)


def policy_metric_references(opportunities, targets, policy, delivery, settings, policy_ref, delivery_ref,
                             first_candidates, first_successes, effective):
    """Reuse independent engines; retain global first-event identities in slices."""
    keys, trips = {snapshot(r) for r in opportunities}, {trip(r) for r in opportunities}
    labels = index(targets)
    candidates = [r for r in policy['candidates'] if snapshot(r) in keys]
    decisions = [r for r in policy['decisions'] if snapshot(r) in keys]
    episode_ids = {r['episode_id'] for r in decisions if r.get('episode_id')}
    segment = dict(policy, candidates=candidates, decisions=decisions,
                   episodes=[r for r in policy['episodes'] if r['episode_id'] in episode_ids])
    candidate = policy_ref.metric_reference(opportunities, targets, segment)
    first = [r for r in candidates if (r['tenant_id'], r['candidate_id']) in first_candidates]
    classes = Counter(policy_ref.classification(labels[snapshot(r)]) for r in first)
    mature, n = classes['true']+classes['false'], len(first)
    candidate['counts'].update(first_candidates=n, confirmed_true_first_candidates=classes['true'],
        known_false_first_candidates=classes['false'], unresolved_first_candidates=classes['unresolved'],
        retrospectively_post_arrival_first_candidates=classes['post_arrival'])
    first_in_episode = {}
    for row in sorted(policy['candidates'], key=lambda r: (clock(r['snapshot_time']), *trip(r), r['snapshot_id'])):
        first_in_episode.setdefault((row['tenant_id'], row['episode_id']), row['candidate_id'])
    candidate['counts']['within_slice_repeated_candidates'] = candidate['counts']['repeated_candidates_within_episode']
    candidate['counts']['within_slice_additional_candidates_per_trip'] = candidate['counts']['additional_candidates_per_trip']
    candidate['counts']['repeated_candidates_within_episode'] = sum(
        first_in_episode[r['tenant_id'], r['episode_id']] != r['candidate_id'] for r in candidates)
    candidate['counts']['additional_candidates_per_trip'] = sum(
        (r['tenant_id'], r['candidate_id']) not in first_candidates for r in candidates)
    candidate['denominators'].update(conditional_first_candidate_precision=mature, first_candidate_target_coverage=n,
                                     first_candidate_precision_bounds=n)
    candidate['metrics'].update(conditional_first_candidate_precision=ratio(classes['true'], mature),
        first_candidate_target_coverage=ratio(mature, n), first_candidate_precision_bounds={
            'lower': ratio(classes['true'], n), 'upper': ratio(classes['true']+classes['unresolved'], n)})
    candidate['lead_seconds'] = {name: {
        'prospective_deadline_lead_seconds': clock_distribution(r['lead_seconds'] for r in group),
        'retrospective_arrival_lead_seconds': clock_distribution(arrival_advance(labels[snapshot(r)], r['simulated_issue_at']) for r in group)}
        for name, group in [('all_candidates', candidates), ('first_candidates', first)]}
    simulated = {name: [r for r in delivery[name] if (trip(r) in trips if name == 'episodes' else snapshot(r) in keys)]
                 for name in ('alerts', 'attempts', 'episodes')}
    observed = delivery_ref.reference_metrics(opportunities, targets, candidates, simulated,
                {'delivery': settings['delivery'], 'metrics': {'sparse_trip_threshold': 20}})
    details = observed['delivery_rows']
    for row in details:
        row['first_successful_delivery_for_trip'] = (row['tenant_id'], row['delivery_id']) in first_successes
    first = [r for r in details if r['first_successful_delivery_for_trip']]
    useful = [r for r in details if (r['tenant_id'], r['delivery_id']) in effective]
    classes = Counter(r['classification'] for r in first)
    n, assessable = len(first), sum(r['classification'] != 'unresolved' for r in first)
    mature = sum(r['target_eligibility'] == 'eligible' for r in first)
    observed['counts'].update(first_successful_deliveries=n, mature_eligible_first_deliveries=mature,
        known_assessable_first_deliveries=assessable, useful_first_deliveries=classes['useful_true'],
        known_false_first_deliveries=classes['known_false'], unresolved_first_deliveries=classes['unresolved'],
        post_arrival_first_deliveries=classes['post_arrival'], insufficient_lead_first_deliveries=classes['insufficient_lead'],
        first_effective_deliveries=len(useful))
    first_in_episode = {}
    cutoff = clock(settings['delivery']['counts_as_of'])
    successes = [r for r in delivery['attempts'] if r['delivery_status'] == 'delivered' and r['delivered_at'] and
                 r['receipt_known_at'] and clock(r['receipt_known_at']) <= cutoff]
    for row in sorted(successes, key=lambda r: (clock(r['delivered_at']), *trip(r), r['alert_id'], r['delivery_id'])):
        first_in_episode.setdefault((row['tenant_id'], row['alert_episode_id']), row['delivery_id'])
    observed['counts']['within_slice_repeated_deliveries'] = observed['counts']['repeated_deliveries_within_episode']
    observed['counts']['within_slice_additional_deliveries_per_trip'] = observed['counts']['additional_deliveries_per_trip']
    observed['counts']['repeated_deliveries_within_episode'] = sum(
        first_in_episode[r['tenant_id'], r['alert_episode_id']] != r['delivery_id'] for r in details)
    observed['counts']['additional_deliveries_per_trip'] = sum(
        (r['tenant_id'], r['delivery_id']) not in first_successes for r in details)
    observed['denominators'].update(conditional_first_delivery_useful_precision=assessable,
        first_delivery_usefulness_bounds=n, first_delivery_target_coverage=n, first_delivery_assessable_coverage=n,
        all_observed_positive_delivered_trip_recall=observed['counts']['observed_positive_trips'],
        attempts_per_100_monitored_trips=len(trips), candidates_per_100_monitored_trips=len(trips))
    observed['metrics'].update(conditional_first_delivery_useful_precision=ratio(classes['useful_true'], assessable),
        first_delivery_usefulness_bounds={'lower': ratio(classes['useful_true'], n), 'upper': ratio(classes['useful_true']+classes['unresolved'], n)},
        first_delivery_target_coverage=ratio(mature, n), first_delivery_assessable_coverage=ratio(assessable, n),
        all_observed_positive_delivered_trip_recall=ratio(observed['counts']['confirmed_useful_delivery_trips'], observed['counts']['observed_positive_trips']),
        attempts_per_100_monitored_trips=ratio(100*observed['counts']['attempts'], len(trips)),
        candidates_per_100_monitored_trips=ratio(100*len(candidates), len(trips)))
    observed['lead_seconds'], observed['timing_rates'] = {}, {}
    for name, group in [('all_successful_deliveries', details), ('first_successful_deliveries', first), ('first_effective_deliveries', useful)]:
        observed['lead_seconds'][name] = {field: clock_distribution(r[field] for r in group) for field in
                ('prospective_deadline_lead_seconds', 'retrospective_arrival_lead_seconds',
                 'candidate_to_delivery_seconds', 'successful_attempt_latency_seconds')}
        arrivals = [r['retrospective_arrival_lead_seconds'] for r in group if r['retrospective_arrival_lead_seconds'] is not None]
        counts = {'deliveries': len(group), 'valid_arrival_times': len(arrivals),
                  'below_1800': sum(r['prospective_deadline_lead_seconds'] < 1800 for r in group),
                  'at_or_after_deadline': sum(r['prospective_deadline_lead_seconds'] <= 0 for r in group),
                  'at_or_after_actual_arrival': sum(v <= 0 for v in arrivals)}
        observed['timing_rates'][name] = {'counts': counts, 'below_1800': ratio(counts['below_1800'], len(group)),
              'at_or_after_deadline': ratio(counts['at_or_after_deadline'], len(group)),
              'at_or_after_actual_arrival': ratio(counts['at_or_after_actual_arrival'], len(arrivals)),
              'null_reasons': {**({} if group else {'below_1800': 'no_deliveries', 'at_or_after_deadline': 'no_deliveries'}),
                               **({} if arrivals else {'at_or_after_actual_arrival': 'no_valid_arrival_times'})}}
    for result in (candidate, observed):
        result['null_reasons'] = {name: 'zero_declared_denominator' for name, value in result['metrics'].items() if value is None}
    if candidate['counts']['first_candidates'] == 0:
        candidate['null_reasons']['first_candidate_precision_bounds'] = 'no_global_first_candidates_in_cohort'
    if observed['counts']['first_successful_deliveries'] == 0:
        observed['null_reasons']['first_delivery_usefulness_bounds'] = 'no_global_first_successes_in_cohort'
    return candidate, observed


def verify_policy_delivery(opportunities, targets, actual_bundle, document, settings, scenario, policy_ref, delivery_ref, audit):
    path = scenario+'/policy_delivery'
    projected = [{name: row[name] for name in policy_ref.OPPORTUNITY_FIELDS} for row in opportunities]
    reference = policy_ref.reference_simulation(projected, settings['pipeline']['selected_rule']['threshold'])
    audit.compare(actual_bundle['policy'], {name: value for name, value in reference.items() if name != 'rule'}, path+'/candidate_ledger')
    audit.compare(actual_bundle['rule'], reference['rule'], path+'/selected_rule')
    transport_settings = deepcopy(settings['delivery'])
    if scenario in ('S07_transport_degraded', 'S08_missing_and_degraded'):
        transport_settings.update(next(s for s in settings['stress']['scenarios'] if s['id'] == 'S07_transport_degraded')['delivery_overrides'])
    transport = delivery_ref.reference_delivery(reference['candidates'], reference['episodes'], {'delivery': transport_settings})
    delivery_ref.verify_simulation(actual_bundle['delivery'], transport, audit)
    first_candidate = {}
    for row in sorted(reference['candidates'], key=lambda r: (clock(r['snapshot_time']), *trip(r), r['snapshot_id'])):
        first_candidate.setdefault(trip(row), row['candidate_id'])
    cutoff = clock(settings['delivery']['counts_as_of'])
    successful = [r for r in transport['attempts'] if r['delivery_status'] == 'delivered' and
                  r['receipt_known_at'] is not None and clock(r['receipt_known_at']) <= cutoff and r['delivered_at'] is not None]
    first_delivery = {}
    for row in sorted(successful, key=lambda r: (clock(r['delivered_at']), *trip(r), r['alert_id'], r['delivery_id'])):
        first_delivery.setdefault(trip(row), row['delivery_id'])
    for field, values, id_name in [('first_candidate_ids', first_candidate, 'candidate_id'),
                                  ('first_successful_delivery_ids', first_delivery, 'delivery_id')]:
        expected = [{'tenant_id': key[0], 'trip_id': key[1], id_name: value} for key, value in sorted(values.items())]
        audit.compare(actual_bundle[field], expected, path+'/'+field)
        audit.compare(document[field], expected, path+'/metrics/'+field)
    initial = delivery_ref.reference_metrics(projected, targets, reference['candidates'], transport,
                {'delivery': transport_settings, 'metrics': {'sparse_trip_threshold': 20}})
    expected_event_slices = {}
    details = initial['delivery_rows']
    attempts = {(r['tenant_id'], r['delivery_id']): r for r in transport['attempts']}
    for value in ('first_attempt_success', 'retry_success', 'never_confirmed'):
        included = []
        for alert in transport['alerts']:
            group = [r for r in details if r['tenant_id'] == alert['tenant_id'] and r['alert_id'] == alert['alert_id']]
            kind = ('never_confirmed' if not group else 'first_attempt_success' if
                    min(attempts[r['tenant_id'], r['delivery_id']]['attempt_index'] for r in group) == 1 else 'retry_success')
            if kind == value:
                included.append(alert)
        identities = {(r['tenant_id'], r['alert_id']) for r in included}
        group = [r for r in details if (r['tenant_id'], r['alert_id']) in identities]
        expected_event_slices['alert_transport_path', value] = {'alerts': len(included),
            'successful_deliveries': len(group), 'first_successful_deliveries': sum(r['first_successful_delivery_for_trip'] for r in group),
            'classification_counts': dict(sorted(Counter(r['classification'] for r in group).items())),
            'candidate_to_delivery_seconds': clock_distribution(r['candidate_to_delivery_seconds'] for r in group)}
    first_details = [r for r in details if r['first_successful_delivery_for_trip']]
    for dimension, values in [('first_delivered_lead', ('ge_1800', '0_to_lt_1800', 'negative')),
                              ('first_delivery_target', ('known', 'unknown', 'post_arrival'))]:
        for value in values:
            def category(r):
                if dimension == 'first_delivery_target':
                    return 'post_arrival' if r['classification'] == 'post_arrival' else 'known' if r['target_eligibility'] == 'eligible' else 'unknown'
                lead = r['prospective_deadline_lead_seconds']
                return 'ge_1800' if lead >= 1800 else '0_to_lt_1800' if lead >= 0 else 'negative'
            group = [r for r in first_details if category(r) == value]
            expected_event_slices[dimension, value] = {'first_successful_deliveries': len(group),
                'classification_counts': dict(sorted(Counter(r['classification'] for r in group).items())),
                'prospective_deadline_lead_seconds': clock_distribution(r['prospective_deadline_lead_seconds'] for r in group)}
    seen_events = set()
    for item in document['delivery_slices']:
        key = item['dimension'], item['value']
        require(key not in seen_events and key in expected_event_slices, 'Duplicate/unknown delivery event slice')
        seen_events.add(key)
        audit.subset(item, expected_event_slices[key], path+'/event_slice/'+str(key))
    audit.compare(sorted(seen_events), sorted(expected_event_slices), path+'/event_slice_inventory')
    effective = {}
    for row in initial['delivery_rows']:
        if row['classification'] == 'useful_true':
            effective.setdefault(trip(row), row['delivery_id'])
    audit.compare(document['first_effective_delivery_ids'],
                  [{'tenant_id': key[0], 'trip_id': key[1], 'delivery_id': value} for key, value in sorted(effective.items())], path+'/first_effective')
    first_c = {(key[0], value) for key, value in first_candidate.items()}
    first_d = {(key[0], value) for key, value in first_delivery.items()}
    first_e = {(key[0], value) for key, value in effective.items()}
    def check_scope(rows, record, suffix):
        keys = {snapshot(r) for r in rows}
        chosen_targets = [r for r in targets if snapshot(r) in keys]
        candidate, delivery = policy_metric_references(rows, chosen_targets, reference, transport, settings,
                            policy_ref, delivery_ref, first_c, first_d, first_e)
        audit.subset(record['candidate'], candidate, path+suffix+'/candidate')
        audit.subset(record['delivery'], delivery, path+suffix+'/delivery')
    check_scope(projected, document, '/overall')
    domains = {'novelty_group': NOVELTY, 'tenant_id': sorted({r['tenant_id'] for r in projected}),
               'departure_month': ('2026-08', '2026-09'), 'route_id': sorted({r['route_id'] for r in projected}),
               'original_position_validity': ('valid', 'low_confidence', 'no_position'),
               'early_trip': ('elapsed_lt_3600', 'elapsed_ge_3600'), 'duration_shift': ('elapsed_lt_7200', 'elapsed_ge_7200'),
               'deadline_advance': ('overdue', '0_to_1800', 'gt_1800_to_7200', 'gt_7200'),
               'probability_calibration': ('fitted', 'unsupported', 'unavailable'),
               'delivery_scenario': (scenario,), 'eta_source': ('native', 'fallback', 'unavailable'),
               'probability_source': ('native', 'fallback', 'unavailable')}
    expected = {(field, value) for field, values in domains.items() for value in values}
    seen = set()
    for item in document['subgroups']:
        identity = item['dimension'], item['value']
        require(identity not in seen, 'Duplicate policy subgroup')
        seen.add(identity)
        rows = [r for r in projected if policy_slice_value(r, identity[0], scenario) == identity[1]]
        audit.compare(item['empty'], not rows, path+'/empty/'+str(identity))
        check_scope(rows, item, '/subgroup/'+str(identity))
    audit.compare(sorted(seen), sorted(expected), path+'/subgroup_inventory')
    ntrips = len({trip(r) for r in projected})
    burden = ratio(100*len(reference['candidates']), ntrips)
    audit.compare(document['quality_descriptors'], {'candidate_budget_50_per_100_met': burden <= 50 if burden is not None else None,
        'candidate_daily_cap_2_met': all(r['candidates'] <= 2 for r in reference['summary']['daily_candidate_counts']),
        'quality_accepted': False, 'operational_approval': False}, path+'/quality_descriptors')
    audit.subset(actual_bundle, {'labels_used': False, 'threshold_reselected': False, 'transport_redrawn': False}, path+'/label_free_flags')
    audit.subset(document, {'subgroup_admission_rerun': False, 'transport_redrawn': False, 'policy_delivery_bootstrap': False,
                           'recipient_behavior_evaluated': False, 'operational_benefit_established': False}, path+'/scope_flags')


class Reader:
    def __init__(self):
        self.hashes = {}

    def hash(self, path):
        path = Path(path).resolve()
        current = digest_file(path)
        require(path not in self.hashes or self.hashes[path] == current, 'Artifact changed during audit: '+str(path))
        self.hashes[path] = current
        return current

    def json(self, path):
        self.hash(path)
        return json.loads(Path(path).read_text())

    def rows(self, path):
        self.hash(path)
        with Path(path).open() as stream:
            return [json.loads(line) for line in stream if line.strip()]

    def stable(self):
        for path, expected in self.hashes.items():
            require(digest_file(path) == expected, 'Artifact changed during audit: '+str(path))


def verify_registration_record(frozen, settings):
    require(frozen['status'] in ('frozen_before_final_target_access', 'registered_correction_after_final_target_exposure'),
            'Unknown implementation registration status')
    corrected = frozen['status'] == 'registered_correction_after_final_target_exposure'
    require(frozen.get('final_outcomes_accessed') is corrected and corrected == ('correction' in frozen),
            'Registration does not disclose prior target exposure consistently')
    require(frozen.get('base_public_commit') == 'e95dcd7f885e20af19f51fc645c9e3f577dedd97', 'Registration base differs')
    require(frozen.get('derived_reporting_exclusions') == sorted(DERIVED_EVIDENCE), 'Registered exclusions differ')
    for field in ('model_or_calibrator_refitted', 'policy_reselected', 'quality_accepted', 'operational_approval'):
        require(frozen.get(field) is False, 'Registration scope changed: '+field)
    require(frozen.get('july_model_calibration_policy_and_delivery_results_previously_observed') is True,
            'Known July exposure must be disclosed')
    require(frozen.get('metadata_inventory_sha256') == digest_json(frozen['metadata_inventory']), 'Metadata freeze digest differs')
    if corrected:
        correction = frozen['correction']
        require(correction.get('kind') == 'engineering_correction_after_final_target_exposure' and
                correction.get('scientific_protocol_or_inputs_changed') is False and
                correction.get('untouched_final_test_claim') is False, 'Invalid disclosed correction scope')
        previous = correction['prior_evidence_sha256']
        require(set(previous) in ({'implementation_freeze.json', 'pretarget_receipt.json', 'exposure_record.json', 'failure_record.json'},
                {'implementation_freeze.json', 'pretarget_receipt.json', 'exposure_record.json', 'run_receipt.json'}),
                'Correction predecessor evidence is incomplete')
        require(correction['prior_implementation_freeze_sha256'] == previous['implementation_freeze.json'], 'Prior freeze digest differs')
        require(clock(correction['prior_exposure_started_at_utc']) <= clock(frozen['recorded_at_utc']), 'Correction precedes prior exposure')
    qa = frozen['conformance']
    require(qa['status'] == 'passed_pre_target_conformance' and type(qa['failures']) is int and qa['failures'] == 0 and
            type(qa['skips']) is int and qa['skips'] == 0 and type(qa['unit_tests']) is int and qa['unit_tests'] > 577 and
            qa['final_outcomes_accessed'] is False and qa['source_sha256'] == frozen['source_sha256'],
            'Implementation lacks passing source-bound conformance tests')
    require(clock(qa['started_at_utc']) <= clock(qa['completed_at_utc']) <= clock(frozen['recorded_at_utc']),
            'Conformance/registration clocks are out of order')
    guard = qa['access_guard']
    require(guard['version'] == 'p08-python-outcome-access-v1' and
            type(guard['protected_outcome_opens_allowed']) is int and guard['protected_outcome_opens_allowed'] == 0 and
            type(guard['guarded_processes']) is int and guard['guarded_processes'] >= 1 and
            isinstance(guard['scope'], str) and bool(guard['scope']) and
            isinstance(guard['events_sha256'], str) and len(guard['events_sha256']) == 64,
            'Observed conformance access guard is missing')
    require(frozen['input_sha256'] == settings['input_sha256'], 'Implementation source pins differ')
    return corrected


def verify_artifact_gates(root, report, reader):
    protocol = root/'config/p08_evaluation.json'
    require(reader.hash(protocol) == 'b9d07fee882785b2de3325ec78048c481e6dc6ee0553bc1830786e597187d194', 'Final scientific protocol changed')
    settings = reader.json(protocol)
    for path, expected in settings['artifact_sha256'].items():
        require(reader.hash(contained(root, path)) == expected, 'Pinned artifact changed: '+path)
    for name, expected in settings['existing_core_sha256'].items():
        require(reader.hash(contained(root, 'src/ds_m01/'+name)) == expected, 'Pinned core changed: '+name)
    for name in ('protocol_freeze.json', 'evaluation_readiness.json'):
        record = reader.json(root/'reproducibility/p07'/name)
        for path, expected in record['artifact_sha256'].items():
            require(reader.hash(contained(root, path)) == expected, 'Historical readiness pin changed: '+path)
    frozen = reader.json(report/'implementation_freeze.json')
    corrected = verify_registration_record(frozen, settings)
    require(frozen['protocol_sha256'] == reader.hash(protocol), 'Implementation protocol differs')
    require(frozen['input_sha256'] == settings['input_sha256'], 'Implementation source pins differ')
    paths = set()
    for folder in ('src', 'scripts', 'tests'):
        paths.update(str(p.relative_to(root)) for p in (root/folder).rglob('*.py') if '__pycache__' not in p.parts)
    for folder in ('config', 'contracts'):
        paths.update(str(p.relative_to(root)) for p in (root/folder).rglob('*') if p.is_file())
    paths -= DERIVED_EVIDENCE
    require(paths == set(frozen['source_sha256']), 'Implementation inventory omissions or additions')
    for name, expected in frozen['source_sha256'].items():
        require(reader.hash(contained(root, name)) == expected, 'Registered source changed: '+name)
        require(reader.hash(contained(report/'implementation_source', name)) == expected, 'Retained implementation source differs: '+name)
    for name, expected in frozen['historical_report_sha256'].items():
        require(reader.hash(contained(root, name)) == expected, 'Historical report changed: '+name)
    historical_paths = {str(p.relative_to(root)) for p in (root/'reports').rglob('*') if p.is_file() and p.relative_to(root/'reports').parts[0] != 'p08'}
    require(historical_paths == set(frozen['historical_report_sha256']), 'Historical evidence inventory differs')
    pretarget = reader.json(report/'pretarget_receipt.json')
    require(pretarget['status'] == 'all_label_free_scenarios_retained_before_target_access' and
            pretarget['final_outcomes_opened'] is False and pretarget['scenario_ids'] == list(SCENARIOS),
            'Missing complete label-free scenario gate')
    require(pretarget['metadata_inventory'] == frozen['metadata_inventory'], 'Metadata inventory changed after registration')
    mandatory = {'inventory.json', 'implementation_freeze.json', 'inputs/inventory.json', 'inputs/source_sha256.json'}
    mandatory |= {'inputs/'+name+'.jsonl' for name in ('memberships', 'snapshots', 'features', 'promise_features', 'promises')}
    for scenario in SCENARIOS:
        mandatory |= {'scenarios/'+scenario+'/'+name+'.jsonl' for name in ('forecast_eta', 'forecast_probability', 'opportunities',
                      'features', 'matrices', 'projection_audit', 'stress_audit', 'model_registry')}
        mandatory |= {'scenarios/'+scenario+'/'+name+'.json' for name in ('stress_summary', 'policy_delivery')}
    mandatory |= {'implementation_source/'+name for name in frozen['source_sha256']}
    require(mandatory <= set(pretarget['output_sha256']), 'Pre-target manifest omits required metadata/scenario artifacts')
    for field in ('quality_accepted', 'model_or_calibrator_refitted', 'policy_reselected'):
        require(pretarget.get(field) is False, 'Pre-target scope changed: '+field)
    for name, expected in pretarget['output_sha256'].items():
        require(not any(forbidden in name for forbidden in ('outcome_source', 'eta_targets', 'probability_targets',
                    'forecast_metrics', 'policy_delivery_metrics')), 'Target-bearing file in pre-target manifest')
        require(reader.hash(contained(report, name)) == expected, 'Label-free artifact changed: '+name)
    exposure = reader.json(report/'exposure_record.json')
    require(exposure['status'] == 'final_outcome_access_started' and exposure['pretarget_receipt_sha256'] == reader.hash(report/'pretarget_receipt.json'),
            'Outcome exposure was not bound to the complete pre-target receipt')
    require(exposure['expected_source_sha256'] == settings['input_sha256']['data/raw/eta_trip_outcome.jsonl'], 'Outcome exposure source pin differs')
    require(clock(frozen['recorded_at_utc']) <= clock(pretarget['completed_at_utc']) <= clock(exposure['started_at_utc']),
            'Implementation/inference/outcome clocks are out of order')
    run = reader.json(report/'run_receipt.json')
    require(clock(frozen['recorded_at_utc']) <= clock(run['started_at_utc']) <= clock(pretarget['completed_at_utc']),
            'Run began before registered implementation or after inference freeze')
    require(run['implementation_freeze_sha256'] == reader.hash(report/'implementation_freeze.json') and
            run['pretarget_receipt_sha256'] == reader.hash(report/'pretarget_receipt.json'), 'Run receipt freeze binding differs')
    if corrected:
        correction = frozen['correction']
        require(run.get('prior_attempt') == 'prior_attempt', 'Correction lacks portable preserved predecessor')
        for name, expected in correction['prior_evidence_sha256'].items():
            require(reader.hash(contained(report/'correction_history', name)) == expected and
                    reader.hash(contained(report/'prior_attempt', name)) == expected, 'Preserved correction predecessor changed: '+name)
        previous = reader.json(report/'correction_history/implementation_freeze.json')
        require(previous['protocol_sha256'] == frozen['protocol_sha256'] and previous['input_sha256'] == settings['input_sha256'],
                'Correction changed scientific protocol/source pins')
        exposure_before = reader.json(report/'correction_history/exposure_record.json')
        require(exposure_before['status'] == 'final_outcome_access_started' and
                exposure_before['pretarget_receipt_sha256'] == correction['prior_evidence_sha256']['pretarget_receipt.json'] and
                exposure_before['expected_source_sha256'] == settings['input_sha256']['data/raw/eta_trip_outcome.jsonl'] and
                exposure_before['started_at_utc'] == correction['prior_exposure_started_at_utc'], 'Prior exposure binding differs')
        for prefix in ('correction_history/',):
            require(all(prefix+name in pretarget['output_sha256'] for name in correction['prior_evidence_sha256']),
                    'Prior correction metadata was not retained before target reaccess')
    else:
        require(run.get('prior_attempt') is None, 'Original run unexpectedly claims a prior attempt')
    for name, expected in run.get('output_sha256', {}).items():
        require(reader.hash(contained(report, name)) == expected, 'Retained result changed: '+name)
    return settings, frozen, pretarget


def verify_source_population(root, input_root, memberships, snapshots, settings, reader, audit):
    """Independent assignment/departure/holdout joins; no target-based pruning."""
    source = {}
    for name, expected in settings['input_sha256'].items():
        path = contained(input_root, name)
        require(reader.hash(path) == expected, 'Raw source pin differs: '+name)
        if 'outcome' not in name:
            source[name] = reader.rows(path)
    assignments = index(source['data/processed/eta_split_assignment.jsonl'], trip)
    original = index(source['data/processed/trip_membership.jsonl'], trip)
    require(assignments.keys() == original.keys(), 'Original membership/assignment keys differ')
    expected = {key for key, row in assignments.items() if row['split_role'] == 'test'}
    audit.compare(sorted(trip(r) for r in memberships), sorted(expected), 'population/original_test_trips')
    departures = {}
    for event in source['data/raw/eta_trip_event.jsonl']:
        if event.get('event_type') not in ('departure', 'departed'):
            continue
        key = trip(event)
        value = clock(event['occurred_at']), clock(event['available_at'])
        require(value[0] <= value[1], 'Departure known before occurrence')
        if key not in departures or value < departures[key]:
            departures[key] = value
    training = [row for key, row in original.items() if assignments[key]['split_role'] == 'train']
    trained_routes = {(r['tenant_id'], r['route_id']) for r in training}
    trained_vehicles = {(r['tenant_id'], r['vehicle_id']) for r in training}
    for row in memberships:
        key, original_row = trip(row), original[trip(row)]
        departed, known = departures[key]
        local_day = departed.astimezone(ZoneInfo('America/Los_Angeles')).date().isoformat()
        require('2026-08-01' <= local_day <= '2026-09-30', 'Test departure lies outside original final calendar')
        unseen_route = original_row['route_id'] in settings['population']['heldout_routes']
        unseen_vehicle = original_row['vehicle_id'] in settings['population']['heldout_vehicles']
        require(((key[0], original_row['route_id']) not in trained_routes) == unseen_route, 'Route novelty differs from training metadata')
        require(((key[0], original_row['vehicle_id']) not in trained_vehicles) == unseen_vehicle, 'Vehicle novelty differs from training metadata')
        group = ('unseen_vehicle' if unseen_vehicle else 'seen_vehicle')+'_'+('unseen_route' if unseen_route else 'seen_route')
        audit.subset(row, {'route_id': original_row['route_id'], 'vehicle_id': original_row['vehicle_id'],
            'unseen_route': unseen_route, 'unseen_vehicle': unseen_vehicle, 'novelty_group': group,
            'departure_time': stamp(departed), 'departure_known_at': stamp(known),
            'original_split_role': 'test', 'role': 'test'}, 'population/member/'+str(key))
    expected_snapshots = [r for r in source['data/processed/eta_snapshot.jsonl'] if trip(r) in expected]
    actual = index(snapshots)
    audit.compare(sorted(actual), sorted(snapshot(r) for r in expected_snapshots), 'population/all_original_snapshots')
    for row in expected_snapshots:
        retained = actual[snapshot(row)]
        audit.subset(retained, {name: row[name] for name in ('tenant_id', 'trip_id', 'snapshot_id',
            'vehicle_id', 'promise_version_id', 'route_version_id', 'position_validity', 'trip_status_as_of', 'provenance_id')},
            'population/snapshot/'+str(snapshot(row)))
        audit.compare(clock(retained['snapshot_time']) == clock(row['snapshot_time']), True, 'population/snapshot_clock')
    outcomes = [r for r in reader.rows(input_root/'data/raw/eta_trip_outcome.jsonl') if trip(r) in expected]
    return outcomes, [r for r in source['data/raw/eta_promise_version.jsonl'] if trip(r) in expected]


def assert_label_free(value):
    forbidden = {'actual_arrival_time', 'remaining_duration_seconds', 'target_seconds', 'delay_label',
                 'delay_seconds', 'eligibility', 'label_status', 'label_known_at', 'outcome_version_id',
                 'outcome_provenance_id', 'arrival_evidence_ref', 'arrival_time_precision'}
    if isinstance(value, dict):
        require(not forbidden.intersection(value), 'Target field reached label-free artifact')
        for child in value.values():
            assert_label_free(child)
    elif isinstance(value, list):
        for child in value:
            assert_label_free(child)


def verify_forecast_projection(eta_rows, probability_rows, opportunities, calibrators, audit, scenario):
    for family, rows in [('eta', eta_rows), ('probability', probability_rows)]:
        grouped = {(method, variant): index([r for r in rows if r['method_id'] == method and r['variant'] == variant])
                   for method, variant in {(r['method_id'], r['variant']) for r in rows}}
        raw, calibrated, canonical = (grouped['pipeline', variant] for variant in ('raw', 'calibrated', 'canonical'))
        require(raw.keys() == calibrated.keys() == canonical.keys(), 'Projection variants omit opportunities')
        states = {r['tenant_id']: r for r in calibrators[family]}
        for key, before in raw.items():
            after, stored, state = calibrated[key], canonical[key], states[key[0]]
            audit.compare(after['status'], before['status'], scenario+'/calibration/availability')
            audit.compare(after['prediction_source'], before['prediction_source'], scenario+'/calibration/source')
            fitted = available(before) and state['status'] == 'fitted'
            audit.compare(after['calibration_applied'], fitted, scenario+'/calibration/applied')
            audit.compare(after['calibration_status'], 'fitted' if fitted else 'unsupported' if available(before) else 'unavailable',
                          scenario+'/calibration/status')
            audit.compare(after['calibrator_sha256'], digest_json(state), scenario+'/calibration/state')
            if family == 'eta':
                values = [before[name] for name in ('lower_seconds', 'median_seconds', 'upper_seconds')]
                if fitted:
                    values = [max(0., values[0]-state['expansion_seconds']), values[1], values[2]+state['expansion_seconds']]
                audit.compare([after[name] for name in ('lower_seconds', 'median_seconds', 'upper_seconds')], values, scenario+'/calibration/interval')
                expected = list(project_eta(*values)) if available(before) else [None]*3
                audit.compare([stored[name] for name in ('lower_seconds', 'median_seconds', 'upper_seconds')], expected, scenario+'/projection/interval')
            else:
                probability = before['probability']
                if fitted:
                    p = min(1-1e-6, max(1e-6, probability))
                    logit = state['a']*math.log(p/(1-p))+state['b']
                    probability = 1/(1+math.exp(-logit)) if logit >= 0 else math.exp(logit)/(1+math.exp(logit))
                audit.compare(after['probability'], probability, scenario+'/calibration/probability')
                expected = project_probability(probability) if probability is not None else None
                audit.compare(stored['canonical_probability'], expected, scenario+'/projection/probability_string')
                audit.compare(stored['probability'], float(expected) if expected is not None else None, scenario+'/projection/probability_value')
        for row in opportunities:
            after = calibrated[snapshot(row)]
            if family == 'eta':
                values = [after[name] for name in ('lower_seconds', 'median_seconds', 'upper_seconds')]
                audit.compare([row[name] for name in ('lower_seconds', 'median_seconds', 'upper_seconds')], values, scenario+'/policy/precise_eta')
                stored = list(project_eta(*values)) if available(after) else [None]*3
                audit.compare([row[name] for name in ('lower_seconds_int', 'median_seconds_int', 'upper_seconds_int')], stored, scenario+'/policy/stored_eta')
            else:
                audit.compare(row['calibrated_probability'], after['probability'], scenario+'/policy/precise_probability')
                audit.compare(row['raw_probability'], raw[snapshot(row)]['probability'], scenario+'/policy/raw_probability')
                audit.compare(row['probability'], project_probability(after['probability']) if available(after) else None, scenario+'/policy/canonical_probability')


def join_targets(forecasts, targets):
    target_index = index(targets)
    require({snapshot(r) for r in forecasts} == set(target_index), 'Forecast/target population differs')
    joined = []
    for row in forecasts:
        label = target_index[snapshot(row)]
        for field in ('tenant_id', 'trip_id', 'snapshot_id', 'snapshot_time', 'promise_version_id', 'deadline_at', 'promise_known_at'):
            if field in row and field in label:
                require(row[field] == label[field], 'Target join changes identity/promise: '+field)
        joined.append({**row, **label})
    return joined


def verify_report(root, report, input_root=None):
    """Verify frozen artifacts, then independently score every retained scenario."""
    root, report = Path(root).resolve(), Path(report).resolve()
    require(Path(__file__).resolve() == root/'scripts/check_final_evaluation.py', 'Executed independent checker belongs to another checkout')
    reader, audit = Reader(), Audit()
    settings, frozen, pretarget = verify_artifact_gates(root, report, reader)
    # The complete gate above is deliberately before even retained target reads.
    memberships = reader.rows(report/'inputs/memberships.jsonl')
    snapshots = reader.rows(report/'inputs/snapshots.jsonl')
    features = reader.rows(report/'inputs/features.jsonl')
    promises = reader.rows(report/'inputs/promises.jsonl')
    index(memberships, trip); index(snapshots); index(features)
    expected_keys = sorted(snapshot(r) for r in snapshots)
    inventory = reader.json(report/'inventory.json')
    audit.compare(inventory, frozen['metadata_inventory'], 'inventory/frozen')
    audit.subset(inventory, {'trips': len(memberships), 'opportunities': len(snapshots),
        'trip_keys': [list(key) for key in sorted(trip(r) for r in memberships)],
        'snapshot_keys': [list(key) for key in expected_keys],
        'novelty_trips': {group: sum(r['novelty_group'] == group for r in memberships) for group in NOVELTY},
        'novelty_opportunities': {group: sum(r['novelty_group'] == group for r in snapshots) for group in NOVELTY},
        'membership_sha256': digest_json(memberships), 'snapshot_context_sha256': digest_json(snapshots),
        'feature_sha256': digest_json(features), 'outcomes_opened': False, 'target_based_pruning': False}, 'inventory/independent')
    outcomes = reader.rows(report/'outcome_source.jsonl')
    require({trip(r) for r in outcomes} <= {trip(r) for r in memberships}, 'Retained outcome group is outside original final cohort')
    if input_root is not None:
        originals, original_promises = verify_source_population(root, Path(input_root).resolve(), memberships, snapshots, settings, reader, audit)
        audit.compare(outcomes, originals, 'source/retained_outcome_rows')
        promises = original_promises
    expected_targets = reference_targets(snapshots, outcomes, promises, settings['population']['label_cutoff_exclusive'])
    targets = {kind: reader.rows(report/(kind+'.jsonl')) for kind in ('eta_targets', 'probability_targets')}
    for kind, expected in expected_targets.items():
        actual = index(targets[kind])
        audit.compare(sorted(actual), expected_keys, kind+'/inventory')
        for row in expected:
            audit.subset(actual[snapshot(row)], row, kind+'/'+str(snapshot(row)))
    policy_ref = load_reference(root, 'check_alert_policy')
    delivery_ref = load_reference(root, 'check_delivery_simulation')
    calibrators = reader.json(root/'reports/m3d/calibrators.json')
    transformers = reader.json(root/'reports/m3d/refit_transformers.json')
    require(all('__P08_UNSEEN_ROUTE__' not in r['categorical']['route_id']['vocabulary'] for r in transformers), 'Frozen unknown-route token is in a saved vocabulary')
    bootstrap = BootstrapReference(memberships)
    verified_forecast_records = {}
    summaries = []
    require(sorted(p.name for p in (report/'scenarios').iterdir() if p.is_dir()) == sorted(SCENARIOS), 'Missing or additional final scenario')
    for scenario in SCENARIOS:
        directory = report/'scenarios'/scenario
        eta = reader.rows(directory/'forecast_eta.jsonl')
        probability = reader.rows(directory/'forecast_probability.jsonl')
        opportunities = reader.rows(directory/'opportunities.jsonl')
        transformed = reader.rows(directory/'features.jsonl')
        stress_audit = reader.rows(directory/'stress_audit.jsonl')
        for rows in (eta, probability, opportunities, transformed, stress_audit):
            assert_label_free(rows)
        audit.compare(sorted(index(opportunities)), expected_keys, scenario+'/opportunity_inventory')
        stress_reference = reference_stress(features, scenario, with_audit=True)
        audit.compare(index(transformed), index(stress_reference['features']), scenario+'/stress/features')
        audit.compare(index(stress_audit), index(stress_reference['audit']), scenario+'/stress/audit_complete')
        audit.compare(reader.json(directory/'stress_summary.json'), stress_reference['summary'], scenario+'/stress/summary')
        before_features, after_features = index(features), index(transformed)
        audit.compare(sorted(index(stress_audit)), expected_keys, scenario+'/stress/audit_inventory')
        for row in stress_audit:
            key = snapshot(row)
            before = {name: before_features[key][name] for name in ('numeric', 'categorical')}
            after = {name: after_features[key][name] for name in ('numeric', 'categorical')}
            audit.subset(row, {'before': before, 'after': after, 'affected': before != after, 'scenario_id': scenario}, scenario+'/stress/audit')
        verify_forecast_projection(eta, probability, opportunities, calibrators, audit, scenario)
        metrics = reader.json(directory/'forecast_metrics.json')
        joined_eta, joined_probability = join_targets(eta, targets['eta_targets']), join_targets(probability, targets['probability_targets'])
        metric_inputs_hash = digest_json([[{k: v for k, v in r.items() if k != 'scenario_id'} for r in rows]
                                         for rows in (joined_eta, joined_probability)])
        if metric_inputs_hash in verified_forecast_records:
            audit.compare(metrics, verified_forecast_records[metric_inputs_hash], scenario+'/same_forecast_and_target_inputs')
        else:
            before_failures = len(audit.failures)
            verify_forecast_metrics(joined_eta, joined_probability, metrics, memberships, audit, scenario+'/forecasts', bootstrap)
            if len(audit.failures) == before_failures:
                verified_forecast_records[metric_inputs_hash] = metrics
        bundle = reader.json(directory/'policy_delivery.json')
        assert_label_free(bundle)
        policy_metrics = reader.json(directory/'policy_delivery_metrics.json')
        verify_policy_delivery(opportunities, targets['probability_targets'], bundle, policy_metrics, settings, scenario,
                               policy_ref, delivery_ref, audit)
        summaries.append({'scenario_id': scenario, 'opportunities': len(opportunities),
                          'forecast_rows': len(eta)+len(probability), 'candidates': len(bundle['policy']['candidates']),
                          'attempts': len(bundle['delivery']['attempts'])})
    summary = reader.json(report/'summary.json')
    audit.subset(summary, {'primary_scenario': SCENARIOS[0], 'scenario_ids': list(SCENARIOS), 'final_test_evaluated': True,
        'quality_accepted': False, 'operational_approval': False, 'model_or_calibrator_refitted': False, 'policy_reselected': False,
        'real_notifications_sent': False, 'adoption_or_action_simulated': False, 'p09_implemented': False}, 'summary/scientific_scope')
    reader.stable()
    return {'status': 'passed' if not audit.failures else 'failed', 'comparisons': audit.comparisons,
            'failures': len(audit.failures), 'failure_details': audit.failures[:50],
            'maximum_numeric_difference': audit.maximum_numeric_difference,
            'production_imports': False, 'standard_library_only': True, 'source_targets_verified': input_root is not None,
            'comparison_tolerance': {'absolute': ABS_TOLERANCE, 'relative': REL_TOLERANCE,
                'identities_counts_statuses_canonical_values': 'exact'},
            'implementation_freeze_sha256': reader.hash(report/'implementation_freeze.json'),
            'pretarget_receipt_sha256': reader.hash(report/'pretarget_receipt.json'),
            'source_sha256': frozen['source_sha256'], 'input_sha256': settings['input_sha256'],
            'scenario_checks': summaries, 'test_script_sha256': reader.hash(Path(__file__)),
            'quality_accepted': False, 'operational_approval': False,
            'scope': 'Independent frozen-artifact/population/target/calibration/projection/stress checks; all forecast methods, slices, paired scores, reliability and bootstrap intervals; independent candidate and transport engines plus delivery accounting. Does not establish operational quality acceptance.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--input-root', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    require(not args.output.exists() and not args.output.is_symlink(), 'Independent output exists; choose a fresh path')
    result = verify_report(args.root, args.report, args.input_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as stream:
        json.dump(result, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'status': result['status'], 'comparisons': result['comparisons'], 'failures': result['failures']}, indent=2))
    return 0 if result['failures'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
