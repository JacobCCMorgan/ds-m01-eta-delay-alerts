"""Deterministic, explicitly synthetic recipient workflow over retained deliveries.

Behavior is selected from delivery identity, never forecast accuracy or outcomes.
Follow-up observes the unchanged P08 arrival; a revised promise is a separate
comparison deadline, not an intervention-induced change in arrival or benefit.
"""
from collections import Counter
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import math

from ds_m01.common import parse_ts, utc
from .inputs import index, require, validate_inputs, validate_settings


ORIGIN = 'synthetic_scenario'
ADOPTION_FIELDS = ('tenant_id', 'provenance_id', 'adoption_id', 'delivery_id',
                   'adoption_status', 'adoption_reason', 'decided_at')
ACTION_FIELDS = ('tenant_id', 'provenance_id', 'action_id', 'adoption_id',
                 'alert_id', 'action_type', 'action_time', 'handling_seconds')
FOLLOWUP_FIELDS = ('tenant_id', 'provenance_id', 'action_id', 'outcome_version_id',
                   'followup_promise_version_id', 'original_promise_version_id',
                   'outcome_observation_end', 'observed_delay_seconds',
                   'original_promise_delay_seconds', 'review_outcome', 'result_origin')


def _number(seed, tenant, delivery_id, purpose):
    encoded = json.dumps([seed, tenant, delivery_id, purpose], separators=(',', ':')).encode()
    return int.from_bytes(hashlib.sha256(encoded).digest()[:8], 'big')


def _id(prefix, *values):
    payload = json.dumps(values, separators=(',', ':'), ensure_ascii=False).encode()
    return prefix + '-' + hashlib.sha256(payload).hexdigest()[:24]


def _at(value, seconds):
    return utc(parse_ts(value) + timedelta(seconds=seconds))


def _delay(arrival, deadline):
    return math.ceil(max(0.0, (parse_ts(arrival) - parse_ts(deadline)).total_seconds()))


def _event(events, tenant, trip, alert, delivery, kind, event_at, known_at,
           adoption=None, action=None):
    events.append(dict(tenant_id=tenant, event_id=_id('P09-EVENT', tenant, delivery, kind),
        event_type=kind, event_at=event_at, known_at=known_at, trip_id=trip,
        alert_id=alert, delivery_id=delivery, adoption_id=adoption, action_id=action,
        provenance_id=_id('P09-PROV', tenant, delivery, kind), result_origin=ORIGIN))


def _delivered(inputs, cutoff):
    """One earliest observed successful service delivery per tenant/alert."""
    selected = {}
    for row in inputs['attempts']:
        if not (row.get('delivery_status') == 'delivered'
                and row.get('receipt_observed') is True
                and row.get('receipt_known_at') is not None
                and row.get('delivered_at') is not None):
            continue
        if parse_ts(row['receipt_known_at']) > cutoff:
            continue
        key = row['tenant_id'], row['alert_id']
        order = parse_ts(row['delivered_at']), parse_ts(row['receipt_known_at']), row['delivery_id']
        if key not in selected or order < selected[key][0]:
            selected[key] = order, row
    return [selected[key][1] for key in sorted(selected)]


def simulate_workflow(inputs, settings):
    validate_settings(settings)
    validate_inputs(inputs)
    cutoff = parse_ts(settings['observation_cutoff_at'])
    cutoff_text = utc(cutoff)
    alerts = index(inputs['alerts'], ('tenant_id', 'alert_id'), 'alerts')
    promises = index(inputs['promises'], ('tenant_id', 'promise_version_id'), 'promises')
    output = {name: [] for name in ('eta_adoption', 'eta_action', 'eta_action_followup',
        'opening_events', 'decision_audit', 'followup_audit', 'promise_versions',
        'outcome_references', 'events')}
    deliveries = _delivered(inputs, cutoff)
    source_outcomes = {}
    for row in inputs['outcomes']:
        source_outcomes.setdefault((row['tenant_id'], row['trip_id']), []).append(row)
    used_outcomes = {}

    # This loop's review/opening/action decisions do not inspect source_outcomes.
    for delivery in deliveries:
        tenant, delivery_id = delivery['tenant_id'], delivery['delivery_id']
        alert = alerts[tenant, delivery['alert_id']]
        trip, alert_id = alert['trip_id'], alert['alert_id']
        original = promises[tenant, alert['promise_version_id']]
        n = lambda purpose: _number(settings['seed'], tenant, delivery_id, purpose)
        profile = n('review_profile') % 10
        adoption_id = _id('P09-ADOPT', tenant, delivery_id)
        opening_id = opened_at = opening_known = decided_at = decision_known = None
        status, reason = 'not_reviewed', 'synthetic_unopened'
        if profile != 0:
            candidate_opening = _at(delivery['receipt_known_at'], settings['opening_delay_seconds_min']
                                    + n('opening_delay') % settings['opening_delay_seconds_span'])
            candidate_known = _at(candidate_opening, 5)
            if parse_ts(candidate_known) <= cutoff:
                opened_at, opening_known = candidate_opening, candidate_known
                opening_id = _id('P09-OPEN', tenant, delivery_id)
                output['opening_events'].append(dict(tenant_id=tenant, opening_id=opening_id,
                    delivery_id=delivery_id, alert_id=alert_id, trip_id=trip, opened_at=opened_at,
                    known_at=opening_known, provenance_id=_id('P09-PROV', opening_id), result_origin=ORIGIN))
                _event(output['events'], tenant, trip, alert_id, delivery_id,
                       'recipient_opened', opened_at, opening_known, adoption_id)
                reason = 'synthetic_opened_not_reviewed'
                candidate_decision = _at(opening_known, settings['decision_delay_seconds_min']
                                        + n('decision_delay') % settings['decision_delay_seconds_span'])
                if profile != 1 and parse_ts(candidate_decision) <= cutoff:
                    decided_at = decision_known = candidate_decision
                    status = ('rejected' if profile == 2 else 'overridden' if profile == 3
                              else 'partially_accepted' if profile in (4, 5) else 'accepted')
                    reason = {'rejected': 'synthetic_reviewer_rejected_recommendation',
                              'overridden': 'synthetic_reviewer_selected_alternative',
                              'partially_accepted': 'synthetic_reviewer_selected_partial_response',
                              'accepted': 'synthetic_reviewer_accepted_recommendation'}[status]
                    _event(output['events'], tenant, trip, alert_id, delivery_id,
                           'review_decided', decided_at, decision_known, adoption_id)
            else:
                reason = 'opening_not_observed_by_cutoff'
        output['eta_adoption'].append(dict(tenant_id=tenant, provenance_id=_id('P09-PROV', adoption_id),
            adoption_id=adoption_id, delivery_id=delivery_id, adoption_status=status,
            adoption_reason=reason, decided_at=decided_at))
        output['decision_audit'].append(dict(tenant_id=tenant, adoption_id=adoption_id,
            delivery_id=delivery_id, alert_id=alert_id, trip_id=trip,
            snapshot_id=alert['snapshot_id'], prediction_version=alert['prediction_version'],
            original_promise_version_id=alert['promise_version_id'], opening_id=opening_id,
            opened_at=opened_at, opening_known_at=opening_known,
            receipt_known_at=delivery['receipt_known_at'], service_delivered_at=delivery['delivered_at'],
            adoption_status=status, decided_at=decided_at, decision_known_at=decision_known,
            decision_profile=profile, reason=reason, result_origin=ORIGIN))
        if status == 'not_reviewed':
            continue
        action_type = ('no_action' if status == 'rejected' else 'reassign' if status == 'overridden'
                       else ('contact_driver', 'contact_customer', 'reschedule', 'reassign')[n('action') % 4])
        handling = settings['handling_seconds_min'] + n('handling') % settings['handling_seconds_span']
        action_time = _at(decided_at, settings['action_delay_seconds'] + handling)
        if parse_ts(action_time) > cutoff:
            continue  # A review does not establish an executed action.
        action_id = _id('P09-ACTION', tenant, adoption_id)
        output['eta_action'].append(dict(tenant_id=tenant, provenance_id=_id('P09-PROV', action_id),
            action_id=action_id, adoption_id=adoption_id, alert_id=alert_id,
            action_type=action_type, action_time=action_time, handling_seconds=handling))
        _event(output['events'], tenant, trip, alert_id, delivery_id,
               'action_recorded', action_time, action_time, adoption_id, action_id)
        followup_promise = original
        if action_type == 'reschedule':
            followup_promise = dict(tenant_id=tenant, trip_id=trip,
                promise_version_id=_id('P09-PROMISE', tenant, action_id),
                provenance_id=_id('P09-PROV', action_id, 'promise'), known_at=action_time,
                window_start_at=_at(original['window_start_at'], settings['reschedule_shift_seconds']),
                window_end_at=_at(original['window_end_at'], settings['reschedule_shift_seconds']))
            output['promise_versions'].append(followup_promise)
        followup_profile = n('followup_profile') % 8
        hours = settings['long_followup_window_hours'] if followup_profile == 2 else settings['followup_window_hours']
        planned_end = _at(action_time, hours * 3600)
        end = utc(min(parse_ts(planned_end), cutoff))
        output['followup_audit'].append(dict(tenant_id=tenant, action_id=action_id,
            adoption_id=adoption_id, alert_id=alert_id, delivery_id=delivery_id, trip_id=trip,
            action_type=action_type, action_time=action_time,
            original_promise_version_id=original['promise_version_id'],
            followup_promise_version_id=followup_promise['promise_version_id'],
            original_deadline_at=original['window_end_at'], followup_deadline_at=followup_promise['window_end_at'],
            planned_observation_end=planned_end, observation_cutoff_at=cutoff_text,
            outcome_observation_end=end, followup_status=None, review_outcome=None,
            outcome_version_id=None, outcome_available_at=None, actual_arrival_time=None,
            source_label_status=None, source_followup_through=None,
            source_arrival_time_precision=None, source_arrival_evidence_ref=None,
            original_promise_delay_seconds=None, observed_delay_seconds=None,
            promise_shift_seconds=settings['reschedule_shift_seconds'] if action_type == 'reschedule' else 0,
            canonical_row_emitted=False, reason=None, result_origin=ORIGIN,
            followup_profile=followup_profile))

    # Outcome observation is a separate pass, after target-independent behavior.
    for audit in output['followup_audit']:
        if audit['action_type'] == 'no_action':
            audit.update(followup_status='no_action', review_outcome='not_reported',
                         reason='Deliberate no-action decision is not an intervention to follow up')
            continue
        end = parse_ts(audit['outcome_observation_end'])
        available = [row for row in source_outcomes.get((audit['tenant_id'], audit['trip_id']), [])
                     if parse_ts(row['label_available_time']) <= end]
        outcome = max(available, key=lambda row: (parse_ts(row['label_available_time']),
                      row['outcome_version_id'])) if available else None
        if outcome is None:
            audit.update(followup_status='no_known_outcome', review_outcome='open',
                         reason='No retained outcome version was available by the observation cutoff')
            continue
        audit.update(outcome_version_id=outcome['outcome_version_id'],
                     outcome_available_at=outcome['label_available_time'],
                     source_label_status=outcome['label_status'],
                     source_followup_through=outcome['followup_through'],
                     source_arrival_time_precision=outcome.get('arrival_time_precision'),
                     source_arrival_evidence_ref=outcome.get('arrival_evidence_ref'))
        profile = audit['followup_profile']
        if parse_ts(audit['planned_observation_end']) > end:
            state, review, reason = 'immature', 'open', 'The prescribed follow-up window is not mature'
        elif profile == 0:
            state, review, reason = 'lost', 'lost', 'Synthetic workflow follow-up contact was lost'
        elif profile == 1:
            state, review, reason = 'not_reported', 'not_reported', 'Synthetic workflow follow-up was not reported'
        elif profile == 3:
            state, review, reason = 'disputed', 'disputed', 'Synthetic follow-up review remains disputed'
        elif outcome['label_status'] == 'cancelled':
            state, review, reason = 'rejected', 'rejected', 'Retained source records a cancellation, not an arrival'
        elif (outcome['label_status'] == 'verified'
                and outcome.get('arrival_time_precision') not in ('second', 'minute')):
            state, review, reason = 'not_reported', 'not_reported', 'Arrival precision is insufficient for a descriptive delay'
        elif (outcome['label_status'] == 'verified'
                and (not isinstance(outcome.get('arrival_evidence_ref'), str)
                     or not outcome['arrival_evidence_ref'].strip())):
            state, review, reason = 'not_reported', 'not_reported', 'Verified arrival lacks independent gate evidence'
        elif outcome['label_status'] == 'verified' and outcome.get('cancellation_time') is not None:
            state, review, reason = 'not_reported', 'not_reported', 'Verified arrival conflicts with cancellation evidence'
        elif (outcome['label_status'] == 'verified' and outcome.get('actual_arrival_time') is not None
                and parse_ts(outcome['actual_arrival_time']) <= end
                and parse_ts(outcome['actual_arrival_time']) >= parse_ts(audit['action_time'])):
            state, review, reason = 'mature', 'confirmed', 'Observed unchanged source arrival after a mature workflow window'
            audit['actual_arrival_time'] = outcome['actual_arrival_time']
            audit['original_promise_delay_seconds'] = _delay(outcome['actual_arrival_time'], audit['original_deadline_at'])
            audit['observed_delay_seconds'] = _delay(outcome['actual_arrival_time'], audit['followup_deadline_at'])
        else:
            state, review, reason = 'not_reported', 'not_reported', 'No verified post-action arrival was observed'
        audit.update(followup_status=state, review_outcome=review, reason=reason, canonical_row_emitted=True)
        row = dict(tenant_id=audit['tenant_id'], provenance_id=_id('P09-PROV', audit['action_id'], 'followup'),
                   action_id=audit['action_id'], outcome_version_id=outcome['outcome_version_id'],
                   followup_promise_version_id=audit['followup_promise_version_id'],
                   original_promise_version_id=audit['original_promise_version_id'],
                   outcome_observation_end=audit['outcome_observation_end'],
                   observed_delay_seconds=audit['observed_delay_seconds'],
                   original_promise_delay_seconds=audit['original_promise_delay_seconds'],
                   review_outcome=review, result_origin=ORIGIN)
        output['eta_action_followup'].append(row)
        used_outcomes[audit['tenant_id'], outcome['outcome_version_id']] = deepcopy(outcome)
        _event(output['events'], audit['tenant_id'], audit['trip_id'], audit['alert_id'], audit['delivery_id'],
               'followup_observed', audit['outcome_observation_end'], audit['outcome_observation_end'],
               audit['adoption_id'], audit['action_id'])
    output['outcome_references'] = [used_outcomes[key] for key in sorted(used_outcomes)]
    for name in output:
        output[name].sort(key=lambda row: json.dumps(row, sort_keys=True, separators=(',', ':')))
    output['summary'] = dict(observation_cutoff_at=cutoff_text, source_scenario=inputs['scenario_id'],
        confirmed_delivered_alerts=len(deliveries), opening_count=len(output['opening_events']),
        counts={name: len(rows) for name, rows in output.items()},
        adoption_status_counts=dict(sorted(Counter(r['adoption_status'] for r in output['eta_adoption']).items())),
        action_type_counts=dict(sorted(Counter(r['action_type'] for r in output['eta_action']).items())),
        followup_status_counts=dict(sorted(Counter(r['followup_status'] for r in output['followup_audit']).items())),
        model_or_calibrator_refitted=False, policy_reselected=False, real_notifications_sent=False,
        real_recipient_observations=False, quality_accepted=False, operational_approval=False,
        causal_benefit_estimated=False,
        scope='Synthetic post-exposure workflow demonstration; original forecasts, deliveries and arrivals unchanged')
    json.dumps(output, allow_nan=False)
    return output


def validate_simulation(inputs, simulation, settings):
    require(simulation == simulate_workflow(inputs, settings),
            'Workflow output differs from deterministic source reconstruction')
    return True
