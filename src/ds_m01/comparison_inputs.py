"""Bind recorded forecasts to one training-period validation cohort and target."""

from collections import Counter
from datetime import timedelta
import hashlib
import io
import json
import math
from pathlib import Path

from .common import parse_ts, utc
from .model_splits import validate_folds


BASELINE = 'historical_baseline'
CANDIDATES = ('linear_quantile', 'gradient_boosting_quantile')
OUTCOME_SOURCE = 'data/raw/eta_trip_outcome.jsonl'
# This receipt binds the candidate predictions, M2/M3a settings, both predecessor
# receipts and original outcome source. Those receipts bind their own outputs.
M3B2_RECEIPT_SHA256 = '88896cbba603c44a253278d5fa2224a28ce60b9c5df299c19d69c05d335c372f'
TRIP = ('tenant_id', 'trip_id')
OPPORTUNITY = ('fold_id', *TRIP, 'snapshot_id')
QUANTILES = ('p10_remaining_seconds', 'p50_remaining_seconds', 'p90_remaining_seconds')
ETA_FIELDS = ('eta_lower', 'eta_median', 'eta_upper')


def _key(row, fields):
    key = tuple(row.get(field) for field in fields)
    if any(not isinstance(value, str) or not value.strip() for value in key):
        raise ValueError(f'Missing identifier for {fields}')
    return key


def _index(rows, fields):
    out = {}
    for row in rows:
        key = _key(row, fields)
        if key in out:
            raise ValueError(f'Duplicate {fields}: {key}')
        out[key] = row
    return out


def _validation_members(memberships, folds):
    validation = {}
    for (fid, tenant, trip), member in _index(memberships, ('fold_id', *TRIP)).items():
        role = member['fold_role']
        if fid not in folds or member['original_split_role'] != 'train' or role not in ('validation', 'fit'):
            raise ValueError('Membership outside original training folds')
        fold = folds[fid]
        fit = role == 'fit'
        start, end = fold['train_departure_dates' if fit else 'validation_departure_dates']
        cutoff = fold['training_cutoff_at' if fit else 'validation_label_cutoff_at']
        if (not start <= member['departure_date'] <= end or member['label_cutoff_at'] != cutoff
                or member['training_cutoff_at'] != fold['training_cutoff_at']):
            raise ValueError('Membership dates or clocks disagree with frozen fold')
        if not fit:
            if (tenant, trip) in validation:
                raise ValueError('Trip occurs in more than one validation fold')
            validation[tenant, trip] = fold
    return validation


def _select_validation_outcomes(rows, validation):
    """Inspect only keys outside the cohort and only clocks on late versions."""
    cutoffs = {key: parse_ts(fold['validation_label_cutoff_at']) for key, fold in validation.items()}
    selected, version_ids, known_times = {}, set(), set()
    for row in rows:
        key = _key(row, TRIP)
        if key not in cutoffs:
            continue
        known = parse_ts(row['label_available_time'])
        if known >= cutoffs[key]:
            continue
        version = (key[0], *_key(row, ('outcome_version_id',)))
        if version in version_ids or (key, known) in known_times:
            raise ValueError('Duplicate known version or ambiguous outcome clock')
        version_ids.add(version)
        known_times.add((key, known))
        if key not in selected or known > selected[key][0]:
            selected[key] = (known, row)
    return {key: row for key, (_, row) in selected.items()}


def _validation_label(reference, outcome, cutoff):
    """Reconstruct the as-of gate arrival; independently reconcile M3a's whole seconds.

    M3a's retained eligibility also checked promise existence. If that check
    would change this cohort, reconciliation fails; no promise supplies a target.
    """
    if reference['evaluation_label_cutoff_at'] != cutoff:
        raise ValueError('M3a label cutoff differs from the validation cutoff')
    status, seconds, version = 'not_mature', None, None
    if outcome is not None:
        if _key(reference, TRIP) != _key(outcome, TRIP):
            raise ValueError('Outcome and opportunity trip identities differ')
        known = parse_ts(outcome['label_available_time'])
        if known >= parse_ts(cutoff):
            raise ValueError('Validation outcome was not known before the cutoff')
        version = outcome['outcome_version_id']
        state = outcome['label_status']
        if state in ('cancelled', 'lost', 'conflict'):
            status = state
        elif state == 'verified':
            if outcome.get('arrival_time_precision') not in ('second', 'minute'):
                status = 'insufficient_precision'
            elif not outcome.get('actual_arrival_time'):
                raise ValueError('Verified, precise outcome requires an arrival timestamp')
            else:
                arrival = parse_ts(outcome['actual_arrival_time'])
                if arrival > known:
                    raise ValueError('Arrival occurs after its verification clock')
                remaining = (arrival - parse_ts(reference['snapshot_time'])).total_seconds()
                if remaining <= 0:
                    status = 'already_arrived'
                else:
                    status, seconds = 'eligible', remaining
        elif state not in ('pending', 'incomplete'):
            raise ValueError('Unknown validation outcome status')
    # M3a tested int(remaining) > 0. For a positive subsecond target, its
    # retained status should be already_arrived even though our protocol scores it.
    legacy_status = 'already_arrived' if seconds is not None and seconds < 1 else status
    legacy_seconds = int(seconds) if legacy_status == 'eligible' else None
    if ((version, legacy_status, legacy_seconds) !=
            (reference['outcome_version_id'], reference['label_eligibility'], reference['remaining_duration_seconds'])):
        raise ValueError('As-of outcome version, status or whole seconds disagree with M3a')
    return status, seconds


def _forecast(row, source):
    values = [row[field] for field in QUANTILES]
    eta = [row[field] for field in ETA_FIELDS]
    if source == 'unavailable':
        if any(x is not None for x in values + eta):
            raise ValueError('Unavailable forecast has non-null estimates')
        values = None
    else:
        if (not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0 for x in values)
                or values != sorted(values)):
            raise ValueError('Forecast quantiles must be positive, finite and ordered')
        at = parse_ts(row['snapshot_time'])
        if [utc(at + timedelta(seconds=x)) for x in values] != eta:
            raise ValueError('Forecast durations and ETA timestamps disagree')
    return {'source': source, 'quantiles': values}


def _candidate_forecast(row, baseline):
    contract = {('candidate', 'available', 'available'): 'native',
                ('baseline_fallback', 'unsupported', 'available'): 'baseline_fallback',
                ('unavailable', 'unsupported', 'unavailable'): 'unavailable'}
    source = contract.get((row['prediction_source'], row['candidate_status'], row['status']))
    if source is None:
        raise ValueError('Candidate availability and declared delivery source disagree')
    entry = _forecast(row, source)
    if source != 'native' and entry['quantiles'] != baseline['quantiles']:
        raise ValueError('Candidate fallback/unavailability differs from retained baseline')
    return entry


def _prepare_rows(memberships, features, baseline_rows, reference_rows, candidate_rows, folds, outcomes):
    validation = _validation_members(memberships, folds)
    expected = {}
    for feature in _index(features, ('tenant_id', 'snapshot_id')).values():
        trip = _key(feature, TRIP)
        if trip in validation:
            fold = validation[trip]
            if not parse_ts(fold['training_cutoff_at']) <= parse_ts(feature['snapshot_time']) < parse_ts(fold['validation_label_cutoff_at']):
                raise ValueError('Snapshot clock outside its training-period validation window')
            expected[(fold['fold_id'], *trip, feature['snapshot_id'])] = feature
    if set(validation) != {key[1:3] for key in expected}:
        raise ValueError('Validation trip without a retained snapshot')
    baseline = _index(baseline_rows, OPPORTUNITY)
    references = _index(reference_rows, OPPORTUNITY)
    candidates = _index(candidate_rows, (*OPPORTUNITY, 'candidate_id'))
    if (expected.keys() != baseline.keys() or expected.keys() != references.keys()
            or candidates.keys() != {(*key, method) for key in expected for method in CANDIDATES}):
        raise ValueError('Missing or extra validation opportunity in retained forecasts/evaluation rows')
    selected = _select_validation_outcomes(outcomes, validation)
    rows = []
    for key, feature in sorted(expected.items()):
        fold = folds[key[0]]
        b, ref = baseline[key], references[key]
        if (any(ref[name] != value for name, value in b.items()) or b['snapshot_time'] != feature['snapshot_time']
                or b['training_cutoff_at'] != fold['training_cutoff_at'] or b['route_id'] != feature['categorical']['route_id']
                or b['departure_time_bucket'] != feature['categorical']['departure_time_bucket']
                or ref['position_validity'] != feature['categorical']['position_validity']):
            raise ValueError('M3a forecast, label reference and M3b1 feature metadata disagree')
        if b['status'] not in ('available', 'unsupported'):
            raise ValueError('Unknown baseline status')
        baseline_forecast = _forecast(b, 'native' if b['status'] == 'available' else 'unavailable')
        forecasts = {BASELINE: baseline_forecast}
        for method in CANDIDATES:
            candidate = candidates[(*key, method)]
            if any(candidate[name] != ref[name] for name in ('training_cutoff_at', 'snapshot_time', 'route_id', 'position_validity')):
                raise ValueError('Candidate and baseline opportunity metadata disagree')
            forecasts[method] = _candidate_forecast(candidate, baseline_forecast)
        cutoff = fold['validation_label_cutoff_at']
        status, target = _validation_label(ref, selected.get(key[1:3]), cutoff)
        rows.append({**dict(zip(OPPORTUNITY, key)), 'snapshot_time': b['snapshot_time'], 'evaluation_label_cutoff_at': cutoff,
                     'outcome_version_id': ref['outcome_version_id'], 'route_support': b['pool'] or 'unsupported',
                     'route_id': b['route_id'], 'departure_time_bucket': b['departure_time_bucket'],
                     'elapsed_slice': ref['elapsed_slice'], 'position_validity': ref['position_validity'],
                     'eligible': status == 'eligible', 'label_status': status, 'target_seconds': target, 'predictions': forecasts})
    return rows


def load_comparison_inputs(root, protocol, *, outcome_path=None):
    """Validate exact source bytes; only fold-validation outcomes enter evaluation.

    The shared raw JSONL still contains protected periods. Its bytes are hashed and
    decoded; their outcome payloads never enter selection or scoring.
    """
    root = Path(root)
    hashes = {}

    def load(path, digest, *, jsonl=False, stream=False):
        location = Path(outcome_path) if path == OUTCOME_SOURCE and outcome_path is not None else root / path
        data = location.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            raise ValueError(f'Frozen comparison input hash mismatch: {path}')
        hashes[path] = actual
        if stream or jsonl:
            lines = (json.loads(line) for line in io.BytesIO(data))
            return lines if stream else list(lines)
        return json.loads(data)

    b2 = load('reports/m3b2/run_receipt.json', M3B2_RECEIPT_SHA256)
    pins = b2['input_sha256']
    a = load('reports/m3a/run_receipt.json', pins['reports/m3a/run_receipt.json'])
    b1 = load('reports/m3b1/run_receipt.json', pins['reports/m3b1/run_receipt.json'])
    m2 = load('config/m2.json', pins['config/m2.json'])
    m3a = load('config/m3a.json', pins['config/m3a.json'])
    m3b2 = load('config/m3b2.json', b2['settings_sha256'])
    folds = validate_folds(m3a['folds'], m2)
    if (folds != validate_folds(m3b2['folds'], m2) or protocol['folds'] != [f['fold_id'] for f in folds]
            or list(CANDIDATES) != m3b2['candidate_ids'] or protocol['methods'] != [BASELINE, *CANDIDATES]):
        raise ValueError('Comparison methods or folds differ from frozen experiment')
    if (a['input_sha256'][OUTCOME_SOURCE] != pins[OUTCOME_SOURCE] or m3b2['expected_outcome_sha256'] != pins[OUTCOME_SOURCE]
            or a['output_sha256']['memberships.jsonl'] != b1['output_sha256']['memberships.jsonl']):
        raise ValueError('Prior stages disagree on memberships or frozen outcomes')

    def artifact(stage, receipt, name):
        return load('reports/' + stage + '/' + name, receipt['output_sha256'][name], jsonl=True)

    memberships = artifact('m3a', a, 'memberships.jsonl')
    if artifact('m3b1', b1, 'memberships.jsonl') != memberships:
        raise ValueError('M3a and M3b1 membership rows disagree')
    rows = _prepare_rows(memberships, artifact('m3b1', b1, 'features.jsonl'), artifact('m3a', a, 'predictions.jsonl'),
                         artifact('m3a', a, 'evaluation_rows.jsonl'), artifact('m3b2', b2, 'predictions.jsonl'),
                         {f['fold_id']: f for f in folds}, load(OUTCOME_SOURCE, pins[OUTCOME_SOURCE], stream=True))
    if len(rows) != protocol['expected_opportunities_per_method']:
        raise ValueError('Unexpected number of validation opportunities')
    return rows, {'input_sha256': hashes, 'opportunities': len(rows), 'validation_trips': len({_key(r, TRIP) for r in rows}),
                  'label_status_counts': dict(sorted(Counter(r['label_status'] for r in rows).items())),
                  'eligible_fractional_second_targets': sum(r['eligible'] and not r['target_seconds'].is_integer() for r in rows),
                  'outcome_use': 'Only frozen original-training validation groups and versions known strictly before each fold cutoff; protected source bytes hashed, no protected targets used.',
                  'm3a_reference': 'Outcome version, eligibility and integer target reconciled per opportunity; all methods score the same fractional seconds.'}
