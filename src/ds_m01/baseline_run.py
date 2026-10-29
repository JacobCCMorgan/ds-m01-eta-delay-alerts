"""Run the M3a historical baseline on training-only chronological folds.

This experiment does not fill the production-shaped model/alert tables. It
leaves July calibration and the August/September final test unscored.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import platform
import tempfile

from .baseline import predict
from .baseline_metrics import summarize
from .common import digest_file, parse_ts, read_jsonl, utc, write_jsonl
from .model_splits import build_fold_memberships, validate_folds
from .pipeline import label_for_snapshot

INPUT_FILES = (
    'data/raw/eta_trip_event.jsonl',
    'data/raw/eta_trip_outcome.jsonl',
    'data/raw/eta_route_version.jsonl',
    'data/raw/eta_promise_version.jsonl',
    'data/processed/eta_snapshot.jsonl',
    'data/processed/eta_split_assignment.jsonl',
    'data/processed/trip_membership.jsonl',
    'data/processed/feature_lineage.jsonl',
)
MODEL_FIELDS = (
    'tenant_id', 'trip_id', 'snapshot_id', 'snapshot_time', 'route_id',
    'departure_time_bucket', 'route_distance_km', 'elapsed_trip_seconds',
)


def _key(row):
    return row['tenant_id'], row['trip_id']


def _read_scoped(path, allowed):
    """Discard protected groups before exposing any payload to model/evaluation."""
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            if _key(row) in allowed:
                yield row


def _unique(rows, fields):
    result = {}
    for row in rows:
        key = tuple(row[field] for field in fields)
        if key in result:
            raise ValueError(f'Duplicate key for {fields}: {key}')
        result[key] = row
    return result


def load_training_inputs(root, source_config):
    """Only original M2 training snapshots/outcomes reach the returned object.

    Source files contain all roles. The row gate is applied by tenant/trip key;
    this is exclusion from model/evaluation use, not a claim that the shared
    file bytes or storage engine pages were never read.
    """
    root = Path(root)
    assignments = _unique(read_jsonl(root/'data/processed/eta_split_assignment.jsonl'), ('tenant_id','trip_id'))
    membership = _unique(read_jsonl(root/'data/processed/trip_membership.jsonl'), ('tenant_id','trip_id'))
    if assignments.keys() != membership.keys():
        raise ValueError('Trip membership and original split keys disagree')
    for key, row in membership.items():
        if row['split_role'] != assignments[key]['split_role']:
            raise ValueError('Trip membership and original split roles disagree')
    allowed = {key for key, row in assignments.items() if row['split_role']=='train'}
    events = list(_read_scoped(root/'data/raw/eta_trip_event.jsonl', allowed))
    departures = defaultdict(list)
    for event in events:
        if event['event_type'] in ('departure','departed'):
            departures[_key(event)].append(event)
    trips = []
    for key in sorted(allowed):
        if not departures[key]:
            raise ValueError(f'Missing explicit departure for {key}')
        trip = dict(membership[key])
        trip['departure_time'] = utc(min(parse_ts(e['occurred_at']) for e in departures[key]))
        trips.append(trip)
    snapshots = list(_read_scoped(root/'data/processed/eta_snapshot.jsonl', allowed))
    _unique(snapshots, ('tenant_id','snapshot_id'))
    routes = _unique(read_jsonl(root/'data/raw/eta_route_version.jsonl'), ('tenant_id','route_version_id'))
    first_snapshot = {}
    enriched = []
    for snapshot in snapshots:
        at = parse_ts(snapshot['snapshot_time'])
        route = routes[(snapshot['tenant_id'], snapshot['route_version_id'])]
        if (parse_ts(route['known_at']) > at
                or (route.get('valid_from') and parse_ts(route['valid_from']) > at)
                or (route.get('valid_to') and at >= parse_ts(route['valid_to']))):
            raise ValueError('Snapshot route was not valid/known at its clock')
        item = dict(snapshot, route_id=route['route_id'], route_distance_km=route['planned_distance_km'])
        enriched.append(item)
        key = _key(item)
        if key not in first_snapshot or at < parse_ts(first_snapshot[key]['snapshot_time']):
            first_snapshot[key] = item
    lineage = {}
    snapshot_by_id = {(r['tenant_id'],r['snapshot_id']):r for r in enriched}
    with (root/'data/processed/feature_lineage.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            key = row['tenant_id'],row['snapshot_id']
            if key not in snapshot_by_id:
                continue
            if key in lineage or parse_ts(row['max_source_available_time']) > parse_ts(snapshot_by_id[key]['snapshot_time']):
                raise ValueError('Duplicate/future feature lineage')
            lineage[key] = row
    if lineage.keys() != snapshot_by_id.keys():
        raise ValueError('Training snapshots require complete feature lineage')
    outcomes, promises = defaultdict(list), defaultdict(list)
    for row in _read_scoped(root/'data/raw/eta_trip_outcome.jsonl', allowed):
        outcomes[_key(row)].append(row)
    for row in _read_scoped(root/'data/raw/eta_promise_version.jsonl', allowed):
        promises[_key(row)].append(row)
    return dict(trips=trips, snapshots=enriched, first_snapshot=first_snapshot,
                outcomes=outcomes, promises=promises,
                original_role_trip_counts=dict(Counter(r['split_role'] for r in assignments.values())))


def freeze_history(inputs, fit_keys, cutoff):
    """Select the latest outcome version known before the fit cutoff.

    Do not filter the latest M2 label version: a correction after fit time must
    leave the earlier known version available for this fold.
    """
    limit = parse_ts(cutoff)
    history, exclusion_counts = [], Counter()
    for key in sorted(fit_keys):
        first = inputs['first_snapshot'].get(key)
        if first is None or parse_ts(first['snapshot_time']) >= limit:
            exclusion_counts['no_snapshot_known_before_fit'] += 1
            continue
        versions = [r for r in inputs['outcomes'].get(key, []) if parse_ts(r['label_available_time']) < limit]
        if not versions:
            exclusion_counts['no_outcome_known_before_fit'] += 1
            continue
        outcome = max(versions, key=lambda r:(parse_ts(r['label_available_time']),json.dumps(r,sort_keys=True)))
        if outcome['label_status'] != 'verified' or not outcome.get('actual_arrival_time'):
            exclusion_counts['latest_outcome_not_verified'] += 1
            continue
        if outcome.get('arrival_time_precision') not in ('second','minute'):
            exclusion_counts['insufficient_arrival_precision'] += 1
            continue
        arrival = parse_ts(outcome['actual_arrival_time'])
        departure = parse_ts(first['snapshot_time']) - timedelta(seconds=first['elapsed_trip_seconds'])
        if arrival >= limit or arrival <= departure:
            exclusion_counts['arrival_outside_fit_or_nonpositive_duration'] += 1
            continue
        history.append(dict(
            tenant_id=key[0], trip_id=key[1], route_id=first['route_id'],
            route_version_id=first['route_version_id'],
            departure_time_bucket=first['departure_time_bucket'],
            route_distance_km=first['route_distance_km'],
            duration_seconds=(arrival-departure).total_seconds(),
            label_known_at=outcome['label_available_time'], actual_arrival_time=outcome['actual_arrival_time'],
            historical_outcome_version_id=outcome['outcome_version_id'],
            grouping_snapshot_id=first['snapshot_id'], grouping_known_at=first['snapshot_time'],
            departure_time=utc(departure), training_cutoff_at=utc(cutoff)))
    return history, dict(sorted(exclusion_counts.items()))


def evaluate(inputs, source_config, settings):
    """Predict each validation opportunity before joining any outcome target."""
    folds = validate_folds(settings['folds'], source_config)
    memberships = build_fold_memberships(inputs['trips'], folds, source_config)
    predictions, evaluation_rows, frozen_history, fold_evidence = [], [], [], []
    for fold in folds:
        fm = [r for r in memberships if r['fold_id']==fold['fold_id']]
        fit_keys = {_key(r) for r in fm if r['fold_role']=='fit'}
        val_keys = {_key(r) for r in fm if r['fold_role']=='validation'}
        history, exclusions = freeze_history(inputs, fit_keys, fold['training_cutoff_at'])
        frozen_history.extend(dict(row, fold_id=fold['fold_id']) for row in history)
        fold_predictions = []
        validation_snapshots = sorted((r for r in inputs['snapshots'] if _key(r) in val_keys),
                                      key=lambda r:(r['tenant_id'],r['trip_id'],r['snapshot_time'],r['snapshot_id']))
        for snapshot in validation_snapshots:
            if parse_ts(snapshot['snapshot_time']) < parse_ts(fold['training_cutoff_at']):
                raise ValueError('Validation prediction precedes model fit cutoff')
            model_input = {field:snapshot.get(field) for field in MODEL_FIELDS}
            answer = predict(model_input, history, fold['training_cutoff_at'],
                             settings['min_support'], settings['distance_bin_km'])
            prediction = dict(model_input, **answer, fold_id=fold['fold_id'],
                              baseline_definition_version=settings['baseline_definition_version'],
                              training_cutoff_at=fold['training_cutoff_at'])
            for key, field in [('p10_remaining_seconds','eta_lower'),('p50_remaining_seconds','eta_median'),('p90_remaining_seconds','eta_upper')]:
                prediction[field] = (utc(parse_ts(snapshot['snapshot_time'])+timedelta(seconds=answer[key]))
                                     if answer['status']=='available' else None)
            fold_predictions.append(prediction)
        # Evaluation is a separate pass. Missing/cancelled/already-arrived labels
        # never decide whether a prediction opportunity is emitted.
        for snapshot, prediction in zip(validation_snapshots, fold_predictions):
            key = _key(snapshot)
            label = label_for_snapshot(snapshot, inputs['outcomes'].get(key,[]), inputs['promises'].get(key,[]), fold['validation_label_cutoff_at'])
            elapsed = snapshot['elapsed_trip_seconds']
            bucket = '0-60m' if elapsed<3600 else '60-180m' if elapsed<10800 else '180-360m' if elapsed<21600 else '360m+'
            evaluation_rows.append(dict(prediction, label_eligibility=label['label_eligibility'],
                                       remaining_duration_seconds=label['remaining_duration_seconds'],
                                       outcome_version_id=label['outcome_version_id'],
                                       evaluation_label_cutoff_at=fold['validation_label_cutoff_at'],
                                       position_validity=snapshot['position_validity'], elapsed_slice=bucket))
        predictions.extend(fold_predictions)
        fold_evidence.append(dict(fold, fit_trips=len(fit_keys), validation_trips=len(val_keys),
                                  validation_snapshots=len(validation_snapshots), historical_trips=len(history),
                                  historical_exclusions=exclusions))
    kwargs = dict(bootstrap_samples=settings['bootstrap_samples'], bootstrap_seed=settings['bootstrap_seed'])
    metrics = {'scope':'training-period chronological validation on synthetic 2026 scenarios',
               'weighting':'Each supported eligible trip has weight one; its snapshots share that weight.',
               'interval_status':'Raw conditional empirical P10/P90; not calibrated.',
               'overall':summarize(evaluation_rows,**kwargs), 'slices':{}}
    for field in ('fold_id','tenant_id','route_id','position_validity','elapsed_slice','pool'):
        groups = defaultdict(list)
        for row in evaluation_rows:
            groups[str(row.get(field) or 'unsupported')].append(row)
        metrics['slices'][field] = {key:summarize(values,**kwargs) for key, values in sorted(groups.items())}
    return dict(memberships=memberships, predictions=predictions, evaluation_rows=evaluation_rows,
                frozen_history=frozen_history, folds=fold_evidence, metrics=metrics)


def verify_frozen_inputs(root):
    """Check the exact model inputs against the retained M2 replay evidence."""
    root = Path(root)
    reference = json.loads((root/'reports/rebuild_report.json').read_text())
    if reference.get('status')!='passed':
        raise ValueError('The retained M2 rebuild receipt is not passed')
    expected = {r['path']:r['original_sha256'] for r in reference['files']}
    actual = {}
    for relative in INPUT_FILES:
        digest = digest_file(root/relative)
        if relative not in expected or digest != expected[relative]:
            raise ValueError(f'Input differs from the frozen M2 dataset: {relative}')
        actual[relative] = digest
    return actual


def _write_json(path, value):
    path.write_text(json.dumps(value,indent=2,sort_keys=True,allow_nan=False)+'\n',encoding='utf-8')


def run(root, settings_path, output):
    root, settings_path, output = Path(root).resolve(), Path(settings_path).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError(f'Use a new output directory to preserve existing evidence: {output}')
    settings = json.loads(settings_path.read_text())
    source_config = json.loads((root/'config/m2.json').read_text())
    if digest_file(root/'config/m2.json') != settings['expected_source_config_sha256']:
        raise ValueError('M2 configuration differs from the M3a frozen input protocol')
    if digest_file(root/'reports/rebuild_report.json') != settings['expected_m2_rebuild_receipt_sha256']:
        raise ValueError('M2 rebuild receipt differs from the M3a frozen input protocol')
    if source_config.get('milestone')!='M2' or source_config.get('sample_size')!=1800:
        raise ValueError('This batch targets the frozen 1,800-trip M2 dataset')
    input_hashes = verify_frozen_inputs(root)
    inputs = load_training_inputs(root, source_config)
    results = evaluate(inputs,source_config,settings)
    manifest = dict(batch_id=settings['batch_id'],baseline_definition_version=settings['baseline_definition_version'],
                    source_kind='synthetic', source_dataset_id=source_config['source_dataset_id'],
                    settings=settings, feature_allowlist=list(MODEL_FIELDS),
                    original_role_trip_counts=inputs['original_role_trip_counts'],
                    evaluated_original_roles=['train'], protected_roles=['calibration','test','excluded'],
                    calibration_performed=False, final_test_evaluated=False,
                    delay_probability_model_fitted=False, operational_alerts_emitted=False,
                    folds=results['folds'])
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.m3a-stage-',dir=output.parent) as temporary:
        stage=Path(temporary)/'complete';stage.mkdir()
        for key in ('memberships','frozen_history','predictions','evaluation_rows'):
            write_jsonl(stage/f'{key}.jsonl', results[key])
        _write_json(stage/'metrics.json',results['metrics'])
        _write_json(stage/'model_manifest.json',manifest)
        modules=('baseline.py','model_splits.py','baseline_metrics.py','baseline_run.py','common.py','pipeline.py')
        receipt=dict(batch_id=settings['batch_id'],executed_at_utc=datetime.now(timezone.utc).isoformat(),
                     python_version=platform.python_version(),platform=platform.platform(),
                     input_sha256=input_hashes,settings_sha256=digest_file(settings_path),
                     source_config_sha256=digest_file(root/'config/m2.json'),
                     code_sha256={name:digest_file(Path(__file__).parent/name) for name in modules},
                     output_sha256={p.name:digest_file(p) for p in sorted(stage.iterdir())},
                     verification='Input hashes matched the retained M2 rebuild receipt; all fold and baseline guards passed.')
        _write_json(stage/'run_receipt.json',receipt)
        stage.rename(output)
    return {'output':str(output),'folds':len(results['folds']), 'overall':results['metrics']['overall']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('.'),help='Existing complete M2 data root')
    parser.add_argument('--config',type=Path,default=None,help='M3a experiment config')
    parser.add_argument('--output',type=Path,default=None,help='New output directory; existing evidence is never overwritten')
    args=parser.parse_args()
    print(json.dumps(run(args.root,args.config or args.root/'config/m3a.json',
                         args.output or args.root/'reports/m3a_local'),indent=2,allow_nan=False))


if __name__=='__main__':
    main()
