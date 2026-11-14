"""Final-cohort targets joined only after the label-free artifact freeze.

This module has no file reader. The caller controls the pre-target access gate
and supplies the complete frozen opportunity inventory. A cutoff-known revision
always wins, including a pending/cancelled/incomplete revision withdrawing an
older verified arrival; an older success is never used as a replacement.
"""
from collections.abc import Mapping

from .common import parse_ts, utc
from .delay_targets import OUTCOME_STATUSES, prepare_promise_features


LABEL_CUTOFF = '2026-10-15T07:00:00Z'
ETA_TARGET_VERSION = 'p08-eta-target-v1'
PROBABILITY_TARGET_VERSION = 'p08-delay-target-v1'
PRECISIONS = frozenset(('second', 'minute', 'date_only', 'interval', 'unknown'))


def _identifier(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(field + ' must be nonempty text')
    return value


def _key(row):
    if not isinstance(row, Mapping):
        raise ValueError('Target source records must be mappings')
    return tuple(_identifier(row.get(field), field) for field in ('tenant_id', 'trip_id'))


def _at(value, field):
    try:
        return parse_ts(value)
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(field + ' must be a timezone-aware timestamp') from exc


def _cutoff(settings):
    try:
        if (settings['protocol_version'] != 'p08-final-evaluation-v1'
                or settings['population']['label_cutoff_exclusive'] != LABEL_CUTOFF
                or settings['population']['timezone'] != 'America/Los_Angeles'):
            raise ValueError('Final target settings differ from the frozen P08 protocol')
    except (KeyError, TypeError) as exc:
        raise ValueError('Incomplete final target settings') from exc
    return parse_ts(LABEL_CUTOFF)


def _select_latest(outcomes, trip_keys, cutoff):
    selected, versions, clocks = {}, set(), set()
    for outcome in outcomes:
        key = _key(outcome)
        if key not in trip_keys:
            continue
        known = _at(outcome.get('label_available_time'), 'label_available_time')
        if known >= cutoff:
            continue
        version = _identifier(outcome.get('outcome_version_id'), 'outcome_version_id')
        if (key[0], version) in versions or (key, known) in clocks:
            raise ValueError('Duplicate final outcome version or ambiguous known time')
        versions.add((key[0], version))
        clocks.add((key, known))
        if key not in selected or known > selected[key][0]:
            selected[key] = known, outcome
    return selected


def _bind_promises(opportunities, promises, cutoff):
    rows, source_by_snapshot, trip_keys = [], {}, set()
    for source in opportunities:
        key = _key(source)
        sid = _identifier(source.get('snapshot_id'), 'snapshot_id')
        identity = key[0], sid
        if identity in source_by_snapshot:
            raise ValueError('Duplicate final opportunity identity')
        at = _at(source.get('snapshot_time'), 'snapshot_time')
        if at >= cutoff:
            raise ValueError('Final snapshot must strictly precede the label cutoff')
        source_by_snapshot[identity] = source
        trip_keys.add(key)
        rows.append(source)
    bound = prepare_promise_features(rows, trip_keys, promises)
    for promise in bound:
        source = source_by_snapshot[promise['tenant_id'], promise['snapshot_id']]
        for field in ('deadline_at', 'promise_known_at', 'promise_window_start_at', 'promise_window_end_at'):
            if field in source and _at(source[field], field) != _at(promise[field], 'bound ' + field):
                raise ValueError('Final opportunity promise clock differs from source: ' + field)
        if ('promise_provenance_id' in source
                and source['promise_provenance_id'] != promise['promise_provenance_id']):
            raise ValueError('Final opportunity promise provenance differs from source')
    return bound, trip_keys


def _base_record(promise, version, cutoff):
    record = {field: value for field, value in promise.items() if field not in ('status', 'reason')}
    record.update(target_definition_version=version, label_cutoff_at=utc(cutoff),
                  eligibility='excluded', reason='no_outcome_known_before_cutoff', label_status=None,
                  outcome_version_id=None, label_known_at=None, actual_arrival_time=None,
                  arrival_time_precision=None, arrival_evidence_ref=None, outcome_provenance_id=None)
    return record


def _arrival(record, selected, allowed_precision):
    """Return a verified independent gate clock or keep an explicit exclusion."""
    if selected is None:
        return None
    known, outcome = selected
    status = outcome.get('label_status')
    if status not in OUTCOME_STATUSES:
        raise ValueError('Unknown final outcome label status')
    record.update(label_status=status, outcome_version_id=outcome['outcome_version_id'],
                  label_known_at=utc(known), outcome_provenance_id=outcome.get('provenance_id'),
                  arrival_time_precision=outcome.get('arrival_time_precision'))
    if status != 'verified':
        record['reason'] = 'latest_outcome_' + status
        return None
    precision = outcome.get('arrival_time_precision')
    if precision is not None and precision not in PRECISIONS:
        raise ValueError('Unknown final arrival precision')
    if precision not in allowed_precision:
        record['reason'] = 'insufficient_arrival_precision'
    elif outcome.get('actual_arrival_time') is None:
        record['reason'] = 'missing_gate_arrival'
    elif not isinstance(outcome.get('arrival_evidence_ref'), str) or not outcome['arrival_evidence_ref'].strip():
        record['reason'] = 'missing_arrival_evidence'
    elif outcome.get('cancellation_time') is not None:
        record['reason'] = 'conflicting_cancellation_evidence'
    else:
        arrival = _at(outcome['actual_arrival_time'], 'actual_arrival_time')
        if arrival > known:
            raise ValueError('Final gate arrival occurs after its verification clock')
        record.update(actual_arrival_time=utc(arrival), arrival_evidence_ref=outcome['arrival_evidence_ref'])
        if arrival <= _at(record['snapshot_time'], 'snapshot_time'):
            record['reason'] = 'already_arrived_at_snapshot'
        else:
            return arrival
    return None


def build_final_targets(opportunities, outcomes, promises, settings):
    """Return separate ETA/binary labels for every supplied frozen opportunity.

    Required opportunity fields are tenant/trip/snapshot IDs, snapshot_time and
    promise_version_id. Supplied deadline/known/provenance bindings are checked
    even for excluded targets. ETA accepts second/minute gate precision; binary
    labels require second precision. All timestamps retain exact microseconds.
    """
    cutoff = _cutoff(settings)
    bound, trip_keys = _bind_promises(opportunities, promises, cutoff)
    selected = _select_latest(outcomes, trip_keys, cutoff)
    eta, probability = [], []
    for promise in bound:
        chosen = selected.get(_key(promise))
        duration = _base_record(promise, ETA_TARGET_VERSION, cutoff)
        duration.update(target_seconds=None)
        arrival = _arrival(duration, chosen, {'second', 'minute'})
        if arrival is not None:
            duration.update(eligibility='eligible', reason='verified_positive_gate_remaining',
                            target_seconds=(arrival - _at(promise['snapshot_time'], 'snapshot_time')).total_seconds())
        eta.append(duration)
        binary = _base_record(promise, PROBABILITY_TARGET_VERSION, cutoff)
        binary.update(delay_label=None, remaining_duration_seconds=None, delay_seconds=None)
        arrival = _arrival(binary, chosen, {'second'})
        if arrival is not None:
            deadline = _at(promise['deadline_at'], 'deadline_at')
            binary.update(eligibility='eligible', reason='verified_gate_arrival', delay_label=arrival > deadline,
                          remaining_duration_seconds=(arrival - _at(promise['snapshot_time'], 'snapshot_time')).total_seconds(),
                          delay_seconds=(arrival - deadline).total_seconds())
        probability.append(binary)
    return {'eta_targets': eta, 'probability_targets': probability}
