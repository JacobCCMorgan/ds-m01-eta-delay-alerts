"""Rebuild every retained P08 scenario without fitting or changing its protocol."""
import argparse
import json
import math
from pathlib import Path

from .common import digest_file, read_jsonl
from .delivery_inputs import require
from .final_gate import verify_pins, verify_registered, verify_correction_history
from .final_run import build_scenario, read_payload, score_scenario


def compare(expected, actual, path='', *, tolerance=None):
    """Exact structure/counts/identities; only retained floating values use tolerances."""
    tolerance = tolerance or {'absolute': 1e-10, 'relative': 1e-9}
    if isinstance(expected, dict):
        require(isinstance(actual, dict) and expected.keys() == actual.keys(), 'Replay keys differ: ' + path)
        return sum(compare(expected[k], actual[k], path + '/' + k, tolerance=tolerance) for k in expected)
    if isinstance(expected, list):
        require(isinstance(actual, list) and len(expected) == len(actual), 'Replay row count differs: ' + path)
        return sum(compare(a, b, path + '/' + str(i), tolerance=tolerance)
                   for i, (a, b) in enumerate(zip(expected, actual)))
    if isinstance(expected, float):
        require(isinstance(actual, (int, float)) and not isinstance(actual, bool)
                and math.isfinite(expected) and math.isfinite(actual)
                and math.isclose(expected, actual, abs_tol=tolerance['absolute'], rel_tol=tolerance['relative']),
                'Replay floating value differs: ' + path)
    else:
        require(type(expected) is type(actual) and expected == actual, 'Replay exact value differs: ' + path)
    return 1


def verify(root, report, *, input_root=None):
    from .final_inputs import prepare_inputs
    from .final_inference import load_states
    from .final_targets import build_final_targets
    from .final_store import build_tables, verify_store
    root, report = Path(root).resolve(), Path(report).resolve()
    settings = verify_pins(root)
    receipt = json.loads((report / 'run_receipt.json').read_text())
    require(receipt['status'] == 'engineering_complete_final_evaluation', 'Final run is not complete')
    for path, expected in receipt['output_sha256'].items():
        require(digest_file(report / path) == expected, 'Retained final output changed: ' + path)
    freeze = json.loads((report / 'implementation_freeze.json').read_text())
    verify_registered(root, freeze, run_started_at=receipt['started_at_utc'])
    if freeze['status'] == 'registered_correction_after_final_target_exposure':
        verify_correction_history(freeze, report / 'correction_history')
        verify_correction_history(freeze, report / 'prior_attempt')
    require(digest_file(report / 'implementation_freeze.json') == receipt['implementation_freeze_sha256'],
            'Implementation freeze differs from run receipt')
    pretarget = json.loads((report / 'pretarget_receipt.json').read_text())
    require(digest_file(report / 'pretarget_receipt.json') == receipt['pretarget_receipt_sha256'],
            'Pre-target receipt differs from run receipt')
    for path, expected in pretarget['output_sha256'].items():
        require(digest_file(report / path) == expected, 'Label-free retained artifact changed: ' + path)
    stored_inputs = read_payload(report / 'inputs')
    inputs = prepare_inputs(root, input_root, settings) if input_root is not None else stored_inputs
    comparisons = compare(stored_inputs, inputs, 'inputs', tolerance=settings['comparison_tolerance'])
    require(inputs['inventory'] == freeze['metadata_inventory'] == pretarget['metadata_inventory'],
            'Final inventory differs from its pre-target freeze')
    outcomes = read_jsonl(report / 'outcome_source.jsonl')
    source_bound = False
    if input_root is not None:
        from .final_gate import OUTCOME_PATH
        source = Path(input_root) / OUTCOME_PATH
        require(digest_file(source) == settings['input_sha256'][OUTCOME_PATH], 'Final source outcome hash differs')
        trips = {(r['tenant_id'], r['trip_id']) for r in inputs['memberships']}
        selected = [r for r in read_jsonl(source) if (r['tenant_id'], r['trip_id']) in trips]
        comparisons += compare(outcomes, selected, 'outcome_source', tolerance=settings['comparison_tolerance'])
        source_bound = True
    targets = build_final_targets(inputs['snapshots'], outcomes, inputs['promises'], settings)
    for name, rows in targets.items():
        comparisons += compare(read_jsonl(report / (name + '.jsonl')), rows, name,
                               tolerance=settings['comparison_tolerance'])
    states = load_states(root, settings)
    source_config = json.loads((root / 'config/m2.json').read_text())
    stores = {}
    for case in settings['stress']['scenarios']:
        sid = case['id']
        directory = report / 'scenarios' / sid
        retained = read_payload(directory, forecasts=True)
        forecasts, bundle = build_scenario(inputs, states, source_config, settings, sid)
        for name, rows in forecasts.items():
            comparisons += compare(retained[name], rows, sid + '/' + name, tolerance=settings['comparison_tolerance'])
        comparisons += compare(retained['policy_delivery'], bundle, sid + '/policy_delivery',
                               tolerance=settings['comparison_tolerance'])
        scores = score_scenario(forecasts, targets, bundle, settings, sid)
        for name, metrics in scores.items():
            comparisons += compare(retained[name], metrics, sid + '/' + name, tolerance=settings['comparison_tolerance'])
        tables = build_tables(forecasts['opportunities'], forecasts['model_registry'], bundle,
                              receipt['materialized_at_utc'], settings)
        stores[sid] = verify_store(root, directory / 'store', tables, settings)
    verify_registered(root, freeze)
    return {'status': 'passed', 'differences': 0, 'logical_comparisons': comparisons,
            'scenario_count': len(settings['stress']['scenarios']), 'source_outcomes_bound': source_bound,
            'mode': 'source_assisted_final_replay' if source_bound else 'retained_final_replay',
            'stores': stores, 'model_or_calibrator_refitted': False, 'policy_reselected': False,
            'scope': 'Reproduces the retained final evaluation after outcome exposure; not a new untouched final test.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--input-root', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = verify(args.root, args.report, input_root=args.input_root)
    if args.output:
        from .final_gate import write_json
        require(not args.output.exists(), 'Replay receipt exists; choose a fresh path')
        write_json(args.output, result)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
