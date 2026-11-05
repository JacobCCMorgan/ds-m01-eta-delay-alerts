"""Snapshot-promise delay labels with independent prediction and label clocks.

The v1 contract is actual gate arrival > the snapshot's applicable promise end,
with zero grace. Only second-precision verified arrivals receive binary labels;
minute/interval/date-only observations have no defined binary boundary here.
Promise features do not open outcomes. Neither service time nor POD substitutes
for actual_arrival_time and its independent arrival_evidence_ref.
"""

from collections.abc import Mapping

from .common import parse_ts, utc


TARGET_DEFINITION_VERSION = 'm3c-delay-target-v1'
GRACE_SECONDS = 0
OUTCOME_STATUSES = frozenset(('verified', 'pending', 'cancelled', 'incomplete', 'lost', 'conflict'))


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be a nonempty string')
    return value


def _key(row):
    if not isinstance(row, Mapping):
        raise ValueError('Records must be mappings')
    return tuple(_identifier(row.get(name), name) for name in ('tenant_id', 'trip_id'))


def _timestamp(value, name):
    try:
        return parse_ts(value)
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f'{name} must be a timezone-aware timestamp') from exc


def _opportunities(rows, trip_keys):
    allowed, seen, out = set(trip_keys), set(), []
    for row in rows:
        key = _key(row)
        if key not in allowed:
            raise ValueError('Opportunity is outside the declared trip groups')
        sid = _identifier(row.get('snapshot_id'), 'snapshot_id')
        if (key[0], sid) in seen:
            raise ValueError('Duplicate snapshot identity in tenant')
        seen.add((key[0], sid))
        _identifier(row.get('promise_version_id'), 'promise_version_id')
        at = _timestamp(row.get('snapshot_time'), 'snapshot_time')
        out.append((key, at, row))
    return sorted(out, key=lambda item: (*item[0], item[1], item[2]['snapshot_id']))


def prepare_promise_features(opportunities, trip_keys, promises):
    """Bind a label-free deadline to every supplied snapshot opportunity.

    The source promise is the latest version known at or before the snapshot,
    not the latest revision at extraction time. Its window describes the
    promised arrival time, not an interval in which the version becomes known.
    Foreign groups and later-version payloads are not inspected. Ambiguous,
    missing or contradictory selected versions fail instead of losing a row.
    """
    rows = _opportunities(opportunities, trip_keys)
    last_snapshot = {}
    for key, at, _ in rows:
        last_snapshot[key] = max(last_snapshot.get(key, at), at)
    by_trip, ids, clocks = {}, set(), set()
    for promise in promises:
        key = _key(promise)
        if key not in last_snapshot:
            continue
        known = _timestamp(promise.get('known_at'), 'promise.known_at')
        if known > last_snapshot[key]:
            continue
        version = _identifier(promise.get('promise_version_id'), 'promise.promise_version_id')
        if (key[0], version) in ids or (key, known) in clocks:
            raise ValueError('Duplicate promise version or ambiguous known time')
        ids.add((key[0], version))
        clocks.add((key, known))
        by_trip.setdefault(key, []).append((known, promise))
    output = []
    for key, at, row in rows:
        visible = [(known, p) for known, p in by_trip.get(key, ()) if known <= at]
        if not visible:
            raise ValueError('No promise was known at snapshot time')
        known, promise = max(visible, key=lambda item: item[0])
        if row['promise_version_id'] != promise['promise_version_id']:
            raise ValueError('Snapshot promise is not the latest version known at its clock')
        start = _timestamp(promise.get('window_start_at'), 'promise.window_start_at')
        end = _timestamp(promise.get('window_end_at'), 'promise.window_end_at')
        if start > end:
            raise ValueError('Promise window start exceeds its end')
        record = dict(tenant_id=key[0], trip_id=key[1], snapshot_id=row['snapshot_id'],
                      snapshot_time=utc(at), promise_version_id=row['promise_version_id'],
                      promise_known_at=utc(known), promise_window_start_at=utc(start),
                      promise_window_end_at=utc(end), deadline_at=utc(end), grace_seconds=GRACE_SECONDS,
                      promise_seconds_remaining=(end - at).total_seconds(),
                      promise_provenance_id=promise.get('provenance_id'), status='available', reason=None)
        for field in ('fold_id', 'fold_role'):
            if field in row:
                record[field] = row[field]
        output.append(record)
    return output


def _select_outcomes(outcomes, trip_keys, cutoff):
    selected, ids, clocks = {}, set(), set()
    for outcome in outcomes:
        key = _key(outcome)
        if key not in trip_keys:
            continue
        known = _timestamp(outcome.get('label_available_time'), 'label_available_time')
        if known >= cutoff:
            continue
        version = _identifier(outcome.get('outcome_version_id'), 'outcome_version_id')
        if (key[0], version) in ids or (key, known) in clocks:
            raise ValueError('Duplicate outcome version or ambiguous known time')
        ids.add((key[0], version))
        clocks.add((key, known))
        if key not in selected or known > selected[key][0]:
            selected[key] = known, outcome
    return selected


def build_delay_targets(opportunities, trip_keys, outcomes, promises, label_cutoff_at):
    """Keep all opportunities and attach an eligible binary label or a reason.

    Fit callers use the training cutoff; evaluation callers use their declared
    label cutoff. Only the latest outcome strictly known before that cutoff
    controls a label. A withdrawn verified version never falls back to an older
    success. Vector support and per-trip training weights belong to the caller.
    """
    allowed = set(trip_keys)
    rows = prepare_promise_features(opportunities, allowed, promises)
    cutoff = _timestamp(label_cutoff_at, 'label_cutoff_at')
    if any(_timestamp(row['snapshot_time'], 'snapshot_time') >= cutoff for row in rows):
        raise ValueError('Snapshot must be strictly before the label cutoff')
    selected = _select_outcomes(outcomes, {_key(row) for row in rows}, cutoff)
    output = []
    for promise in rows:
        record = {key: value for key, value in promise.items() if key not in ('status', 'reason')}
        record.update(target_definition_version=TARGET_DEFINITION_VERSION, label_cutoff_at=utc(cutoff),
                      eligibility='excluded', reason='no_outcome_known_before_cutoff', delay_label=None,
                      label_status=None, outcome_version_id=None, label_known_at=None,
                      actual_arrival_time=None, arrival_time_precision=None, arrival_evidence_ref=None,
                      outcome_provenance_id=None, remaining_duration_seconds=None, delay_seconds=None)
        chosen = selected.get(_key(record))
        if chosen is not None:
            known, outcome = chosen
            state = outcome.get('label_status')
            if state not in OUTCOME_STATUSES:
                raise ValueError('Unknown outcome label status')
            record.update(outcome_version_id=outcome['outcome_version_id'], label_known_at=utc(known),
                          label_status=state, outcome_provenance_id=outcome.get('provenance_id'),
                          arrival_time_precision=outcome.get('arrival_time_precision'))
            if state != 'verified':
                record['reason'] = 'latest_outcome_' + state
            elif outcome.get('arrival_time_precision') != 'second':
                record['reason'] = 'insufficient_arrival_precision'
            elif outcome.get('actual_arrival_time') is None:
                record['reason'] = 'missing_gate_arrival'
            elif not isinstance(outcome.get('arrival_evidence_ref'), str) or not outcome['arrival_evidence_ref'].strip():
                record['reason'] = 'missing_arrival_evidence'
            elif outcome.get('cancellation_time') is not None:
                record['reason'] = 'conflicting_cancellation_evidence'
            else:
                arrival = _timestamp(outcome['actual_arrival_time'], 'actual_arrival_time')
                if arrival > known:
                    raise ValueError('Arrival occurred after its verification clock')
                record.update(actual_arrival_time=utc(arrival), arrival_evidence_ref=outcome['arrival_evidence_ref'])
                at = _timestamp(record['snapshot_time'], 'snapshot_time')
                if arrival <= at:
                    record['reason'] = 'already_arrived_at_snapshot'
                else:
                    deadline = _timestamp(record['deadline_at'], 'deadline_at')
                    record.update(eligibility='eligible', reason='verified_gate_arrival', delay_label=arrival > deadline,
                                  remaining_duration_seconds=(arrival - at).total_seconds(),
                                  delay_seconds=(arrival - deadline).total_seconds())
        output.append(record)
    return output
