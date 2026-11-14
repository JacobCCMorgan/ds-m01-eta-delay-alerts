"""Train fixed quantile candidates and retain label-free validation predictions.

M3b.2 does not compare accuracy, select a model, calibrate intervals or score
the final test. It consumes frozen M3b.1 features and per-fold mature fit labels.
"""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import tempfile

from .baseline import predict as predict_baseline
from .candidate_model import CANDIDATE_IDS, CANDIDATE_PARAMETERS, canonical_digest, fit_candidate, predict_candidate, transformer_digest
from .candidate_targets import load_fit_outcomes, prepare_training_rows
from .common import digest_file, parse_ts, read_jsonl, utc, write_jsonl
from .feature_transform import validate_transformer
from .model_splits import validate_folds


CANDIDATE_RUN_VERSION = 'm3b2-candidates-v1'
RETAINED_FEATURE_FILES = ('features.jsonl', 'matrices.jsonl', 'memberships.jsonl', 'summary.json', 'transformers.json')
REQUIRED_PINNED_FILES = (
    'config/m2.json', 'config/m3a.json', 'config/m3b1.json',
    'contracts/m3b1_features.json', 'reports/rebuild_report.json',
    'reports/m3a/frozen_history.jsonl', 'reports/m3a/run_receipt.json',
    'reports/m3b1/run_receipt.json',
    *(f'reports/m3b1/{name}' for name in RETAINED_FEATURE_FILES),
    'src/ds_m01/baseline.py', 'src/ds_m01/common.py',
    'src/ds_m01/features.py', 'src/ds_m01/feature_transform.py',
    'src/ds_m01/model_splits.py',
    'requirements.lock', 'requirements-models.lock',
)
OUTCOME_SOURCE = 'data/raw/eta_trip_outcome.jsonl'
OUTPUT_FILES = ('models.json', 'predictions.jsonl', 'training_rows.jsonl', 'summary.json')
CODE_MODULES = ('candidate_model.py', 'candidate_targets.py', 'candidate_run.py', 'baseline.py',
                'feature_transform.py', 'features.py', 'model_splits.py', 'common.py')
MODEL_PACKAGES = {'numpy', 'scipy', 'scikit-learn', 'joblib', 'threadpoolctl'}


def _key(row):
    try:
        values = row['tenant_id'], row['trip_id']
    except (KeyError, TypeError) as exc:
        raise ValueError('Record requires tenant and trip keys') from exc
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError('Record requires nonempty tenant and trip identifiers')
    return values


def _index(rows, fields):
    result = {}
    for row in rows:
        try:
            key = tuple(row[field] for field in fields)
        except (KeyError, TypeError) as exc:
            raise ValueError(f'Missing index fields: {fields}') from exc
        if any(not isinstance(value, str) or not value.strip() for value in key) or key in result:
            raise ValueError(f'Invalid or duplicate index: {fields}')
        result[key] = row
    return result


def verify_inputs(root, settings):
    """Bind all retained artifacts and the source target to their exact versions."""
    root = Path(root)
    pins = settings.get('expected_retained_sha256')
    if not isinstance(pins, dict) or set(pins) != set(REQUIRED_PINNED_FILES):
        raise ValueError('Candidate settings must pin the complete retained input set')
    if settings.get('candidate_definition_version') != CANDIDATE_RUN_VERSION:
        raise ValueError('Candidate settings version does not match implementation')
    if settings.get('candidate_ids') != list(CANDIDATE_IDS) or settings.get('candidate_parameters') != CANDIDATE_PARAMETERS:
        raise ValueError('Candidate families and fixed defaults must match implementation')
    if settings.get('min_fit_trips') != 5:
        raise ValueError('The retained candidate experiment fixes minimum fit support at five trips')
    actual = {}
    for relative, expected in pins.items():
        if digest_file(root / relative) != expected:
            raise ValueError(f'Retained candidate input changed: {relative}')
        actual[relative] = expected
    feature_receipt = json.loads((root / 'reports/m3b1/run_receipt.json').read_text())
    if set(feature_receipt['output_sha256']) != set(RETAINED_FEATURE_FILES):
        raise ValueError('Feature receipt must cover all five retained outputs')
    for name, expected in feature_receipt['output_sha256'].items():
        if actual[f'reports/m3b1/{name}'] != expected:
            raise ValueError('Feature output and receipt hashes disagree')
    baseline_receipt = json.loads((root / 'reports/m3a/run_receipt.json').read_text())
    if actual['reports/m3a/frozen_history.jsonl'] != baseline_receipt['output_sha256']['frozen_history.jsonl']:
        raise ValueError('Frozen fallback history and baseline receipt disagree')
    m2_receipt = json.loads((root / 'reports/rebuild_report.json').read_text())
    if m2_receipt.get('status') != 'passed':
        raise ValueError('Frozen M2 receipt is not passed')
    expected = {row['path']: row['original_sha256'] for row in m2_receipt['files']}
    digest = digest_file(root / OUTCOME_SOURCE)
    if digest != expected.get(OUTCOME_SOURCE) or digest != settings.get('expected_outcome_sha256'):
        raise ValueError('Outcome source differs from the frozen M2 dataset')
    actual[OUTCOME_SOURCE] = digest
    source_config = json.loads((root / 'config/m2.json').read_text())
    frozen = json.loads((root / 'config/m3a.json').read_text())
    if settings.get('folds') != frozen['folds']:
        raise ValueError('Candidate folds must remain the frozen M3a folds')
    validate_folds(settings['folds'], source_config)
    return actual


def load_retained_inputs(root):
    root = Path(root)
    return dict(
        features=read_jsonl(root / 'reports/m3b1/features.jsonl'),
        matrices=read_jsonl(root / 'reports/m3b1/matrices.jsonl'),
        memberships=read_jsonl(root / 'reports/m3b1/memberships.jsonl'),
        transformers=json.loads((root / 'reports/m3b1/transformers.json').read_text()),
        fallback_history=read_jsonl(root / 'reports/m3a/frozen_history.jsonl'),
    )


def verify_runtime(root):
    """Require the five numerical execution pins used for this retained run."""
    declared = {}
    for line in (Path(root) / 'requirements-models.lock').read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or line == '-r requirements.lock':
            continue
        if line.count('==') != 1:
            raise ValueError('Model requirements must contain exact package pins')
        name, expected = line.split('==')
        if name in declared or name not in MODEL_PACKAGES or not expected:
            raise ValueError('Invalid model dependency lock entries')
        declared[name] = expected
    if set(declared) != MODEL_PACKAGES:
        raise ValueError('The model dependency lock must cover all numerical packages')
    actual = {name: importlib.metadata.version(name) for name in sorted(MODEL_PACKAGES)}
    if declared != actual:
        raise ValueError(f'Install requirements-models.lock before candidate execution; expected {declared}, found {actual}')
    return actual


def validate_retained_inputs(inputs, folds):
    """Check cross-artifact identities and opportunity completeness before fitting."""
    features = _index(inputs['features'], ('tenant_id', 'snapshot_id'))
    matrices = _index(inputs['matrices'], ('fold_id', 'tenant_id', 'snapshot_id'))
    memberships = _index(inputs['memberships'], ('fold_id', 'tenant_id', 'trip_id'))
    transformers = _index(inputs['transformers'], ('fold_id', 'tenant_id'))
    fold_by_id = {row['fold_id']: row for row in folds}
    for key, membership in memberships.items():
        if membership.get('original_split_role') != 'train' or membership.get('fold_role') not in ('fit', 'validation'):
            raise ValueError('Only original training memberships may enter candidate work')
        if key[0] not in fold_by_id:
            raise ValueError('Membership references an unknown fold')
        if parse_ts(membership['training_cutoff_at']) != parse_ts(fold_by_id[key[0]]['training_cutoff_at']):
            raise ValueError('Membership and fold cutoff disagree')
    expected_matrix_keys = set()
    for (tenant, sid), feature in features.items():
        _key(feature)
        for fold_id in fold_by_id:
            membership = memberships.get((fold_id, tenant, feature['trip_id']))
            if membership is not None:
                expected_matrix_keys.add((fold_id, tenant, sid))
    if set(matrices) != expected_matrix_keys:
        raise ValueError('Candidate matrices must retain every declared feature opportunity exactly once')
    for (fold_id, tenant, sid), row in matrices.items():
        feature = features[tenant, sid]
        membership = memberships[fold_id, tenant, row['trip_id']]
        if (_key(row) != _key(feature) or row['snapshot_time'] != feature['snapshot_time']
                or row.get('fold_role') != membership['fold_role']):
            raise ValueError('Matrix, feature and membership identities disagree')
        at, cutoff = parse_ts(row['snapshot_time']), parse_ts(fold_by_id[fold_id]['training_cutoff_at'])
        if (row['fold_role'] == 'fit' and at >= cutoff) or (row['fold_role'] == 'validation' and at < cutoff):
            raise ValueError('Matrix snapshot lies outside its fold clock boundary')
        if row.get('status') not in ('available', 'unsupported'):
            raise ValueError('Unknown feature-vector availability')
        if row['status'] == 'available':
            state = transformers.get((fold_id, tenant))
            if state is None or not isinstance(row.get('vector'), list) or len(row['vector']) != len(state['feature_names']):
                raise ValueError('Available vector requires its matching transformer and width')
        elif row.get('vector') is not None:
            raise ValueError('Unsupported feature vectors must be null')
    for (fold_id, tenant), state in transformers.items():
        validate_transformer(state)
        if fold_id not in fold_by_id or parse_ts(state['training_cutoff_at']) != parse_ts(fold_by_id[fold_id]['training_cutoff_at']):
            raise ValueError('Transformer and fold cutoff disagree')
        declared = {(tenant_id, trip_id) for (fid, tenant_id, trip_id), row in memberships.items()
                    if fid == fold_id and tenant_id == tenant and row['fold_role'] == 'fit'}
        if {tuple(key) for key in state['fit_trip_keys']} != declared:
            raise ValueError('Transformer fit groups disagree with frozen membership')
    history_keys = set()
    for row in inputs['fallback_history']:
        key = row.get('fold_id'), *_key(row)
        if key in history_keys:
            raise ValueError('Duplicate historical fallback trip in fold')
        history_keys.add(key)
        membership = memberships.get(key)
        if membership is None or membership['fold_role'] != 'fit':
            raise ValueError('Fallback history must be restricted to the same fold fit groups')
        cutoff = parse_ts(fold_by_id[key[0]]['training_cutoff_at'])
        known, arrival = parse_ts(row['label_known_at']), parse_ts(row['actual_arrival_time'])
        departure = parse_ts(row['departure_time'])
        if (known >= cutoff or arrival >= cutoff or arrival > known or departure >= arrival
                or parse_ts(row['training_cutoff_at']) != cutoff):
            raise ValueError('Fallback history violates the fold availability boundary')
        duration = row['duration_seconds']
        if (isinstance(duration, bool) or not isinstance(duration, (float, int))
                or not math.isfinite(duration) or not math.isclose(duration, (arrival - departure).total_seconds(), abs_tol=1e-6)):
            raise ValueError('Fallback history has inconsistent duration')
    return features, transformers


def _baseline_input(feature):
    return dict(
        tenant_id=feature['tenant_id'], trip_id=feature['trip_id'],
        snapshot_id=feature['snapshot_id'], snapshot_time=feature['snapshot_time'],
        route_id=feature['categorical']['route_id'],
        departure_time_bucket=feature['categorical']['departure_time_bucket'],
        route_distance_km=feature['numeric']['route_distance_km'],
        elapsed_trip_seconds=feature['numeric']['elapsed_trip_seconds'],
    )


def _prediction(row, answer, feature, history, fold, candidate_id, transformer):
    # A finite positive duration can still exceed the timestamp representation.
    # Treat that prediction as unavailable, rather than aborting all other rows.
    if answer['status'] == 'available':
        try:
            for quantile in (10, 50, 90):
                parse_ts(row['snapshot_time']) + timedelta(seconds=answer[f'p{quantile}_remaining_seconds'])
        except (OverflowError, ValueError):
            answer = dict(answer, status='unsupported', reason='eta_out_of_range')
    candidate_ok = answer['status'] == 'available'
    fallback = None
    if not candidate_ok:
        fallback = predict_baseline(_baseline_input(feature), history, fold['training_cutoff_at'], 5, 50)
    chosen = answer if candidate_ok else fallback
    available = chosen['status'] == 'available'
    if available:
        try:
            for quantile in (10, 50, 90):
                parse_ts(row['snapshot_time']) + timedelta(seconds=chosen[f'p{quantile}_remaining_seconds'])
        except (OverflowError, ValueError):
            available = False
            fallback = dict(fallback, reason='fallback_eta_out_of_range')
    result = dict(
        tenant_id=row['tenant_id'], trip_id=row['trip_id'], snapshot_id=row['snapshot_id'],
        snapshot_time=row['snapshot_time'], fold_id=fold['fold_id'], candidate_id=candidate_id,
        training_cutoff_at=fold['training_cutoff_at'],
        transformer_sha256=(answer['transformer_sha256'] if 'transformer_sha256' in answer
                            else transformer_digest(transformer) if transformer is not None else None),
        candidate_status=answer['status'], candidate_reason=answer.get('reason'),
        model_sha256=answer.get('model_sha256'),
        raw_quantiles=answer.get('raw_quantiles'), quantile_crossing=answer.get('quantile_crossing', False),
        invalid_quantile_indices=answer.get('invalid_quantile_indices', []),
        quantile_postprocessing=answer.get('quantile_postprocessing', 'none') if candidate_ok else 'none',
        status='available' if available else 'unavailable',
        prediction_source='candidate' if candidate_ok else 'baseline_fallback' if available else 'unavailable',
        fallback_reason=fallback['reason'] if fallback is not None else None,
        fallback_pool=fallback['pool'] if fallback is not None else None,
        fallback_surviving_count=fallback['surviving_count'] if fallback is not None else None,
        missing_numeric_features=row.get('missing_numeric_features', []),
        unknown_categorical_features=row.get('unknown_categorical_features', []),
        position_validity=feature['categorical']['position_validity'],
        route_id=feature['categorical']['route_id'],
    )
    for quantile, eta_field in ((10, 'eta_lower'), (50, 'eta_median'), (90, 'eta_upper')):
        name = f'p{quantile}_remaining_seconds'
        result[name] = chosen[name] if available else None
        result[eta_field] = utc(parse_ts(row['snapshot_time']) + timedelta(seconds=chosen[name])) if available else None
    return result


def train_and_predict(inputs, settings, source_config, outcome_loader):
    """Fit each fold/tenant independently, then emit all validation opportunities.

    ``outcome_loader`` receives only each fold's fit keys and fit cutoff. It is
    never called with validation keys, validation label cutoffs or heldout roles.
    """
    folds = validate_folds(settings['folds'], source_config)
    features, transformers = validate_retained_inputs(inputs, folds)
    models, training_rows, predictions, fold_summaries = [], [], [], []
    for fold in folds:
        fold_id = fold['fold_id']
        memberships = [row for row in inputs['memberships'] if row['fold_id'] == fold_id]
        fit_keys = {_key(row) for row in memberships if row['fold_role'] == 'fit'}
        selected_outcomes = outcome_loader(fit_keys, fold['training_cutoff_at'])
        fit_matrices = [row for row in inputs['matrices'] if row['fold_id'] == fold_id and row['fold_role'] == 'fit']
        validation = [row for row in inputs['matrices'] if row['fold_id'] == fold_id and row['fold_role'] == 'validation']
        targets = prepare_training_rows(fit_matrices, inputs['features'], selected_outcomes,
                                       fold_id=fold_id, fit_trip_keys=fit_keys, training_cutoff_at=fold['training_cutoff_at'])
        target_by_key = {(row['tenant_id'], row['snapshot_id']): row for row in targets}
        training_rows.extend(targets)
        history = [row for row in inputs['fallback_history'] if row['fold_id'] == fold_id]
        for row in history:
            selected = selected_outcomes.get(_key(row))
            if (selected is None or selected.get('outcome_version_id') != row['historical_outcome_version_id']
                    or selected.get('label_status') != 'verified'
                    or selected.get('arrival_time_precision') not in ('second', 'minute')
                    or parse_ts(selected['label_available_time']) != parse_ts(row['label_known_at'])
                    or parse_ts(selected['actual_arrival_time']) != parse_ts(row['actual_arrival_time'])):
                raise ValueError('Frozen fallback history differs from the cutoff-specific selected outcome')
        tenants = sorted({row['tenant_id'] for row in memberships})
        tenant_summaries = []
        for tenant in tenants:
            transformer = transformers.get((fold_id, tenant))
            fit = []
            for row in fit_matrices:
                target = target_by_key[row['tenant_id'], row['snapshot_id']]
                if row['tenant_id'] == tenant and target['eligibility'] == 'eligible':
                    fit.append(dict(row, remaining_duration_seconds=target['remaining_duration_seconds'],
                                    sample_weight=target['sample_weight']))
            val = [row for row in validation if row['tenant_id'] == tenant]
            eligible_trips = {_key(row) for row in fit}
            tenant_summaries.append(dict(tenant_id=tenant, eligible_fit_rows=len(fit),
                                         eligible_fit_trips=len(eligible_trips), validation_opportunities=len(val)))
            for candidate_id in settings['candidate_ids']:
                if transformer is None:
                    model = dict(candidate_id=candidate_id, tenant_id=tenant, fold_id=fold_id,
                                 training_cutoff_at=fold['training_cutoff_at'], status='unsupported',
                                 reason='no_tenant_transformer', transformer_sha256=None)
                    answers = [dict(status='unsupported', reason='no_tenant_transformer', raw_quantiles=None,
                                    quantile_crossing=False) for _ in val]
                else:
                    model = fit_candidate(fit, transformer, candidate_id=candidate_id,
                                          min_fit_trips=settings['min_fit_trips'])
                    available_rows = [row for row in val if row['status'] == 'available']
                    available_answers = _index(predict_candidate(model, available_rows, transformer), ('tenant_id', 'snapshot_id'))
                    if set(available_answers) != {(row['tenant_id'], row['snapshot_id']) for row in available_rows}:
                        raise ValueError('Candidate predictions must cover every available input exactly once')
                    answers = [available_answers[row['tenant_id'], row['snapshot_id']] if row['status'] == 'available'
                               else dict(status='unsupported', reason='unsupported_feature_vector',
                                         raw_quantiles=None, quantile_crossing=False) for row in val]
                models.append(model)
                model_hash = canonical_digest(model)
                transformer_hash = transformer_digest(transformer) if transformer is not None else None
                for row, answer in zip(val, answers):
                    answer = dict(answer, model_sha256=model_hash, transformer_sha256=transformer_hash)
                    predictions.append(_prediction(row, answer, features[row['tenant_id'], row['snapshot_id']],
                                                   history, fold, candidate_id, transformer))
        fold_summaries.append(dict(fold_id=fold_id, fit_opportunities=len(targets),
                                   training_eligibility_counts=dict(sorted(Counter(row['reason'] for row in targets).items())),
                                   validation_opportunities=len(validation), tenants=tenant_summaries))
    predictions.sort(key=lambda row: (row['candidate_id'], row['fold_id'], row['tenant_id'], row['trip_id'], row['snapshot_time'], row['snapshot_id']))
    summary = dict(
        stage='M3b.2 fixed quantile candidate training and inference', source_kind='original synthetic data',
        candidate_ids=settings['candidate_ids'], candidate_definition_version=CANDIDATE_RUN_VERSION,
        model_count=len(models), available_model_count=sum(row['status'] == 'available' for row in models),
        fit_opportunities=len(training_rows), eligible_fit_rows=sum(row['eligibility'] == 'eligible' for row in training_rows),
        eligible_trip_fold_memberships=len({(row['fold_id'], row['tenant_id'], row['trip_id']) for row in training_rows if row['eligibility'] == 'eligible'}),
        validation_opportunities=sum(row['fold_role'] == 'validation' for row in inputs['matrices']),
        prediction_rows=len(predictions),
        training_weighting='Each eligible trip has total weight one within its tenant/fold; eligible snapshots share that weight.',
        target_units='Fractional seconds from snapshot to verified arrival; strictly positive regression targets.',
        interval_status='Raw conditional quantiles with ascending rearrangement; uncalibrated.',
        accuracy_evaluated=False, candidate_selected=False, calibration_used=False, final_test_evaluated=False,
        validation_outcomes_used=False, production_tables_populated=False,
        candidates={identifier: dict(
            prediction_source_counts=dict(sorted(Counter(row['prediction_source'] for row in predictions if row['candidate_id'] == identifier).items())),
            candidate_reason_counts=dict(sorted(Counter(str(row['candidate_reason']) for row in predictions if row['candidate_id'] == identifier).items())),
            crossing_rows=sum(row['quantile_crossing'] for row in predictions if row['candidate_id'] == identifier),
        ) for identifier in settings['candidate_ids']},
        folds=fold_summaries,
    )
    return dict(models=models, training_rows=training_rows, predictions=predictions, summary=summary)


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')


def run(root, settings_path, output):
    root, settings_path, output = Path(root).resolve(), Path(settings_path).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError(f'Use a new output directory to preserve evidence: {output}')
    settings_hash = digest_file(settings_path)
    settings = json.loads(settings_path.read_text())
    if digest_file(settings_path) != settings_hash:
        raise ValueError('Candidate settings changed while being read')
    code_hashes = {name: digest_file(Path(__file__).parent / name) for name in CODE_MODULES}
    input_hashes = verify_inputs(root, settings)
    package_versions = verify_runtime(root)
    source_config = json.loads((root / 'config/m2.json').read_text())
    inputs = load_retained_inputs(root)
    result = train_and_predict(inputs, settings, source_config,
                               lambda keys, cutoff: load_fit_outcomes(root / OUTCOME_SOURCE, keys, cutoff))
    # Detect source changes during fitting before publishing any final output.
    if verify_inputs(root, settings) != input_hashes:
        raise ValueError('Candidate input changed during training')
    if digest_file(settings_path) != settings_hash:
        raise ValueError('Candidate settings changed during training')
    if {name: digest_file(Path(__file__).parent / name) for name in CODE_MODULES} != code_hashes:
        raise ValueError('Candidate implementation changed during training')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.m3b2-stage-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'complete'
        stage.mkdir()
        _write_json(stage / 'models.json', result['models'])
        write_jsonl(stage / 'training_rows.jsonl', result['training_rows'])
        write_jsonl(stage / 'predictions.jsonl', result['predictions'])
        _write_json(stage / 'summary.json', result['summary'])
        receipt = dict(
            batch_id=settings['batch_id'], executed_at_utc=datetime.now(timezone.utc).isoformat(),
            python_version=platform.python_version(), platform=platform.platform(),
            package_versions=package_versions,
            input_sha256=input_hashes, settings_sha256=settings_hash,
            code_sha256=code_hashes,
            output_sha256={name: digest_file(stage / name) for name in OUTPUT_FILES},
            validation_scope='Fixed candidate training and inference on original-training folds; no accuracy comparison or heldout scoring.',
        )
        _write_json(stage / 'run_receipt.json', receipt)
        stage.rename(output)
    return dict(output=str(output), **result['summary'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--config', type=Path, default=Path('config/m3b2.json'))
    parser.add_argument('--output', type=Path, default=Path('reports/m3b2_local'))
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.config, args.output), indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
