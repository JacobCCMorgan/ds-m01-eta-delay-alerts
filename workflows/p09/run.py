"""Publish an atomic, separately retained synthetic workflow demonstration."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile

from .registry import digest_file, inventory, verify_registration
from .inputs import load_parent, load_settings
from .engine import simulate_workflow
from .metrics import score_workflow
from .store import build_tables, write_store
from .audit import audit_workflow
from .viewer import write_viewer


def write_json(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def now():
    return datetime.now(timezone.utc).isoformat()


def make_summary(registration, simulation, metrics, tables, viewer):
    return {'status': 'completed_synthetic_operator_workflow',
            'parent': registration, 'source_scenario': 'S00_unmodified',
            'workflow': simulation['summary'], 'metrics': metrics,
            'canonical_counts': {k: len(tables[k]) for k in ('eta_adoption', 'eta_action', 'eta_action_followup')},
            'viewer': viewer, 'm4_2_engineering_scope_completed': True,
            'm4_3_read_only_viewer_implemented': True,
            'browser_render_validation': 'pending: browser rendering unavailable in the preparation environment',
            'real_recipient_observations': False, 'causal_benefit_estimated': False,
            'quality_accepted': False, 'operational_approval': False,
            'quality_limits': ['G14', 'G15', 'G16'],
            'scope': 'Post-P08 synthetic workflow demonstration; frozen arrivals, predictions and final-test scores are unchanged.'}


def require_passing_audit(audit):
    if (audit.get('status') != 'passed' or type(audit.get('failure_count')) is not int
            or audit['failure_count'] != 0 or audit.get('failures') != []):
        raise ValueError('Independent workflow audit failed')


def run(root, output):
    root, output = Path(root).resolve(), Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('Output already exists; retain it and choose a fresh directory')
    if output.resolve() == root or root.is_relative_to(output.resolve()):
        raise ValueError('Output must not contain the checkout')
    if output.resolve().is_relative_to(root) and output.resolve() != root / 'workflows/p09/evidence':
        raise ValueError('Use the reserved P09 evidence directory or a fresh path outside the checkout')
    registration = verify_registration(root)
    started = now()
    settings = load_settings(root)
    inputs = load_parent(root)
    simulation = simulate_workflow(inputs, settings)
    metrics = score_workflow(inputs, simulation, settings)
    audit = audit_workflow(inputs, simulation, metrics, settings)
    require_passing_audit(audit)
    generated = now()
    tables = build_tables(root, inputs, simulation, generated, settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Retained output lives beside registered code; staging evidence there would
    # make the exhaustive source inventory change during its own verification.
    staging_parent = root.parent if output.resolve().is_relative_to(root) else output.parent
    temporary = Path(tempfile.mkdtemp(prefix='.p09-attempt-', dir=staging_parent)).resolve()
    try:
        write_json(temporary / 'simulation.json', simulation)
        write_json(temporary / 'metrics.json', metrics)
        write_json(temporary / 'independent_checks.json', audit)
        write_json(temporary / 'input_manifest.json', inputs['source_sha256'])
        store = write_store(root, temporary / 'store', tables, settings)
        write_json(temporary / 'store_verification.json', store)
        viewer = write_viewer(temporary / 'viewer.html', inputs, simulation, metrics, settings)
        summary = make_summary(registration, simulation, metrics, tables, viewer)
        write_json(temporary / 'summary.json', summary)
        if verify_registration(root) != registration:
            raise ValueError('Parent/workflow registration changed during execution')
        if load_parent(root)['source_sha256'] != inputs['source_sha256']:
            raise ValueError('Parent input evidence changed during execution')
        head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
        write_json(temporary / 'run_receipt.json', {
            'schema_version': 1, 'status': 'completed_synthetic_operator_workflow',
            'execution_git_head': head, 'registration_sha256': registration['registration_sha256'],
            'execution_checkout_changes_present': bool(subprocess.check_output(
                ['git', '-C', str(root), 'status', '--porcelain'], text=True).strip()),
            'execution_git_head_scope': 'Observed checkout HEAD; executed workflow code is bound by registered file hashes.',
            'started_at_utc': started, 'materialized_at_utc': generated, 'completed_at_utc': now(),
            'input_sha256': inputs['source_sha256'], 'output_sha256': inventory(temporary),
            'runtime': {'python': platform.python_version(), 'system': platform.system(), 'machine': platform.machine()},
            'quality_accepted': False, 'operational_approval': False,
            'new_untouched_final_test': False, 'real_notifications_sent': False})
        if output.exists() or output.is_symlink():
            raise ValueError('Output appeared during execution; existing evidence retained')
        os.rename(temporary, output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return summary
