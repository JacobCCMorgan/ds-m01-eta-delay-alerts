"""Fit frozen delay classifiers and optionally evaluate training-period folds."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import csv
import json
from pathlib import Path
import platform
import tempfile

from .candidate_model import canonical_digest
from .candidate_run import load_retained_inputs, validate_retained_inputs, verify_runtime
from .common import digest_file, parse_ts, read_jsonl, write_jsonl
from .delay_model import METHOD_IDS, PARAMETERS, FEATURE_VERSION, fit_deadline_transformer, fit_delay_model, predict_delay_model
from .delay_targets import prepare_promise_features, build_delay_targets
from .model_splits import validate_folds

PROTOCOL_SHA256 = 'df9895e9b9c7854d2deb7ed0489ba0cb00bb2794c87dadea13a0b206620ef989'
OUTPUT_FILES = ('models.json', 'training_rows.jsonl', 'predictions.jsonl', 'promise_features.jsonl', 'summary.json')
EVALUATION_FILES = ('evaluation_rows.jsonl', 'metrics.json', 'reliability.csv', 'slices.csv')
CODE_MODULES = ('delay_model.py', 'delay_targets.py', 'delay_run.py', 'candidate_model.py', 'candidate_run.py',
                'candidate_targets.py', 'baseline.py', 'common.py', 'feature_transform.py', 'features.py', 'model_splits.py')


def _trip(row):
    return row['tenant_id'], row['trip_id']


def _snapshot(row):
    return row['tenant_id'], row['snapshot_id']


def _index(rows, fields):
    result = {}
    for row in rows:
        key = tuple(row[name] for name in fields)
        if key in result:
            raise ValueError(f'Duplicate delay input identity: {fields}')
        result[key] = row
    return result


def _check_target_binding(target, opportunity, cutoff):
    fields = ('tenant_id', 'trip_id', 'snapshot_id', 'promise_version_id', 'fold_id', 'fold_role')
    if any(target.get(name) != opportunity.get(name) for name in fields):
        raise ValueError('Delay target identity or promise differs from its opportunity')
    if parse_ts(target['snapshot_time']) != parse_ts(opportunity['snapshot_time']) or parse_ts(target['label_cutoff_at']) != parse_ts(cutoff):
        raise ValueError('Delay target snapshot or cutoff clock differs from its opportunity')


def verify_inputs(root, input_root, settings):
    hashes = {}
    for key, source_root in (('expected_retained_sha256', root), ('expected_source_sha256', input_root)):
        for relative, expected in settings[key].items():
            actual = digest_file(Path(source_root) / relative)
            if actual != expected:
                raise ValueError(f'Frozen delay input changed: {relative}')
            hashes[relative] = actual
    if settings['method_ids'] != list(METHOD_IDS) or settings['logistic_parameters'] != PARAMETERS:
        raise ValueError('Fixed classification methods or parameters changed')
    return hashes


def load_sources(root, input_root):
    inputs = load_retained_inputs(root)
    allowed = {_trip(row) for row in inputs['memberships']}
    # Shared source bytes contain protected groups. Gate by keys before exposing
    # their payloads to the experiment, while retaining their hashes separately.
    def scoped(relative):
        return [row for row in read_jsonl(Path(input_root) / relative) if _trip(row) in allowed]
    return inputs, scoped('data/processed/eta_snapshot.jsonl'), scoped('data/raw/eta_promise_version.jsonl'), scoped('data/raw/eta_trip_outcome.jsonl')


def prepare_bound_promises(inputs, snapshots, promises):
    feature_index = _index(inputs['features'], ('tenant_id', 'snapshot_id'))
    bound = _index(snapshots, ('tenant_id', 'snapshot_id'))
    if bound.keys() != feature_index.keys():
        raise ValueError('Source snapshots must match every retained training feature')
    for key, row in bound.items():
        feature = feature_index[key]
        if _trip(row) != _trip(feature) or row['snapshot_time'] != feature['snapshot_time']:
            raise ValueError('Raw snapshot and retained feature identity or clock differs')
    return prepare_promise_features(list(bound.values()), {_trip(r) for r in inputs['memberships']}, promises)


def _prediction_rows(models, validation, features, transformers, promises):
    """Public label-free inference path, shared by retained JSON replay."""
    states = _index(transformers, ('fold_id', 'tenant_id'))
    model_index = _index(models, ('fold_id', 'tenant_id', 'method_id'))
    by_promise = _index(promises, ('tenant_id', 'snapshot_id'))
    by_feature = _index(features, ('tenant_id', 'snapshot_id'))
    output = []
    for fold_id, tenant in sorted({(r['fold_id'], r['tenant_id']) for r in validation}):
        state = states[fold_id, tenant]
        rows = [dict(row, promise_seconds_remaining=by_promise[_snapshot(row)]['promise_seconds_remaining'])
                for row in validation if (row['fold_id'], row['tenant_id']) == (fold_id, tenant)]
        answers = {method: _index(predict_delay_model(model_index[fold_id, tenant, method], rows, state), ('tenant_id', 'snapshot_id'))
                   for method in METHOD_IDS}
        for row in rows:
            promise, feature = by_promise[_snapshot(row)], by_feature[_snapshot(row)]
            baseline = answers['fold_prevalence'][_snapshot(row)]
            for method in METHOD_IDS:
                native = answers[method][_snapshot(row)]
                chosen = native if native['status'] == 'available' else baseline
                available = chosen['status'] == 'available'
                output.append({**{k: row[k] for k in ('tenant_id', 'trip_id', 'snapshot_id', 'snapshot_time', 'fold_id')},
                    'method_id': method, 'status': 'available' if available else 'unavailable',
                    'probability': chosen['probability'] if available else None,
                    'prediction_source': 'model' if native['status'] == 'available' else 'prevalence_fallback' if available else 'unavailable',
                    'candidate_status': native['status'], 'candidate_reason': native['reason'],
                    'model_sha256': native['model_sha256'], 'fallback_model_sha256': baseline['model_sha256'] if chosen is not native and available else None,
                    'training_cutoff_at': state['training_cutoff_at'], 'feature_definition_version': FEATURE_VERSION,
                    'promise_version_id': promise['promise_version_id'], 'promise_known_at': promise['promise_known_at'],
                    'deadline_at': promise['deadline_at'], 'deadline_remaining_seconds': promise['promise_seconds_remaining'],
                    'route_id': feature['categorical']['route_id'], 'position_validity': feature['categorical']['position_validity'],
                    'departure_time_bucket': feature['categorical']['departure_time_bucket'],
                    'elapsed_trip_seconds': feature['numeric']['elapsed_trip_seconds']})
    return sorted(output, key=lambda r: (r['method_id'], r['fold_id'], r['tenant_id'], r['trip_id'], r['snapshot_time'], r['snapshot_id']))


def train_and_predict(inputs, snapshots, promises, settings, source_config, target_builder):
    """Call target_builder only with fit snapshots, fit keys and the fit cutoff."""
    folds = validate_folds(settings['folds'], source_config)
    _, transformers = validate_retained_inputs(inputs, folds)
    bound_promises = prepare_bound_promises(inputs, snapshots, promises)
    promise_index = _index(bound_promises, ('tenant_id', 'snapshot_id'))
    snapshot_index = _index(snapshots, ('tenant_id', 'snapshot_id'))
    models, training_rows = [], []
    for fold in folds:
        fit = [row for row in inputs['matrices'] if row['fold_id'] == fold['fold_id'] and row['fold_role'] == 'fit']
        fit_keys = {_trip(row) for row in fit}
        # All fold-local deadline preprocessing is frozen before target use.
        deadline_states = {}
        for tenant in sorted({row['tenant_id'] for row in fit}):
            local = [dict(row, promise_seconds_remaining=promise_index[_snapshot(row)]['promise_seconds_remaining']) for row in fit if row['tenant_id'] == tenant]
            deadline_states[tenant] = fit_deadline_transformer(local, transformers[fold['fold_id'], tenant])
        opportunities = [dict(snapshot_index[_snapshot(row)], fold_id=fold['fold_id'], fold_role='fit') for row in fit]
        targets = target_builder(opportunities, fit_keys, fold['training_cutoff_at'])
        target_index = _index(targets, ('tenant_id', 'snapshot_id'))
        if target_index.keys() != {_snapshot(r) for r in fit}:
            raise ValueError('Fit targets do not preserve every opportunity')
        for opportunity in opportunities:
            _check_target_binding(target_index[_snapshot(opportunity)], opportunity, fold['training_cutoff_at'])
        eligible = [r for r in fit if r['status'] == 'available' and target_index[_snapshot(r)]['eligibility'] == 'eligible']
        eligible_keys = {_snapshot(r) for r in eligible}
        counts = Counter(_trip(row) for row in eligible)
        for row in fit:
            target = target_index[_snapshot(row)]
            weight = 1 / counts[_trip(row)] if _snapshot(row) in eligible_keys else 0.0
            training_rows.append(dict(target, sample_weight=weight, matrix_status=row['status']))
        for tenant in sorted(deadline_states):
            rows = [dict(row, **{k: target_index[_snapshot(row)][k] for k in
                                ('delay_label', 'eligibility', 'label_status', 'actual_arrival_time', 'label_known_at')},
                         sample_weight=1 / counts[_trip(row)],
                         promise_seconds_remaining=promise_index[_snapshot(row)]['promise_seconds_remaining'])
                    for row in eligible if row['tenant_id'] == tenant]
            for method in METHOD_IDS:
                models.append(fit_delay_model(rows, transformers[fold['fold_id'], tenant], deadline_states[tenant], method))
    validation = [r for r in inputs['matrices'] if r['fold_role'] == 'validation']
    predictions = _prediction_rows(models, validation, inputs['features'], inputs['transformers'], bound_promises)
    summary = {'stage': 'M3c.1 fold delay classification', 'method_ids': list(METHOD_IDS),
               'models': len(models), 'available_models': sum(m['status'] == 'available' for m in models),
               'fit_opportunities': len(training_rows), 'eligible_fit_opportunities': sum(r['eligibility'] == 'eligible' for r in training_rows),
               'eligible_trip_fold_memberships': len({(r['fold_id'], r['tenant_id'], r['trip_id']) for r in training_rows if r['sample_weight'] > 0}),
               'validation_opportunities_per_method': len(validation), 'prediction_rows': len(predictions),
               'training_eligibility_counts': dict(sorted(Counter(r['reason'] for r in training_rows).items())),
               'prediction_sources': {m: dict(sorted(Counter(r['prediction_source'] for r in predictions if r['method_id'] == m).items())) for m in METHOD_IDS},
               'validation_targets_used_in_fitting': False, 'calibration_used': False, 'final_test_evaluated': False,
               'probability_state': 'Raw independent classification probabilities before calibration.'}
    return {'models': models, 'training_rows': training_rows, 'predictions': predictions,
            'promise_features': bound_promises, 'summary': summary}


def evaluate_predictions(result, inputs, snapshots, outcomes, promises, settings):
    from .delay_metrics import evaluate_methods
    snapshot_index = _index(snapshots, ('tenant_id', 'snapshot_id'))
    targets = {}
    for fold in settings['folds']:
        matrices = [r for r in inputs['matrices'] if r['fold_id'] == fold['fold_id'] and r['fold_role'] == 'validation']
        opportunities = [dict(snapshot_index[_snapshot(r)], fold_id=fold['fold_id'], fold_role='validation') for r in matrices]
        rows = build_delay_targets(opportunities, {_trip(r) for r in matrices}, outcomes, promises, fold['validation_label_cutoff_at'])
        by_key = _index(rows, ('tenant_id', 'snapshot_id'))
        if by_key.keys() != {_snapshot(r) for r in opportunities}:
            raise ValueError('Evaluation targets do not preserve every opportunity')
        for opportunity in opportunities:
            _check_target_binding(by_key[_snapshot(opportunity)], opportunity, fold['validation_label_cutoff_at'])
        targets.update({(r['fold_id'], *_snapshot(r)): r for r in rows})
    for prediction in result['predictions']:
        target = targets[(prediction['fold_id'], *_snapshot(prediction))]
        for field in ('tenant_id', 'trip_id', 'snapshot_id', 'snapshot_time', 'fold_id', 'promise_version_id', 'deadline_at'):
            if prediction[field] != target[field]:
                raise ValueError('Prediction and evaluation target bindings disagree')
    evaluated = [dict(prediction, **{k: v for k, v in targets[(prediction['fold_id'], *_snapshot(prediction))].items()
                                    if k not in prediction}) for prediction in result['predictions']]
    metrics, reliability, slices = evaluate_methods(evaluated, settings['evaluation'])
    return evaluated, metrics, reliability, slices


def _json(path, obj):
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')


def _csv(path, rows):
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else [], lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def run(root, input_root, output, *, evaluate=False):
    root, input_root, output = Path(root).resolve(), Path(input_root).resolve(), Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError('Use a new delay output directory')
    config = root / 'config/m3c.json'
    if digest_file(config) != PROTOCOL_SHA256:
        raise ValueError('Delay protocol differs from the pre-fit freeze')
    settings = json.loads(config.read_text())
    code_modules = (*CODE_MODULES, 'delay_metrics.py') if evaluate else CODE_MODULES
    code_hashes = {name: digest_file(Path(__file__).parent / name) for name in code_modules}
    input_hashes = verify_inputs(root, input_root, settings)
    runtime = verify_runtime(root)
    inputs, snapshots, promises, outcomes = load_sources(root, input_root)
    source_config = json.loads((root / 'config/m2.json').read_text())
    result = train_and_predict(inputs, snapshots, promises, settings, source_config,
                              lambda opportunities, keys, cutoff: build_delay_targets(opportunities, keys, outcomes, promises, cutoff))
    if result['summary']['validation_opportunities_per_method'] != settings['expected_validation_opportunities_per_method']:
        raise ValueError('Prediction opportunity count changed')
    evaluation = evaluate_predictions(result, inputs, snapshots, outcomes, promises, settings) if evaluate else None
    if verify_inputs(root, input_root, settings) != input_hashes or digest_file(config) != PROTOCOL_SHA256:
        raise ValueError('Inputs or protocol changed during delay execution')
    if code_hashes != {name: digest_file(Path(__file__).parent / name) for name in code_modules}:
        raise ValueError('Delay implementation changed during execution')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.delay-stage-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'complete'
        stage.mkdir()
        for name in ('models', 'summary'):
            _json(stage / (name + '.json'), result[name])
        for name in ('training_rows', 'predictions', 'promise_features'):
            write_jsonl(stage / (name + '.jsonl'), result[name])
        if evaluation:
            rows, metrics, reliability, slices = evaluation
            write_jsonl(stage / 'evaluation_rows.jsonl', rows)
            _json(stage / 'metrics.json', metrics)
            _csv(stage / 'reliability.csv', reliability)
            _csv(stage / 'slices.csv', slices)
        names = (*OUTPUT_FILES, *EVALUATION_FILES) if evaluate else OUTPUT_FILES
        receipt = {'batch_id': settings['batch_id'], 'protocol_sha256': PROTOCOL_SHA256,
                   'executed_at_utc': datetime.now(timezone.utc).isoformat(), 'python_version': platform.python_version(),
                   'platform': platform.platform(), 'package_versions': runtime, 'input_sha256': input_hashes,
                   'code_sha256': code_hashes, 'output_sha256': {name: digest_file(stage / name) for name in names},
                   'evaluation_executed': evaluate, 'validation_targets_used_in_fitting': False,
                   'calibration_used': False, 'final_test_evaluated': False}
        _json(stage / 'run_receipt.json', receipt)
        if output.exists() or output.is_symlink():
            raise FileExistsError('Delay output appeared during execution')
        stage.rename(output)
    return result['summary']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--inputs', type=Path, help='Root containing the three exact M2 source files')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--evaluate', action='store_true', help='Join validation labels only after fitting and all predictions')
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.inputs or args.root, args.output, evaluate=args.evaluate), indent=2))


if __name__ == '__main__':
    main()
