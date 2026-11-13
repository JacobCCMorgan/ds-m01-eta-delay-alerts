"""Replay frozen P06 candidates through synthetic delivery; never contact recipients."""
import argparse
import json
import os
import platform
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from .common import digest_file, write_jsonl, parse_ts
from .alert_run import CODE_MODULES as P06_CODE_MODULES
from .alert_replay import verify as verify_policy
from .delivery_inputs import PROTOCOL_SHA256, require, verify_settings, verify_parent, verify_freeze, load_inputs
from .delivery_simulation import simulate_delivery
from .delivery_metrics import score_delivery
from .delivery_store import build_tables, write_store

CODE_MODULES = sorted(set(P06_CODE_MODULES) | {'delivery_inputs.py', 'delivery_simulation.py',
    'delivery_metrics.py', 'delivery_store.py', 'delivery_run.py', 'delivery_replay.py'})


def write_json(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n')


def code_hashes(root):
    result = {}
    for name in CODE_MODULES:
        expected = digest_file(Path(root) / 'src/ds_m01' / name)
        require(expected == digest_file(Path(__file__).parent / name), 'Executed code differs: ' + name)
        result[name] = expected
    return result


def inventory(directory):
    directory = Path(directory)
    return {str(p.relative_to(directory)): digest_file(p) for p in sorted(directory.rglob('*')) if p.is_file()}


def summarize(simulation, metrics, tables, settings):
    return {'status': 'completed_synthetic_delivery_development',
            'simulation': simulation['summary'], 'metrics': metrics,
            'canonical_table_counts': {name: len(tables[name]) for name in settings['release']['canonical_tables']},
            'reserved_table_counts': {name: len(tables[name]) for name in settings['release']['remaining_reserved_tables']},
            'quality_limits': settings['quality_limits'],
            'model_or_calibrator_refitted': False, 'policy_reselected': False,
            'real_notifications_sent': False, 'recipient_opening_simulated': False,
            'adoption_or_action_simulated': False, 'final_test_evaluated': False,
            'm4_2_completed': False, 'p08_evaluation_protocol_frozen': True}


def run(root, output, *, input_root=None):
    root, output = Path(root).resolve(), Path(output).absolute()
    require(not output.exists() and not output.is_symlink(), 'Output exists; retain it and choose a fresh path')
    settings = verify_settings(root)
    codes = code_hashes(root)
    freeze_path = root / 'reproducibility/p07/protocol_freeze.json'
    freeze = verify_freeze(root, json.loads(freeze_path.read_text()))
    freeze_digest = digest_file(freeze_path)
    started = datetime.now(timezone.utc).isoformat()
    require(parse_ts(freeze['frozen_at_utc']) <= parse_ts(started), 'Freeze must precede execution')
    # Replaying the parent's completed selection verifies its receipt. No new
    # delivery outcome is fed back into selection and no parameter is changed.
    upstream = verify_policy(root, root / settings['source_report'], input_root=input_root)
    inputs = load_inputs(root, settings)
    simulation = simulate_delivery(inputs['candidates'], inputs['episodes'], settings)
    metrics = score_delivery(inputs['opportunities'], inputs['targets'], inputs['candidates'], simulation, settings)
    generated = datetime.now(timezone.utc).isoformat()
    tables = build_tables(inputs['parent_tables'], simulation, generated, settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.' + output.name + '-', dir=output.parent))
    try:
        for name in ('alerts', 'attempts', 'episodes', 'events'):
            write_jsonl(temporary / (name + '.jsonl'), simulation[name])
        write_json(temporary / 'simulation_summary.json', simulation['summary'])
        write_json(temporary / 'metrics.json', metrics)
        write_json(temporary / 'protocol_freeze.json', freeze)
        write_json(temporary / 'parent_replay.json', upstream)
        store = write_store(root, temporary / 'store', tables, settings)
        write_json(temporary / 'store_verification.json', store)
        summary = summarize(simulation, metrics, tables, settings)
        write_json(temporary / 'summary.json', summary)
        verify_parent(root, settings)
        verify_settings(root)
        verify_freeze(root, freeze)
        require(codes == code_hashes(root), 'Executed code changed during run')
        require(digest_file(freeze_path) == freeze_digest, 'Freeze record changed during run')
        completed = datetime.now(timezone.utc).isoformat()
        write_json(temporary / 'run_receipt.json', {
            'status': 'completed_synthetic_delivery_development', 'protocol_sha256': PROTOCOL_SHA256,
            'freeze_sha256': freeze_digest, 'base_public_commit': settings['base_public_commit'],
            'execution_git_head': subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip(),
            'started_at_utc': started, 'materialized_at_utc': generated, 'completed_at_utc': completed,
            'code_sha256': codes, 'parent_report_sha256': settings['source_report_sha256'],
            'input_sha256': settings['input_sha256'], 'output_sha256': inventory(temporary),
            'july_source_targets_verified': input_root is not None,
            'delivery_mode': 'synthetic_scenario', 'simulation_clock': 'historical July; counterfactual offline events',
            'model_or_calibrator_refitted': False, 'policy_reselected': False,
            'real_notifications_sent': False, 'final_test_evaluated': False, 'quality_accepted': False,
            'runtime': {'python': platform.python_version(), 'system': platform.system(), 'machine': platform.machine()}})
        require(not output.exists() and not output.is_symlink(), 'Output appeared; prior evidence preserved')
        os.rename(temporary, output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--input-root', type=Path)
    a = p.parse_args()
    print(json.dumps(run(a.root, a.output, input_root=a.input_root), indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
