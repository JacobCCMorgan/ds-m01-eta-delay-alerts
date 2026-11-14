"""Mature gate-arrival duration targets for fixed July trip roles.

Probability targets continue to use the stricter second-precision M3c builder.
Only declared trip keys and outcome versions known before a role cutoff are
inspected; buffer and final-test outcomes are never selected.
"""
from .candidate_targets import select_fit_outcomes
from .common import parse_ts, utc
from .delay_targets import OUTCOME_STATUSES


def build_eta_targets(opportunities, trip_keys, outcomes, label_cutoff_at):
    allowed = set(trip_keys)
    cutoff = parse_ts(label_cutoff_at)
    selected = select_fit_outcomes(outcomes, allowed, label_cutoff_at)
    rows, seen = [], set()
    for source in opportunities:
        key = source['tenant_id'], source['trip_id']
        sid = source['tenant_id'], source['snapshot_id']
        if key not in allowed or sid in seen:
            raise ValueError('ETA target opportunity outside declared groups or duplicate')
        seen.add(sid)
        at = parse_ts(source['snapshot_time'])
        if at >= cutoff:
            raise ValueError('ETA snapshot must precede label cutoff')
        record = {name: source[name] for name in ('tenant_id', 'trip_id', 'snapshot_id', 'snapshot_time')}
        record.update(label_cutoff_at=utc(cutoff), eligibility='excluded', reason='no_outcome_known_before_cutoff',
                      label_status=None, target_seconds=None, actual_arrival_time=None,
                      outcome_version_id=None, label_known_at=None, arrival_time_precision=None,
                      arrival_evidence_ref=None)
        outcome = selected.get(key)
        if outcome is not None:
            known = parse_ts(outcome['label_available_time'])
            status = outcome.get('label_status')
            if status not in OUTCOME_STATUSES:
                raise ValueError('Unknown ETA outcome label status')
            record.update(label_status=status, label_known_at=utc(known),
                          outcome_version_id=outcome['outcome_version_id'],
                          arrival_time_precision=outcome.get('arrival_time_precision'))
            if status != 'verified':
                record['reason'] = 'latest_outcome_' + status
            elif outcome.get('arrival_time_precision') not in ('second', 'minute'):
                record['reason'] = 'insufficient_arrival_precision'
            elif outcome.get('actual_arrival_time') is None:
                record['reason'] = 'missing_gate_arrival'
            elif not isinstance(outcome.get('arrival_evidence_ref'), str) or not outcome['arrival_evidence_ref'].strip():
                record['reason'] = 'missing_arrival_evidence'
            elif outcome.get('cancellation_time') is not None:
                record['reason'] = 'conflicting_cancellation_evidence'
            else:
                arrival = parse_ts(outcome['actual_arrival_time'])
                if arrival > known:
                    raise ValueError('Gate arrival occurs after its verification clock')
                record.update(actual_arrival_time=utc(arrival), arrival_evidence_ref=outcome['arrival_evidence_ref'])
                if arrival <= at:
                    record['reason'] = 'already_arrived_at_snapshot'
                else:
                    record.update(eligibility='eligible', reason='verified_positive_gate_remaining',
                                  target_seconds=(arrival-at).total_seconds())
        rows.append(record)
    return sorted(rows, key=lambda r:(r['tenant_id'], r['trip_id'], r['snapshot_time'], r['snapshot_id']))
