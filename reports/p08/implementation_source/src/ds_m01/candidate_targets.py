"""Cutoff-specific supervised targets, separated from prediction opportunities.

Only fold-fit trip outcomes enter this module. The latest version known strictly
before fit time determines eligibility, including a withdrawal of an earlier
verified label. Targets retain fractional seconds; no validation labels, promise
windows or generator truth are used. This is not an evaluation-label builder.
"""

from collections import Counter
from collections.abc import Mapping
import json
import math
from pathlib import Path

from .common import parse_ts, utc


TARGET_DEFINITION_VERSION = 'm3b2-fit-target-v1'


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be a nonempty string')
    return value


def _key(row):
    if not isinstance(row, Mapping):
        raise ValueError('Outcome and matrix records must be mappings')
    return tuple(_identifier(row.get(name), name) for name in ('tenant_id', 'trip_id'))


def _timestamp(value, name):
    try:
        return parse_ts(value)
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f'{name} must be timezone-aware') from exc


def select_fit_outcomes(rows, fit_trip_keys, training_cutoff_at):
    """Select only fit groups and known versions, without reading later payloads.

    Key and availability fields provide the use gate. Other fields belonging to
    non-fit groups or later versions are not inspected. Duplicate known version
    identifiers and equal known times are rejected rather than resolved through
    file order or arbitrary lexical order.
    """
    allowed = set(fit_trip_keys)
    cutoff = _timestamp(training_cutoff_at, 'training_cutoff_at')
    selected, seen_ids, seen_times = {}, set(), set()
    for row in rows:
        key = _key(row)
        if key not in allowed:
            continue
        known = _timestamp(row.get('label_available_time'), 'label_available_time')
        if known >= cutoff:
            continue
        version = _identifier(row.get('outcome_version_id'), 'outcome_version_id')
        identity = key[0], version
        time_key = key, known
        if identity in seen_ids:
            raise ValueError('Duplicate known outcome version in tenant')
        if time_key in seen_times:
            raise ValueError('Ambiguous simultaneous known outcome versions')
        seen_ids.add(identity)
        seen_times.add(time_key)
        if key not in selected or known > selected[key][0]:
            selected[key] = known, row
    return {key: row for key, (_, row) in selected.items()}


def load_fit_outcomes(path, fit_trip_keys, training_cutoff_at):
    """Stream a shared source through the fold-fit gate; bytes are still read."""
    def records():
        with Path(path).open(encoding='utf-8') as stream:
            for line in stream:
                if not line.strip():
                    raise ValueError('Blank outcome source record')
                yield json.loads(line)
    return select_fit_outcomes(records(), fit_trip_keys, training_cutoff_at)


def prepare_training_rows(matrix_rows, feature_rows, selected_outcomes, *,
                          fold_id, fit_trip_keys, training_cutoff_at):
    """Retain one eligibility record per fit opportunity and equal trip weights.

    Each eligible trip has total supervised weight one across its eligible
    snapshots. This differs deliberately from M3b.1's snapshot-weighted,
    unsupervised preprocessing. Missing GPS is not a target exclusion.
    """
    cutoff = _timestamp(training_cutoff_at, 'training_cutoff_at')
    allowed = set(fit_trip_keys)
    if not set(selected_outcomes) <= allowed:
        raise ValueError('Selected targets include a non-fit trip')
    features = {}
    for row in feature_rows:
        key = row.get('tenant_id'), row.get('snapshot_id')
        if key in features:
            raise ValueError('Duplicate feature snapshot key')
        features[key] = row
    output, seen = [], set()
    for row in matrix_rows:
        key = _key(row)
        sid = _identifier(row.get('snapshot_id'), 'snapshot_id')
        snapshot_key = key[0], sid
        if snapshot_key in seen:
            raise ValueError('Duplicate fit matrix snapshot')
        seen.add(snapshot_key)
        if key not in allowed or row.get('fold_id') != fold_id or row.get('fold_role') != 'fit':
            raise ValueError('Training matrix row is outside the declared fit groups')
        at = _timestamp(row.get('snapshot_time'), 'snapshot_time')
        if at >= cutoff:
            raise ValueError('Fit snapshot must be strictly before cutoff')
        feature = features.get(snapshot_key)
        if (feature is None or _key(feature) != key
                or _timestamp(feature.get('snapshot_time'), 'feature snapshot_time') != at):
            raise ValueError('Fit matrix and raw feature identities disagree')
        elapsed = feature.get('numeric', {}).get('elapsed_trip_seconds')
        if (isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
                or not math.isfinite(elapsed) or elapsed < 0):
            raise ValueError('Fit feature elapsed seconds must be finite and nonnegative')
        record = dict(
            tenant_id=key[0], trip_id=key[1], snapshot_id=sid,
            snapshot_time=utc(at), fold_id=fold_id,
            training_cutoff_at=utc(cutoff),
            target_definition_version=TARGET_DEFINITION_VERSION,
            eligibility='excluded', reason=None, outcome_version_id=None,
            label_known_at=None, actual_arrival_time=None,
            remaining_duration_seconds=None, sample_weight=0.0,
        )
        outcome = selected_outcomes.get(key)
        if row.get('status') != 'available' or row.get('vector') is None:
            record['reason'] = 'unsupported_feature_vector'
        elif outcome is None:
            record['reason'] = 'no_outcome_known_before_fit'
        else:
            if _key(outcome) != key:
                raise ValueError('Selected outcome tenant/trip does not match its key')
            known = _timestamp(outcome.get('label_available_time'), 'label_available_time')
            if known >= cutoff:
                raise ValueError('Selected outcome must be strictly known before cutoff')
            record.update(outcome_version_id=_identifier(outcome.get('outcome_version_id'), 'outcome_version_id'),
                          label_known_at=utc(known))
            status = outcome.get('label_status')
            if status not in ('verified', 'pending', 'cancelled', 'incomplete', 'lost', 'conflict'):
                raise ValueError('Unknown outcome label status')
            if status != 'verified':
                record['reason'] = 'latest_outcome_not_verified'
            elif outcome.get('arrival_time_precision') not in ('second', 'minute'):
                record['reason'] = 'insufficient_arrival_precision'
            elif outcome.get('actual_arrival_time') is None:
                raise ValueError('Verified precise outcome requires an arrival time')
            else:
                arrival = _timestamp(outcome['actual_arrival_time'], 'actual_arrival_time')
                if arrival > known:
                    raise ValueError('Verified arrival cannot occur after it was known')
                if arrival >= cutoff:
                    raise ValueError('Selected arrival must be strictly before fit cutoff')
                record['actual_arrival_time'] = utc(arrival)
                remaining = (arrival - at).total_seconds()
                total = remaining + elapsed
                if total <= 0:
                    record['reason'] = 'nonpositive_trip_duration'
                elif remaining <= 0:
                    record['reason'] = 'already_arrived_at_snapshot'
                else:
                    record.update(eligibility='eligible', reason='verified_positive_remaining_duration',
                                  remaining_duration_seconds=remaining)
        output.append(record)
    counts = Counter((row['tenant_id'], row['trip_id']) for row in output if row['eligibility'] == 'eligible')
    for row in output:
        if row['eligibility'] == 'eligible':
            row['sample_weight'] = 1.0 / counts[(row['tenant_id'], row['trip_id'])]
    return sorted(output, key=lambda row: (row['tenant_id'], row['trip_id'], row['snapshot_time'], row['snapshot_id']))
