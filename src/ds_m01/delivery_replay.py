"""Read-only replay of frozen synthetic delivery and typed output evidence."""
import argparse
import json
from pathlib import Path
from .common import digest_file, read_jsonl, parse_ts
from .alert_replay import verify as verify_policy
from .delivery_inputs import PROTOCOL_SHA256, require, verify_settings, verify_parent, verify_freeze, verify_evaluation_readiness, load_inputs
from .delivery_simulation import simulate_delivery
from .delivery_metrics import score_delivery
from .delivery_store import build_tables, verify_store, verify_store_receipts, ORDER
from .delivery_run import code_hashes, summarize

CORE_FILES = {'alerts.jsonl', 'attempts.jsonl', 'episodes.jsonl', 'events.jsonl',
              'simulation_summary.json', 'metrics.json', 'protocol_freeze.json',
              'parent_replay.json', 'store_verification.json', 'summary.json'}


def equal(a, b, what):
    require(json.dumps(a, sort_keys=True, allow_nan=False) == json.dumps(b, sort_keys=True, allow_nan=False),
            'Delivery replay differs: ' + what)


def execution_clock(receipt, freeze):
    started, generated, completed = [parse_ts(receipt[k]) for k in
        ('started_at_utc', 'materialized_at_utc', 'completed_at_utc')]
    require(parse_ts(freeze['frozen_at_utc']) <= started <= generated <= completed,
            'Freeze and execution timestamps are out of order')
    return receipt['materialized_at_utc']


def output_inventory():
    result = CORE_FILES | {'store/experiment.duckdb', 'store/schema.sql', 'store/verification.json'}
    return result | {'store/' + folder + '/' + table + suffix for table in ORDER
                     for folder, suffix in [('jsonl', '.jsonl'), ('parquet', '.parquet')]}


def verify(root, report, *, input_root=None):
    root, report = Path(root).resolve(), Path(report).resolve()
    settings = verify_settings(root)
    parent = verify_parent(root, settings)
    receipt = json.loads((report / 'run_receipt.json').read_text())
    freeze = verify_freeze(root, json.loads((report / 'protocol_freeze.json').read_text()))
    equal(freeze, json.loads((root / 'reproducibility/p07/protocol_freeze.json').read_text()), 'authoritative freeze')
    generated = execution_clock(receipt, freeze)
    equal(receipt['status'], 'completed_synthetic_delivery_development', 'receipt status')
    equal(receipt['protocol_sha256'], PROTOCOL_SHA256, 'protocol digest')
    equal(receipt['freeze_sha256'], digest_file(root / 'reproducibility/p07/protocol_freeze.json'), 'freeze digest')
    equal(receipt['base_public_commit'], settings['base_public_commit'], 'base commit')
    equal(receipt['code_sha256'], code_hashes(root), 'executed source inventory')
    equal(receipt['parent_report_sha256'], settings['source_report_sha256'], 'parent inventory')
    equal(receipt['input_sha256'], settings['input_sha256'], 'private source pins')
    equal(receipt['delivery_mode'], 'synthetic_scenario', 'synthetic delivery scope')
    for key in ('model_or_calibrator_refitted', 'policy_reselected', 'real_notifications_sent',
                'final_test_evaluated', 'quality_accepted'):
        require(receipt[key] is False, 'Receipt must declare false: ' + key)
    require(isinstance(receipt['july_source_targets_verified'], bool), 'Invalid source verification claim')
    equal(sorted(receipt['output_sha256']), sorted(output_inventory()), 'complete output inventory')
    for path, expected in receipt['output_sha256'].items():
        equal(digest_file(report / path), expected, 'output hash ' + path)
    upstream = verify_policy(root, parent, input_root=input_root)
    retained_upstream = json.loads((report / 'parent_replay.json').read_text())
    # The historical writer may have run either retained or source-assisted mode.
    original_source_mode = receipt['july_source_targets_verified']
    expected_upstream = dict(upstream, source_context_verified=original_source_mode,
                             july_source_targets_verified=original_source_mode)
    equal(retained_upstream, expected_upstream, 'historical parent replay scope')
    inputs = load_inputs(root, settings)
    simulation = simulate_delivery(inputs['candidates'], inputs['episodes'], settings)
    for key in ('alerts', 'attempts', 'episodes', 'events'):
        equal(read_jsonl(report / (key + '.jsonl')), simulation[key], key)
    equal(json.loads((report / 'simulation_summary.json').read_text()), simulation['summary'], 'simulation counts')
    metrics = score_delivery(inputs['opportunities'], inputs['targets'], inputs['candidates'], simulation, settings)
    equal(json.loads((report / 'metrics.json').read_text()), metrics, 'delivery metrics')
    tables = build_tables(inputs['parent_tables'], simulation, generated, settings)
    store = verify_store(root, report / 'store', tables, settings)
    outer = json.loads((report / 'store_verification.json').read_text())
    inner = json.loads((report / 'store/verification.json').read_text())
    verify_store_receipts(outer, inner, store)
    equal(json.loads((report / 'summary.json').read_text()), summarize(simulation, metrics, tables, settings), 'summary')
    verify_settings(root)
    verify_freeze(root, freeze)
    verify_evaluation_readiness(root)
    verify_parent(root, settings)
    equal(receipt['code_sha256'], code_hashes(root), 'source unchanged during replay')
    for path, expected in receipt['output_sha256'].items():
        equal(digest_file(report / path), expected, 'output unchanged ' + path)
    return {'status': 'passed', 'protocol_sha256': PROTOCOL_SHA256, 'differences': 0,
            'canonical_tables': len(settings['release']['canonical_tables']),
            'reserved_tables': len(settings['release']['remaining_reserved_tables']),
            'opportunities': len(inputs['opportunities']), 'candidate_alerts': len(simulation['alerts']),
            'risk_episodes': len(simulation['episodes']), 'attempts': len(simulation['attempts']),
            'typed_store_logical_rows_compared': store['logical_rows_compared'],
            'july_source_targets_verified': input_root is not None,
            'parent_policy_replayed': True, 'delivery_replayed': True, 'metrics_verified': True,
            'model_or_calibrator_refitted': False, 'policy_reselected': False,
            'real_notifications_sent': False, 'final_test_evaluated': False, 'quality_accepted': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--report', type=Path, default=Path('reports/p07'))
    p.add_argument('--input-root', type=Path)
    a = p.parse_args()
    report = a.report if a.report.is_absolute() else a.root / a.report
    print(json.dumps(verify(a.root, report, input_root=a.input_root), indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
