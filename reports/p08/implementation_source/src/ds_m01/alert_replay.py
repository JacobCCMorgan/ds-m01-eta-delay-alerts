"""Read-only P06 policy, metric and typed-store replay.

Without --input-root, metadata comes from retained source context. With it,
the eight frozen source files independently rebuild that context and July
targets through the P05 source replay. Neither mode fits models or sends alerts.
"""
import argparse
import json
from pathlib import Path

from .common import digest_file, read_jsonl, parse_ts
from .calibration_replay import verify as verify_calibration
from .alert_inputs import (PROTOCOL_SHA256, verify_settings, verify_source_report, prepare_inputs,
                           bind_opportunities, build_registry)
from .alert_metrics import select_policy
from .release_store import build_tables, verify_store, CANONICAL, RESERVED, AUX
from .alert_run import code_hashes, summarize

CORE_FILES = {
    'opportunities.jsonl', 'targets.jsonl', 'source_context.json', 'model_registry.json',
    'selected_policy.json', 'policy_trials.json', 'selection_manifest.json', 'metrics.json',
    'decisions.jsonl', 'candidates.jsonl', 'episodes.jsonl', 'simulation_summary.json',
    'store_verification.json', 'source_replay.json', 'summary.json',
}


def _equal(a, b, name):
    if a != b:
        raise ValueError('Retained replay differs: ' + name)


def execution_clock(receipt):
    started, selected, completed = (parse_ts(receipt[key]) for key in
        ('started_at_utc', 'selection_completed_at_utc', 'completed_at_utc'))
    if not started <= selected <= completed:
        raise ValueError('Execution receipt timestamps are out of order')
    _equal(parse_ts(receipt['canonical_generated_at']), selected, 'canonical generation after selection')
    return receipt['canonical_generated_at']


def verify_upstream_receipt(retained, replayed):
    # The original writer always verifies source targets. A replay without an
    # input root checks this retained claim, but does not independently repeat it.
    expected = dict(replayed, source_targets_verified=True, outcome_source_opened=True)
    _equal(retained, expected, 'original P05 source replay receipt')


def verify_store_receipts(outer, inner, replayed):
    _equal(outer, inner, 'inner and outer store receipts')
    _equal(inner['counts'], replayed['counts'], 'store receipt counts')
    checks = replayed['checks'] + [{'name': 'parquet_roundtrip_all_tables', 'violations': 0, 'passed': True}]
    _equal(inner['checks'], checks, 'store receipt checks')
    for name in ('foreign_keys_enforced', 'parquet_roundtrip_verified'):
        _equal(inner[name], True, name)


def verify(root, report, *, input_root=None):
    root, report = Path(root).resolve(), Path(report).resolve()
    settings = verify_settings(root)
    parent = root / settings['source_report']
    verify_source_report(parent, settings)
    receipt = json.loads((report / 'run_receipt.json').read_text())
    generated_at = execution_clock(receipt)
    _equal(receipt['status'], 'completed_offline_policy_development', 'receipt status')
    _equal(receipt['protocol_sha256'], PROTOCOL_SHA256, 'protocol')
    _equal(receipt['base_public_commit'], settings['base_public_commit'], 'base commit')
    _equal(receipt['code_sha256'], code_hashes(root), 'executed code inventory')
    _equal(receipt['input_sha256'], settings['input_sha256'], 'input pins')
    _equal(receipt['parent_report_sha256'], settings['source_report_sha256'], 'P05 pins')
    for field in ('base_model_training_executed', 'calibrator_fitting_executed', 'final_test_evaluated',
                  'buffer_targets_used', 'notification_delivery_executed', 'quality_accepted'):
        _equal(receipt[field], False, field)
    names = set(CANONICAL) | set(RESERVED) | set(AUX)
    expected = CORE_FILES | {'store/experiment.duckdb', 'store/schema.sql', 'store/verification.json'}
    expected |= {'store/' + folder + '/' + name + suffix for name in names
                 for folder, suffix in (('jsonl', '.jsonl'), ('parquet', '.parquet'))}
    _equal(set(receipt['output_sha256']), expected, 'complete output inventory')
    for name, digest in receipt['output_sha256'].items():
        _equal(digest_file(report / name), digest, 'output hash ' + name)
    stored = {'opportunities': read_jsonl(report / 'opportunities.jsonl'),
              'targets': read_jsonl(report / 'targets.jsonl'),
              'registry': json.loads((report / 'model_registry.json').read_text()),
              'context': json.loads((report / 'source_context.json').read_text())}
    context = stored['context']
    _equal(set(context), {'snapshots', 'promises', 'memberships'}, 'source context inventory')
    parent_members = [r for r in read_jsonl(parent / 'memberships.jsonl') if r['role'] == 'assessment']
    _equal(context['memberships'], parent_members, 'assessment membership context')
    _equal(len({r['split_version'] for r in stored['registry']}), 1, 'split version count')
    registry = build_registry(parent, settings, stored['registry'][0]['split_version'])
    _equal(registry, stored['registry'], 'model registry')
    opportunities, targets = bind_opportunities(
        read_jsonl(parent / 'assessment_eta_rows.jsonl'), read_jsonl(parent / 'assessment_probability_rows.jsonl'),
        context['snapshots'], context['promises'], context['memberships'], registry, settings)
    _equal(opportunities, stored['opportunities'], 'outcome-free opportunities')
    _equal(targets, stored['targets'], 'separate target rows')
    upstream = verify_calibration(root, parent, input_root=input_root)
    verify_upstream_receipt(json.loads((report / 'source_replay.json').read_text()), upstream)
    if input_root is not None:
        rebuilt = prepare_inputs(root, input_root, settings)
        _equal(rebuilt, stored, 'independently reconstructed source context and targets')
    selection = select_policy(opportunities, targets, settings)
    for filename, value in (('selected_policy', selection['rule']), ('policy_trials', selection['trials']),
                            ('selection_manifest', selection['selection_manifest']),
                            ('metrics', selection['selected_metrics']), ('simulation_summary', selection['simulation']['summary'])):
        _equal(json.loads((report / (filename + '.json')).read_text()), value, filename)
    for name in ('decisions', 'candidates', 'episodes'):
        _equal(read_jsonl(report / (name + '.jsonl')), selection['simulation'][name], name)
    tables = build_tables(opportunities, selection, registry, generated_at, settings)
    store = verify_store(root, report / 'store', tables, settings)
    verify_store_receipts(json.loads((report / 'store_verification.json').read_text()),
                          json.loads((report / 'store/verification.json').read_text()), store)
    _equal(json.loads((report / 'summary.json').read_text()), summarize(stored, selection, tables, settings), 'summary')
    _equal(receipt['code_sha256'], code_hashes(root), 'code unchanged during replay')
    _equal(digest_file(root / 'config/p06.json'), PROTOCOL_SHA256, 'protocol unchanged during replay')
    verify_source_report(parent, settings)
    for name, digest in receipt['output_sha256'].items():
        _equal(digest_file(report / name), digest, 'output unchanged during replay: ' + name)
    return {'status': 'passed', 'protocol_sha256': PROTOCOL_SHA256, 'differences': 0,
            'opportunities': len(opportunities), 'monitored_trips': len({(r['tenant_id'], r['trip_id']) for r in opportunities}),
            'policy_trials': len(selection['trials']), 'admitted_candidates': len(selection['simulation']['candidates']),
            'canonical_tables': len(CANONICAL), 'reserved_tables': len(RESERVED),
            'source_context_verified': input_root is not None, 'july_source_targets_verified': input_root is not None,
            'retained_forecasts_verified': True, 'policy_selection_replayed': True,
            'typed_store_logical_rows_compared': store['logical_rows_compared'],
            'base_model_training_executed': False, 'calibrator_fitting_executed': False,
            'notification_delivery_executed': False, 'final_test_evaluated': False,
            'quality_accepted': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--report', type=Path, default=Path('reports/p06'))
    parser.add_argument('--input-root', type=Path)
    args = parser.parse_args()
    report = args.report if args.report.is_absolute() else args.root / args.report
    print(json.dumps(verify(args.root, report, input_root=args.input_root), indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
