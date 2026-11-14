"""Verify retained delay probabilities and optional source-bound label evidence."""

import argparse
from collections import Counter
import csv
import json
import math
from pathlib import Path

from .common import digest_file, read_jsonl, parse_ts
from .delay_run import (PROTOCOL_SHA256, OUTPUT_FILES, EVALUATION_FILES, CODE_MODULES, _prediction_rows,
                        _snapshot, _trip, _index, _check_target_binding,
                        verify_inputs, load_sources, evaluate_predictions)
from .delay_targets import build_delay_targets
from .candidate_run import load_retained_inputs, validate_retained_inputs
from .model_splits import validate_folds


def compare_values(expected, actual, *, absolute_tolerance, relative_tolerance, path='root'):
    """Require exact structure and decisions, with fixed finite probability tolerances."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or set(expected) != set(actual):
            raise ValueError(f'Replay dictionary fields differ at {path}')
        for key in expected:
            compare_values(expected[key], actual[key], absolute_tolerance=absolute_tolerance,
                           relative_tolerance=relative_tolerance, path=path + '.' + key)
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            raise ValueError(f'Replay list size differs at {path}')
        for index, (left, right) in enumerate(zip(expected, actual)):
            compare_values(left, right, absolute_tolerance=absolute_tolerance,
                           relative_tolerance=relative_tolerance, path=f'{path}[{index}]')
    elif isinstance(expected, float):
        if type(actual) not in (int, float) or not math.isfinite(expected) or not math.isfinite(actual) or not math.isclose(expected, actual, abs_tol=absolute_tolerance, rel_tol=relative_tolerance):
            raise ValueError(f'Replay numeric value differs at {path}')
    elif type(expected) is not type(actual) or expected != actual:
        raise ValueError(f'Replay exact value differs at {path}')


def _verify_report(root, report):
    if digest_file(root / 'config/m3c.json') != PROTOCOL_SHA256:
        raise ValueError('Replay protocol differs from the pre-fit freeze')
    settings = json.loads((root / 'config/m3c.json').read_text())
    receipt = json.loads((report / 'run_receipt.json').read_text())
    if receipt.get('protocol_sha256') != PROTOCOL_SHA256:
        raise ValueError('Retained delay receipt protocol mismatch')
    if type(receipt.get('evaluation_executed')) is not bool:
        raise ValueError('Retained evaluation scope must be Boolean')
    for name in ('validation_targets_used_in_fitting', 'calibration_used', 'final_test_evaluated'):
        if receipt.get(name) is not False:
            raise ValueError(f'Retained receipt exceeds the declared scope: {name}')
    pins = {**settings['expected_retained_sha256'], **settings['expected_source_sha256']}
    if receipt.get('input_sha256') != pins:
        raise ValueError('Retained delay receipt must bind every frozen input')
    for relative, expected in settings['expected_retained_sha256'].items():
        if digest_file(root / relative) != expected:
            raise ValueError(f'Retained dependency changed: {relative}')
    names = set(OUTPUT_FILES)
    if receipt.get('evaluation_executed'):
        names.update(EVALUATION_FILES)
    if set(receipt.get('output_sha256', {})) != names:
        raise ValueError('Retained delay receipt omits or adds an output')
    for relative in names:
        if digest_file(report / relative) != receipt['output_sha256'][relative]:
            raise ValueError(f'Retained delay output changed: {relative}')
    modules = set(CODE_MODULES) | ({'delay_metrics.py'} if receipt['evaluation_executed'] else set())
    if set(receipt.get('code_sha256', {})) != modules:
        raise ValueError('Retained code manifest must cover every execution module')
    for name, expected in receipt['code_sha256'].items():
        if digest_file(root / 'src/ds_m01' / name) != expected or digest_file(Path(__file__).parent / name) != expected:
            raise ValueError(f'Retained delay implementation changed: {name}')
    return settings, receipt


def _rebuild_training_rows(inputs, snapshots, outcomes, promises, settings):
    snapshot_index = _index(snapshots, ('tenant_id', 'snapshot_id'))
    all_targets = []
    for fold in validate_folds(settings['folds'], settings['_source_config']):
        matrices = [r for r in inputs['matrices'] if r['fold_id'] == fold['fold_id'] and r['fold_role'] == 'fit']
        opportunities = [dict(snapshot_index[_snapshot(r)], fold_id=fold['fold_id'], fold_role='fit') for r in matrices]
        targets = build_delay_targets(opportunities, {_trip(r) for r in matrices}, outcomes, promises, fold['training_cutoff_at'])
        target_index = _index(targets, ('tenant_id', 'snapshot_id'))
        for opportunity in opportunities:
            _check_target_binding(target_index[_snapshot(opportunity)], opportunity, fold['training_cutoff_at'])
        counts = Counter(_trip(r) for r in matrices if r['status'] == 'available' and target_index[_snapshot(r)]['eligibility'] == 'eligible')
        for row in matrices:
            target = target_index[_snapshot(row)]
            weight = 1 / counts[_trip(row)] if row['status'] == 'available' and target['eligibility'] == 'eligible' else 0.0
            all_targets.append(dict(target, sample_weight=weight, matrix_status=row['status']))
    return all_targets


def _csv_values(path, expected):
    """Convert declared CSV columns back to their generated value types."""
    with path.open(encoding='utf-8', newline='') as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        fields = reader.fieldnames
    if not expected:
        if rows:
            raise ValueError('Unexpected nonempty CSV')
        return []
    if fields != list(expected[0]) or len(rows) != len(expected):
        raise ValueError('Retained CSV rows or columns differ')
    output = []
    for saved, target in zip(rows, expected):
        typed = {}
        for key, reference in target.items():
            value = saved[key]
            if reference is None:
                if value != '':
                    raise ValueError('Null CSV metric was filled')
                typed[key] = None
            elif isinstance(reference, bool):
                if value not in ('True', 'False'):
                    raise ValueError('Invalid CSV Boolean')
                typed[key] = value == 'True'
            elif isinstance(reference, int):
                typed[key] = int(value)
            elif isinstance(reference, float):
                typed[key] = float(value)
            else:
                typed[key] = value
        output.append(typed)
    return output


def verify(root, report, *, input_root=None):
    root, report = Path(root).resolve(), Path(report).resolve()
    settings, receipt = _verify_report(root, report)
    source_config = json.loads((root / 'config/m2.json').read_text())
    inputs = load_retained_inputs(root)
    validate_retained_inputs(inputs, validate_folds(settings['folds'], source_config))
    predictions = read_jsonl(report / 'predictions.jsonl')
    models = json.loads((report / 'models.json').read_text())
    validation = [r for r in inputs['matrices'] if r['fold_role'] == 'validation']
    expected_models = {(r['fold_id'], r['tenant_id'], method) for r in validation for method in settings['method_ids']}
    if _index(models, ('fold_id', 'tenant_id', 'method_id')).keys() != expected_models:
        raise ValueError('Retained model set differs from the frozen validation folds')
    actual = _prediction_rows(models, validation,
                             inputs['features'], inputs['transformers'], read_jsonl(report / 'promise_features.jsonl'))
    tolerances = {'absolute_tolerance': settings['replay']['absolute_probability_tolerance'],
                  'relative_tolerance': settings['replay']['relative_probability_tolerance']}
    compare_values(predictions, actual, **tolerances)
    expected = len(settings['method_ids']) * settings['expected_validation_opportunities_per_method']
    if len(actual) != expected:
        raise ValueError('Replay prediction opportunity count differs')
    targets_verified = metrics_verified = False
    if input_root is not None:
        verify_inputs(root, input_root, settings)
        inputs, snapshots, promises, outcomes = load_sources(root, input_root)
        settings = dict(settings, _source_config=source_config)
        rebuilt = _rebuild_training_rows(inputs, snapshots, outcomes, promises, settings)
        compare_values(read_jsonl(report / 'training_rows.jsonl'), rebuilt, **tolerances)
        from .delay_run import prepare_bound_promises
        compare_values(read_jsonl(report / 'promise_features.jsonl'), prepare_bound_promises(inputs, snapshots, promises), **tolerances)
        targets_verified = True
        if receipt['evaluation_executed']:
            rows, metrics, reliability, slices = evaluate_predictions({'predictions': actual}, inputs, snapshots, outcomes, promises, settings)
            compare_values(read_jsonl(report / 'evaluation_rows.jsonl'), rows, **tolerances)
            compare_values(json.loads((report / 'metrics.json').read_text()), metrics, **tolerances)
            compare_values(_csv_values(report / 'reliability.csv', reliability), reliability, **tolerances)
            compare_values(_csv_values(report / 'slices.csv', slices), slices, **tolerances)
            metrics_verified = True
    return {'status': 'passed', 'prediction_rows': len(actual), 'differences': 0,
            'outcomes_opened': input_root is not None, 'training_executed': False,
            'targets_verified': targets_verified, 'metrics_verified': metrics_verified,
            'calibration_used': False, 'final_test_evaluated': False, **tolerances}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--report', type=Path, default=Path('reports/m3c'))
    parser.add_argument('--inputs', type=Path, help='Optional verified M2 source root to rebuild labels and metrics without fitting')
    args = parser.parse_args()
    print(json.dumps(verify(args.root, args.report, input_root=args.inputs), indent=2))


if __name__ == '__main__':
    main()
