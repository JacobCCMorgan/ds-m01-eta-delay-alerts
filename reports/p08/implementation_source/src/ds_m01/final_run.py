"""Execute the frozen P08 final evaluation without refitting or selecting models."""
import argparse
import importlib.util
import json
import os
import platform
import shutil
import tempfile
from pathlib import Path

from .common import digest_file, read_jsonl, write_jsonl, parse_ts
from .delivery_inputs import require
from .final_gate import OUTCOME_PATH, utc_now, verify_pins, verify_registered, verify_correction_history, write_json


FORECAST_FILES = {'eta_rows': 'forecast_eta', 'probability_rows': 'forecast_probability'}
LABEL_FREE_SCENARIO_FILES = ('forecast_eta.jsonl', 'forecast_probability.jsonl',
    'opportunities.jsonl', 'model_registry.jsonl', 'matrices.jsonl', 'features.jsonl',
    'projection_audit.jsonl', 'stress_audit.jsonl', 'stress_summary.json', 'policy_delivery.json')


def inventory(directory):
    directory = Path(directory)
    return {str(p.relative_to(directory)): digest_file(p)
            for p in sorted(directory.rglob('*')) if p.is_file()}


def save_payload(directory, payload):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in payload.items():
        filename = FORECAST_FILES.get(name, name)
        if isinstance(value, list):
            write_jsonl(directory / (filename + '.jsonl'), value)
        else:
            write_json(directory / (filename + '.json'), value)


def read_payload(directory, *, forecasts=False):
    directory = Path(directory)
    result = {p.stem: read_jsonl(p) for p in sorted(directory.glob('*.jsonl'))}
    result.update({p.stem: json.loads(p.read_text()) for p in sorted(directory.glob('*.json'))})
    if forecasts:
        for key, name in FORECAST_FILES.items():
            if name in result:
                result[key] = result.pop(name)
    return result


def build_scenario(inputs, states, source_config, settings, scenario_id):
    from .final_inference import predict
    from .final_policy import run_policy_delivery
    forecasts = predict(inputs, states, source_config, settings, scenario_id)
    bundle = run_policy_delivery(forecasts['opportunities'], settings, scenario_id)
    return forecasts, bundle


def score_scenario(forecasts, targets, bundle, settings, scenario_id):
    from .final_metrics import attach_targets, build_metrics
    from .final_delivery_metrics import score_policy_delivery
    eta = attach_targets(forecasts['eta_rows'], targets['eta_targets'], 'eta')
    probability = attach_targets(forecasts['probability_rows'], targets['probability_targets'], 'probability')
    return {'forecast_metrics': build_metrics(eta, probability, settings),
            'policy_delivery_metrics': score_policy_delivery(forecasts['opportunities'],
                targets['probability_targets'], bundle, settings, scenario_id)}


def independent_check(root, report, input_root=None):
    name = '_p08_independent_reference'
    spec = importlib.util.spec_from_file_location(name, Path(root) / 'scripts/check_final_evaluation.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify_report(root, report, input_root=input_root)


def open_final_outcomes(input_root, settings, inputs, attempt, pretarget):
    """The sole final-outcome reader runs only after durable label-free evidence."""
    attempt = Path(attempt)
    receipt_path = attempt / 'pretarget_receipt.json'
    require(receipt_path.is_file(), 'The pre-target receipt must exist before outcome access')
    require(json.loads(receipt_path.read_text()) == pretarget, 'Pre-target receipt differs')
    require(pretarget.get('status') == 'all_label_free_scenarios_retained_before_target_access',
            'All label-free scenarios must be complete before target access')
    for name in ('final_outcomes_opened', 'quality_accepted', 'model_or_calibrator_refitted', 'policy_reselected'):
        require(pretarget.get(name) is False, 'Invalid pre-target scope: ' + name)
    scenario_ids = [s['id'] for s in settings['stress']['scenarios']]
    require(pretarget.get('scenario_ids') == scenario_ids and len(set(scenario_ids)) == 9,
            'The complete nine-scenario inventory must precede target access')
    require(pretarget.get('metadata_inventory') == inputs['inventory'], 'Pre-target metadata inventory differs')
    required = {'implementation_freeze.json', 'inventory.json', 'inputs/memberships.jsonl',
                'inputs/snapshots.jsonl', 'inputs/features.jsonl', 'inputs/promise_features.jsonl',
                'inputs/promises.jsonl', 'inputs/inventory.json', 'inputs/source_sha256.json'}
    required |= {'scenarios/' + sid + '/' + name for sid in scenario_ids for name in LABEL_FREE_SCENARIO_FILES}
    require(isinstance(pretarget.get('output_sha256'), dict) and required <= set(pretarget['output_sha256']),
            'Pre-target artifact inventory is incomplete')
    for relative, expected in pretarget['output_sha256'].items():
        path = (attempt / relative).resolve()
        require(path.is_relative_to(attempt.resolve()) and not (attempt / relative).is_symlink(),
                'Pre-target artifact path escapes its attempt')
        require(digest_file(attempt / relative) == expected, 'Label-free output changed: ' + relative)
    record = {'status': 'final_outcome_access_started', 'started_at_utc': utc_now(),
              'pretarget_receipt_sha256': digest_file(receipt_path),
              'expected_source_sha256': settings['input_sha256'][OUTCOME_PATH],
              'scope': 'Final outcomes are now exposed. Any correction must retain this attempt and disclose prior exposure.'}
    # Exclusive creation leaves a durable event even when reading or scoring fails.
    with (attempt / 'exposure_record.json').open('x') as stream:
        stream.write(json.dumps(record, sort_keys=True, indent=2) + '\n')
    source = Path(input_root) / OUTCOME_PATH
    require(digest_file(source) == settings['input_sha256'][OUTCOME_PATH], 'Frozen final outcome source changed')
    trip_keys = {(r['tenant_id'], r['trip_id']) for r in inputs['memberships']}
    outcomes = [r for r in read_jsonl(source) if (r['tenant_id'], r['trip_id']) in trip_keys]
    require(digest_file(source) == settings['input_sha256'][OUTCOME_PATH], 'Final outcome source changed during read')
    write_jsonl(attempt / 'outcome_source.jsonl', outcomes)
    return outcomes


def run(root, input_root, output, implementation_freeze, *, prior_attempt=None):
    from .final_inputs import prepare_inputs
    from .final_inference import load_states
    from .final_targets import build_final_targets
    from .final_store import build_tables, write_store
    root, input_root = Path(root).resolve(), Path(input_root).resolve()
    output = Path(output).absolute()
    require(not output.exists() and not output.is_symlink(), 'Output exists; retain it and choose a fresh path')
    settings = verify_pins(root)
    frozen = json.loads(Path(implementation_freeze).read_text())
    started = utc_now()
    verify_registered(root, frozen, run_started_at=started)
    require(parse_ts(frozen['recorded_at_utc']) <= parse_ts(started), 'Implementation freeze must precede execution')
    if prior_attempt is not None:
        prior_attempt = Path(prior_attempt).resolve()
        require((prior_attempt / 'exposure_record.json').is_file(), 'Prior exposed attempt is missing')
        require(frozen['status'] == 'registered_correction_after_final_target_exposure',
                'A correction requires an explicitly disclosed post-exposure registration')
        verify_correction_history(frozen, prior_attempt)
    require(not frozen.get('final_outcomes_accessed') or prior_attempt is not None,
            'A post-exposure correction must name the preserved prior attempt')
    output.parent.mkdir(parents=True, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix='.' + output.name + '-attempt-', dir=output.parent))
    try:
        inputs = prepare_inputs(root, input_root, settings)
        require(inputs['inventory'] == frozen['metadata_inventory'], 'Frozen final metadata inventory differs')
        save_payload(attempt / 'inputs', inputs)
        write_json(attempt / 'inventory.json', inputs['inventory'])
        write_json(attempt / 'implementation_freeze.json', frozen)
        if prior_attempt is not None:
            for path, expected in frozen['correction']['prior_evidence_sha256'].items():
                retained = attempt / 'correction_history' / path
                retained.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(prior_attempt / path, retained)
                require(digest_file(retained) == expected, 'Correction predecessor changed: ' + path)
        for path, expected in frozen['source_sha256'].items():
            retained = attempt / 'implementation_source' / path
            retained.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / path, retained)
            require(digest_file(retained) == expected, 'Registered source snapshot differs: ' + path)
        # A newly created output attempt must not contaminate historical pins.
        # Check this before expensive inference as well as before target access.
        verify_registered(root, frozen)
        states = load_states(root, settings)
        source_config = json.loads((root / 'config/m2.json').read_text())
        scenarios = {}
        for case in settings['stress']['scenarios']:
            scenario_id = case['id']
            forecasts, bundle = build_scenario(inputs, states, source_config, settings, scenario_id)
            destination = attempt / 'scenarios' / scenario_id
            save_payload(destination, forecasts)
            write_json(destination / 'policy_delivery.json', bundle)
            scenarios[scenario_id] = forecasts, bundle
        verify_registered(root, frozen)
        pretarget = {'status': 'all_label_free_scenarios_retained_before_target_access',
                     'completed_at_utc': utc_now(), 'scenario_ids': list(scenarios),
                     'metadata_inventory': inputs['inventory'], 'output_sha256': inventory(attempt),
                     'final_outcomes_opened': False, 'quality_accepted': False,
                     'model_or_calibrator_refitted': False, 'policy_reselected': False}
        write_json(attempt / 'pretarget_receipt.json', pretarget)
        outcomes = open_final_outcomes(input_root, settings, inputs, attempt, pretarget)
        if prior_attempt is not None:
            # Keep the entire first result/failure and its source snapshot. Target
            # bytes enter this history only after this run's pre-target gate.
            shutil.copytree(prior_attempt, attempt / 'prior_attempt')
        targets = build_final_targets(inputs['snapshots'], outcomes, inputs['promises'], settings)
        save_payload(attempt, targets)
        materialized = utc_now()
        for scenario_id, (forecasts, bundle) in scenarios.items():
            destination = attempt / 'scenarios' / scenario_id
            scores = score_scenario(forecasts, targets, bundle, settings, scenario_id)
            save_payload(destination, scores)
            tables = build_tables(forecasts['opportunities'], forecasts['model_registry'], bundle,
                                  materialized, settings)
            store = write_store(root, destination / 'store', tables, settings)
            write_json(destination / 'store_verification.json', store)
        summary = {'status': 'evaluation_complete_pending_independent_verification',
                   'primary_scenario': 'S00_unmodified', 'scenario_ids': list(scenarios),
                   'population': inputs['inventory'], 'final_test_evaluated': True,
                   'quality_accepted': False, 'operational_approval': False,
                   'model_or_calibrator_refitted': False, 'policy_reselected': False,
                   'real_notifications_sent': False, 'adoption_or_action_simulated': False,
                   'p09_implemented': False, 'prior_exposure_disclosed': prior_attempt is not None,
                   'interpretation': 'Fixed synthetic final evaluation; all scenarios are retained. Poor quality does not justify tuning, changing the population, or replacing the primary scenario.'}
        write_json(attempt / 'summary.json', summary)
        draft_receipt = {'status': 'evaluation_complete_pending_independent_verification',
                         'started_at_utc': started, 'materialized_at_utc': materialized,
                         'implementation_freeze_sha256': digest_file(attempt / 'implementation_freeze.json'),
                         'pretarget_receipt_sha256': digest_file(attempt / 'pretarget_receipt.json'),
                         'source_sha256': settings['input_sha256'],
                         'prior_attempt': 'prior_attempt' if prior_attempt else None,
                         'runtime': {'python': platform.python_version(), 'system': platform.system(),
                                     'machine': platform.machine()},
                         'quality_accepted': False, 'operational_approval': False}
        write_json(attempt / 'run_receipt.json', draft_receipt)
        independent = independent_check(root, attempt, input_root=input_root)
        require(independent.get('status') == 'passed' and type(independent.get('failures')) is int
                and independent['failures'] == 0 and type(independent.get('comparisons')) is int
                and independent['comparisons'] > 0, 'Independent final evaluation check failed')
        write_json(attempt / 'independent_checks.json', independent)
        verify_registered(root, frozen)
        summary['status'] = 'engineering_complete_final_evaluation'
        write_json(attempt / 'summary.json', summary)
        draft_receipt.update(status=summary['status'], completed_at_utc=utc_now())
        draft_receipt['output_sha256'] = {k: v for k, v in inventory(attempt).items() if k != 'run_receipt.json'}
        write_json(attempt / 'run_receipt.json', draft_receipt)
        # This verification rebuilds all forecasts, policy/transport and metrics
        # while the result is still a private attempt, before atomic publication.
        from .final_replay import verify as replay
        replay_receipt = replay(root, attempt)
        write_json(attempt / 'retained_replay.json', replay_receipt)
        draft_receipt['output_sha256'] = {k: v for k, v in inventory(attempt).items() if k != 'run_receipt.json'}
        write_json(attempt / 'run_receipt.json', draft_receipt)
        require(not output.exists() and not output.is_symlink(), 'Output appeared; prior evidence preserved')
        os.rename(attempt, output)
        return summary
    except BaseException as exc:
        write_json(attempt / 'failure_record.json', {'status': 'failed_evaluation_attempt',
            'recorded_at_utc': utc_now(), 'exception_type': type(exc).__name__, 'message': str(exc),
            'final_outcomes_exposed': (attempt / 'exposure_record.json').exists(),
            'scope': 'Retained first attempt; not a successful result. Preserve this directory for any correction.'})
        raise RuntimeError('P08 attempt failed; evidence retained at ' + str(attempt)) from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--input-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--implementation-freeze', required=True, type=Path)
    parser.add_argument('--prior-attempt', type=Path)
    args = parser.parse_args()
    result = run(args.root, args.input_root, args.output, args.implementation_freeze,
                 prior_attempt=args.prior_attempt)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
