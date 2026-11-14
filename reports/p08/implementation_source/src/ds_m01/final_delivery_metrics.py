"""Frozen final candidate/delivery accounting without reselecting a policy.

Global first candidates and successful deliveries are selected in the label-free
ledger. Slices attribute those existing first events to their original snapshot;
they never rerun admission or choose a more favorable later event.
"""
from collections import Counter
from copy import deepcopy
from zoneinfo import ZoneInfo

from . import alert_metrics as candidate_metrics
from . import delivery_metrics as delivered_metrics
from .alert_policy import simulate_policy
from .common import parse_ts
from .final_policy import SCENARIOS, policy_opportunities, validate_settings, first_ids, novelty_group


def _key(row):
    return row['tenant_id'], row['snapshot_id']


def _trip(row):
    return row['tenant_id'], row['trip_id']


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def distribution(values, total=None):
    values = sorted(v for v in values if v is not None)
    total = len(values) if total is None else total
    result = {'N': len(values), 'missing': total - len(values), 'unit': 'seconds',
              'null_reason': None if values else 'no_observed_values'}
    for name, p in (('min', 0), ('p10', .1), ('p25', .25), ('median', .5), ('p75', .75), ('p90', .9), ('max', 1)):
        if not values:
            result[name] = None
        else:
            rank = (len(values) - 1) * p
            lo = int(rank)
            result[name] = values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (rank - lo)
    return result


def _arrival_advance(target, at):
    valid = (target['eligibility'] == 'eligible' or target.get('reason') == 'already_arrived_at_snapshot')
    if (not valid or target.get('actual_arrival_time') is None or target.get('label_status') != 'verified'
            or target.get('arrival_time_precision') != 'second'):
        return None
    return (parse_ts(target['actual_arrival_time']) - at).total_seconds()


def _candidate_summary(opp, labels, policy, first_candidate_ids, repeat_candidate_ids):
    keys = set(opp)
    decisions = {_key(r): r for r in policy['decisions'] if _key(r) in keys}
    candidates = {_key(r): r for r in policy['candidates'] if _key(r) in keys}
    episode_ids = {r['episode_id'] for r in decisions.values() if r.get('episode_id') is not None}
    episodes = {r['episode_id']: r for r in policy['episodes'] if r['episode_id'] in episode_ids}
    result = candidate_metrics._summarize(opp, labels, decisions, candidates, episodes,
                                         {'metrics': {'sparse_trip_threshold': 20}})
    first = [r for r in candidates.values() if (r['tenant_id'], r['candidate_id']) in first_candidate_ids]
    classes = Counter(candidate_metrics._candidate_class(labels[_key(r)]) for r in first)
    n, mature = len(first), classes['true'] + classes['false']
    result['counts'].update(first_candidates=n, confirmed_true_first_candidates=classes['true'],
        known_false_first_candidates=classes['false'], unresolved_first_candidates=classes['unresolved'],
        retrospectively_post_arrival_first_candidates=classes['post_arrival'])
    result['counts']['within_slice_repeated_candidates'] = result['counts']['repeated_candidates_within_episode']
    result['counts']['within_slice_additional_candidates_per_trip'] = result['counts']['additional_candidates_per_trip']
    result['counts']['repeated_candidates_within_episode'] = sum((r['tenant_id'], r['candidate_id']) in repeat_candidate_ids for r in candidates.values())
    result['counts']['additional_candidates_per_trip'] = sum((r['tenant_id'], r['candidate_id']) not in first_candidate_ids for r in candidates.values())
    result['denominators'].update(conditional_first_candidate_precision=mature,
        first_candidate_target_coverage=n, first_candidate_precision_bounds=n)
    result['metrics'].update(conditional_first_candidate_precision=_ratio(classes['true'], mature),
        first_candidate_target_coverage=_ratio(mature, n),
        first_candidate_precision_bounds={'lower': _ratio(classes['true'], n), 'upper': _ratio(classes['true'] + classes['unresolved'], n)})
    result['null_reasons'] = {name: 'zero_declared_denominator' for name, value in result['metrics'].items() if value is None}
    if not n:
        result['null_reasons']['first_candidate_precision_bounds'] = 'no_global_first_candidates_in_cohort'
    result['first_selection_scope'] = 'global_label_free_first_candidate_before_subgroup_filter'
    result['repeat_attribution'] = 'Global episode/trip repeats attributed to their snapshot slice; within_slice counts are also shown.'
    result['lead_seconds'] = {}
    for name, rows in (('all_candidates', list(candidates.values())), ('first_candidates', first)):
        result['lead_seconds'][name] = {
            'prospective_deadline_lead_seconds': distribution([r['lead_seconds'] for r in rows]),
            'retrospective_arrival_lead_seconds': distribution([
                _arrival_advance(labels[_key(r)], parse_ts(r['simulated_issue_at'])) for r in rows], len(rows))}
    return result


def _delivery_summary(opp, labels, candidates, alerts, attempts, episodes, successes,
                      cutoff, minimum, first_delivery_ids, first_effective_ids, repeat_delivery_ids):
    keys, trips = set(opp), {_trip(r) for r in opp.values()}
    candidates = [r for r in candidates if _key(r) in keys]
    alerts = [r for r in alerts if _key(r) in keys]
    attempts = [r for r in attempts if _key(r) in keys]
    successes = [r for r in successes if _key(r) in keys]
    # All episodes for a monitored trip remain visible, including zero delivery.
    episodes = [r for r in episodes if _trip(r) in trips]
    result = delivered_metrics._summarize(opp, labels, candidates, alerts, attempts, episodes,
                                          successes, cutoff, minimum, 20)
    details = result['delivery_rows']
    for row in details:
        row['first_successful_delivery_for_trip'] = (row['tenant_id'], row['delivery_id']) in first_delivery_ids
    first = [r for r in details if r['first_successful_delivery_for_trip']]
    effective = [r for r in details if (r['tenant_id'], r['delivery_id']) in first_effective_ids]
    classes = Counter(r['classification'] for r in first)
    n, assessable = len(first), sum(r['classification'] != 'unresolved' for r in first)
    eligible = sum(r['target_eligibility'] == 'eligible' for r in first)
    result['counts'].update(first_successful_deliveries=n, mature_eligible_first_deliveries=eligible,
        known_assessable_first_deliveries=assessable, useful_first_deliveries=classes['useful_true'],
        known_false_first_deliveries=classes['known_false'], unresolved_first_deliveries=classes['unresolved'],
        post_arrival_first_deliveries=classes['post_arrival'], insufficient_lead_first_deliveries=classes['insufficient_lead'],
        first_effective_deliveries=len(effective))
    result['counts']['within_slice_repeated_deliveries'] = result['counts']['repeated_deliveries_within_episode']
    result['counts']['within_slice_additional_deliveries_per_trip'] = result['counts']['additional_deliveries_per_trip']
    result['counts']['repeated_deliveries_within_episode'] = sum((r['tenant_id'], r['delivery_id']) in repeat_delivery_ids for r in details)
    result['counts']['additional_deliveries_per_trip'] = sum((r['tenant_id'], r['delivery_id']) not in first_delivery_ids for r in details)
    result['denominators'].update(conditional_first_delivery_useful_precision=assessable,
        first_delivery_usefulness_bounds=n, first_delivery_target_coverage=n, first_delivery_assessable_coverage=n,
        all_observed_positive_delivered_trip_recall=result['counts']['observed_positive_trips'],
        attempts_per_100_monitored_trips=len(trips), candidates_per_100_monitored_trips=len(trips))
    result['metrics'].update(conditional_first_delivery_useful_precision=_ratio(classes['useful_true'], assessable),
        first_delivery_usefulness_bounds={'lower': _ratio(classes['useful_true'], n), 'upper': _ratio(classes['useful_true'] + classes['unresolved'], n)},
        first_delivery_target_coverage=_ratio(eligible, n), first_delivery_assessable_coverage=_ratio(assessable, n),
        all_observed_positive_delivered_trip_recall=_ratio(result['counts']['confirmed_useful_delivery_trips'], result['counts']['observed_positive_trips']),
        attempts_per_100_monitored_trips=_ratio(100 * result['counts']['attempts'], len(trips)),
        candidates_per_100_monitored_trips=_ratio(100 * len(candidates), len(trips)))
    result['first_selection_scope'] = 'global_label_free_first_success_before_subgroup_filter'
    result['repeat_attribution'] = 'Global episode/trip repeats attributed to their snapshot slice; within_slice counts are also shown.'
    result['first_effective_scope'] = 'secondary_first_useful_delivery_among_captured_trips_only'
    result['null_reasons'] = {name: 'zero_declared_denominator' for name, value in result['metrics'].items() if value is None}
    if not n:
        result['null_reasons']['first_delivery_usefulness_bounds'] = 'no_global_first_successes_in_cohort'
    result['lead_seconds'] = {}
    result['timing_rates'] = {}
    for name, rows in (('all_successful_deliveries', details), ('first_successful_deliveries', first), ('first_effective_deliveries', effective)):
        result['lead_seconds'][name] = {field: distribution([r[field] for r in rows], len(rows))
            for field in ('prospective_deadline_lead_seconds', 'retrospective_arrival_lead_seconds',
                          'candidate_to_delivery_seconds', 'successful_attempt_latency_seconds')}
        arrival = [r['retrospective_arrival_lead_seconds'] for r in rows if r['retrospective_arrival_lead_seconds'] is not None]
        lead = [r['prospective_deadline_lead_seconds'] for r in rows]
        counts = {'deliveries': len(rows), 'valid_arrival_times': len(arrival),
            'below_1800': sum(v < minimum for v in lead), 'at_or_after_deadline': sum(v <= 0 for v in lead),
            'at_or_after_actual_arrival': sum(v <= 0 for v in arrival)}
        result['timing_rates'][name] = {'counts': counts,
            'below_1800': _ratio(counts['below_1800'], len(rows)),
            'at_or_after_deadline': _ratio(counts['at_or_after_deadline'], len(rows)),
            'at_or_after_actual_arrival': _ratio(counts['at_or_after_actual_arrival'], len(arrival)),
            'null_reasons': {**({} if rows else {'below_1800': 'no_deliveries', 'at_or_after_deadline': 'no_deliveries'}),
                             **({} if arrival else {'at_or_after_actual_arrival': 'no_valid_arrival_times'})}}
    return result


def _groups(rows, scenario_id, settings):
    zone = ZoneInfo(settings['population']['timezone'])
    specifications = [
        ('novelty_group', settings['population']['primary_novelty_groups'], novelty_group),
        ('tenant_id', sorted({r['tenant_id'] for r in rows}), lambda r: r['tenant_id']),
        ('departure_month', ['2026-08', '2026-09'], lambda r: parse_ts(r['departure_time']).astimezone(zone).strftime('%Y-%m')),
        ('route_id', sorted({r['route_id'] for r in rows}), lambda r: r['route_id']),
        ('original_position_validity', ['valid', 'low_confidence', 'no_position'], lambda r: r['position_validity']),
        ('early_trip', ['elapsed_lt_3600', 'elapsed_ge_3600'], lambda r: 'elapsed_lt_3600' if r['elapsed_trip_seconds'] < 3600 else 'elapsed_ge_3600'),
        ('duration_shift', ['elapsed_lt_7200', 'elapsed_ge_7200'], lambda r: 'elapsed_lt_7200' if r['elapsed_trip_seconds'] < 7200 else 'elapsed_ge_7200'),
        ('deadline_advance', ['overdue', '0_to_1800', 'gt_1800_to_7200', 'gt_7200'], lambda r:
            'overdue' if (parse_ts(r['deadline_at']) - parse_ts(r['snapshot_time'])).total_seconds() < 0 else
            '0_to_1800' if (parse_ts(r['deadline_at']) - parse_ts(r['snapshot_time'])).total_seconds() <= 1800 else
            'gt_1800_to_7200' if (parse_ts(r['deadline_at']) - parse_ts(r['snapshot_time'])).total_seconds() <= 7200 else 'gt_7200'),
        ('probability_calibration', ['fitted', 'unsupported', 'unavailable'], lambda r: r['probability_calibration_status']),
        ('delivery_scenario', [scenario_id], lambda r: scenario_id)]
    for kind in ('eta', 'probability'):
        def source(row, kind=kind):
            if row[kind + '_status'] == 'unavailable':
                return 'unavailable'
            return 'native' if row[kind + '_source'] in ('candidate', 'model', 'native', 'gradient_boosting_quantile', 'logistic_delay') else 'fallback'
        specifications.append((kind + '_source', ['native', 'fallback', 'unavailable'], source))
    for name, values, function in specifications:
        for value in values:
            yield name, value, {_key(r) for r in rows if function(r) == value}


def _event_slices(delivery, alerts, attempts):
    details = delivery['delivery_rows']
    by_id = {(r['tenant_id'], r['delivery_id']): r for r in attempts}
    details_by_alert = {}
    for row in details:
        details_by_alert.setdefault((row['tenant_id'], row['alert_id']), []).append(row)
    transport = []
    for value in ('first_attempt_success', 'retry_success', 'never_confirmed'):
        members = []
        for alert in alerts:
            successes = details_by_alert.get((alert['tenant_id'], alert['alert_id']), [])
            label = ('never_confirmed' if not successes else 'first_attempt_success'
                     if min(by_id[r['tenant_id'], r['delivery_id']]['attempt_index'] for r in successes) == 1 else 'retry_success')
            if label == value:
                members.append(alert)
        ids = {(r['tenant_id'], r['alert_id']) for r in members}
        selected = [r for r in details if (r['tenant_id'], r['alert_id']) in ids]
        transport.append({'dimension': 'alert_transport_path', 'value': value, 'alerts': len(members),
            'successful_deliveries': len(selected), 'first_successful_deliveries': sum(r['first_successful_delivery_for_trip'] for r in selected),
            'classification_counts': dict(sorted(Counter(r['classification'] for r in selected).items())),
            'candidate_to_delivery_seconds': distribution([r['candidate_to_delivery_seconds'] for r in selected]),
            'interpretation': 'Descriptive event cohort; not a new recall denominator'})
    first = [r for r in details if r['first_successful_delivery_for_trip']]
    for name, values, function in (
        ('first_delivered_lead', ['ge_1800', '0_to_lt_1800', 'negative'], lambda r: 'ge_1800' if r['prospective_deadline_lead_seconds'] >= 1800 else '0_to_lt_1800' if r['prospective_deadline_lead_seconds'] >= 0 else 'negative'),
        ('first_delivery_target', ['known', 'unknown', 'post_arrival'], lambda r: 'post_arrival' if r['classification'] == 'post_arrival' else 'known' if r['target_eligibility'] == 'eligible' else 'unknown')):
        for value in values:
            selected = [r for r in first if function(r) == value]
            transport.append({'dimension': name, 'value': value, 'first_successful_deliveries': len(selected),
                'classification_counts': dict(sorted(Counter(r['classification'] for r in selected).items())),
                'prospective_deadline_lead_seconds': distribution([r['prospective_deadline_lead_seconds'] for r in selected]),
                'interpretation': 'Descriptive global-first-success cohort; no outcome-selected policy or recall denominator'})
    return transport


def score_policy_delivery(opportunities, probability_targets, bundle, settings, scenario_id=None):
    """Join authorized targets only after the complete policy/transport ledger."""
    validate_settings(settings)
    scenario_id = bundle['scenario_id'] if scenario_id is None else scenario_id
    if scenario_id != bundle['scenario_id'] or scenario_id not in SCENARIOS:
        raise ValueError('Scoring scenario differs from frozen ledger')
    if any(bundle.get(name) is not False for name in ('labels_used', 'threshold_reselected', 'transport_redrawn')):
        raise ValueError('Final ledger must retain outcome-free fixed-policy execution')
    rows = policy_opportunities(opportunities)
    if bundle['rule'] != settings['pipeline']['selected_rule'] or bundle['policy'] != simulate_policy(rows, bundle['rule']):
        raise ValueError('Candidate ledger differs from unchanged frozen rule execution')
    expected_first, expected_success = first_ids(bundle['policy'], bundle['delivery'])
    if bundle['first_candidate_ids'] != expected_first or bundle['first_successful_delivery_ids'] != expected_success:
        raise ValueError('First-event identities differ from label-free global ordering')
    cutoff = parse_ts(settings['delivery']['counts_as_of'])
    minimum = settings['delivery']['minimum_action_lead_seconds']
    opp, labels = delivered_metrics._population(rows, probability_targets, parse_ts(settings['population']['label_cutoff_exclusive']))
    candidates, alerts, attempts, episodes, successes = delivered_metrics._ledger(opp, bundle['policy']['candidates'], bundle['delivery'], cutoff)
    first_candidates = {(r['tenant_id'], r['candidate_id']) for r in expected_first}
    first_successes = {(r['tenant_id'], r['delivery_id']) for r in expected_success}
    repeat_candidates, observed_episodes = set(), set()
    for row in sorted(candidates, key=lambda r: (parse_ts(r['simulated_issue_at']), r['tenant_id'], r['trip_id'], r['snapshot_id'])):
        episode_key = row['tenant_id'], row['episode_id']
        if episode_key in observed_episodes:
            repeat_candidates.add((row['tenant_id'], row['candidate_id']))
        observed_episodes.add(episode_key)
    repeat_deliveries, observed_episodes = set(), set()
    for row in sorted(successes, key=lambda r: (parse_ts(r['delivered_at']), r['tenant_id'], r['trip_id'], r['alert_id'], r['delivery_id'])):
        episode_key = row['tenant_id'], row['alert_episode_id']
        if episode_key in observed_episodes:
            repeat_deliveries.add((row['tenant_id'], row['delivery_id']))
        observed_episodes.add(episode_key)
    # Classification does not choose first success; it only identifies the
    # explicitly secondary earliest useful event after the ledger is frozen.
    preliminary = delivered_metrics._summarize(opp, labels, candidates, alerts, attempts, episodes, successes, cutoff, minimum, 20)
    first_effective = {}
    for row in preliminary['delivery_rows']:
        if row['classification'] == 'useful_true':
            first_effective.setdefault(_trip(row), (row['tenant_id'], row['delivery_id']))
    effective_ids = set(first_effective.values())

    def summaries(keys):
        selected = {k: r for k, r in opp.items() if k in keys}
        targets = {k: r for k, r in labels.items() if k in keys}
        return (_candidate_summary(selected, targets, bundle['policy'], first_candidates, repeat_candidates),
                _delivery_summary(selected, targets, candidates, alerts, attempts, episodes, successes,
                                  cutoff, minimum, first_successes, effective_ids, repeat_deliveries))

    candidate_result, delivery_result = summaries(set(opp))
    slices = []
    for dimension, value, keys in _groups(rows, scenario_id, settings):
        candidate_slice, delivery_slice = summaries(keys)
        slices.append({'dimension': dimension, 'value': value, 'empty': not keys,
                       'candidate': candidate_slice, 'delivery': delivery_slice})
    daily = bundle['policy']['summary']['daily_candidate_counts']
    burden = candidate_result['metrics']['all_candidates_per_100_monitored_trips']
    return {'scope': 'frozen_final_policy_and_synthetic_delivery', 'scenario_id': scenario_id,
        'candidate': candidate_result, 'delivery': delivery_result, 'subgroups': slices,
        'delivery_slices': _event_slices(delivery_result, alerts, attempts),
        'first_candidate_ids': deepcopy(expected_first), 'first_successful_delivery_ids': deepcopy(expected_success),
        'first_effective_delivery_ids': [{'tenant_id': key[0], 'trip_id': key[1], 'delivery_id': value[1]} for key, value in sorted(first_effective.items())],
        'quality_descriptors': {'candidate_budget_50_per_100_met': None if burden is None else burden <= 50,
            'candidate_daily_cap_2_met': all(r['candidates'] <= 2 for r in daily),
            'quality_accepted': False, 'operational_approval': False},
        'cutoffs': {'outcome_knowledge': settings['population']['label_cutoff_exclusive'], 'outcome_comparison': 'strictly_before',
                    'receipt_knowledge': settings['delivery']['counts_as_of'], 'receipt_comparison': 'at_or_before'},
        'first_event_selection': 'Selected globally without labels, then attributed to the original snapshot subgroup.',
        'subgroup_admission_rerun': False, 'transport_redrawn': False, 'policy_delivery_bootstrap': False,
        'recipient_behavior_evaluated': False, 'operational_benefit_established': False}
