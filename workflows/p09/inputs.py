"""Read an immutable P08 primary scenario for a later workflow demonstration.

The original receipt, contract and every retained P08 output remain byte-bound.
This reader does not rerun, change or reselect a forecast or final-test outcome.
"""
from copy import deepcopy
import json
from pathlib import Path

from ds_m01.common import digest_file, parse_ts, read_jsonl


BASE_PUBLIC_COMMIT = '12b904696b87d90369d85dabeee01eadadc854cd'
RECEIPT_SHA256 = 'a4f8b184792dfa3d6417a61fa8544ca4b94ea4440f10058b57f17e8d26976fc0'
CONTRACT_SHA256 = '3bf382e0b24442afe73872f8b1bd9d12e2383bbb155eec2e116b48cc3bd0addc'
SETTINGS_SHA256 = '3e9fb7a7ce50338405f60e1dbc898f01b383b12adfbaf3a9aa0f3c95ad52c851'
SCENARIO = 'S00_unmodified'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_settings(settings):
    require(isinstance(settings, dict), 'Workflow settings must be an object')
    require(settings.get('protocol_version') == 'p09-workflow-v1'
            and settings.get('base_public_commit') == BASE_PUBLIC_COMMIT
            and settings.get('source_scenario') == SCENARIO
            and settings.get('result_origin') == 'synthetic_scenario',
            'Workflow source, version or synthetic origin differs')
    for field in ('quality_accepted', 'operational_approval', 'causal_benefit_estimated'):
        require(settings.get(field) is False, 'Workflow cannot grant acceptance: ' + field)
    parse_ts(settings['observation_cutoff_at'])
    for field in ('seed', 'opening_delay_seconds_min', 'opening_delay_seconds_span',
                  'decision_delay_seconds_min', 'decision_delay_seconds_span',
                  'action_delay_seconds', 'handling_seconds_min', 'handling_seconds_span',
                  'followup_window_hours', 'long_followup_window_hours',
                  'reschedule_shift_seconds'):
        require(type(settings.get(field)) is int and settings[field] >= 0,
                'Nonnegative integer setting required: ' + field)
    for field in ('opening_delay_seconds_span', 'decision_delay_seconds_span',
                  'handling_seconds_span', 'followup_window_hours',
                  'long_followup_window_hours'):
        require(settings[field] > 0, 'Positive setting required: ' + field)
    require(settings['long_followup_window_hours'] >= settings['followup_window_hours'],
            'Long follow-up window must not be shorter than the ordinary window')
    json.dumps(settings, allow_nan=False)
    return settings


def load_settings(root):
    path = Path(root).resolve() / 'workflows/p09/settings.json'
    require(digest_file(path) == SETTINGS_SHA256, 'Published workflow settings changed')
    return validate_settings(json.loads(path.read_text()))


def index(rows, fields, name):
    result = {}
    require(isinstance(rows, list), 'Expected row array: ' + name)
    for row in rows:
        require(isinstance(row, dict), 'Expected object: ' + name)
        key = tuple(row.get(field) for field in fields)
        require(all(isinstance(x, str) and x.strip() for x in key),
                'Missing stable identity: ' + name)
        require(key not in result, 'Duplicate identity: ' + name)
        result[key] = row
    return result


def validate_inputs(inputs):
    require(inputs.get('scenario_id') == SCENARIO, 'Only the primary S00 population is supported')
    alerts = index(inputs['alerts'], ('tenant_id', 'alert_id'), 'alerts')
    attempts = index(inputs['attempts'], ('tenant_id', 'delivery_id'), 'attempts')
    opportunities = index(inputs['opportunities'], ('tenant_id', 'snapshot_id'), 'opportunities')
    promises = index(inputs['promises'], ('tenant_id', 'promise_version_id'), 'promises')
    index(inputs['outcomes'], ('tenant_id', 'outcome_version_id'), 'outcomes')
    for alert in alerts.values():
        opportunity = opportunities.get((alert['tenant_id'], alert['snapshot_id']))
        promise = promises.get((alert['tenant_id'], alert['promise_version_id']))
        require(opportunity is not None and promise is not None, 'Alert source lineage is missing')
        require(opportunity['trip_id'] == promise['trip_id'] == alert['trip_id'],
                'Alert source lineage crosses trips')
        require(opportunity['prediction_version'] == alert['prediction_version']
                and opportunity['promise_version_id'] == alert['promise_version_id'],
                'Alert model or original promise binding differs')
        require(parse_ts(promise['known_at']) <= parse_ts(opportunity['snapshot_time'])
                <= parse_ts(alert['created_at']), 'Alert uses a future source')
        require(parse_ts(promise['window_end_at']) == parse_ts(alert['deadline_at']),
                'Alert original deadline differs from its promise')
    for attempt in attempts.values():
        alert = alerts.get((attempt['tenant_id'], attempt['alert_id']))
        require(alert is not None and alert['trip_id'] == attempt['trip_id'],
                'Delivery lineage crosses tenant or trip')
        require(parse_ts(attempt['sent_at']) >= parse_ts(alert['created_at']),
                'Delivery attempt precedes alert')
        if attempt.get('delivered_at') is not None:
            require(parse_ts(attempt['delivered_at']) >= parse_ts(attempt['sent_at']),
                    'Delivery precedes sending')
        if attempt.get('receipt_known_at') is not None and attempt.get('delivered_at') is not None:
            require(parse_ts(attempt['receipt_known_at']) >= parse_ts(attempt['delivered_at']),
                    'Receipt is known before delivery')
        require(attempt.get('result_origin') == 'synthetic_scenario',
                'Only synthetic retained deliveries may enter this workflow')
    for outcome in inputs['outcomes']:
        parse_ts(outcome['label_available_time'])
        parse_ts(outcome['followup_through'])
        if outcome.get('actual_arrival_time') is not None:
            require(parse_ts(outcome['actual_arrival_time']) <= parse_ts(outcome['label_available_time']),
                    'Outcome was available before observed arrival')
    json.dumps(inputs, allow_nan=False)
    return inputs


def load_parent(root):
    """Load a complete retained primary scenario after checking its original receipt."""
    root = Path(root).resolve()
    report = root / 'reports/p08'
    receipt_path = report / 'run_receipt.json'
    require(digest_file(receipt_path) == RECEIPT_SHA256, 'Original P08 receipt changed')
    require(digest_file(root / 'contracts/fields.json') == CONTRACT_SHA256,
            'Original canonical contracts changed')
    receipt = json.loads(receipt_path.read_text())
    require(receipt['status'] == 'engineering_complete_final_evaluation'
            and receipt['quality_accepted'] is False
            and receipt['operational_approval'] is False, 'Original P08 scope differs')
    source_sha256 = {'reports/p08/run_receipt.json': RECEIPT_SHA256,
                     'contracts/fields.json': CONTRACT_SHA256}
    for relative, expected in receipt['output_sha256'].items():
        relative_path = Path(relative)
        require(not relative_path.is_absolute() and '..' not in relative_path.parts,
                'Invalid parent receipt path')
        path = report / relative_path
        require(path.is_file() and not path.is_symlink() and digest_file(path) == expected,
                'Retained P08 evidence changed: ' + relative)
        source_sha256['reports/p08/' + relative] = expected
    scenario = report / 'scenarios' / SCENARIO
    bundle = json.loads((scenario / 'policy_delivery.json').read_text())
    tables = {path.stem: read_jsonl(path) for path in sorted((scenario / 'store/jsonl').glob('*.jsonl'))}
    require(all(not tables[name] for name in ('eta_adoption', 'eta_action', 'eta_action_followup')),
            'P08 reserved workflow tables must remain empty')
    inputs = {'parent_tables': tables,
              'attempts': deepcopy(bundle['delivery']['attempts']),
              'alerts': deepcopy(bundle['delivery']['alerts']),
              'opportunities': read_jsonl(scenario / 'opportunities.jsonl'),
              'promises': read_jsonl(report / 'inputs/promises.jsonl'),
              'outcomes': read_jsonl(report / 'outcome_source.jsonl'),
              'source_sha256': source_sha256, 'parent_report': 'reports/p08',
              'scenario_id': SCENARIO,
              'parent_delivery_summary': deepcopy(bundle['delivery']['summary'])}
    return validate_inputs(inputs)
