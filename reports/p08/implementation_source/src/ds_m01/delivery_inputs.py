"""Bind the published P06 development stream without changing its selected rule."""
import json
from pathlib import Path
from .common import digest_file, read_jsonl, parse_ts

PROTOCOL_SHA256 = '589d96fe09e60059eafa1f984a578098e52880c6c114dbb8621a6f7ce40c3399'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify_settings(root):
    root = Path(root)
    require(digest_file(root / 'config/p07.json') == PROTOCOL_SHA256, 'P07 protocol changed')
    settings = json.loads((root / 'config/p07.json').read_text())
    require(digest_file(root / 'config/p06.json') == settings['source_protocol_sha256'], 'P06 protocol changed')
    require(digest_file(root / 'contracts/fields.json') == settings['field_contract_sha256'], 'Field contract changed')
    require(settings['policy']['reselection'] is False, 'Policy must remain frozen')
    return settings


def verify_parent(root, settings):
    parent = Path(root) / settings['source_report']
    for name, expected in settings['source_report_sha256'].items():
        require(digest_file(parent / name) == expected, 'P06 source changed: ' + name)
    return parent


def verify_freeze(root, freeze):
    """A retained freeze is a dated, hash-bound protocol record, not a blind-test claim."""
    root = Path(root)
    require(freeze['status'] == 'frozen_before_delivery_run_and_final_test', 'Invalid freeze status')
    parse_ts(freeze['frozen_at_utc'])
    require(freeze['final_test_evaluated'] is False, 'Final outcomes are still protected')
    require(freeze['july_assessment_previously_observed'] is True, 'July knowledge must be disclosed')
    require(freeze['p08_scoring_implementation_pending'] is True, 'Do not imply P08 scoring is already implemented')
    for path, expected in freeze['artifact_sha256'].items():
        require(digest_file(root / path) == expected, 'Frozen artifact changed: ' + path)
    required = {'config/p07.json', 'config/p08_evaluation.json', 'contracts/fields.json',
                'config/m1.json', 'config/m3d.json', 'config/p06.json',
                'reports/p06/selected_policy.json', 'reports/m3d/refit_eta_models.json',
                'reports/m3d/refit_delay_models.json', 'reports/m3d/refit_transformers.json',
                'reports/m3d/refit_frozen_history.json', 'reports/m3d/calibrators.json'}
    require(required <= set(freeze['artifact_sha256']), 'Freeze omits a required model/protocol artifact')
    return freeze


def load_inputs(root, settings):
    parent = verify_parent(root, settings)
    result = {name: read_jsonl(parent / (name + '.jsonl')) for name in
              ('opportunities', 'targets', 'candidates', 'episodes')}
    result['parent_tables'] = {p.stem: read_jsonl(p) for p in sorted((parent / 'store/jsonl').glob('*.jsonl'))}
    rule = json.loads((parent / 'selected_policy.json').read_text())
    require(rule['policy_version'] == settings['policy']['selected_policy_version'], 'Selected rule identity changed')
    require(rule['threshold'] == settings['policy']['threshold'], 'Selected threshold changed')
    require(len(result['opportunities']) == settings['population']['expected_opportunities'], 'Opportunity inventory differs')
    require(len({(r['tenant_id'], r['trip_id']) for r in result['opportunities']}) == settings['population']['expected_trips'], 'Trip inventory differs')
    return result


def verify_evaluation_readiness(root):
    """Bind the tested delivery implementation before the separate P08 evaluator."""
    root = Path(root)
    record = json.loads((root / 'reproducibility/p07/evaluation_readiness.json').read_text())
    require(record['status'] == 'ready_for_p08_implementation', 'Invalid evaluation readiness status')
    require(record['july_delivery_results_previously_observed'] is True, 'Disclose the development result known at this freeze')
    require(record['final_test_evaluated'] is False and record['quality_accepted'] is False,
            'Readiness is not final evaluation or quality acceptance')
    require(record['p08_evaluator_source_freeze_required'] is True, 'P08 implementation must be frozen before targets')
    required = {'config/p07.json', 'config/p08_evaluation.json',
                'reproducibility/p07/protocol_freeze.json', 'reports/p07/run_receipt.json',
                'reports/p07/metrics.json', 'reports/p07/summary.json'}
    required |= {'src/ds_m01/' + name for name in ('delivery_inputs.py', 'delivery_simulation.py',
                    'delivery_metrics.py', 'delivery_store.py', 'delivery_run.py', 'delivery_replay.py')}
    require(required <= set(record['artifact_sha256']), 'Readiness omits a required delivery implementation artifact')
    for path, expected in record['artifact_sha256'].items():
        require(digest_file(root / path) == expected, 'Evaluation readiness artifact changed: ' + path)
    run = json.loads((root / 'reports/p07/run_receipt.json').read_text())
    require(parse_ts(run['completed_at_utc']) <= parse_ts(record['recorded_at_utc']), 'Readiness precedes its supporting run')
    return record
