"""Read-only reconstruction of registered P09 outputs and retained parent lineage."""
import json
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from .registry import digest_file, inventory, verify_registration
from .inputs import load_parent, load_settings
from .engine import simulate_workflow
from .metrics import score_workflow
from .store import build_tables, verify_store
from .audit import audit_workflow
from .viewer import build_viewer, VERSION as VIEWER_VERSION
from .run import make_summary, require_passing_audit


def validate_receipt(receipt, registration_time):
    if (type(receipt.get('schema_version')) is not int or receipt['schema_version'] != 1
            or not isinstance(receipt.get('execution_git_head'), str)
            or re.fullmatch('[0-9a-f]{40}', receipt['execution_git_head']) is None
            or type(receipt.get('execution_checkout_changes_present')) is not bool):
        raise ValueError('P09 receipt version or execution identity is invalid')
    clocks = []
    for value in [registration_time, *[receipt.get(k) for k in
                  ('started_at_utc', 'materialized_at_utc', 'completed_at_utc')]]:
        if not isinstance(value, str):
            raise ValueError('P09 receipt clock is missing')
        try:
            clock = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError as error:
            raise ValueError('P09 receipt clock is invalid') from error
        if clock.tzinfo is None:
            raise ValueError('P09 receipt clock must include a timezone')
        clocks.append(clock)
    if clocks != sorted(clocks) or clocks[-1] > datetime.now(timezone.utc):
        raise ValueError('P09 receipt clock order is invalid')


def verify(root, report):
    root, report = Path(root).resolve(), Path(report).resolve()
    registration = verify_registration(root)
    before = inventory(report)
    receipt = json.loads((report / 'run_receipt.json').read_text())
    registered = json.loads((root / 'workflows/p09/registration.json').read_text())
    validate_receipt(receipt, registered['registered_at_utc'])
    if receipt.get('status') != 'completed_synthetic_operator_workflow':
        raise ValueError('P09 receipt is not a completed workflow')
    if receipt.get('registration_sha256') != registration['registration_sha256']:
        raise ValueError('P09 registration changed after the retained run')
    for key in ('quality_accepted', 'operational_approval', 'new_untouched_final_test', 'real_notifications_sent'):
        if receipt.get(key) is not False:
            raise ValueError('P09 receipt overclaims scope: ' + key)
    expected = receipt.get('output_sha256', {})
    # Supplemental report prose, a test log and its verification receipt are
    # separately hashed by the signed delivery manifest, not regenerated data.
    supplements = {'README.md', 'unittest_results.txt', 'verification.json', 'repeatability.json'}
    actual = {p: h for p, h in before.items() if p != 'run_receipt.json' and p not in supplements}
    if not expected or actual != expected:
        raise ValueError('P09 output file inventory or bytes changed')
    settings, inputs = load_settings(root), load_parent(root)
    if inputs['source_sha256'] != receipt.get('input_sha256'):
        raise ValueError('P09 parent source binding changed')
    if json.loads((report / 'input_manifest.json').read_text()) != inputs['source_sha256']:
        raise ValueError('P09 input manifest differs from parent evidence')
    simulation = simulate_workflow(inputs, settings)
    metrics = score_workflow(inputs, simulation, settings)
    audit = audit_workflow(inputs, simulation, metrics, settings)
    for name, value in [('simulation.json', simulation), ('metrics.json', metrics), ('independent_checks.json', audit)]:
        if json.loads((report / name).read_text()) != value:
            raise ValueError('Reconstructed workflow differs: ' + name)
    require_passing_audit(audit)
    tables = build_tables(root, inputs, simulation, receipt['materialized_at_utc'], settings)
    store = verify_store(root, report / 'store', tables, settings)
    if json.loads((report / 'store_verification.json').read_text()) != json.loads((report / 'store/verification.json').read_text()):
        raise ValueError('P09 store receipt copy differs')
    html = build_viewer(inputs, simulation, metrics, settings)
    if (report / 'viewer.html').read_text(encoding='utf-8') != html:
        raise ValueError('Read-only viewer differs from its registered data')
    encoded = html.encode('utf-8')
    viewer = {'status': 'written_read_only_viewer', 'version': VIEWER_VERSION,
              'bytes': len(encoded), 'sha256': hashlib.sha256(encoded).hexdigest()}
    if json.loads((report / 'summary.json').read_text()) != make_summary(registration, simulation, metrics, tables, viewer):
        raise ValueError('P09 summary differs from reconstructed facts')
    if inventory(report) != before or verify_registration(root) != registration:
        raise ValueError('Replay modified evidence or registered files')
    return {'status': 'passed_p09_replay', 'differences': 0,
            'registration': registration, 'store': store, 'independent_checks': audit,
            'read_only': True, 'quality_accepted': False, 'operational_approval': False}


def audit_report(root, report):
    # Always validate complete output hashes before reporting an audit result.
    result = verify(root, report)
    return result['independent_checks']
