"""Frozen tenant-local interval expansion and monotone sigmoid calibration.

Trip-max, nonnegative CQR scores use a finite-sample order statistic. This
protocol makes no coverage guarantee under the synthetic temporal shift.
Probability fitting minimizes a trip-balanced SUM of cross-entropies plus
0.5*((a-1)**2+b**2), with a >= 0. Identity fallbacks remain uncalibrated.
"""

from collections import Counter, defaultdict
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import math
from zoneinfo import ZoneInfo

from .common import parse_ts, utc


COVERAGE = 0.8
MINIMUM_TRIPS = 10
MINIMUM_CLASS_TRIPS = 2
PROBABILITY_CLIP = 1e-6
OPTIMIZER_OPTIONS = {'maxiter': 2000, 'ftol': 1e-12, 'gtol': 1e-9, 'maxls': 50}
FIT_START = '2026-07-01T07:00:00Z'
FIT_CUTOFF = '2026-07-15T07:00:00Z'
ZONE = ZoneInfo('America/Los_Angeles')
VERSIONS = {'interval': 'm3d-trip-max-cqr-v1', 'probability': 'm3d-sigmoid-v1'}


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be a nonempty string')
    return value


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be finite numeric')
    return float(value)


def _timestamp(value, name):
    try:
        return parse_ts(value)
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f'{name} must be timezone-aware') from exc


def _count(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f'{name} must be a nonnegative integer')
    return value


def _pairs(values, tenant, name):
    if not isinstance(values, (list, tuple)):
        raise ValueError(f'{name} must contain tenant/key pairs')
    pairs = []
    for pair in values:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError(f'{name} must contain tenant/key pairs')
        a, b = [_identifier(v, name) for v in pair]
        if a != tenant:
            raise ValueError(f'{name} crosses tenant boundaries')
        pairs.append((a, b))
    if len(set(pairs)) != len(pairs):
        raise ValueError(f'Duplicate {name}')
    return sorted(pairs)


def _sha(value, name):
    if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError(f'{name} must be a SHA256 hex digest')
    return value


def calibrator_digest(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _metadata(metadata, tenant):
    if not isinstance(metadata, Mapping):
        raise ValueError('Fit metadata must be a mapping')
    expected = {'snapshot_start_at': FIT_START, 'snapshot_end_at': FIT_CUTOFF, 'label_cutoff_at': FIT_CUTOFF}
    if set(metadata) != set(expected) | {'fit_trip_keys'}:
        raise ValueError('Fit metadata must declare clocks and complete fit-role trip keys')
    for name, value in expected.items():
        if _timestamp(metadata[name], name) != parse_ts(value):
            raise ValueError('Fit clocks differ from the fixed July calibration protocol')
    return {**expected, 'fit_trip_keys': [list(k) for k in _pairs(metadata['fit_trip_keys'], tenant, 'fit_trip_keys')]}


def _interval(row):
    lower = _number(row.get('lower_seconds'), 'lower_seconds')
    upper = _number(row.get('upper_seconds'), 'upper_seconds')
    if not 0 <= lower <= upper:
        raise ValueError('Raw interval must be nonnegative and ordered')
    return lower, upper


def _probability(value):
    p = _number(value, 'probability')
    if not 0 <= p <= 1:
        raise ValueError('Probability must be in [0,1]')
    return p


def _fit_rows(rows, tenant, metadata, kind):
    declared = {tuple(k) for k in metadata['fit_trip_keys']}
    output, seen = [], set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError('Fit rows must be mappings')
        trip = (_identifier(row.get('tenant_id'), 'tenant_id'), _identifier(row.get('trip_id'), 'trip_id'))
        sid = _identifier(row.get('snapshot_id'), 'snapshot_id')
        if trip not in declared or trip[0] != tenant:
            raise ValueError('Fit row is outside the declared tenant/trip membership')
        if (tenant, sid) in seen:
            raise ValueError('Duplicate calibration fit snapshot')
        seen.add((tenant, sid))
        if row.get('calibration_role') != 'calibration_fit':
            raise ValueError('Only calibration_fit rows may fit a calibrator')
        if row.get('status', 'available') != 'available' or row.get('eligibility', 'eligible') != 'eligible':
            raise ValueError('Calibration fit requires an eligible available forecast')
        depart = _timestamp(row.get('departure_time'), 'departure_time')
        at = _timestamp(row.get('snapshot_time'), 'snapshot_time')
        known = _timestamp(row.get('label_known_at'), 'label_known_at')
        if not '2026-07-01' <= depart.astimezone(ZONE).date().isoformat() <= '2026-07-10':
            raise ValueError('Fit departure must belong to July 1-10')
        if not parse_ts(FIT_START) <= depart <= at < known < parse_ts(FIT_CUTOFF):
            raise ValueError('Fit event/availability clocks cross the calibration boundary')
        item = dict(tenant_id=tenant, trip_id=trip[1], snapshot_id=sid,
                    departure_time=utc(depart), snapshot_time=utc(at), label_known_at=utc(known))
        if kind == 'interval':
            lower, upper = _interval(row)
            y = _number(row.get('target_seconds'), 'target_seconds')
            if y <= 0:
                raise ValueError('Remaining-duration target must be positive')
            if y > (known-at).total_seconds():
                raise ValueError('Arrival target occurs after its label became known')
            item.update(lower_seconds=lower, upper_seconds=upper, target_seconds=y)
        else:
            if type(row.get('delay_label')) is not bool:
                raise ValueError('Delay target must be Boolean')
            item.update(probability=_probability(row.get('probability')), delay_label=row['delay_label'])
        output.append(item)
    return sorted(output, key=lambda r: (r['tenant_id'], r['trip_id'], r['snapshot_time'], r['snapshot_id']))


def _base_state(rows, tenant_id, fit_metadata, source_bindings, kind):
    tenant = _identifier(tenant_id, 'tenant_id')
    metadata = _metadata(fit_metadata, tenant)
    bindings = {} if source_bindings is None else dict(source_bindings)
    for name, digest in bindings.items():
        _identifier(name, 'source binding name')
        _sha(digest, 'source binding digest')
    clean = _fit_rows(rows, tenant, metadata, kind)
    trips = sorted({(tenant, row['trip_id']) for row in clean})
    state = dict(definition_version=VERSIONS[kind], kind=kind, tenant_id=tenant, fit_metadata=metadata,
                 source_bindings=dict(sorted(bindings.items())), minimum_fit_trips=MINIMUM_TRIPS,
                 fit_snapshot_count=len(clean), fit_trip_count=len(trips),
                 fitted_trip_keys=[list(k) for k in trips],
                 fitted_snapshot_keys=[list(k) for k in sorted((tenant, r['snapshot_id']) for r in clean)],
                 fit_rows_sha256=calibrator_digest(clean), status='identity', reason='insufficient_fit_trips')
    return clean, state


def _validate_base(state, kind):
    if not isinstance(state, Mapping) or state.get('definition_version') != VERSIONS[kind] or state.get('kind') != kind:
        raise ValueError('Unsupported calibrator kind or version')
    tenant = _identifier(state.get('tenant_id'), 'tenant_id')
    metadata = _metadata(state.get('fit_metadata'), tenant)
    if state['fit_metadata'] != metadata or state.get('minimum_fit_trips') != MINIMUM_TRIPS:
        raise ValueError('Calibrator metadata or support policy changed')
    trips = _pairs(state.get('fitted_trip_keys'), tenant, 'fitted_trip_keys')
    snaps = _pairs(state.get('fitted_snapshot_keys'), tenant, 'fitted_snapshot_keys')
    if not set(trips) <= {tuple(k) for k in metadata['fit_trip_keys']}:
        raise ValueError('Calibrator fitted trips were outside declared fit roles')
    n = _count(state.get('fit_trip_count'), 'fit_trip_count')
    size = _count(state.get('fit_snapshot_count'), 'fit_snapshot_count')
    if n != len(trips) or size != len(snaps) or n > size or bool(n) != bool(size):
        raise ValueError('Calibrator fit counts and identities disagree')
    _sha(state.get('fit_rows_sha256'), 'fit_rows_sha256')
    if not isinstance(state.get('source_bindings'), Mapping):
        raise ValueError('Source bindings must be a mapping')
    for name, value in state['source_bindings'].items():
        _identifier(name, 'source binding name')
        _sha(value, 'source binding digest')
    return n, size, trips


def fit_interval_calibrator(rows, *, tenant_id, fit_metadata, source_bindings=None):
    clean, state = _base_state(rows, tenant_id, fit_metadata, source_bindings, 'interval')
    grouped = defaultdict(list)
    for row in clean:
        grouped[row['trip_id']].append(max(row['lower_seconds']-row['target_seconds'],
                                           row['target_seconds']-row['upper_seconds'], 0.0))
    trip_scores = [dict(tenant_id=tenant_id, trip_id=trip, snapshot_count=len(scores), max_score_seconds=max(scores))
                   for trip, scores in sorted(grouped.items())]
    ordered = sorted(row['max_score_seconds'] for row in trip_scores)
    n = len(ordered)
    # Exact integer arithmetic for ceil((n+1)*4/5); no interpolated quantile.
    rank = (4*(n+1)+4)//5 if n else None
    state.update(coverage_target=COVERAGE, trip_scores=trip_scores, ordered_trip_scores_seconds=ordered,
                 quantile_rank=rank, expansion_seconds=0.0,
                 coverage_scope='No coverage guarantee under temporal synthetic shift.')
    if n >= MINIMUM_TRIPS:
        state.update(status='fitted', reason=None, expansion_seconds=ordered[rank-1])
    validate_interval_calibrator(state)
    return state


def validate_interval_calibrator(state):
    n, size, trips = _validate_base(state, 'interval')
    if state.get('coverage_target') != COVERAGE:
        raise ValueError('Interval coverage target changed')
    scores = state.get('trip_scores')
    if not isinstance(scores, list) or len(scores) != n:
        raise ValueError('Trip-score count differs from fit trips')
    values, keys, counts = [], [], []
    for row in scores:
        keys.append((row.get('tenant_id'), row.get('trip_id')))
        count = _count(row.get('snapshot_count'), 'trip snapshot count')
        score = _number(row.get('max_score_seconds'), 'trip max score')
        if count == 0 or score < 0:
            raise ValueError('Trip score must be nonnegative with positive support')
        counts.append(count)
        values.append(score)
    if keys != trips or sum(counts) != size or state.get('ordered_trip_scores_seconds') != sorted(values):
        raise ValueError('Trip scores, ordering or snapshot counts disagree')
    rank = (4*(n+1)+4)//5 if n else None
    if state.get('quantile_rank') != rank:
        raise ValueError('Finite-sample rank differs from the fixed order statistic')
    fitted = n >= MINIMUM_TRIPS
    if (state.get('status'), state.get('reason')) != ('fitted' if fitted else 'identity', None if fitted else 'insufficient_fit_trips'):
        raise ValueError('Interval calibration support status is inconsistent')
    expansion = _number(state.get('expansion_seconds'), 'interval expansion')
    expected = sorted(values)[rank-1] if fitted else 0.0
    if expansion != expected:
        raise ValueError('Interval expansion differs from selected trip score')
    return state


def _sigmoid(value):
    if value >= 0:
        return 1.0/(1.0+math.exp(-value))
    e = math.exp(value)
    return e/(1.0+e)


def fit_probability_calibrator(rows, *, tenant_id, fit_metadata, source_bindings=None):
    clean, state = _base_state(rows, tenant_id, fit_metadata, source_bindings, 'probability')
    counts = Counter(row['trip_id'] for row in clean)
    weights = [1.0/counts[row['trip_id']] for row in clean]
    support = {str(label).lower(): {'snapshots': sum(row['delay_label'] == label for row in clean),
                                   'trips': len({row['trip_id'] for row in clean if row['delay_label'] == label})}
               for label in (False, True)}
    state.update(probability_clip=PROBABILITY_CLIP, minimum_class_contributing_trips=MINIMUM_CLASS_TRIPS,
                 class_support=support, a=1.0, b=0.0, optimizer=None,
                 objective='sum_trip_balanced_cross_entropy_plus_half_squared_distance_from_identity',
                 objective_value=None, weight_sum=float(len(counts)),
                 clipped_fit_snapshots=sum(r['probability'] < PROBABILITY_CLIP or r['probability'] > 1-PROBABILITY_CLIP for r in clean))
    if len(counts) >= MINIMUM_TRIPS:
        if min(s['trips'] for s in support.values()) < MINIMUM_CLASS_TRIPS:
            state['reason'] = 'insufficient_class_trip_support'
        else:
            import numpy as np
            from scipy.optimize import minimize
            from scipy.special import expit
            x = np.array([math.log(p/(1-p)) for p in
                          (min(1-PROBABILITY_CLIP, max(PROBABILITY_CLIP, r['probability'])) for r in clean)])
            y = np.array([int(r['delay_label']) for r in clean], dtype=float)
            w = np.array(weights)
            def objective(theta):
                a, b = theta
                z = a*x+b
                loss = float(np.sum(w*(np.logaddexp(0, z)-y*z)) + .5*((a-1)**2+b*b))
                residual = w*(expit(z)-y)
                gradient = np.array([float(np.sum(residual*x)) + a-1, float(np.sum(residual)) + b])
                return loss, gradient
            result = minimize(objective, np.array([1.0, 0.0]), method='L-BFGS-B', jac=True,
                              bounds=((0, None), (None, None)), options=deepcopy(OPTIMIZER_OPTIONS))
            if not result.success or not np.isfinite(result.x).all() or not math.isfinite(float(result.fun)):
                raise ValueError('Probability calibration optimization failed: ' + str(result.message))
            state.update(status='fitted', reason=None, a=float(result.x[0]), b=float(result.x[1]),
                         objective_value=float(result.fun), optimizer=dict(method='L-BFGS-B', options=deepcopy(OPTIMIZER_OPTIONS),
                         success=True, iterations=int(result.nit), message=str(result.message),
                         projected_bound='a>=0', initial=[1.0, 0.0]))
    validate_probability_calibrator(state)
    return state


def validate_probability_calibrator(state):
    n, size, _ = _validate_base(state, 'probability')
    if state.get('probability_clip') != PROBABILITY_CLIP or state.get('minimum_class_contributing_trips') != MINIMUM_CLASS_TRIPS:
        raise ValueError('Probability calibration policy changed')
    support = state.get('class_support')
    if not isinstance(support, Mapping) or set(support) != {'false', 'true'}:
        raise ValueError('Probability calibration requires binary support counts')
    for item in support.values():
        if not isinstance(item, Mapping) or set(item) != {'snapshots', 'trips'}:
            raise ValueError('Invalid probability class support')
        if (_count(item['trips'], 'class trips') > min(n, _count(item['snapshots'], 'class snapshots'))
                or bool(item['trips']) != bool(item['snapshots'])):
            raise ValueError('Invalid probability class support')
    if sum(item['snapshots'] for item in support.values()) != size or sum(item['trips'] for item in support.values()) < n:
        raise ValueError('Probability class counts differ from fit rows/trips')
    if _number(state.get('weight_sum'), 'weight_sum') != n:
        raise ValueError('Probability fit weight must equal the number of trips')
    if _count(state.get('clipped_fit_snapshots'), 'clipped_fit_snapshots') > size:
        raise ValueError('Clipped count exceeds fit snapshots')
    if state.get('objective') != 'sum_trip_balanced_cross_entropy_plus_half_squared_distance_from_identity':
        raise ValueError('Probability calibration objective changed')
    reason = 'insufficient_fit_trips' if n < MINIMUM_TRIPS else ('insufficient_class_trip_support' if min(v['trips'] for v in support.values()) < MINIMUM_CLASS_TRIPS else None)
    if (state.get('status'), state.get('reason')) != ('identity' if reason else 'fitted', reason):
        raise ValueError('Probability calibration support status is inconsistent')
    a, b = _number(state.get('a'), 'slope'), _number(state.get('b'), 'intercept')
    if a < 0:
        raise ValueError('Probability calibration slope cannot be negative')
    if reason:
        if (a, b) != (1.0, 0.0) or state.get('optimizer') is not None or state.get('objective_value') is not None:
            raise ValueError('Unsupported calibrator must preserve identity without fitting')
    else:
        opt = state.get('optimizer')
        if (not isinstance(opt, Mapping) or opt.get('method') != 'L-BFGS-B' or opt.get('options') != OPTIMIZER_OPTIONS
                or opt.get('success') is not True or opt.get('initial') != [1.0, 0.0] or opt.get('projected_bound') != 'a>=0'):
            raise ValueError('Probability optimizer metadata changed or failed')
        if _count(opt.get('iterations'), 'optimizer iterations') > OPTIMIZER_OPTIONS['maxiter'] or _number(state.get('objective_value'), 'objective value') < 0:
            raise ValueError('Probability optimizer result is invalid')
    return state


def _apply_rows(state, rows):
    declared = {tuple(k) for k in state['fit_metadata']['fit_trip_keys']}
    seen = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError('Application rows must be mappings')
        key = (_identifier(row.get('tenant_id'), 'tenant_id'), _identifier(row.get('trip_id'), 'trip_id'))
        sid = _identifier(row.get('snapshot_id'), 'snapshot_id')
        if key[0] != state['tenant_id'] or key in declared:
            raise ValueError('Calibration application crosses tenant or overlaps fit trips')
        if (key[0], sid) in seen:
            raise ValueError('Duplicate calibration application snapshot')
        seen.add((key[0], sid))
        if _timestamp(row.get('snapshot_time'), 'snapshot_time') < parse_ts(state['fit_metadata']['label_cutoff_at']):
            raise ValueError('Cannot apply calibrator before its fit label cutoff')
        status = row.get('status', 'available')
        if status not in ('available', 'unavailable'):
            raise ValueError('Unknown forecast availability')
        yield dict(row), status


def apply_interval_calibrator(state, rows):
    validate_interval_calibrator(state)
    output, digest = [], calibrator_digest(state)
    for row, status in _apply_rows(state, rows):
        lower, upper = row.get('lower_seconds'), row.get('upper_seconds')
        if status == 'unavailable':
            if lower is not None or upper is not None:
                raise ValueError('Unavailable interval must have null bounds')
        else:
            lower, upper = _interval(row)
        fitted = status == 'available' and state['status'] == 'fitted'
        row.update(raw_lower_seconds=lower, raw_upper_seconds=upper,
                   lower_seconds=max(0.0, lower-state['expansion_seconds']) if fitted else lower,
                   upper_seconds=upper+state['expansion_seconds'] if fitted else upper,
                   calibration_applied=fitted, calibration_reason='unavailable_forecast' if status == 'unavailable' else state['reason'],
                   calibrator_sha256=digest)
        if fitted and not math.isfinite(row['upper_seconds']):
            raise ValueError('Calibrated interval overflowed')
        output.append(row)
    return output


def apply_probability_calibrator(state, rows):
    validate_probability_calibrator(state)
    output, digest = [], calibrator_digest(state)
    for row, status in _apply_rows(state, rows):
        probability = row.get('probability')
        if status == 'unavailable':
            if probability is not None:
                raise ValueError('Unavailable probability must be null')
        else:
            probability = _probability(probability)
        fitted = status == 'available' and state['status'] == 'fitted'
        calibrated = probability
        if fitted:
            p = min(1-PROBABILITY_CLIP, max(PROBABILITY_CLIP, probability))
            z = state['a']*math.log(p/(1-p))+state['b']
            if not math.isfinite(z):
                raise ValueError('Calibrated probability logit overflowed')
            calibrated = _sigmoid(z)
        row.update(raw_probability=probability, probability=calibrated,
                   calibration_applied=fitted, calibration_reason='unavailable_forecast' if status == 'unavailable' else state['reason'],
                   calibrator_sha256=digest)
        output.append(row)
    return output
