"""Refit frozen model families on original training trips and forecast July.

Only original-training outcomes enter this module. July roles are established
from departure metadata, and all admitted July opportunities are forecast before
calibration or assessment targets are joined by the separate stage runner.
"""

from collections import Counter, defaultdict
from pathlib import Path
import json
from zoneinfo import ZoneInfo

from .baseline_run import freeze_history
from .candidate_model import canonical_digest, fit_candidate, predict_candidate
from .candidate_run import _prediction
from .candidate_targets import select_fit_outcomes, prepare_training_rows
from .common import digest_file, parse_ts, read_jsonl, utc
from .delay_model import fit_deadline_transformer, fit_delay_model
from .delay_run import _prediction_rows as delay_prediction_rows
from .delay_targets import prepare_promise_features, build_delay_targets
from .feature_run import FEATURE_INPUT_FILES, build_snapshot_feature_row
from .feature_transform import fit_transformer, transform_rows

SOURCE_FILES = (*FEATURE_INPUT_FILES, 'data/raw/eta_promise_version.jsonl', 'data/raw/eta_trip_outcome.jsonl')
ADMITTED_ROLES = ('train', 'calibration_fit', 'calibration_buffer', 'assessment')


def _trip(row):
    return row['tenant_id'], row['trip_id']


def _snapshot(row):
    return row['tenant_id'], row['snapshot_id']


def _index(rows, fields):
    result = {}
    for row in rows:
        key = tuple(row[name] for name in fields)
        if key in result or any(not isinstance(v, str) or not v.strip() for v in key):
            raise ValueError(f'Duplicate or invalid calibration key: {fields}')
        result[key] = row
    return result


def verify_source_inputs(input_root, settings):
    pins = settings.get('expected_source_sha256')
    if not isinstance(pins, dict) or set(pins) != set(SOURCE_FILES):
        raise ValueError('Calibration must pin all eight source files exactly')
    actual = {name: digest_file(Path(input_root) / name) for name in SOURCE_FILES}
    if actual != pins:
        changed = sorted(name for name in actual if actual[name] != pins[name])
        raise ValueError(f'Frozen calibration source changed: {changed}')
    return actual


def prepare_refit_inputs(input_root, source_config, settings):
    """Read label-free train/July features and expose only train target payloads."""
    from .calibration_split import build_departures, build_memberships, validate_settings

    validate_settings(settings, source_config)
    root = Path(input_root)
    source_hashes = verify_source_inputs(root, settings)
    assignments = read_jsonl(root / 'data/processed/eta_split_assignment.jsonl')
    original = read_jsonl(root / 'data/processed/trip_membership.jsonl')
    events = read_jsonl(root / 'data/raw/eta_trip_event.jsonl')
    departures = build_departures(events)
    memberships = build_memberships(assignments, original, departures, source_config, settings)
    member_index = _index(memberships, ('tenant_id', 'trip_id'))
    allowed = {key for key, row in member_index.items() if row['role'] in ADMITTED_ROLES}
    train_keys = {key for key, row in member_index.items() if row['role'] == 'train'}

    def scoped(name, keys):
        # Decode shared bytes, then inspect the group key before any payload.
        with (root / name).open(encoding='utf-8') as stream:
            for line in stream:
                row = json.loads(line)
                if _trip(row) in keys:
                    yield row

    snapshots = list(scoped('data/processed/eta_snapshot.jsonl', allowed))
    snapshots.sort(key=lambda r: (*_trip(r), r['snapshot_time'], r['snapshot_id']))
    snapshot_index = _index(snapshots, ('tenant_id', 'snapshot_id'))
    if {_trip(r) for r in snapshots} != allowed:
        raise ValueError('Every admitted trip must retain its frozen snapshot opportunities')
    routes = _index(read_jsonl(root / 'data/raw/eta_route_version.jsonl'), ('tenant_id', 'route_version_id'))
    lineage = {}
    with (root / 'data/processed/feature_lineage.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            key = _snapshot(row)
            if key not in snapshot_index:
                continue
            if key in lineage:
                raise ValueError('Duplicate admitted feature lineage')
            lineage[key] = row
    if lineage.keys() != snapshot_index.keys():
        raise ValueError('Admitted snapshots require complete feature lineage')
    departure_events = defaultdict(list)
    for event in events:
        if _trip(event) in allowed and event.get('event_type') in ('departure', 'departed'):
            departure_events[_trip(event)].append(event)
    features = []
    for snapshot in snapshots:
        member = member_index[_trip(snapshot)]
        route = routes[(snapshot['tenant_id'], snapshot['route_version_id'])]
        if (snapshot.get('vehicle_id') in source_config['heldout']['vehicle_ids']
                or route['route_id'] in source_config['heldout']['route_ids']):
            raise ValueError('Heldout entity entered calibration features')
        at = parse_ts(snapshot['snapshot_time'])
        departure = parse_ts(member['departure_time'])
        event = next((r for r in departure_events[_trip(snapshot)]
                      if parse_ts(r['occurred_at']) == departure and parse_ts(r['available_at']) <= at), None)
        if event is None:
            raise ValueError('Snapshot lacks available departure evidence')
        features.append(build_snapshot_feature_row(snapshot, route, lineage[_snapshot(snapshot)], departure_event=event,
                                                   position_stale_seconds=source_config.get('position_stale_seconds', 900)))
    promises = list(scoped('data/raw/eta_promise_version.jsonl', allowed))
    train_outcomes = list(scoped('data/raw/eta_trip_outcome.jsonl', train_keys))
    if verify_source_inputs(root, settings) != source_hashes:
        raise ValueError('Calibration source changed during preparation')
    return {'memberships': memberships, 'features': features, 'snapshots': snapshots,
            'promises': promises, 'train_outcomes': train_outcomes, 'source_sha256': source_hashes}


def _freeze_full_training_history(inputs, selected, cutoff):
    """Build a single immutable training history, never updated with July labels."""
    members = { _trip(r): r for r in inputs['memberships'] if r['role'] == 'train'}
    features = _index(inputs['features'], ('tenant_id', 'snapshot_id'))
    first = {}
    for snapshot in sorted(inputs['snapshots'], key=lambda r: (*_trip(r), r['snapshot_time'], r['snapshot_id'])):
        key = _trip(snapshot)
        if key in members and key not in first:
            feature = features[_snapshot(snapshot)]
            first[key] = dict(snapshot, route_id=feature['categorical']['route_id'],
                              route_distance_km=feature['numeric']['route_distance_km'],
                              departure_time_bucket=feature['categorical']['departure_time_bucket'])
    for key, row in selected.items():
        if row.get('label_status') == 'verified' and row.get('actual_arrival_time') is not None:
            arrival, known = parse_ts(row['actual_arrival_time']), parse_ts(row['label_available_time'])
            if not arrival <= known < parse_ts(cutoff):
                raise ValueError('Training history arrival or availability exceeds refit cutoff')
    # Selecting versions first enforces the newer ambiguity/withdrawal guard
    # before the older history constructor applies its established group rules.
    history, exclusions = freeze_history({'trips': list(members.values()), 'first_snapshot': first,
                                         'outcomes': {key: [row] for key, row in selected.items()}}, set(members), cutoff)
    return history, exclusions


def refit_and_predict(inputs, source_config, settings):
    """Reuse fixed model families, preprocessing and fallback with a fresh fit ID."""
    refit_id = settings['final_training_id']
    cutoff = utc(settings['training_cutoff_at'])
    if cutoff != utc(source_config['split']['train_label_cutoff_exclusive']):
        raise ValueError('Refit must preserve the original training label cutoff')
    if settings['eta']['base_candidate_id'] != 'gradient_boosting_quantile' or settings['probability']['base_method_id'] != 'logistic_delay':
        raise ValueError('Calibration refit cannot change the selected model families')
    members = _index(inputs['memberships'], ('tenant_id', 'trip_id'))
    admitted = {key: row for key, row in members.items() if row['role'] in ADMITTED_ROLES}
    train_keys = {key for key, row in admitted.items() if row['role'] == 'train'}
    zone = ZoneInfo(source_config['timezone'])
    for member in admitted.values():
        original = 'train' if member['role'] == 'train' else 'calibration'
        day = parse_ts(member['departure_time']).astimezone(zone).date().isoformat()
        start, end = source_config['split'][original + '_departure_dates']
        if member.get('original_split_role') != original or not start <= day <= end:
            raise ValueError('Admitted role lies outside its original frozen split')
        role_start, role_end = settings['calendar'][member['role'] + '_departure_dates']
        if not role_start <= day <= role_end:
            raise ValueError('Calibration role disagrees with whole-trip departure date')
        if (member.get('unseen_vehicle') or member.get('unseen_route')
                or member.get('vehicle_id') in source_config['heldout']['vehicle_ids']
                or member.get('route_id') in source_config['heldout']['route_ids']):
            raise ValueError('Heldout entity entered model refit or July prediction')
    if any(_trip(r) not in train_keys for r in inputs['train_outcomes']):
        raise ValueError('Non-training outcome entered base-model refit')
    feature_index = _index(inputs['features'], ('tenant_id', 'snapshot_id'))
    snapshot_index = _index(inputs['snapshots'], ('tenant_id', 'snapshot_id'))
    if feature_index.keys() != snapshot_index.keys() or {_trip(r) for r in inputs['features']} != set(admitted):
        raise ValueError('Refit features and admitted snapshot groups disagree')
    for key, feature in feature_index.items():
        raw = snapshot_index[key]
        if _trip(raw) != _trip(feature) or parse_ts(raw['snapshot_time']) != parse_ts(feature['snapshot_time']):
            raise ValueError('Refit feature and raw snapshot identities disagree')
        member = admitted[_trip(raw)]
        if parse_ts(raw['snapshot_time']) < parse_ts(member['departure_time']):
            raise ValueError('Refit snapshot precedes its trip departure')
        if feature['categorical']['route_id'] in source_config['heldout']['route_ids']:
            raise ValueError('Heldout route entered refit feature values')
    promise_features = prepare_promise_features(inputs['snapshots'], set(admitted), inputs['promises'])
    promise_index = _index(promise_features, ('tenant_id', 'snapshot_id'))
    transformers, matrices, deadline_states = [], [], {}
    for tenant in sorted({key[0] for key in admitted}):
        fit = [r for r in inputs['features'] if r['tenant_id'] == tenant and _trip(r) in train_keys]
        future = [r for r in inputs['features'] if r['tenant_id'] == tenant and _trip(r) not in train_keys]
        if not fit:
            raise ValueError('Admitted tenant lacks original training feature support')
        transformer = fit_transformer(fit, tenant_id=tenant, fold_id=refit_id, training_cutoff_at=cutoff,
                                      fit_trip_keys={key for key in train_keys if key[0] == tenant})
        transformers.append(transformer)
        for role, features in (('fit', fit), ('validation', future)):
            transformed = [dict(row, status='available', reason=None,
                                calibration_role=admitted[_trip(row)]['role']) for row in transform_rows(features, transformer, role=role)]
            matrices.extend(transformed)
            if role == 'fit':
                deadline_states[tenant] = fit_deadline_transformer(
                    [dict(row, promise_seconds_remaining=promise_index[_snapshot(row)]['promise_seconds_remaining']) for row in transformed], transformer)
    matrices.sort(key=lambda r: (r['tenant_id'], r['trip_id'], r['snapshot_time'], r['snapshot_id']))
    fit_matrices = [row for row in matrices if row['fold_role'] == 'fit']
    selected = select_fit_outcomes(inputs['train_outcomes'], train_keys, cutoff)
    eta_training = prepare_training_rows(fit_matrices, inputs['features'], selected, fold_id=refit_id,
                                         fit_trip_keys=train_keys, training_cutoff_at=cutoff)
    opportunities = [dict(snapshot_index[_snapshot(row)], fold_id=refit_id, fold_role='fit') for row in fit_matrices]
    delay_training = build_delay_targets(opportunities, train_keys, inputs['train_outcomes'], inputs['promises'], cutoff)
    counts = Counter(_trip(row) for row in delay_training if row['eligibility'] == 'eligible')
    for row in delay_training:
        row['sample_weight'] = 1 / counts[_trip(row)] if row['eligibility'] == 'eligible' else 0.0
        row['matrix_status'] = 'available'
    eta_targets = _index(eta_training, ('tenant_id', 'snapshot_id'))
    delay_targets = _index(delay_training, ('tenant_id', 'snapshot_id'))
    history, exclusions = _freeze_full_training_history(inputs, selected, cutoff)
    eta_models, delay_models = [], []
    for transformer in transformers:
        tenant = transformer['tenant_id']
        eta_fit = [dict(row, remaining_duration_seconds=eta_targets[_snapshot(row)]['remaining_duration_seconds'],
                        sample_weight=eta_targets[_snapshot(row)]['sample_weight'])
                   for row in fit_matrices if row['tenant_id'] == tenant and eta_targets[_snapshot(row)]['eligibility'] == 'eligible']
        eta_model = fit_candidate(eta_fit, transformer, candidate_id='gradient_boosting_quantile', min_fit_trips=5)
        eta_models.append(eta_model)
        delayed_fit = [dict(row, **{field: delay_targets[_snapshot(row)][field] for field in
                                    ('eligibility', 'label_status', 'delay_label', 'actual_arrival_time', 'label_known_at', 'sample_weight')},
                            promise_seconds_remaining=promise_index[_snapshot(row)]['promise_seconds_remaining'])
                       for row in fit_matrices if row['tenant_id'] == tenant and delay_targets[_snapshot(row)]['eligibility'] == 'eligible']
        for method in ('fold_prevalence', 'logistic_delay'):
            delay_models.append(fit_delay_model(delayed_fit, transformer, deadline_states[tenant], method))
    artifacts = {'memberships': inputs['memberships'], 'features': inputs['features'], 'transformers': transformers,
            'matrices': matrices, 'promise_features': promise_features, 'eta_models': eta_models,
            'delay_models': delay_models, 'eta_training_rows': eta_training, 'delay_training_rows': delay_training,
            'frozen_history': history,
            'summary': {'training_id': refit_id, 'training_cutoff_at': cutoff, 'training_trips': len(train_keys),
                        'fit_snapshots': len(fit_matrices), 'july_opportunities': sum(r['fold_role'] == 'validation' for r in matrices),
                        'eta_models': len(eta_models), 'delay_models': len(delay_models),
                        'training_history_trips': len(history), 'history_exclusions': exclusions,
                        'july_roles': dict(sorted(Counter(r['calibration_role'] for r in matrices if r['fold_role'] == 'validation').items())),
                        'july_outcomes_used_in_refit': False, 'test_outcomes_used': False}}

    artifacts.update(replay_predictions(artifacts, source_config, settings))
    return artifacts


def replay_predictions(artifacts, source_config, settings):
    """Replay the retained JSON models without outcome reads or model fitting.

    Validate model scope and reconstruct every retained matrix from feature rows
    and frozen transformers before performing label-free inference. File hashes
    and raw-source equivalence are the enclosing stage receipt's responsibility.
    """
    cutoff = utc(settings['training_cutoff_at'])
    if cutoff != utc(source_config['split']['train_label_cutoff_exclusive']):
        raise ValueError('Replay must preserve original training cutoff')
    refit_id = settings['final_training_id']
    members = _index(artifacts['memberships'], ('tenant_id', 'trip_id'))
    admitted = {key: r for key, r in members.items() if r['role'] in ADMITTED_ROLES}
    train_keys = {key for key, r in admitted.items() if r['role'] == 'train'}
    features = _index(artifacts['features'], ('tenant_id', 'snapshot_id'))
    matrices = _index(artifacts['matrices'], ('tenant_id', 'snapshot_id'))
    promises = _index(artifacts['promise_features'], ('tenant_id', 'snapshot_id'))
    if features.keys() != matrices.keys() or features.keys() != promises.keys():
        raise ValueError('Replay feature, matrix and promise opportunities differ')
    if {_trip(r) for r in features.values()} != set(admitted):
        raise ValueError('Replay features do not cover exactly admitted groups')
    tenants = {key[0] for key in admitted}
    states = _index(artifacts['transformers'], ('fold_id', 'tenant_id'))
    eta_models = _index(artifacts['eta_models'], ('fold_id', 'tenant_id', 'candidate_id'))
    delay_models = _index(artifacts['delay_models'], ('fold_id', 'tenant_id', 'method_id'))
    if (set(states) != {(refit_id, tenant) for tenant in tenants}
            or set(eta_models) != {(refit_id, tenant, 'gradient_boosting_quantile') for tenant in tenants}
            or set(delay_models) != {(refit_id, tenant, method) for tenant in tenants
                                    for method in ('fold_prevalence', 'logistic_delay')}):
        raise ValueError('Replay model set differs from frozen tenant and method scope')
    regenerated = []
    for (_, tenant), state in sorted(states.items()):
        fit = [r for r in features.values() if r['tenant_id'] == tenant and _trip(r) in train_keys]
        future = [r for r in features.values() if r['tenant_id'] == tenant and _trip(r) not in train_keys]
        if (utc(state['training_cutoff_at']) != cutoff
                or {tuple(k) for k in state['fit_trip_keys']} != {key for key in train_keys if key[0] == tenant}
                or {tuple(k) for k in state['fitted_snapshot_keys']} != {_snapshot(r) for r in fit}):
            raise ValueError('Replay transformer scope differs from complete original training')
        for role, rows in (('fit', fit), ('validation', future)):
            regenerated.extend(dict(row, status='available', reason=None,
                                    calibration_role=admitted[_trip(row)]['role'])
                               for row in transform_rows(rows, state, role=role))
    if _index(regenerated, ('tenant_id', 'snapshot_id')) != matrices:
        raise ValueError('Retained matrices differ from frozen feature transformation')
    history = artifacts['frozen_history']
    _index(history, ('tenant_id', 'trip_id'))
    for row in history:
        if (_trip(row) not in train_keys or utc(row['training_cutoff_at']) != cutoff
                or not parse_ts(row['actual_arrival_time']) <= parse_ts(row['label_known_at']) < parse_ts(cutoff)):
            raise ValueError('Replay historical baseline exceeds original training scope')
    eta_predictions, baseline_predictions = [], []
    for (_, tenant), state in sorted(states.items()):
        model = eta_models[refit_id, tenant, 'gradient_boosting_quantile']
        future = [r for r in artifacts['matrices'] if r['tenant_id'] == tenant and r['fold_role'] == 'validation']
        answers = _index(predict_candidate(model, future, state), ('tenant_id', 'snapshot_id'))
        for row in future:
            key = _snapshot(row)
            feature, promise, member = features[key], promises[key], admitted[_trip(row)]
            if (_trip(promise) != _trip(row) or parse_ts(promise['snapshot_time']) != parse_ts(row['snapshot_time'])
                    or parse_ts(promise['promise_known_at']) > parse_ts(row['snapshot_time'])):
                raise ValueError('Replay promise differs from opportunity identity or clock')
            answer = dict(answers[key], model_sha256=canonical_digest(model))
            for output, candidate, forecast in (
                    (eta_predictions, 'gradient_boosting_quantile', answer),
                    (baseline_predictions, 'historical_baseline', {'status': 'unsupported', 'reason': 'reference_baseline'})):
                prediction = _prediction(row, forecast, feature, history,
                                         {'fold_id': refit_id, 'training_cutoff_at': cutoff}, candidate, state)
                prediction.update(calibration_role=member['role'], departure_time=member['departure_time'],
                                  elapsed_trip_seconds=feature['numeric']['elapsed_trip_seconds'],
                                  departure_time_bucket=feature['categorical']['departure_time_bucket'],
                                  deadline_remaining_seconds=promise['promise_seconds_remaining'],
                                  promise_version_id=promise['promise_version_id'], deadline_at=promise['deadline_at'],
                                  promise_known_at=promise['promise_known_at'])
                if candidate == 'historical_baseline':
                    prediction['prediction_source'] = 'baseline' if prediction['status'] == 'available' else 'unavailable'
                output.append(prediction)
    order = lambda row: (row['tenant_id'], row['trip_id'], row['snapshot_time'], row['snapshot_id'])
    eta_predictions.sort(key=order)
    baseline_predictions.sort(key=order)
    delay_predictions = delay_prediction_rows(artifacts['delay_models'],
                                              [r for r in artifacts['matrices'] if r['fold_role'] == 'validation'],
                                              artifacts['features'], artifacts['transformers'], artifacts['promise_features'])
    for row in delay_predictions:
        row.update(calibration_role=admitted[_trip(row)]['role'], departure_time=admitted[_trip(row)]['departure_time'])
    return {'eta_predictions': eta_predictions, 'baseline_predictions': baseline_predictions, 'delay_predictions': delay_predictions}
