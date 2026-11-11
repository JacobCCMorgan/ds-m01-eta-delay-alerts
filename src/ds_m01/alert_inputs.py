"""Bind P06 policy opportunities to frozen P05 forecasts and as-of metadata.

Outcome fields are projected into a separate target list. The policy engine
never receives them. Original precise forecasts and canonical numeric values
are both retained so storage and threshold decisions have the same meaning.
"""
import json
import math
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_FLOOR, ROUND_CEILING
from pathlib import Path
from zoneinfo import ZoneInfo

from .common import digest_file, read_jsonl, parse_ts, utc
from .candidate_model import canonical_digest

PROTOCOL_SHA256 = '7b7dbe70ec1d44199bd971607eeec9a029fdee5567a3c4fa1f42f7fac9ee0470'
TARGET_FIELDS = ('eligibility', 'delay_label', 'reason', 'actual_arrival_time',
                 'label_known_at', 'label_cutoff_at', 'label_status',
                 'arrival_time_precision', 'outcome_version_id', 'outcome_provenance_id')


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def verify_settings(root):
    root = Path(root)
    path = root / 'config/p06.json'
    _require(digest_file(path) == PROTOCOL_SHA256, 'Frozen P06 protocol changed')
    settings = json.loads(path.read_text())
    _require(digest_file(root / 'config/m3d.json') == settings['source_protocol_sha256'],
             'P05 protocol changed')
    _require(digest_file(root / 'contracts/fields.json') == settings['field_contract_sha256'],
             'Canonical field contract changed')
    return settings


def verify_source_report(report, settings):
    report = Path(report)
    expected = settings['source_report_sha256']
    _require({p.name for p in report.iterdir() if p.is_file()} == set(expected),
             'P05 report inventory changed')
    for name, digest in expected.items():
        _require(digest_file(report / name) == digest, 'P05 report changed: ' + name)


def verify_input_files(input_root, settings):
    for name, digest in settings['input_sha256'].items():
        _require(digest_file(Path(input_root) / name) == digest, 'Frozen source changed: ' + name)


def probability_projection(value):
    _require(isinstance(value, (int, float)) and not isinstance(value, bool)
             and math.isfinite(value) and 0 <= value <= 1, 'Invalid calibrated probability')
    return format(Decimal(str(value)).quantize(Decimal('0.000001'), rounding=ROUND_HALF_EVEN), '.6f')


def eta_projection(lower, median, upper):
    values = (lower, median, upper)
    _require(all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
                 for x in values) and 0 <= lower <= median <= upper, 'Invalid ETA interval')
    lo = int(Decimal(str(lower)).to_integral_value(rounding=ROUND_FLOOR))
    mid = int(Decimal(str(median)).to_integral_value(rounding=ROUND_HALF_EVEN))
    hi = int(Decimal(str(upper)).to_integral_value(rounding=ROUND_CEILING))
    return min(lo, mid), mid, max(hi, mid)


def _index(rows, fields, description):
    result = {}
    for row in rows:
        key = tuple(row[name] for name in fields)
        _require(key not in result, 'Duplicate ' + description + ': ' + str(key))
        result[key] = row
    return result


def build_registry(report, settings, split_version):
    report = Path(report)
    states = json.loads((report / 'calibrators.json').read_text())
    transforms = json.loads((report / 'refit_transformers.json').read_text())
    eta_models = json.loads((report / 'refit_eta_models.json').read_text())
    delay_models = json.loads((report / 'refit_delay_models.json').read_text())
    result = []
    for transform in sorted(transforms, key=lambda x: x['tenant_id']):
        tenant = transform['tenant_id']
        eta = [x for x in eta_models if x['tenant_id'] == tenant]
        probability = [x for x in delay_models if x['tenant_id'] == tenant and x['method_id'] == 'logistic_delay']
        intervals = [x for x in states['eta'] if x['tenant_id'] == tenant]
        probabilities = [x for x in states['probability'] if x['tenant_id'] == tenant]
        _require(all(len(x) == 1 for x in (eta, probability, intervals, probabilities)),
                 'Ambiguous tenant model or calibrator registry')
        bindings = {'tenant_id': tenant, 'p06_protocol_sha256': PROTOCOL_SHA256,
                    'p05_report_sha256': settings['source_report_sha256'],
                    'transformer_sha256': canonical_digest(transform),
                    'eta_model_sha256': canonical_digest(eta[0]),
                    'probability_model_sha256': canonical_digest(probability[0]),
                    'interval_calibrator_sha256': canonical_digest(intervals[0]),
                    'probability_calibrator_sha256': canonical_digest(probabilities[0]),
                    'quality_accepted': False, 'release_mode': 'offline_replay'}
        version = 'p06-r01-' + tenant + '-' + canonical_digest(bindings)[:16]
        result.append({'tenant_id': tenant, 'prediction_version': version,
                       'training_cutoff_at': '2026-07-01T07:00:00Z',
                       'feature_definition_version': transform['feature_definition_version'],
                       'nominal_interval_coverage': '0.800000', 'split_version': split_version,
                       'interval_calibration_version': 'p05-interval-' + bindings['interval_calibrator_sha256'][:20],
                       'probability_calibration_version': 'p05-probability-' + bindings['probability_calibrator_sha256'][:20],
                       'calibration_cutoff_at': '2026-07-15T07:00:00Z',
                       'baseline_definition': 'Frozen original-training historical route/departure-band, distance-group and tenant fallback; state SHA256 ' + settings['source_report_sha256']['refit_frozen_history.json'],
                       'source_bindings': bindings})
    return result


def bind_opportunities(eta_rows, probability_rows, snapshots, promises, memberships, registry, settings):
    """Pure binding/projection used by the runner and source-context replay."""
    members = _index([x for x in memberships if x['role'] == 'assessment'],
                     ('tenant_id', 'trip_id'), 'assessment membership')
    _require(len(members) == settings['population']['expected_trips'], 'Assessment trip inventory changed')
    _require(all(not x['unseen_route'] and not x['unseen_vehicle'] for x in members.values()),
             'Heldout identity entered policy development')
    dates = settings['population'].get('departure_dates', ['2026-07-15', '2026-07-24'])
    zone = ZoneInfo(settings.get('display_timezone', 'America/Los_Angeles'))
    _require(all(dates[0] <= parse_ts(x['departure_time']).astimezone(zone).date().isoformat() <= dates[1]
                 for x in members.values()), 'Assessment departure calendar changed')
    selected = [x for x in snapshots if (x['tenant_id'], x['trip_id']) in members]
    context = _index(selected, ('tenant_id', 'snapshot_id'), 'source snapshot')
    eta = _index([x for x in eta_rows if x['variant'] == 'calibrated'], ('tenant_id', 'snapshot_id'), 'ETA prediction')
    probability = _index([x for x in probability_rows if x['variant'] == 'calibrated'], ('tenant_id', 'snapshot_id'), 'probability prediction')
    _require(len(context) == settings['population']['expected_opportunities'] and
             set(eta) == set(probability) == set(context), 'Incomplete policy opportunity inventory')
    _require({(x['tenant_id'], x['trip_id']) for x in context.values()} == set(members),
             'Assessment opportunity trip inventory changed')
    promise_index = _index(promises, ('tenant_id', 'promise_version_id'), 'promise')
    promises_by_trip = defaultdict(list)
    for p in promises:
        if (p['tenant_id'], p['trip_id']) in members:
            promises_by_trip[p['tenant_id'], p['trip_id']].append(p)
    versions = _index(registry, ('tenant_id',), 'tenant registry')
    opportunities, targets = [], []
    cutoff = parse_ts(settings['population']['label_cutoff_exclusive'])
    for key, snapshot in sorted(context.items(), key=lambda x: (parse_ts(x[1]['snapshot_time']), *x[0])):
        a, b = eta[key], probability[key]
        member = members[snapshot['tenant_id'], snapshot['trip_id']]
        now = parse_ts(snapshot['snapshot_time'])
        _require(parse_ts(member['departure_time']) <= now < cutoff,
                 'Policy snapshot outside departure/cutoff window')
        for row in (a, b):
            _require(row['trip_id'] == snapshot['trip_id'] and parse_ts(row['snapshot_time']) == now
                     and row['route_id'] == member['route_id'] and row['calibration_role'] == 'assessment'
                     and parse_ts(row['departure_time']) == parse_ts(member['departure_time'])
                     and row['position_validity'] == snapshot['position_validity'], 'Forecast/context identity mismatch')
        promise = promise_index.get((snapshot['tenant_id'], snapshot['promise_version_id']))
        _require(promise is not None and promise['trip_id'] == snapshot['trip_id']
                 and parse_ts(promise['known_at']) <= now, 'Promise unavailable at snapshot')
        available = [p for p in promises_by_trip[snapshot['tenant_id'], snapshot['trip_id']]
                     if parse_ts(p['known_at']) <= now]
        _require(len({parse_ts(p['known_at']) for p in available}) == len(available),
                 'Ambiguous known promise versions')
        _require(max(parse_ts(p['known_at']) for p in available) == parse_ts(promise['known_at']),
                 'Snapshot uses a superseded known promise')
        _require(b['promise_version_id'] == promise['promise_version_id']
                 and parse_ts(b['deadline_at']) == parse_ts(promise['window_end_at'])
                 and parse_ts(b['promise_known_at']) == parse_ts(promise['known_at'])
                 and b['promise_provenance_id'] == promise['provenance_id'], 'Forecast promise binding changed')
        _require(parse_ts(b['label_cutoff_at']) == cutoff, 'Policy target cutoff changed')
        _require(b['eligibility'] != 'eligible' or
                 (type(b['delay_label']) is bool and b['label_known_at'] is not None
                  and parse_ts(b['label_known_at']) < cutoff), 'Eligible policy target is unavailable at cutoff')
        _require(b['eligibility'] == 'eligible' or b['delay_label'] is None,
                 'Unscored target cannot be marked on-time or delayed')
        item = {name: snapshot[name] for name in ('tenant_id', 'trip_id', 'snapshot_id', 'vehicle_id',
                                                'trip_status_as_of', 'position_validity')}
        item.update(snapshot_time=utc(now), route_id=member['route_id'], departure_time=utc(member['departure_time']),
                    promise_version_id=promise['promise_version_id'], deadline_at=utc(promise['window_end_at']),
                    promise_known_at=utc(promise['known_at']), snapshot_provenance_id=snapshot['provenance_id'],
                    promise_provenance_id=promise['provenance_id'],
                    prediction_version=versions[(snapshot['tenant_id'],)]['prediction_version'],
                    elapsed_trip_seconds=a['elapsed_trip_seconds'], unseen_route=member['unseen_route'],
                    unseen_vehicle=member['unseen_vehicle'], eta_status=a['status'],
                    eta_reason=a.get('calibration_reason') or ('available' if a['status'] == 'available' else 'eta_forecast_unavailable'),
                    eta_source=a['prediction_source'], eta_calibration_status=a['calibration_status'],
                    probability_status=b['status'],
                    probability_reason=b.get('calibration_reason') or ('available' if b['status'] == 'available' else 'probability_forecast_unavailable'),
                    probability_source=b['prediction_source'], probability_calibration_status=b['calibration_status'])
        _require(item['eta_status'] in ('available', 'unavailable') and
                 item['probability_status'] in ('available', 'unavailable'), 'Unknown forecast status')
        for name in ('lower_seconds', 'median_seconds', 'upper_seconds'):
            item[name] = a.get(name) if a['status'] == 'available' else None
        projection = eta_projection(*(item[x] for x in ('lower_seconds', 'median_seconds', 'upper_seconds'))) if a['status'] == 'available' else (None, None, None)
        for name, value in zip(('lower_seconds_int', 'median_seconds_int', 'upper_seconds_int'), projection):
            item[name] = value
        item['raw_probability'] = b.get('raw_probability') if b['status'] == 'available' else None
        item['calibrated_probability'] = b.get('probability') if b['status'] == 'available' else None
        item['probability'] = probability_projection(b['probability']) if b['status'] == 'available' else None
        item['fallback_reason'] = ('eta=' + str(a['prediction_source']) + ';probability=' + str(b['prediction_source'])
                                   + ';interval_calibration=' + str(a['calibration_status'])
                                   + ';probability_calibration=' + str(b['calibration_status']))
        opportunities.append(item)
        target = {name: b.get(name) for name in TARGET_FIELDS}
        target.update({name: item[name] for name in ('tenant_id', 'trip_id', 'snapshot_id', 'promise_version_id', 'deadline_at')})
        targets.append(target)
    _require(not set(TARGET_FIELDS).intersection(opportunities[0]), 'Outcome field leaked to policy input')
    return opportunities, targets


def prepare_inputs(root, input_root, settings=None):
    root, input_root = Path(root), Path(input_root)
    settings = settings or verify_settings(root)
    report = root / settings['source_report']
    verify_source_report(report, settings)
    verify_input_files(input_root, settings)
    memberships = read_jsonl(report / 'memberships.jsonl')
    snapshots = read_jsonl(input_root / 'data/processed/eta_snapshot.jsonl')
    promises = read_jsonl(input_root / 'data/raw/eta_promise_version.jsonl')
    splits = read_jsonl(input_root / 'data/processed/eta_split_assignment.jsonl')
    split_versions = {x['split_version'] for x in splits}
    _require(len(split_versions) == 1, 'Ambiguous frozen split version')
    registry = build_registry(report, settings, next(iter(split_versions)))
    eta_rows = read_jsonl(report / 'assessment_eta_rows.jsonl')
    probability_rows = read_jsonl(report / 'assessment_probability_rows.jsonl')
    opportunities, targets = bind_opportunities(eta_rows, probability_rows, snapshots, promises,
                                                memberships, registry, settings)
    keys = {(x['tenant_id'], x['trip_id']) for x in opportunities}
    context = {'snapshots': [x for x in snapshots if (x['tenant_id'], x['trip_id']) in keys],
               'promises': [x for x in promises if (x['tenant_id'], x['trip_id']) in keys],
               'memberships': [x for x in memberships if (x['tenant_id'], x['trip_id']) in keys]}
    verify_input_files(input_root, settings)
    verify_source_report(report, settings)
    return {'opportunities': opportunities, 'targets': targets, 'registry': registry, 'context': context}
