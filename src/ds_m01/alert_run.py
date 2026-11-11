"""Develop an offline alert policy and publish typed release evidence atomically.

July is policy-development data. This command does not train estimators, alter
calibrators, inspect final-test outcomes or deliver any notification.
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from .common import digest_file, write_jsonl
from .calibration_replay import verify as verify_calibration
from .calibration_run import CODE_MODULES as P05_CODE_MODULES
from .alert_inputs import PROTOCOL_SHA256, verify_settings, prepare_inputs, verify_input_files, verify_source_report
from .alert_metrics import select_policy
from .release_store import build_tables, write_store

CODE_MODULES = sorted(set(P05_CODE_MODULES) | {
    'alert_inputs.py', 'alert_policy.py', 'alert_metrics.py', 'release_store.py',
    'alert_run.py', 'alert_replay.py', 'calibration_replay.py', 'delay_replay.py', 'common.py', 'candidate_model.py'})


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + '\n')


def code_hashes(root):
    root = Path(root)
    result = {}
    for name in CODE_MODULES:
        retained, executed = root / 'src/ds_m01' / name, Path(__file__).parent / name
        a, b = digest_file(retained), digest_file(executed)
        if a != b:
            raise ValueError('Executed code differs from requested repository: ' + name)
        result[name] = a
    return result


def inventory(directory):
    directory = Path(directory)
    return {str(p.relative_to(directory)): digest_file(p) for p in sorted(directory.rglob('*')) if p.is_file()}


def summarize(inputs, selection, tables, settings):
    targets, opportunities = inputs['targets'], inputs['opportunities']
    return {
        'status': 'completed_offline_policy_development',
        'population': {'opportunities': len(opportunities),
                       'monitored_trips': len({(r['tenant_id'], r['trip_id']) for r in opportunities}),
                       'eligible_target_snapshots': sum(r['eligibility'] == 'eligible' for r in targets),
                       'eligible_target_trips': len({(r['tenant_id'], r['trip_id']) for r in targets if r['eligibility'] == 'eligible'})},
        'selection_status': selection['status'], 'selected_rule': selection['rule'],
        'trials_retained': len(selection['trials']), 'metrics': selection['selected_metrics'],
        'canonical_table_counts': {name: len(tables[name]) for name in settings['release']['canonical_tables']},
        'reserved_table_counts': {name: len(tables[name]) for name in settings['release']['remaining_reserved_tables']},
        'auxiliary_table_counts': {name: len(rows) for name, rows in tables.items()
                                   if name not in settings['release']['canonical_tables'] + settings['release']['remaining_reserved_tables']},
        'budget_assumptions': {'candidates_per_100_monitored_trips': 50, 'daily_candidates_per_tenant': 2,
                              'measured_fleet_capacity': False},
        'release_mode': 'offline_replay', 'independent_policy_test': False,
        'base_model_training_executed': False, 'calibrator_fitting_executed': False,
        'notification_delivery_executed': False, 'final_test_evaluated': False,
        'quality_limits': settings['quality_limits'],
    }


def run(root, input_root, output):
    root, input_root, output = Path(root).resolve(), Path(input_root).resolve(), Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('Output already exists; choose a new path and retain previous evidence')
    settings = verify_settings(root)
    codes = code_hashes(root)
    started = datetime.now(timezone.utc).isoformat()
    source_report = root / settings['source_report']
    upstream = verify_calibration(root, source_report, input_root=input_root)
    inputs = prepare_inputs(root, input_root, settings)
    # The full chronological policy sweep begins only after binding all inputs.
    selection = select_policy(inputs['opportunities'], inputs['targets'], settings)
    selection_at = datetime.now(timezone.utc).isoformat()
    tables = build_tables(inputs['opportunities'], selection, inputs['registry'], selection_at, settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix='.' + output.name + '-', dir=output.parent))
    try:
        for name in ('opportunities', 'targets'):
            write_jsonl(staged / (name + '.jsonl'), inputs[name])
        write_json(staged / 'source_context.json', inputs['context'])
        write_json(staged / 'model_registry.json', inputs['registry'])
        for name, value in (('selected_policy', selection['rule']), ('policy_trials', selection['trials']),
                            ('selection_manifest', selection['selection_manifest']),
                            ('metrics', selection['selected_metrics'])):
            write_json(staged / (name + '.json'), value)
        for name in ('decisions', 'candidates', 'episodes'):
            write_jsonl(staged / (name + '.jsonl'), selection['simulation'][name])
        write_json(staged / 'simulation_summary.json', selection['simulation']['summary'])
        store_verification = write_store(root, staged / 'store', tables, settings)
        write_json(staged / 'store_verification.json', store_verification)
        write_json(staged / 'source_replay.json', upstream)
        summary = summarize(inputs, selection, tables, settings)
        write_json(staged / 'summary.json', summary)
        verify_input_files(input_root, settings)
        verify_source_report(source_report, settings)
        if codes != code_hashes(root) or digest_file(root / 'config/p06.json') != PROTOCOL_SHA256:
            raise ValueError('Policy code or protocol changed during execution')
        completed = datetime.now(timezone.utc).isoformat()
        head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
        receipt = {
            'status': 'completed_offline_policy_development', 'protocol_sha256': PROTOCOL_SHA256,
            'base_public_commit': settings['base_public_commit'], 'execution_git_head': head,
            'started_at_utc': started, 'canonical_generated_at': selection_at,
            'selection_completed_at_utc': selection_at, 'completed_at_utc': completed,
            'code_sha256': codes, 'input_sha256': settings['input_sha256'],
            'parent_report_sha256': settings['source_report_sha256'], 'output_sha256': inventory(staged),
            'population_role': 'July policy development; reused P05 assessment',
            'base_model_training_executed': False, 'calibrator_fitting_executed': False,
            'final_test_evaluated': False, 'buffer_targets_used': False,
            'notification_delivery_executed': False, 'quality_accepted': False,
            'runtime': {'python': platform.python_version(), 'system': platform.system(), 'machine': platform.machine(),
                        'packages': {name: version(name) for name in ('numpy', 'scipy', 'scikit-learn', 'duckdb', 'pyarrow')}},
        }
        write_json(staged / 'run_receipt.json', receipt)
        if output.exists() or output.is_symlink():
            raise ValueError('Output appeared during execution; existing evidence was not overwritten')
        os.rename(staged, output)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--input-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.input_root, args.output), indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
