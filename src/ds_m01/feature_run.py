"""Prepare label-free features on the frozen M3a training-period folds.

Only six input files are consumed. Outcomes, snapshot labels, historical target
aggregates and simulator truth are not opened. This is feature preparation, not
candidate training, calibration, final-test evaluation or an alert deployment.
"""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import tempfile

from .common import digest_file, parse_ts, read_jsonl, utc, write_jsonl
from .features import build_feature_row, FEATURE_DEFINITION_VERSION
from .feature_transform import fit_transformer, transform_rows
from .model_splits import build_fold_memberships, validate_folds


FEATURE_INPUT_FILES = (
    'data/raw/eta_trip_event.jsonl',
    'data/raw/eta_route_version.jsonl',
    'data/processed/eta_snapshot.jsonl',
    'data/processed/eta_split_assignment.jsonl',
    'data/processed/trip_membership.jsonl',
    'data/processed/feature_lineage.jsonl',
)


def _key(row):
    try:
        key = (row['tenant_id'], row['trip_id'])
    except (KeyError, TypeError) as exc:
        raise ValueError('Records require tenant_id and trip_id for scoping') from exc
    if any(not isinstance(value, str) or not value.strip() for value in key):
        raise ValueError('Tenant and trip identifiers must be nonempty strings')
    return key


def _unique(rows, fields):
    result = {}
    for row in rows:
        try:
            key = tuple(row[field] for field in fields)
        except (KeyError, TypeError) as exc:
            raise ValueError(f'Missing key fields: {fields}') from exc
        if any(not isinstance(value, str) or not value.strip() for value in key):
            raise ValueError(f'Invalid key fields: {fields}')
        if key in result:
            raise ValueError(f'Duplicate key for {fields}: {key}')
        result[key] = row
    return result


def _scoped_rows(path, allowed):
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            if line.strip():
                row = json.loads(line)
                if _key(row) in allowed:
                    yield row


def load_feature_inputs(root, source_config):
    """Gate original split roles before examining feature or event payloads.

    Shared files contain all roles. Reading and hashing those files is not a
    claim that their protected bytes were never accessed; no protected payload
    is exposed to feature construction or preprocessing. Route metadata is
    shared, versioned context. Lineage is an upstream assertion, independently
    paired here with route and selected-position clock checks.
    """
    root = Path(root)
    assignments = _unique(read_jsonl(root / FEATURE_INPUT_FILES[3]), ('tenant_id', 'trip_id'))
    membership = _unique(read_jsonl(root / FEATURE_INPUT_FILES[4]), ('tenant_id', 'trip_id'))
    if assignments.keys() != membership.keys():
        raise ValueError('Trip membership and original split keys disagree')
    for key, row in membership.items():
        if row.get('split_role') not in ('train', 'calibration', 'test', 'excluded'):
            raise ValueError('Unknown original split role')
        if row['split_role'] != assignments[key].get('split_role'):
            raise ValueError('Trip membership and original split roles disagree')
    allowed = {key for key, row in membership.items() if row['split_role'] == 'train'}
    departures = defaultdict(list)
    for event in _scoped_rows(root / FEATURE_INPUT_FILES[0], allowed):
        if event.get('event_type') in ('departure', 'departed'):
            # Validate only the departure clocks used for fold membership.
            try:
                occurred, available = parse_ts(event['occurred_at']), parse_ts(event['available_at'])
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                raise ValueError('Departure requires aware event and availability clocks') from exc
            if available < occurred:
                raise ValueError('Departure availability precedes its event')
            departures[_key(event)].append(event)
    trips = []
    for key in sorted(allowed):
        if not departures[key]:
            raise ValueError(f'Missing explicit departure for {key}')
        departure = min(parse_ts(e['occurred_at']) for e in departures[key])
        trips.append(dict(membership[key], departure_time=utc(departure)))
    trip_by_key = {_key(row): row for row in trips}
    snapshots = list(_scoped_rows(root / FEATURE_INPUT_FILES[2], allowed))
    snapshot_by_id = _unique(snapshots, ('tenant_id', 'snapshot_id'))
    routes = _unique(read_jsonl(root / FEATURE_INPUT_FILES[1]), ('tenant_id', 'route_version_id'))
    lineage = {}
    with (root / FEATURE_INPUT_FILES[5]).open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            key = (row.get('tenant_id'), row.get('snapshot_id'))
            if key not in snapshot_by_id:
                continue
            if key in lineage:
                raise ValueError('Duplicate feature lineage')
            lineage[key] = row
    if lineage.keys() != snapshot_by_id.keys():
        raise ValueError('Training snapshots require complete feature lineage')
    heldout = source_config.get('heldout', {})
    rows = []
    for snapshot in sorted(snapshots, key=lambda r: (r['tenant_id'], r['trip_id'], r['snapshot_time'], r['snapshot_id'])):
        key = _key(snapshot)
        try:
            route = routes[(snapshot['tenant_id'], snapshot['route_version_id'])]
        except KeyError as exc:
            raise ValueError('Snapshot route version is missing in its tenant') from exc
        if (snapshot.get('vehicle_id') in heldout.get('vehicle_ids', [])
                or route.get('route_id') in heldout.get('route_ids', [])):
            raise ValueError('Heldout snapshot vehicle/route cannot enter training preparation')
        if not isinstance(snapshot.get('vehicle_id'), str) or not snapshot['vehicle_id'].strip():
            raise ValueError('Snapshot requires a vehicle for heldout checks')
        at = parse_ts(snapshot['snapshot_time'])
        depart = parse_ts(trip_by_key[key]['departure_time'])
        if snapshot.get('elapsed_trip_seconds') != (at - depart).total_seconds():
            raise ValueError('Snapshot elapsed time disagrees with explicit trip departure')
        if not any(parse_ts(e['occurred_at']) == depart and parse_ts(e['available_at']) <= at
                   for e in departures[key]):
            raise ValueError('Snapshot departure was not available at its clock')
        rows.append(build_feature_row(
            snapshot, route, lineage[(snapshot['tenant_id'], snapshot['snapshot_id'])],
            position_stale_seconds=source_config.get('position_stale_seconds', 900)))
    return dict(trips=trips, feature_rows=rows,
                original_role_trip_counts=dict(sorted(Counter(r['split_role'] for r in assignments.values()).items())))


def prepare_folds(inputs, source_config, settings):
    """Fit preprocessing by tenant and fold, without consulting target eligibility."""
    folds = validate_folds(settings['folds'], source_config)
    memberships = build_fold_memberships(inputs['trips'], folds, source_config)
    # Trip metadata is authoritative for grouping; injected feature-only groups
    # must not disappear silently when membership is assembled.
    trip_keys = {_key(row) for row in inputs['trips']}
    _unique(inputs['feature_rows'], ('tenant_id', 'snapshot_id'))
    if any(_key(row) not in trip_keys for row in inputs['feature_rows']):
        raise ValueError('Feature row has no original trip membership')
    transformers, matrix_rows, fold_summaries = [], [], []
    for fold in folds:
        selected = [row for row in memberships if row['fold_id'] == fold['fold_id']]
        roles = {role: {_key(row) for row in selected if row['fold_role'] == role}
                 for role in ('fit', 'validation')}
        if roles['fit'] & roles['validation']:
            raise ValueError('A trip cannot occupy both fold roles')
        fold_rows = {role: [row for row in inputs['feature_rows'] if _key(row) in roles[role]]
                     for role in roles}
        tenants = sorted({row['tenant_id'] for row in selected})
        tenant_evidence = []
        for tenant in tenants:
            fit_rows = [row for row in fold_rows['fit'] if row['tenant_id'] == tenant]
            validation_rows = [row for row in fold_rows['validation'] if row['tenant_id'] == tenant]
            keys = {key for key in roles['fit'] if key[0] == tenant}
            state = (fit_transformer(fit_rows, tenant_id=tenant, fold_id=fold['fold_id'],
                                    training_cutoff_at=fold['training_cutoff_at'], fit_trip_keys=keys)
                     if fit_rows else None)
            if state is not None:
                transformers.append(state)
                for role, rows in (('fit', fit_rows), ('validation', validation_rows)):
                    for item in transform_rows(rows, state, role=role):
                        matrix_rows.append(dict(item, fold_id=fold['fold_id'], fold_role=role,
                                                status='available', reason=None))
            else:
                for row in validation_rows:
                    if parse_ts(row['snapshot_time']) < parse_ts(fold['training_cutoff_at']):
                        raise ValueError('Validation snapshot precedes fit cutoff')
                    matrix_rows.append(dict(
                        tenant_id=tenant, trip_id=row['trip_id'], snapshot_id=row['snapshot_id'],
                        snapshot_time=row['snapshot_time'], feature_definition_version=FEATURE_DEFINITION_VERSION,
                        fold_id=fold['fold_id'], fold_role='validation', status='unsupported',
                        reason='no_tenant_fit_support', vector=None))
            tenant_evidence.append(dict(tenant_id=tenant, fit_trips=len(keys), fit_snapshots=len(fit_rows),
                                        validation_trips=sum(key[0] == tenant for key in roles['validation']),
                                        validation_snapshots=len(validation_rows),
                                        output_dimensions=len(state['feature_names']) if state else None,
                                        preprocessing_status='available' if state else 'unsupported'))
        fold_summaries.append(dict(fold, tenants=tenant_evidence))
    matrix_rows.sort(key=lambda r: (r['fold_id'], r['fold_role'], r['tenant_id'], r['trip_id'], r['snapshot_time'], r['snapshot_id']))
    summary = dict(
        source_kind='original synthetic data', stage='M3b.1 feature preparation',
        feature_definition_version=FEATURE_DEFINITION_VERSION,
        original_role_trip_counts=inputs['original_role_trip_counts'],
        original_training_snapshots=len(inputs['feature_rows']),
        fold_count=len(folds), transformer_count=len(transformers),
        fit_matrix_rows=sum(row['fold_role'] == 'fit' for row in matrix_rows),
        validation_matrix_rows=sum(row['fold_role'] == 'validation' for row in matrix_rows),
        unsupported_validation_rows=sum(row['status'] == 'unsupported' for row in matrix_rows),
        position_quality_counts=dict(sorted(Counter(row['categorical']['position_validity'] for row in inputs['feature_rows']).items())),
        feature_missing_counts={name: sum(row['numeric'][name] is None for row in inputs['feature_rows'])
                                for name in ('elapsed_trip_seconds', 'route_distance_km', 'remaining_distance_km', 'gnss_speed_kmh', 'position_age_seconds')},
        fit_population='All fit-trip snapshots before cutoff, independent of target availability; snapshot-weighted preprocessing.',
        target_files_opened=False, candidate_model_fitted=False, calibration_used=False,
        final_test_evaluated=False, production_tables_populated=False,
        folds=fold_summaries)
    return dict(memberships=memberships, transformers=transformers, matrix_rows=matrix_rows, summary=summary)


def verify_feature_inputs(root):
    root = Path(root)
    reference = json.loads((root / 'reports/rebuild_report.json').read_text())
    if reference.get('status') != 'passed':
        raise ValueError('The retained M2 rebuild receipt is not passed')
    expected = {row['path']: row['original_sha256'] for row in reference['files']}
    actual = {}
    for relative in FEATURE_INPUT_FILES:
        digest = digest_file(root / relative)
        if digest != expected.get(relative):
            raise ValueError(f'Feature input differs from frozen M2: {relative}')
        actual[relative] = digest
    return actual


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')


def run(root, settings_path, output):
    root, settings_path, output = Path(root).resolve(), Path(settings_path).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError(f'Use a new output directory to preserve existing evidence: {output}')
    settings = json.loads(settings_path.read_text())
    if settings.get('feature_definition_version') != FEATURE_DEFINITION_VERSION:
        raise ValueError('Settings feature version does not match the implementation')
    source_config = json.loads((root / 'config/m2.json').read_text())
    frozen = json.loads((root / 'config/m3a.json').read_text())
    if digest_file(root / 'config/m2.json') != settings['expected_source_config_sha256']:
        raise ValueError('M2 configuration differs from the frozen feature protocol')
    if digest_file(root / 'reports/rebuild_report.json') != settings['expected_m2_rebuild_receipt_sha256']:
        raise ValueError('M2 rebuild receipt differs from the frozen feature protocol')
    if (digest_file(root / 'config/m3a.json') != settings['expected_m3a_config_sha256']
            or settings['folds'] != frozen['folds']):
        raise ValueError('M3a chronological folds must remain frozen')
    input_hashes = verify_feature_inputs(root)
    inputs = load_feature_inputs(root, source_config)
    result = prepare_folds(inputs, source_config, settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.m3b1-stage-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'complete'
        stage.mkdir()
        write_jsonl(stage / 'features.jsonl', inputs['feature_rows'])
        write_jsonl(stage / 'memberships.jsonl', result['memberships'])
        write_jsonl(stage / 'matrices.jsonl', result['matrix_rows'])
        _write_json(stage / 'transformers.json', result['transformers'])
        _write_json(stage / 'summary.json', result['summary'])
        modules = ('features.py', 'feature_transform.py', 'feature_run.py', 'model_splits.py', 'common.py')
        receipt = dict(
            batch_id=settings['batch_id'], executed_at_utc=datetime.now(timezone.utc).isoformat(),
            python_version=platform.python_version(), platform=platform.platform(),
            input_sha256=input_hashes, settings_sha256=digest_file(settings_path),
            feature_contract_sha256=digest_file(root / 'contracts/m3b1_features.json'),
            code_sha256={name: digest_file(Path(__file__).parent / name) for name in modules},
            output_sha256={path.name: digest_file(path) for path in sorted(stage.iterdir())},
            validation_scope='Frozen six-file feature replay; no targets, model training or heldout scoring.')
        _write_json(stage / 'run_receipt.json', receipt)
        stage.rename(output)
    return dict(output=str(output), **result['summary'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--config', type=Path, default=Path('config/m3b1.json'))
    parser.add_argument('--output', type=Path, default=Path('reports/m3b1_local'))
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.config, args.output), indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
