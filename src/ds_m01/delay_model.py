"""Fold-local binary delay models with portable, validated JSON inference."""

from collections import defaultdict
from copy import deepcopy
from importlib.metadata import version
import math
import warnings

from .candidate_model import canonical_digest, transformer_digest
from .common import parse_ts, utc
from .feature_transform import validate_transformer

METHOD_IDS = ('fold_prevalence', 'logistic_delay')
FEATURE_VERSION = 'm3c-promise-features-v1'
MODEL_VERSION = 'm3c-delay-model-v1'
PARAMETERS = {'C': 1.0, 'solver': 'lbfgs', 'max_iter': 2000, 'tol': 1e-8,
              'fit_intercept': True, 'class_weight': None, 'random_state': 20261105,
              'l1_ratio': 0.0}


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    return float(value)


def _keys(rows):
    return sorted({(r['tenant_id'], r['trip_id']) for r in rows})


def _check_identity(row, transformer, *, fit):
    for name in ('tenant_id', 'trip_id', 'snapshot_id', 'snapshot_time'):
        if not isinstance(row.get(name), str) or not row[name].strip():
            raise ValueError('Missing model row identity')
    key = row['tenant_id'], row['trip_id']
    at = parse_ts(row['snapshot_time'])
    cutoff = parse_ts(transformer['training_cutoff_at'])
    if row['tenant_id'] != transformer['tenant_id'] or row['fold_id'] != transformer['fold_id']:
        raise ValueError('Model row crosses tenant or fold')
    role = 'fit' if fit else 'validation'
    if row['fold_role'] != role:
        raise ValueError('Model row has the wrong fold role')
    if row.get('feature_definition_version') != transformer['feature_definition_version']:
        raise ValueError('Model row feature definition differs from frozen transformer')
    if row.get('status') not in ('available', 'unsupported'):
        raise ValueError('Unknown model vector status')
    if row['status'] == 'unsupported' and row.get('vector') is not None:
        raise ValueError('Unsupported vector must be null')
    declared = {tuple(k) for k in transformer['fit_trip_keys']}
    if fit:
        snapshots = {tuple(k) for k in transformer['fitted_snapshot_keys']}
        if key not in declared or (key[0], row['snapshot_id']) not in snapshots or at >= cutoff:
            raise ValueError('Model fit row outside frozen fit groups or cutoff')
    elif key in declared or at < cutoff:
        raise ValueError('Prediction overlaps fit groups or precedes cutoff')


def fit_deadline_transformer(rows, transformer):
    """Fit the added deadline feature before inspecting any target eligibility."""
    validate_transformer(transformer)
    available = []
    seen = set()
    for row in rows:
        _check_identity(row, transformer, fit=True)
        key = row['tenant_id'], row['snapshot_id']
        if key in seen:
            raise ValueError('Duplicate deadline fit snapshot')
        seen.add(key)
        if row['status'] == 'available':
            available.append(row)
    expected = {tuple(key) for key in transformer['fitted_snapshot_keys']}
    if seen != expected or len(available) != len(expected):
        raise ValueError('Deadline scaling must retain every frozen fit snapshot before label filtering')
    if not available:
        raise ValueError('Deadline scaling requires available fit opportunities')
    available.sort(key=lambda r: (r['tenant_id'], r['snapshot_id']))
    values = [_number(r['promise_seconds_remaining'], 'promise seconds remaining') for r in available]
    mean = math.fsum(values) / len(values)
    scale = math.sqrt(math.fsum((value - mean) ** 2 for value in values) / len(values)) or 1.0
    return {'feature_definition_version': FEATURE_VERSION, 'mean_seconds': mean, 'scale_seconds': scale,
            'fit_snapshot_keys': [[r['tenant_id'], r['snapshot_id']] for r in available],
            'fit_rows': len(available), 'base_transformer_sha256': transformer_digest(transformer)}


def validate_deadline_transformer(state, transformer):
    if state.get('feature_definition_version') != FEATURE_VERSION or state.get('base_transformer_sha256') != transformer_digest(transformer):
        raise ValueError('Deadline transformer binding mismatch')
    _number(state.get('mean_seconds'), 'deadline mean')
    if _number(state.get('scale_seconds'), 'deadline scale') <= 0:
        raise ValueError('Deadline scale must be positive')
    keys = state.get('fit_snapshot_keys')
    if not isinstance(keys, list) or not keys or any(not isinstance(k, list) or len(k) != 2 for k in keys):
        raise ValueError('Deadline fit snapshot identities invalid')
    pairs = [tuple(k) for k in keys]
    if len(set(pairs)) != len(pairs) or state.get('fit_rows') != len(pairs):
        raise ValueError('Deadline fit snapshot counts invalid')
    if set(pairs) != {tuple(k) for k in transformer['fitted_snapshot_keys']}:
        raise ValueError('Deadline transformer must retain every frozen fit snapshot')


def extend_vector(row, transformer, deadline_state):
    values = row.get('vector')
    if not isinstance(values, list) or len(values) != len(transformer['feature_names']):
        raise ValueError('Model vector width differs from frozen layout')
    vector = [_number(v, 'vector element') for v in values]
    remaining = _number(row.get('promise_seconds_remaining'), 'promise seconds remaining')
    vector.append((remaining - deadline_state['mean_seconds']) / deadline_state['scale_seconds'])
    if not all(math.isfinite(v) for v in vector):
        raise ValueError('Scaled deadline vector is nonfinite')
    return vector


def fit_delay_model(rows, transformer, deadline_state, method_id):
    """Fit on eligible rows with equal total weight per trip and no class balancing."""
    validate_deadline_transformer(deadline_state, transformer)
    if method_id not in METHOD_IDS:
        raise ValueError('Unknown delay model')
    clean = sorted(rows, key=lambda r: (r['tenant_id'], r['trip_id'], r['snapshot_time'], r['snapshot_id']))
    seen, weights = set(), defaultdict(list)
    for row in clean:
        _check_identity(row, transformer, fit=True)
        key = row['tenant_id'], row['snapshot_id']
        if key in seen or type(row.get('delay_label')) is not bool:
            raise ValueError('Duplicate snapshot or nonbinary delay label')
        if row.get('eligibility') != 'eligible' or row.get('label_status') != 'verified':
            raise ValueError('Fit row requires a verified eligible binary label')
        cutoff = parse_ts(transformer['training_cutoff_at'])
        if not parse_ts(row['snapshot_time']) < parse_ts(row['actual_arrival_time']) <= parse_ts(row['label_known_at']) < cutoff:
            raise ValueError('Fit label clocks violate snapshot or training cutoff')
        seen.add(key)
        weight = _number(row.get('sample_weight'), 'sample weight')
        if weight <= 0:
            raise ValueError('Sample weight must be positive')
        weights[(row['tenant_id'], row['trip_id'])].append(weight)
    if any(not all(math.isclose(value, 1.0 / len(w), rel_tol=0, abs_tol=1e-9) for value in w) for w in weights.values()):
        raise ValueError('Each eligible trip must have total weight one shared equally among snapshots')
    vectors = [extend_vector(row, transformer, deadline_state) for row in clean]
    trip_keys = _keys(clean)
    counts = {str(label).lower(): {'snapshots': sum(r['delay_label'] == label for r in clean),
                                  'trips': len(_keys([r for r in clean if r['delay_label'] == label]))}
              for label in (False, True)}
    total_weight = math.fsum(r['sample_weight'] for r in clean)
    prior = math.fsum(r['sample_weight'] for r in clean if r['delay_label']) / total_weight if total_weight else None
    model = {'model_definition_version': MODEL_VERSION, 'method_id': method_id,
             'tenant_id': transformer['tenant_id'], 'fold_id': transformer['fold_id'],
             'training_cutoff_at': transformer['training_cutoff_at'],
             'base_transformer_sha256': transformer_digest(transformer),
             'deadline_transformer': deepcopy(deadline_state),
             'feature_names': list(transformer['feature_names']) + ['promise_seconds_remaining_scaled'],
             'fitted_trip_keys': [list(k) for k in trip_keys],
             'fitted_snapshot_keys': [[r['tenant_id'], r['snapshot_id']] for r in clean],
             'fitted_trips': len(trip_keys), 'fitted_rows': len(clean), 'class_support': counts,
             'minimum_fit_trips': 5, 'minimum_class_contributing_trips': 2,
             'parameters': deepcopy(PARAMETERS) if method_id == 'logistic_delay' else {},
             'weighted_training_prevalence': prior, 'total_training_weight': total_weight,
             'status': 'unsupported', 'reason': 'insufficient_fit_trips',
             'coefficients': None, 'intercept': None, 'iterations': None, 'library_versions': {}}
    if len(trip_keys) >= 5:
        if method_id == 'fold_prevalence':
            model.update(status='available', reason=None)
        elif min(c['trips'] for c in counts.values()) < 2:
            model['reason'] = 'insufficient_class_trip_support'
        else:
            from sklearn.exceptions import ConvergenceWarning
            from sklearn.linear_model import LogisticRegression
            estimator = LogisticRegression(**PARAMETERS)
            with warnings.catch_warnings():
                warnings.simplefilter('error', ConvergenceWarning)
                estimator.fit(vectors, [r['delay_label'] for r in clean], sample_weight=[r['sample_weight'] for r in clean])
            if estimator.classes_.tolist() != [False, True]:
                raise ValueError('Unexpected logistic class order')
            model.update(status='available', reason=None, coefficients=estimator.coef_[0].tolist(),
                         intercept=float(estimator.intercept_[0]), iterations=int(estimator.n_iter_[0]),
                         library_versions={name: version(name) for name in ('numpy', 'scipy', 'scikit-learn')})
    validate_delay_model(model, transformer)
    return model


def validate_delay_model(model, transformer):
    validate_transformer(transformer)
    if model.get('model_definition_version') != MODEL_VERSION or model.get('method_id') not in METHOD_IDS:
        raise ValueError('Unknown model version or family')
    if any(model.get(k) != transformer[k] for k in ('tenant_id', 'fold_id', 'training_cutoff_at')):
        raise ValueError('Model tenant, fold or cutoff binding mismatch')
    if model.get('base_transformer_sha256') != transformer_digest(transformer):
        raise ValueError('Base transformer hash mismatch')
    validate_deadline_transformer(model['deadline_transformer'], transformer)
    if model.get('feature_names') != list(transformer['feature_names']) + ['promise_seconds_remaining_scaled']:
        raise ValueError('Model feature order mismatch')
    for key, parent, count in (('fitted_trip_keys', 'fit_trip_keys', 'fitted_trips'),
                               ('fitted_snapshot_keys', 'fitted_snapshot_keys', 'fitted_rows')):
        values = model.get(key)
        if not isinstance(values, list) or any(not isinstance(k, list) or len(k) != 2 for k in values):
            raise ValueError('Invalid model fit identities')
        pairs = [tuple(k) for k in values]
        if len(set(pairs)) != len(pairs) or model.get(count) != len(pairs) or not set(pairs) <= {tuple(k) for k in transformer[parent]}:
            raise ValueError('Invalid model fit membership or counts')
    counts = model.get('class_support', {})
    if set(counts) != {'false', 'true'}:
        raise ValueError('Missing binary class support')
    for values in counts.values():
        if set(values) != {'snapshots', 'trips'} or any(type(v) is not int or v < 0 for v in values.values()):
            raise ValueError('Invalid class support')
        if values['trips'] > min(values['snapshots'], model['fitted_trips']):
            raise ValueError('Inconsistent class trip support')
    if sum(v['snapshots'] for v in counts.values()) != model['fitted_rows']:
        raise ValueError('Class counts differ from fitted rows')
    if (model.get('minimum_fit_trips'), model.get('minimum_class_contributing_trips')) != (5, 2):
        raise ValueError('Model support policy changed')
    expected_parameters = PARAMETERS if model['method_id'] == 'logistic_delay' else {}
    if model.get('parameters') != expected_parameters:
        raise ValueError('Model parameters changed')
    prior = model.get('weighted_training_prevalence')
    if model['fitted_rows']:
        if not 0 <= _number(prior, 'prevalence') <= 1 or not math.isclose(_number(model.get('total_training_weight'), 'weight'), model['fitted_trips'], abs_tol=1e-8):
            raise ValueError('Invalid model prevalence or training weight')
    elif prior is not None or model.get('total_training_weight') != 0:
        raise ValueError('Empty model must have null prevalence')
    reason = 'insufficient_fit_trips' if model['fitted_trips'] < 5 else None
    if reason is None and model['method_id'] == 'logistic_delay' and min(v['trips'] for v in counts.values()) < 2:
        reason = 'insufficient_class_trip_support'
    if (model.get('status'), model.get('reason')) != ('unsupported' if reason else 'available', reason):
        raise ValueError('Model status contradicts support')
    if reason is None and model['method_id'] == 'logistic_delay':
        coefficients = model.get('coefficients')
        if not isinstance(coefficients, list) or len(coefficients) != len(model['feature_names']):
            raise ValueError('Logistic coefficient shape mismatch')
        for value in coefficients:
            _number(value, 'coefficient')
        _number(model.get('intercept'), 'intercept')
    elif model.get('coefficients') is not None or model.get('intercept') is not None:
        raise ValueError('Nonlogistic or unsupported state contains coefficients')


def sigmoid(value):
    value = _number(value, 'logit')
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exponent = math.exp(value)
    return exponent / (1.0 + exponent)


def predict_delay_model(model, rows, transformer):
    """Validate JSON state and emit every supplied opportunity without target access."""
    validate_delay_model(model, transformer)
    model_hash = canonical_digest(model)
    results, seen = [], set()
    for row in sorted(rows, key=lambda r: (r['tenant_id'], r['trip_id'], r['snapshot_time'], r['snapshot_id'])):
        _check_identity(row, transformer, fit=False)
        key = row['tenant_id'], row['snapshot_id']
        if key in seen:
            raise ValueError('Duplicate prediction opportunity')
        seen.add(key)
        vector = extend_vector(row, transformer, model['deadline_transformer']) if row['status'] == 'available' else None
        status, reason, probability = model['status'], model['reason'], None
        if status == 'available':
            if model['method_id'] == 'fold_prevalence':
                probability = model['weighted_training_prevalence']
            elif row['status'] == 'unsupported':
                status, reason = 'unsupported', 'unsupported_feature_vector'
            else:
                logit = math.fsum([model['intercept'], *(c * v for c, v in zip(model['coefficients'], vector))])
                probability = sigmoid(logit)
        results.append({**{k: row[k] for k in ('tenant_id', 'trip_id', 'snapshot_id', 'snapshot_time')},
                        'method_id': model['method_id'], 'fold_id': model['fold_id'], 'status': status,
                        'reason': reason, 'probability': probability, 'model_sha256': model_hash})
    return results
