"""Descriptive workflow accounting; simulated decisions do not establish benefit."""
from collections import Counter
from ds_m01.common import parse_ts

STATUSES = ('accepted', 'partially_accepted', 'rejected', 'not_reviewed', 'overridden')
FOLLOWUP_STATUSES = ('mature', 'immature', 'lost', 'not_reported', 'disputed', 'rejected',
                     'no_known_outcome', 'no_action')
ACTIONS = ('contact_driver', 'contact_customer', 'reschedule', 'reassign', 'no_action', 'other')


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _index(rows, key):
    result = {}
    for row in rows:
        identity = tuple(row[k] for k in key)
        _require(identity not in result, 'Duplicate workflow metric identity: ' + str(identity))
        result[identity] = row
    return result


def _ratio(numerator, denominator):
    _require(type(numerator) is int and type(denominator) is int and 0 <= numerator <= denominator,
             'Invalid workflow metric numerator or denominator')
    return {'numerator': numerator, 'denominator': denominator,
            'value': numerator / denominator if denominator else None}


def _core(inputs, simulation, settings):
    cutoff = parse_ts(settings['observation_cutoff_at'])
    opportunities = _index(inputs['opportunities'], ('tenant_id', 'snapshot_id'))
    alerts = _index(inputs['alerts'], ('tenant_id', 'alert_id'))
    attempts = _index(inputs['attempts'], ('tenant_id', 'delivery_id'))
    delivered = {}
    for row in attempts.values():
        akey = row['tenant_id'], row['alert_id']
        _require(akey in alerts, 'Transport metric row references an absent or other-tenant alert')
        if row['delivery_status'] == 'delivered' and row['receipt_observed'] is True:
            _require(row['delivered_at'] is not None and row['receipt_known_at'] is not None,
                     'Confirmed service delivery lacks observation clocks')
            if parse_ts(row['receipt_known_at']) <= cutoff:
                prior = delivered.get(akey)
                if prior is None or (parse_ts(row['delivered_at']), parse_ts(row['receipt_known_at']), row['delivery_id']) < (
                        parse_ts(prior['delivered_at']), parse_ts(prior['receipt_known_at']), prior['delivery_id']):
                    delivered[akey] = row
    adoptions = _index(simulation['eta_adoption'], ('tenant_id', 'adoption_id'))
    adoption_by_alert = {}
    for row in adoptions.values():
        delivery = attempts.get((row['tenant_id'], row['delivery_id']))
        _require(delivery is not None, 'Adoption metric row references another tenant or absent delivery')
        akey = row['tenant_id'], delivery['alert_id']
        _require(akey in delivered and delivered[akey]['delivery_id'] == row['delivery_id'],
                 'Adoption denominator includes an unconfirmed or duplicate retry delivery')
        _require(akey not in adoption_by_alert and row['adoption_status'] in STATUSES,
                 'Duplicate or invalid adoption status')
        if row['adoption_status'] == 'not_reviewed':
            _require(row['decided_at'] is None, 'Not-reviewed cannot be a completed decision')
        else:
            _require(row['decided_at'] is not None and parse_ts(row['decided_at']) <= cutoff,
                     'Future adoption decision included in observed metrics')
        adoption_by_alert[akey] = row
    _require(set(adoption_by_alert) == set(delivered),
             'Every observed confirmed alert needs one explicit adoption state')
    opened = set()
    opening_known = {}
    for row in simulation['opening_events']:
        akey = row['tenant_id'], row['alert_id']
        _require(akey in delivered and row['delivery_id'] == delivered[akey]['delivery_id'],
                 'Opening metric references a different confirmed delivery')
        _require(akey not in opened and parse_ts(delivered[akey]['receipt_known_at']) <=
                 parse_ts(row['opened_at']) <= parse_ts(row['known_at']) <= cutoff,
                 'Opening metric is duplicated or outside its observation clocks')
        opened.add(akey)
        opening_known[akey] = parse_ts(row['known_at'])
    for akey, row in adoption_by_alert.items():
        if row['adoption_status'] != 'not_reviewed':
            _require(akey in opened and opening_known[akey] <= parse_ts(row['decided_at']),
                     'Reviewed adoption requires a previously observed opening')
    status_counts = Counter(row['adoption_status'] for row in adoptions.values())
    reviewed = len(adoptions) - status_counts['not_reviewed']
    actions = _index(simulation['eta_action'], ('tenant_id', 'action_id'))
    action_counts = Counter()
    handling = []
    for row in actions.values():
        adoption = adoptions.get((row['tenant_id'], row['adoption_id']))
        _require(adoption is not None and row['action_type'] in ACTIONS,
                 'Action metric identity or controlled action type differs')
        delivery = attempts[row['tenant_id'], adoption['delivery_id']]
        _require(row['alert_id'] == delivery['alert_id'], 'Action metric alert differs from its adoption')
        if row['action_type'] != 'no_action':
            _require(adoption['adoption_status'] in {'accepted', 'partially_accepted', 'overridden'}
                     and row['action_time'] is not None and adoption['decided_at'] is not None
                     and parse_ts(adoption['decided_at']) <= parse_ts(row['action_time']) <= cutoff,
                     'Executed action metric lacks a valid observed decision')
        value = row['handling_seconds']
        _require(value is None or type(value) is int and value >= 0,
                 'Handling seconds must be a nonnegative integer or unknown')
        if value is not None: handling.append(value)
        action_counts[row['action_type']] += 1
    audits = _index(simulation['followup_audit'], ('tenant_id', 'action_id'))
    _require(set(audits) == set(actions), 'Every action must retain its follow-up state, including no_action')
    for key, row in audits.items():
        _require(row['followup_status'] in FOLLOWUP_STATUSES, 'Unknown follow-up status')
        _require(row['action_type'] == actions[key]['action_type'], 'Follow-up audit action type differs')
    followups = _index(simulation['eta_action_followup'], ('tenant_id', 'action_id', 'outcome_version_id'))
    known = []
    known_actions = set()
    for row in followups.values():
        akey = row['tenant_id'], row['action_id']
        _require(akey in actions and actions[akey]['action_type'] != 'no_action',
                 'Follow-up metrics require an executed same-tenant action')
        _require(parse_ts(row['outcome_observation_end']) <= cutoff, 'Future follow-up metric')
        audit = audits[akey]
        _require(row['outcome_version_id'] == audit['outcome_version_id']
                 and audit['canonical_row_emitted'] is True, 'Canonical follow-up differs from its audit')
        values = [row['original_promise_delay_seconds'], row['observed_delay_seconds']]
        _require(all(v is None or type(v) is int and v >= 0 for v in values)
                 and (values[0] is None) == (values[1] is None), 'Delay comparison uses incomparable or invalid values')
        if values[0] is not None:
            _require(row['review_outcome'] == 'confirmed' and audit['followup_status'] == 'mature'
                     and akey not in known_actions, 'Known delay requires one mature confirmed outcome per action')
            known_actions.add(akey); known.append(values)
    for key, audit in audits.items():
        rows = [r for r in followups.values() if (r['tenant_id'], r['action_id']) == key]
        _require(bool(rows) == (audit['canonical_row_emitted'] is True), 'Missing or extra canonical follow-up')
    executed = len(actions) - action_counts['no_action']
    accepted = status_counts['accepted'] + status_counts['partially_accepted']
    original_sum = sum(pair[0] for pair in known); revised_sum = sum(pair[1] for pair in known)
    return {
        'population': {'opportunities': len(opportunities),
            'trips': len({(r['tenant_id'], r['trip_id']) for r in opportunities.values()}),
            'alerts': len(alerts), 'transport_attempts': len(attempts),
            'confirmed_delivered_alerts': len(delivered), 'non_delivered_alerts': len(alerts) - len(delivered)},
        'funnel': {'delivered_alerts': len(delivered), 'opened_alerts': len(opened), 'reviewed_alerts': reviewed,
            'not_reviewed_alerts': status_counts['not_reviewed'], 'accepted_alerts': status_counts['accepted'],
            'partially_accepted_alerts': status_counts['partially_accepted'], 'rejected_alerts': status_counts['rejected'],
            'overridden_alerts': status_counts['overridden'], 'accepted_or_partial_alerts': accepted},
        'rates': {'opened_per_delivery': _ratio(len(opened), len(delivered)),
            'reviewed_per_delivery': _ratio(reviewed, len(delivered)),
            'accepted_or_partial_per_reviewed': _ratio(accepted, reviewed),
            'accepted_or_partial_per_delivery': _ratio(accepted, len(delivered))},
        'actions': {'records': len(actions), 'executed_actions': executed,
            'no_action_records': action_counts['no_action'], 'by_type': {k: action_counts[k] for k in ACTIONS},
            'handling_known_records': len(handling), 'handling_unknown_records': len(actions) - len(handling),
            'handling_seconds_sum': sum(handling)},
        'followup': {'audit_records': len(audits), 'canonical_records': len(followups),
            'by_status': {k: sum(r['followup_status'] == k for r in audits.values()) for k in FOLLOWUP_STATUSES},
            'known_arrival_comparisons': len(known), 'actions_without_comparable_arrival': executed - len(known_actions),
            'original_delay_seconds_sum': original_sum, 'comparison_delay_seconds_sum': revised_sum,
            'deadline_definition_difference_seconds': original_sum - revised_sum, 'arrival_change_claimed': False},
    }


def score_workflow(inputs, simulation, settings):
    """Count one adoption per confirmed alert; retain failures and unknown states."""
    result = _core(inputs, simulation, settings)
    tenants = sorted({r['tenant_id'] for r in inputs['opportunities']} |
                     {r['tenant_id'] for r in inputs['alerts']})
    per_tenant = {}
    for tenant in tenants:
        local_inputs = {k: [r for r in inputs[k] if r['tenant_id'] == tenant]
                        for k in ('opportunities', 'alerts', 'attempts')}
        local_sim = {k: [r for r in simulation[k] if r['tenant_id'] == tenant]
                     for k in ('eta_adoption', 'eta_action', 'eta_action_followup', 'opening_events', 'followup_audit')}
        per_tenant[tenant] = _core(local_inputs, local_sim, settings)
    return {'status': 'descriptive_synthetic_workflow', 'source_scenario': 'S00_unmodified',
            'observation_cutoff_at': settings['observation_cutoff_at'], **result, 'by_tenant': per_tenant,
            'denominator_notes': {
                'delivery_unit': 'Unique same-tenant alert with a confirmed observed delivery; failed retries are not non-adoption.',
                'adoption_unit': 'Accepted plus partially accepted among reviewed confirmed alerts; overrides are separate.',
                'followup_unit': 'Executed actions; unknown, immature, disputed and lost follow-up are not negative outcomes.',
                'delay_comparison': 'The same retained arrival against two promise definitions. A deadline change is not an arrival improvement.',
                'interpretation': 'Workflow profiles are engineering fixtures, not estimated recipient behaviour or causal fleet benefit.'},
            'real_notifications_sent': False, 'real_recipient_behaviour_observed': False,
            'causal_benefit_estimated': False, 'quality_accepted': False, 'operational_approval': False}
