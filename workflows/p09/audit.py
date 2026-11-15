"""Independent relationship, clock and arithmetic audit for the P09 workflow.

This module deliberately does not import the workflow simulator or its scorer.
It joins retained source evidence, recounts denominators and computes delay
arithmetic directly. A passing audit establishes internal demonstration
consistency, not real recipient behaviour or causal fleet benefit.
"""
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
import math


VERSION = 'p09-independent-workflow-audit-v1'
ORIGIN = 'synthetic_scenario'
CANONICAL_FIELDS = {
    'eta_adoption': {'tenant_id', 'provenance_id', 'adoption_id', 'delivery_id',
                     'adoption_status', 'adoption_reason', 'decided_at'},
    'eta_action': {'tenant_id', 'provenance_id', 'action_id', 'adoption_id',
                   'alert_id', 'action_type', 'action_time', 'handling_seconds'},
    'eta_action_followup': {'tenant_id', 'provenance_id', 'action_id',
        'outcome_version_id', 'followup_promise_version_id',
        'original_promise_version_id', 'outcome_observation_end',
        'observed_delay_seconds', 'original_promise_delay_seconds',
        'review_outcome', 'result_origin'},
}
FALSE_CLAIMS = ('model_or_calibrator_refitted', 'policy_reselected',
    'real_notifications_sent', 'real_recipient_observations',
    'quality_accepted', 'operational_approval', 'causal_benefit_estimated')
STATUSES = {'accepted', 'partially_accepted', 'rejected', 'not_reviewed', 'overridden'}
ACTION_TYPES = {'contact_driver', 'contact_customer', 'reschedule', 'reassign',
                'no_action', 'other'}


class _Audit:
    def __init__(self):
        self.comparisons = 0
        self.failures = []

    def check(self, condition, code, context=None):
        self.comparisons += 1
        if not condition:
            self.failures.append({'check': code, 'context': context})
        return bool(condition)

    def equal(self, actual, expected, code, context=None):
        return self.check(_exact(actual, expected), code, context)

    def timestamp(self, value, code, context=None):
        try:
            if not isinstance(value, str):
                raise ValueError('Timestamp must be text')
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError('Timestamp must have an offset')
        except (ValueError, TypeError, OverflowError):
            self.check(False, code, context)
            return None
        self.check(True, code, context)
        return parsed

    def index(self, rows, fields, name):
        result = {}
        if not self.check(isinstance(rows, list), 'array_type', name):
            return result
        for ordinal, row in enumerate(rows):
            if not self.check(isinstance(row, dict), 'row_type', [name, ordinal]):
                continue
            key = tuple(row.get(k) for k in fields)
            valid = all(isinstance(k, str) and k.strip() for k in key)
            if not self.check(valid, 'identity_nonempty', [name, ordinal]):
                continue
            self.check(key not in result, 'identity_unique', [name, *key])
            result[key] = row
        return result


def _same_fields(audit, left, right, fields, code, key):
    for field in fields:
        audit.equal(left.get(field), right.get(field), code + ':' + field, key)


def _exact(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_exact(left[k], right[k]) for k in left)
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(_exact(a, b) for a, b in zip(left, right))
    return left == right


def _numeric_delay(audit, value, code, key):
    return audit.check(value is None or (type(value) is int and value >= 0), code, key)


def _delay(arrival, deadline):
    return math.ceil(max(0, (arrival - deadline).total_seconds()))


def _profile_number(settings, tenant, delivery, purpose):
    """The public identity-only fixture assignment, independent of outcome data."""
    raw = json.dumps([settings['seed'], tenant, delivery, purpose],
                     separators=(',', ':')).encode('utf-8')
    return int(hashlib.sha256(raw).hexdigest()[:16], 16)


def audit_workflow(inputs, simulation, metrics, settings):
    """Return accumulated independent checks; never regenerate the simulation."""
    audit = _Audit()
    if not all(isinstance(x, dict) for x in (inputs, simulation, metrics, settings)):
        audit.check(False, 'top_level_mapping')
        return _report(audit, {})
    cutoff = audit.timestamp(settings.get('observation_cutoff_at'), 'observation_cutoff')
    if cutoff is None:
        return _report(audit, {})
    for flag in ('quality_accepted', 'operational_approval', 'causal_benefit_estimated'):
        audit.equal(settings.get(flag), False, 'settings_false_claim:' + flag)
    audit.equal(settings.get('result_origin'), ORIGIN, 'settings_origin')
    summary = simulation.get('summary', {})
    audit.check(isinstance(summary, dict), 'summary_type')
    if not isinstance(summary, dict):
        summary = {}
    for flag in FALSE_CLAIMS:
        audit.equal(summary.get(flag), False, 'summary_false_claim:' + flag)
    audit.equal(summary.get('observation_cutoff_at'), settings.get('observation_cutoff_at'),
                'summary_cutoff')
    audit.equal(summary.get('source_scenario'), settings.get('source_scenario'), 'scenario_binding')
    audit.equal(inputs.get('scenario_id'), settings.get('source_scenario'), 'input_scenario_binding')

    arrays = {name: simulation.get(name, []) for name in (
        *CANONICAL_FIELDS, 'opening_events', 'decision_audit', 'followup_audit',
        'promise_versions', 'outcome_references', 'events')}
    for name, rows in arrays.items():
        if not audit.check(isinstance(rows, list), 'output_array', name):
            arrays[name] = []
        else:
            for ordinal, row in enumerate(rows):
                audit.check(isinstance(row, dict), 'output_row_mapping', [name, ordinal])
            arrays[name] = [row for row in rows if isinstance(row, dict)]
    for name, fields in CANONICAL_FIELDS.items():
        for ordinal, row in enumerate(arrays[name]):
            audit.check(isinstance(row, dict) and set(row) == fields,
                        'canonical_field_contract', [name, ordinal])
            audit.check(isinstance(row.get('provenance_id'), str) and bool(row['provenance_id'].strip()),
                        'canonical_provenance_nonempty', [name, ordinal])
    attempts = audit.index(inputs.get('attempts', []), ('tenant_id', 'delivery_id'), 'source_attempts')
    alerts = audit.index(inputs.get('alerts', []), ('tenant_id', 'alert_id'), 'source_alerts')
    opportunities = audit.index(inputs.get('opportunities', []), ('tenant_id', 'snapshot_id'), 'source_opportunities')
    original_promises = audit.index(inputs.get('promises', []), ('tenant_id', 'promise_version_id'), 'source_promises')
    outcomes = audit.index(inputs.get('outcomes', []), ('tenant_id', 'outcome_version_id'), 'source_outcomes')
    adoptions = audit.index(arrays['eta_adoption'], ('tenant_id', 'adoption_id'), 'eta_adoption')
    actions = audit.index(arrays['eta_action'], ('tenant_id', 'action_id'), 'eta_action')
    canonical_followups = audit.index(arrays['eta_action_followup'], ('tenant_id', 'action_id'), 'eta_action_followup')
    openings = audit.index(arrays['opening_events'], ('tenant_id', 'opening_id'), 'opening_events')
    decisions = audit.index(arrays['decision_audit'], ('tenant_id', 'adoption_id'), 'decision_audit')
    followups = audit.index(arrays['followup_audit'], ('tenant_id', 'action_id'), 'followup_audit')
    revised_promises = audit.index(arrays['promise_versions'], ('tenant_id', 'promise_version_id'), 'promise_versions')
    references = audit.index(arrays['outcome_references'], ('tenant_id', 'outcome_version_id'), 'outcome_references')
    audit.index(arrays['events'], ('tenant_id', 'event_id'), 'events')
    audit.equal(set(adoptions), set(decisions), 'canonical_decision_inventory')
    audit.equal(set(actions), set(followups), 'action_followup_audit_inventory')
    audit.equal(set(openings), {(key[0], row.get('opening_id')) for key, row in decisions.items()
                              if row.get('opening_id') is not None}, 'opening_decision_inventory')
    for key, alert in alerts.items():
        opportunity = opportunities.get((key[0], alert.get('snapshot_id')))
        promise = original_promises.get((key[0], alert.get('promise_version_id')))
        if audit.check(opportunity is not None and promise is not None, 'parent_alert_source_fk', key):
            _same_fields(audit, alert, opportunity, ('tenant_id', 'trip_id', 'prediction_version', 'promise_version_id'), 'parent_alert_snapshot', key)
            audit.equal(alert.get('trip_id'), promise.get('trip_id'), 'parent_alert_promise_trip', key)
            audit.equal(alert.get('deadline_at'), promise.get('window_end_at'), 'parent_alert_deadline', key)
            known = audit.timestamp(promise.get('known_at'), 'parent_promise_known', key)
            snapshot = audit.timestamp(opportunity.get('snapshot_time'), 'parent_snapshot_time', key)
            created = audit.timestamp(alert.get('created_at'), 'parent_alert_created', key)
            if None not in (known, snapshot, created):
                audit.check(known <= snapshot <= created, 'parent_alert_point_in_time', key)
    for key, attempt in attempts.items():
        alert = alerts.get((key[0], attempt.get('alert_id')))
        if audit.check(alert is not None, 'parent_attempt_alert_fk', key):
            audit.equal(attempt.get('trip_id'), alert.get('trip_id'), 'parent_attempt_trip', key)
            sent = audit.timestamp(attempt.get('sent_at'), 'parent_attempt_sent', key)
            created = audit.timestamp(alert.get('created_at'), 'parent_attempt_alert_created', key)
            if None not in (sent, created):
                audit.check(created <= sent, 'parent_attempt_after_alert', key)
            if attempt.get('delivered_at') is not None:
                delivered_at = audit.timestamp(attempt['delivered_at'], 'parent_attempt_delivered', key)
                if None not in (sent, delivered_at):
                    audit.check(sent <= delivered_at, 'parent_delivery_after_send', key)
                if attempt.get('receipt_known_at') is not None:
                    known = audit.timestamp(attempt['receipt_known_at'], 'parent_attempt_known', key)
                    if None not in (delivered_at, known):
                        audit.check(delivered_at <= known, 'parent_receipt_after_delivery', key)
        audit.equal(attempt.get('result_origin'), ORIGIN, 'parent_attempt_origin', key)
    for key, source in outcomes.items():
        through = audit.timestamp(source.get('followup_through'), 'source_followup_through', key)
        available = audit.timestamp(source.get('label_available_time'), 'source_label_available_time', key)
        if None not in (through, available):
            audit.check(through <= available, 'source_observation_precedes_knowledge', key)
        if source.get('actual_arrival_time') is not None:
            arrival = audit.timestamp(source.get('actual_arrival_time'), 'source_arrival_clock', key)
            if None not in (arrival, through):
                audit.check(arrival <= through, 'arrival_within_source_observation', key)

    delivered = {}
    for key, row in attempts.items():
        if row.get('delivery_status') != 'delivered' or row.get('receipt_observed') is not True:
            continue
        at = audit.timestamp(row.get('receipt_known_at'), 'source_receipt_known_at', key)
        if at is not None and at <= cutoff:
            delivered[key] = row
    delivered_alerts = {(key[0], row.get('alert_id')) for key, row in delivered.items()}
    earliest_delivery = {}
    for key, row in delivered.items():
        alert_key = key[0], row.get('alert_id')
        delivered_at = audit.timestamp(row.get('delivered_at'), 'confirmed_service_clock', key)
        receipt_at = audit.timestamp(row.get('receipt_known_at'), 'confirmed_receipt_clock', key)
        if None not in (delivered_at, receipt_at):
            order = delivered_at, receipt_at, row['delivery_id']
            if alert_key not in earliest_delivery or order < earliest_delivery[alert_key][0]:
                earliest_delivery[alert_key] = order, row['delivery_id']
    selected_alerts = [(key[0], row.get('alert_id')) for key, row in decisions.items()]
    audit.equal(set(selected_alerts), delivered_alerts, 'all_confirmed_alerts_accounted')
    audit.equal(len(selected_alerts), len(set(selected_alerts)), 'retry_does_not_duplicate_decision')
    audit.equal(summary.get('confirmed_delivered_alerts'), len(delivered_alerts), 'summary_confirmed_alerts')
    audit.equal(summary.get('opening_count'), len(openings), 'summary_opening_count')

    for key, row in openings.items():
        attempt = delivered.get((key[0], row.get('delivery_id')))
        if not audit.check(attempt is not None, 'opening_has_confirmed_receipt', key):
            continue
        _same_fields(audit, row, attempt, ('tenant_id', 'alert_id', 'trip_id'), 'opening_source', key)
        opened = audit.timestamp(row.get('opened_at'), 'opened_at', key)
        known = audit.timestamp(row.get('known_at'), 'opening_known_at', key)
        receipt = audit.timestamp(attempt.get('receipt_known_at'), 'opening_receipt_clock', key)
        if None not in (opened, known, receipt):
            audit.check(receipt <= opened <= known <= cutoff, 'opening_clock_order', key)
        audit.equal(row.get('result_origin'), ORIGIN, 'opening_origin', key)

    for key, row in decisions.items():
        attempt = delivered.get((key[0], row.get('delivery_id')))
        canonical = adoptions.get(key, {})
        if not audit.check(attempt is not None, 'decision_has_confirmed_receipt', key):
            continue
        selected_source = earliest_delivery.get((key[0], row.get('alert_id')))
        if selected_source is not None:
            audit.equal(row.get('delivery_id'), selected_source[1], 'earliest_confirmed_delivery_selected', key)
        _same_fields(audit, row, attempt, ('tenant_id', 'alert_id', 'trip_id', 'receipt_known_at'), 'decision_source', key)
        alert = alerts.get((key[0], row.get('alert_id')))
        if audit.check(alert is not None, 'decision_alert_exists', key):
            _same_fields(audit, row, alert, ('tenant_id', 'alert_id', 'trip_id', 'snapshot_id',
                         'prediction_version'), 'decision_alert', key)
            audit.equal(row.get('original_promise_version_id'), alert.get('promise_version_id'), 'decision_original_promise', key)
        audit.equal(row.get('service_delivered_at'), attempt.get('delivered_at'), 'decision_service_clock', key)
        _same_fields(audit, canonical, row, ('tenant_id', 'adoption_id', 'delivery_id',
                     'adoption_status', 'decided_at'), 'adoption_projection', key)
        audit.equal(canonical.get('adoption_reason'), row.get('reason'), 'adoption_reason_projection', key)
        status = row.get('adoption_status')
        audit.check(status in STATUSES, 'adoption_status_value', key)
        if status in ('partially_accepted', 'rejected', 'overridden'):
            audit.check(isinstance(row.get('reason'), str) and bool(row['reason'].strip()), 'decision_reason_present', key)
        opportunity = opportunities.get((key[0], row.get('snapshot_id')))
        if audit.check(opportunity is not None, 'decision_source_snapshot', key):
            _same_fields(audit, row, opportunity, ('tenant_id', 'trip_id', 'prediction_version'), 'decision_opportunity', key)
        if status == 'not_reviewed':
            audit.equal(row.get('decided_at'), None, 'unreviewed_no_decision_clock', key)
            audit.equal(row.get('decision_known_at'), None, 'unreviewed_no_known_decision_clock', key)
            if row.get('opening_id') is not None:
                opening = openings.get((key[0], row.get('opening_id')))
                if audit.check(opening is not None, 'unreviewed_opening_reference', key):
                    audit.equal(row.get('opened_at'), opening.get('opened_at'), 'unreviewed_opening_clock_binding', key)
                    audit.equal(row.get('opening_known_at'), opening.get('known_at'), 'unreviewed_opening_known_binding', key)
                    _same_fields(audit, row, opening, ('tenant_id', 'trip_id', 'delivery_id', 'alert_id'), 'unreviewed_opening', key)
        else:
            opening = openings.get((key[0], row.get('opening_id')))
            if audit.check(opening is not None, 'review_requires_opening', key):
                audit.equal(row.get('opened_at'), opening.get('opened_at'), 'decision_opened_clock_binding', key)
                audit.equal(row.get('opening_known_at'), opening.get('known_at'), 'decision_opening_known_binding', key)
                _same_fields(audit, row, opening, ('tenant_id', 'trip_id', 'delivery_id', 'alert_id'), 'decision_opening', key)
                known_open = audit.timestamp(opening.get('known_at'), 'review_open_known', key)
                decided = audit.timestamp(row.get('decided_at'), 'decided_at', key)
                known = audit.timestamp(row.get('decision_known_at'), 'decision_known_at', key)
                if None not in (known_open, decided, known):
                    audit.check(known_open <= decided <= known <= cutoff, 'decision_clock_order', key)
        audit.equal(row.get('result_origin'), ORIGIN, 'decision_origin', key)
        _audit_behavior(audit, key, row, attempt, actions, settings, cutoff)

    for key, row in actions.items():
        decision = decisions.get((key[0], row.get('adoption_id')))
        if not audit.check(decision is not None, 'action_decision_fk', key):
            continue
        audit.equal(row.get('alert_id'), decision.get('alert_id'), 'action_alert_fk', key)
        audit.check(row.get('action_type') in ACTION_TYPES, 'action_type_value', key)
        audit.check(decision.get('adoption_status') != 'not_reviewed', 'unreviewed_has_no_action', key)
        if decision.get('adoption_status') == 'rejected':
            audit.equal(row.get('action_type'), 'no_action', 'rejected_has_no_execution', key)
        seconds = row.get('handling_seconds')
        audit.check(seconds is None or type(seconds) is int and seconds >= 0, 'handling_seconds_value', key)
        action_at = audit.timestamp(row.get('action_time'), 'action_time', key)
        decision_known = audit.timestamp(decision.get('decision_known_at'), 'action_decision_known', key)
        if None not in (action_at, decision_known):
            audit.check(decision_known <= action_at <= cutoff, 'action_clock_order', key)

    used_outcomes, used_revisions = set(), set()
    for key, row in followups.items():
        action = actions.get(key)
        if not audit.check(action is not None, 'followup_action_fk', key):
            continue
        decision = decisions.get((key[0], action.get('adoption_id')), {})
        _same_fields(audit, row, action, ('tenant_id', 'action_id', 'adoption_id', 'alert_id',
                     'action_type', 'action_time'), 'followup_action', key)
        _same_fields(audit, row, decision, ('tenant_id', 'trip_id', 'delivery_id',
                     'original_promise_version_id'), 'followup_decision', key)
        audit.equal(row.get('result_origin'), ORIGIN, 'followup_origin', key)
        audit.equal(row.get('observation_cutoff_at'), settings['observation_cutoff_at'], 'followup_cutoff', key)
        _audit_followup(audit, key, row, action, original_promises, revised_promises,
                        outcomes, references, canonical_followups, cutoff,
                        settings, used_outcomes, used_revisions)
    audit.equal(set(references), used_outcomes, 'retained_outcome_reference_inventory')
    audit.equal(set(revised_promises), used_revisions, 'reschedule_promise_inventory')
    emitted = {key for key, row in followups.items() if row.get('canonical_row_emitted') is True}
    audit.equal(set(canonical_followups), emitted, 'canonical_followup_inventory')
    counts = {name: len(rows) for name, rows in arrays.items()}
    for name, field in (('eta_adoption', 'adoption_status'), ('eta_action', 'action_type'),
                        ('followup_audit', 'followup_status')):
        expected = dict(sorted(Counter(row.get(field) for row in arrays[name]).items()))
        audit.equal(summary.get(field + '_counts'), expected, 'summary_' + field + '_counts')
    _audit_counts(audit, counts, summary, metrics, arrays, delivered_alerts, inputs)
    _audit_events(audit, arrays['events'], decisions, actions, followups, cutoff)
    return _report(audit, counts)


def _audit_followup(audit, key, row, action, original_promises, revised_promises,
                    outcomes, references, canonical, cutoff, settings,
                    used_outcomes, used_revisions):
    original_key = (key[0], row.get('original_promise_version_id'))
    followup_key = (key[0], row.get('followup_promise_version_id'))
    original = original_promises.get(original_key)
    revised = revised_promises.get(followup_key)
    selected_promise = revised or original_promises.get(followup_key)
    if audit.check(original is not None, 'original_promise_exists', key):
        audit.equal(original.get('trip_id'), row.get('trip_id'), 'original_promise_trip', key)
        audit.equal(original.get('window_end_at'), row.get('original_deadline_at'), 'original_deadline_binding', key)
    if audit.check(selected_promise is not None, 'followup_promise_exists', key):
        audit.equal(selected_promise.get('trip_id'), row.get('trip_id'), 'followup_promise_trip', key)
        audit.equal(selected_promise.get('window_end_at'), row.get('followup_deadline_at'), 'followup_deadline_binding', key)
    if revised is not None:
        used_revisions.add(followup_key)
        audit.check(followup_key not in original_promises, 'reschedule_has_new_version', key)
        audit.equal(action.get('action_type'), 'reschedule', 'reschedule_requires_action', key)
        audit.equal(revised.get('known_at'), action.get('action_time'), 'reschedule_known_at_action', key)
        if original is not None:
            for field in ('window_start_at', 'window_end_at'):
                before = audit.timestamp(original.get(field), 'original_' + field, key)
                after = audit.timestamp(revised.get(field), 'revised_' + field, key)
                if None not in (before, after):
                    audit.equal((after - before).total_seconds(), float(settings['reschedule_shift_seconds']), 'promise_fixed_shift:' + field, key)
    else:
        audit.equal(followup_key, original_key, 'non_reschedule_retains_promise', key)

    start = audit.timestamp(action.get('action_time'), 'followup_action_clock', key)
    planned = audit.timestamp(row.get('planned_observation_end'), 'planned_followup_end', key)
    observed = audit.timestamp(row.get('outcome_observation_end'), 'observed_followup_end', key)
    if None not in (start, planned, observed):
        audit.check(start <= observed <= cutoff and observed == min(planned, cutoff), 'followup_window_censoring', key)
        expected_profile = _profile_number(settings, key[0], row.get('delivery_id'), 'followup_profile') % 8
        audit.equal(row.get('followup_profile'), expected_profile, 'followup_profile_identity_only', key)
        hours = settings['long_followup_window_hours'] if expected_profile == 2 else settings['followup_window_hours']
        audit.equal(planned, start + timedelta(hours=hours), 'followup_prescribed_window', key)
    else:
        expected_profile = None
    original_delay = row.get('original_promise_delay_seconds')
    revised_delay = row.get('observed_delay_seconds')
    _numeric_delay(audit, original_delay, 'original_delay_type', key)
    _numeric_delay(audit, revised_delay, 'observed_delay_type', key)
    outcome_id = row.get('outcome_version_id')
    source = outcomes.get((key[0], outcome_id)) if outcome_id is not None else None
    if outcome_id is not None:
        used_outcomes.add((key[0], outcome_id))
        if audit.check(source is not None, 'retained_outcome_exists', key):
            audit.equal(source.get('trip_id'), row.get('trip_id'), 'outcome_same_trip', key)
            audit.equal(references.get((key[0], outcome_id)), source, 'outcome_reference_unmodified', key)
            audit.equal(row.get('outcome_available_at'), source.get('label_available_time'), 'outcome_available_binding', key)
            if row.get('actual_arrival_time') is not None:
                audit.equal(row.get('actual_arrival_time'), source.get('actual_arrival_time'), 'arrival_not_shifted', key)
            audit.equal(row.get('source_label_status'), source.get('label_status'), 'outcome_status_binding', key)
            audit.equal(row.get('source_followup_through'), source.get('followup_through'), 'source_observation_clock_binding', key)
            audit.equal(row.get('source_arrival_time_precision'), source.get('arrival_time_precision'), 'source_precision_binding', key)
            audit.equal(row.get('source_arrival_evidence_ref'), source.get('arrival_evidence_ref'), 'source_arrival_evidence_binding', key)
            known = audit.timestamp(source.get('label_available_time'), 'selected_outcome_known', key)
            if None not in (known, observed):
                audit.check(known <= observed, 'outcome_known_by_observation_end', key)
    if row.get('canonical_row_emitted') is True:
        audit.check(source is not None, 'canonical_followup_requires_real_version', key)
        projected = canonical.get(key, {})
        _same_fields(audit, projected, row, CANONICAL_FIELDS['eta_action_followup'] - {'provenance_id'}, 'followup_projection', key)
    else:
        audit.check(key not in canonical, 'aux_only_followup_not_canonical', key)
    mature_confirmed = row.get('followup_status') == 'mature' and row.get('review_outcome') == 'confirmed'
    if mature_confirmed and source is not None:
        audit.equal(source.get('label_status'), 'verified', 'mature_requires_verified_arrival', key)
        audit.check(source.get('arrival_time_precision') in ('second', 'minute'), 'mature_arrival_has_point_precision', key)
        audit.check(isinstance(source.get('arrival_evidence_ref'), str) and bool(source['arrival_evidence_ref'].strip()),
                    'mature_arrival_has_evidence_reference', key)
        audit.equal(source.get('cancellation_time'), None, 'mature_arrival_not_cancelled', key)
        arrival = audit.timestamp(source.get('actual_arrival_time'), 'verified_arrival', key)
        original_at = audit.timestamp(row.get('original_deadline_at'), 'original_deadline', key)
        revised_at = audit.timestamp(row.get('followup_deadline_at'), 'followup_deadline', key)
        if None not in (arrival, original_at, revised_at, observed, planned):
            audit.check(arrival <= observed and planned <= cutoff, 'mature_arrival_and_window', key)
            audit.equal(original_delay, _delay(arrival, original_at), 'hand_computed_original_delay', key)
            audit.equal(revised_delay, _delay(arrival, revised_at), 'hand_computed_revised_delay', key)
    else:
        audit.equal(original_delay, None, 'unobserved_original_delay_is_null', key)
        audit.equal(revised_delay, None, 'unobserved_followup_delay_is_null', key)
    if row.get('followup_status') in ('no_known_outcome', 'no_action'):
        audit.equal(outcome_id, None, 'missing_or_no_action_no_version', key)
        audit.equal(row.get('canonical_row_emitted'), False, 'missing_or_no_action_aux_only', key)
    if observed is not None:
        eligible = []
        if action.get('action_type') != 'no_action':
            for source_key, candidate in outcomes.items():
                if source_key[0] != key[0] or candidate.get('trip_id') != row.get('trip_id'):
                    continue
                known = audit.timestamp(candidate.get('label_available_time'), 'candidate_outcome_known', source_key)
                if known is not None and known <= observed:
                    eligible.append((known, candidate['outcome_version_id'], candidate))
        selected = max(eligible, key=lambda x: (x[0], x[1]))[2] if eligible else None
        audit.equal(outcome_id, selected.get('outcome_version_id') if selected else None,
                    'latest_cutoff_known_outcome_selected', key)
        if action.get('action_type') == 'no_action':
            expected_state, expected_review = 'no_action', 'not_reported'
        elif selected is None:
            expected_state, expected_review = 'no_known_outcome', 'open'
        elif planned is not None and planned > observed:
            expected_state, expected_review = 'immature', 'open'
        elif expected_profile == 0:
            expected_state, expected_review = 'lost', 'lost'
        elif expected_profile == 1:
            expected_state, expected_review = 'not_reported', 'not_reported'
        elif expected_profile == 3:
            expected_state, expected_review = 'disputed', 'disputed'
        elif selected.get('label_status') == 'cancelled':
            expected_state, expected_review = 'rejected', 'rejected'
        else:
            point_arrival = (selected.get('label_status') == 'verified'
                             and selected.get('arrival_time_precision') in ('second', 'minute')
                             and isinstance(selected.get('arrival_evidence_ref'), str)
                             and bool(selected['arrival_evidence_ref'].strip())
                             and selected.get('cancellation_time') is None
                             and selected.get('actual_arrival_time') is not None)
            arrival = audit.timestamp(selected.get('actual_arrival_time'), 'candidate_verified_arrival', key) if point_arrival else None
            if arrival is not None and start is not None and start <= arrival <= observed:
                expected_state, expected_review = 'mature', 'confirmed'
            else:
                expected_state, expected_review = 'not_reported', 'not_reported'
        audit.equal(row.get('followup_status'), expected_state, 'independent_followup_state', key)
        audit.equal(row.get('review_outcome'), expected_review, 'independent_review_state', key)
        audit.equal(row.get('canonical_row_emitted'), selected is not None and action.get('action_type') != 'no_action', 'independent_canonical_eligibility', key)
        if expected_state != 'mature':
            audit.equal(row.get('actual_arrival_time'), None, 'unconfirmed_arrival_withheld', key)
    audit.equal(row.get('promise_shift_seconds'), settings['reschedule_shift_seconds'] if action.get('action_type') == 'reschedule' else 0, 'promise_shift_disclosure', key)


def _audit_behavior(audit, key, row, attempt, actions, settings, cutoff):
    tenant, delivery = key[0], row['delivery_id']
    number = lambda purpose: _profile_number(settings, tenant, delivery, purpose)
    profile = number('review_profile') % 10
    audit.equal(row.get('decision_profile'), profile, 'review_profile_identity_only', key)
    receipt = audit.timestamp(attempt.get('receipt_known_at'), 'profile_receipt_clock', key)
    if receipt is None:
        return
    opened = open_known = decided = None
    status = 'not_reviewed'
    reason = 'synthetic_unopened'
    candidate_open = receipt + timedelta(seconds=settings['opening_delay_seconds_min']
        + number('opening_delay') % settings['opening_delay_seconds_span'])
    if profile != 0:
        if candidate_open + timedelta(seconds=5) <= cutoff:
            opened, open_known = candidate_open, candidate_open + timedelta(seconds=5)
            reason = 'synthetic_opened_not_reviewed'
            candidate_decision = open_known + timedelta(seconds=settings['decision_delay_seconds_min']
                + number('decision_delay') % settings['decision_delay_seconds_span'])
            if profile != 1 and candidate_decision <= cutoff:
                decided = candidate_decision
                status = {2: 'rejected', 3: 'overridden', 4: 'partially_accepted',
                          5: 'partially_accepted'}.get(profile, 'accepted')
                reason = {'rejected': 'synthetic_reviewer_rejected_recommendation',
                          'overridden': 'synthetic_reviewer_selected_alternative',
                          'partially_accepted': 'synthetic_reviewer_selected_partial_response',
                          'accepted': 'synthetic_reviewer_accepted_recommendation'}[status]
        else:
            reason = 'opening_not_observed_by_cutoff'
    for field, expected in (('opened_at', opened), ('opening_known_at', open_known),
                            ('decided_at', decided), ('decision_known_at', decided)):
        actual = audit.timestamp(row.get(field), 'profile_' + field, key) if row.get(field) is not None else None
        audit.equal(actual, expected, 'identity_only_' + field, key)
    audit.equal(row.get('adoption_status'), status, 'identity_only_review_status', key)
    audit.equal(row.get('reason'), reason, 'identity_only_review_reason', key)
    audit.equal(row.get('opening_id') is not None, opened is not None, 'opening_identity_presence', key)
    matching = [r for (t, _), r in actions.items() if t == tenant and r.get('adoption_id') == key[1]]
    handling = settings['handling_seconds_min'] + number('handling') % settings['handling_seconds_span']
    action_time = decided + timedelta(seconds=settings['action_delay_seconds'] + handling) if decided else None
    expected_action = action_time is not None and action_time <= cutoff
    audit.equal(len(matching), int(expected_action), 'identity_only_action_presence', key)
    if expected_action and matching:
        expected_type = ('no_action' if status == 'rejected' else 'reassign' if status == 'overridden'
                         else ('contact_driver', 'contact_customer', 'reschedule', 'reassign')[number('action') % 4])
        audit.equal(matching[0].get('action_type'), expected_type, 'identity_only_action_type', key)
        audit.equal(matching[0].get('handling_seconds'), handling, 'identity_only_handling_seconds', key)
        actual_time = audit.timestamp(matching[0].get('action_time'), 'profile_action_time', key)
        audit.equal(actual_time, action_time, 'identity_only_action_time', key)


def _audit_counts(audit, counts, summary, metrics, arrays, delivered_alerts, inputs):
    audit.equal(summary.get('counts'), counts, 'summary_array_counts')
    audit.equal(set(metrics), {'status', 'source_scenario', 'observation_cutoff_at',
        'population', 'funnel', 'rates', 'actions', 'followup', 'by_tenant', 'denominator_notes',
        'real_notifications_sent', 'real_recipient_behaviour_observed',
        'causal_benefit_estimated', 'quality_accepted', 'operational_approval'}, 'metric_field_contract')
    for flag in ('quality_accepted', 'operational_approval', 'causal_benefit_estimated',
                 'real_notifications_sent', 'real_recipient_behaviour_observed'):
        audit.equal(metrics.get(flag), False, 'metric_false_claim:' + flag)
    audit.equal(metrics.get('status'), 'descriptive_synthetic_workflow', 'metric_scope')
    audit.equal(metrics.get('source_scenario'), summary.get('source_scenario'), 'metric_scenario')
    audit.equal(metrics.get('observation_cutoff_at'), summary.get('observation_cutoff_at'), 'metric_cutoff')
    expected = _count_metrics(inputs, arrays, delivered_alerts)
    for section, values in expected.items():
        audit.equal(metrics.get(section), values, 'metric_recount:' + section)
    tenants = sorted({r['tenant_id'] for r in inputs.get('opportunities', [])}
                     | {r['tenant_id'] for r in inputs.get('alerts', [])})
    per_tenant = {}
    for tenant in tenants:
        source = {name: [r for r in inputs.get(name, []) if r.get('tenant_id') == tenant]
                  for name in ('opportunities', 'alerts', 'attempts')}
        selected = {name: [r for r in rows if r.get('tenant_id') == tenant]
                    for name, rows in arrays.items()}
        per_tenant[tenant] = _count_metrics(source, selected, {k for k in delivered_alerts if k[0] == tenant})
    audit.equal(metrics.get('by_tenant'), per_tenant, 'metric_recount:by_tenant')


def _count_metrics(inputs, arrays, delivered_alerts):
    """Count source/record sets directly rather than reuse the production scorer."""
    opportunities, alerts, attempts = (inputs.get(n, []) for n in ('opportunities', 'alerts', 'attempts'))
    adoptions, actions, followups = (arrays[n] for n in ('eta_adoption', 'eta_action', 'eta_action_followup'))
    adoption_counts = {s: sum(r.get('adoption_status') == s for r in adoptions) for s in STATUSES}
    action_counts = {s: sum(r.get('action_type') == s for r in actions) for s in ACTION_TYPES}
    opened = len({(r.get('tenant_id'), r.get('alert_id')) for r in arrays['opening_events']})
    reviewed = len(adoptions) - adoption_counts['not_reviewed']
    accepted = adoption_counts['accepted'] + adoption_counts['partially_accepted']
    handling = [r['handling_seconds'] for r in actions if type(r.get('handling_seconds')) is int]
    known = [r for r in followups if type(r.get('original_promise_delay_seconds')) is int
             and type(r.get('observed_delay_seconds')) is int]
    original_total = sum(r['original_promise_delay_seconds'] for r in known)
    revised_total = sum(r['observed_delay_seconds'] for r in known)
    executed = len(actions) - action_counts['no_action']
    ratio = lambda n, d: {'numerator': n, 'denominator': d, 'value': n / d if d else None}
    return {
        'population': {'opportunities': len(opportunities),
            'trips': len({(r['tenant_id'], r['trip_id']) for r in opportunities}),
            'alerts': len(alerts), 'transport_attempts': len(attempts),
            'confirmed_delivered_alerts': len(delivered_alerts),
            'non_delivered_alerts': len(alerts) - len(delivered_alerts)},
        'funnel': {'delivered_alerts': len(delivered_alerts), 'opened_alerts': opened,
            'reviewed_alerts': reviewed, 'not_reviewed_alerts': adoption_counts['not_reviewed'],
            'accepted_alerts': adoption_counts['accepted'],
            'partially_accepted_alerts': adoption_counts['partially_accepted'],
            'rejected_alerts': adoption_counts['rejected'], 'overridden_alerts': adoption_counts['overridden'],
            'accepted_or_partial_alerts': accepted},
        'rates': {'opened_per_delivery': ratio(opened, len(delivered_alerts)),
            'reviewed_per_delivery': ratio(reviewed, len(delivered_alerts)),
            'accepted_or_partial_per_delivery': ratio(accepted, len(delivered_alerts)),
            'accepted_or_partial_per_reviewed': ratio(accepted, reviewed)},
        'actions': {'records': len(actions), 'executed_actions': executed,
            'no_action_records': action_counts['no_action'], 'by_type': action_counts,
            'handling_known_records': len(handling), 'handling_unknown_records': len(actions) - len(handling),
            'handling_seconds_sum': sum(handling)},
        'followup': {'audit_records': len(arrays['followup_audit']), 'canonical_records': len(followups),
            'by_status': {s: sum(r.get('followup_status') == s for r in arrays['followup_audit'])
                          for s in ('mature', 'immature', 'lost', 'not_reported', 'disputed', 'rejected', 'no_known_outcome', 'no_action')},
            'known_arrival_comparisons': len(known), 'actions_without_comparable_arrival': executed - len(known),
            'original_delay_seconds_sum': original_total, 'comparison_delay_seconds_sum': revised_total,
            'deadline_definition_difference_seconds': original_total - revised_total,
            'arrival_change_claimed': False},
    }


def _audit_events(audit, events, decisions, actions, followups, cutoff):
    expected = {}
    for key, row in decisions.items():
        if row.get('opening_id') is not None:
            expected[(key[0], row.get('delivery_id'), 'recipient_opened')] = (
                row.get('opened_at'), row.get('opening_known_at'), key[1], None)
        if row.get('decided_at') is not None:
            expected[(key[0], row.get('delivery_id'), 'review_decided')] = (
                row.get('decided_at'), row.get('decision_known_at'), key[1], None)
    for key, row in actions.items():
        parent = decisions.get((key[0], row.get('adoption_id')), {})
        expected[(key[0], parent.get('delivery_id'), 'action_recorded')] = (
            row.get('action_time'), row.get('action_time'), row.get('adoption_id'), key[1])
    for key, row in followups.items():
        if row.get('canonical_row_emitted') is True:
            expected[(key[0], row.get('delivery_id'), 'followup_observed')] = (
                row.get('outcome_observation_end'), row.get('outcome_observation_end'),
                row.get('adoption_id'), key[1])
    actual_keys = []
    for row in events:
        key = [row.get('tenant_id'), row.get('event_id')]
        event_key = row.get('tenant_id'), row.get('delivery_id'), row.get('event_type')
        actual_keys.append(event_key)
        values = (row.get('event_at'), row.get('known_at'), row.get('adoption_id'), row.get('action_id'))
        audit.equal(values, expected.get(event_key), 'event_matches_workflow_observation', key)
        at = audit.timestamp(row.get('event_at'), 'event_at', key)
        known = audit.timestamp(row.get('known_at'), 'event_known_at', key)
        if None not in (at, known):
            audit.check(at <= known <= cutoff, 'event_clock_order', key)
        audit.equal(row.get('result_origin'), ORIGIN, 'event_origin', key)
        if row.get('adoption_id') is not None:
            parent = decisions.get((row.get('tenant_id'), row['adoption_id']))
            if audit.check(parent is not None, 'event_adoption_fk', key):
                _same_fields(audit, row, parent, ('tenant_id', 'trip_id', 'alert_id', 'delivery_id'), 'event_decision', key)
        if row.get('action_id') is not None:
            parent = actions.get((row.get('tenant_id'), row['action_id']))
            if audit.check(parent is not None, 'event_action_fk', key):
                audit.equal(row.get('adoption_id'), parent.get('adoption_id'), 'event_action_adoption', key)
    audit.equal(set(actual_keys), set(expected), 'event_inventory_complete')
    audit.equal(len(actual_keys), len(set(actual_keys)), 'event_observations_not_duplicated')


def _report(audit, counts):
    return {'audit_version': VERSION, 'status': 'passed' if not audit.failures else 'failed',
            'comparison_count': audit.comparisons, 'failure_count': len(audit.failures),
            'failures': audit.failures[:200], 'failure_details_truncated': len(audit.failures) > 200,
            'output_row_counts': counts, 'result_origin': ORIGIN,
            'quality_accepted': False, 'operational_approval': False,
            'causal_benefit_estimated': False,
            'scope': 'Independent source relationships, event clocks, denominators and delay arithmetic; synthetic workflow consistency only.'}
