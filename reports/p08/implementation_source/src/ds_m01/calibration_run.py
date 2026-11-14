"""Frozen July calibration experiment, separating base fit, calibration and assessment."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import tempfile

from .common import digest_file, read_jsonl
from .candidate_model import canonical_digest
from .calibration_split import validate_settings
from .calibration_refit import prepare_refit_inputs, refit_and_predict, verify_source_inputs
from .calibration_models import (fit_interval_calibrator, fit_probability_calibrator,
                                 apply_interval_calibrator, apply_probability_calibrator,
                                 calibrator_digest)
from .calibration_targets import build_eta_targets
from .delay_targets import build_delay_targets
from .calibration_metrics import build_metrics

PROTOCOL_SHA256 = '1c898e5a4e6f095f58d1b6321deb029b4a34ce4cc6456ecaf15e58582147aa87'
ARTIFACT_LISTS = ('memberships', 'features', 'matrices', 'promise_features', 'eta_training_rows',
                  'delay_training_rows', 'eta_predictions', 'delay_predictions', 'baseline_predictions')
ARTIFACT_JSON = ('transformers', 'eta_models', 'delay_models', 'frozen_history', 'summary')
EVALUATION_LISTS = ('calibration_fit_eta_rows', 'calibration_fit_probability_rows',
                    'assessment_eta_rows', 'assessment_probability_rows')
CODE_MODULES = ('calibration_split.py','calibration_refit.py','calibration_models.py','calibration_targets.py',
                'calibration_metrics.py','calibration_run.py','calibration_replay.py','delay_targets.py','delay_model.py',
                'delay_run.py','delay_metrics.py','candidate_model.py','candidate_targets.py','candidate_run.py',
                'feature_run.py','feature_transform.py','features.py','baseline.py','baseline_run.py',
                'baseline_metrics.py','common.py','model_splits.py')


def _key(r):
    return r['tenant_id'], r['snapshot_id']


def _trip(r):
    return r['tenant_id'], r['trip_id']


def _index(rows):
    result = {}
    for r in rows:
        if _key(r) in result:
            raise ValueError('Duplicate retained opportunity')
        result[_key(r)] = r
    return result


def verify_settings(root, settings_path):
    root, path = Path(root), Path(settings_path)
    if digest_file(path) != PROTOCOL_SHA256:
        raise ValueError('Protocol differs from the pre-outcome freeze')
    settings = json.loads(path.read_text())
    source = json.loads((root/'config/m2.json').read_text())
    validate_settings(settings, source)
    for name, expected in settings['expected_parent_config_sha256'].items():
        if digest_file(root/name) != expected:
            raise ValueError(f'Frozen parent configuration changed: {name}')
    return source, settings


def prepare_role_rows(artifacts, input_root, settings, source_config):
    """Join only fit/assessment outcomes, after all raw predictions are retained."""
    verify_source_inputs(input_root, settings)
    source_inputs=prepare_refit_inputs(input_root,source_config,settings)
    for name in ('memberships','features'):
        if artifacts[name] != source_inputs[name]:
            raise ValueError(f'Retained {name} differs from complete source inventory')
    members = {_trip(r): r for r in source_inputs['memberships']}
    eta = _index(artifacts['eta_predictions'])
    probability = _index(r for r in artifacts['delay_predictions'] if r['method_id'] == 'logistic_delay')
    promises = _index(artifacts['promise_features'])
    if eta.keys() != probability.keys():
        raise ValueError('ETA and probability pipelines lost different July opportunities')
    expected = {_key(r) for r in artifacts['features'] if members[_trip(r)]['role'] in ('calibration_fit','calibration_buffer','assessment')}
    if eta.keys() != expected:
        raise ValueError('Raw forecasts do not preserve all July opportunities')
    feature_index=_index(source_inputs['features'])
    snapshot_index=_index(source_inputs['snapshots'])
    for key,pred in eta.items():
        p=probability[key]
        promise=promises[key]
        snap=snapshot_index[key]
        for field in ('tenant_id','trip_id','snapshot_id','snapshot_time','promise_version_id'):
            if any(row.get(field)!=snap[field] for row in (pred,p,promise)):
                raise ValueError(f'Raw forecast/target identity mismatch: {field}')
        for field in ('fold_id','route_id','position_validity','elapsed_trip_seconds','deadline_remaining_seconds'):
            if pred.get(field)!=p.get(field):
                raise ValueError(f'Raw ETA/probability covariates disagree: {field}')
        f=feature_index[key]
        if (pred['fold_id']!=settings['final_training_id'] or pred['route_id']!=f['categorical']['route_id']
                or pred['position_validity']!=f['categorical']['position_validity']
                or pred['elapsed_trip_seconds']!=f['numeric']['elapsed_trip_seconds']
                or pred['deadline_remaining_seconds']!=promise['promise_seconds_remaining']
                or pred['deadline_at']!=promise['deadline_at'] or p['deadline_at']!=promise['deadline_at']):
            raise ValueError('Raw forecast metadata differs from frozen feature/promise inputs')
    result = {}
    for role, cutoff in (('calibration_fit', settings['calibration_fit_cutoff_at']), ('assessment', settings['assessment_label_cutoff_at'])):
        keys = {key for key,r in members.items() if r['role'] == role}
        raw = [r for r in eta.values() if _trip(r) in keys]
        opportunities = [promises[_key(r)] for r in raw]
        eta_targets = _index(build_eta_targets(opportunities, keys,
            read_jsonl(Path(input_root)/'data/raw/eta_trip_outcome.jsonl'), cutoff))
        delay_targets = _index(build_delay_targets(opportunities, keys,
            read_jsonl(Path(input_root)/'data/raw/eta_trip_outcome.jsonl'),
            read_jsonl(Path(input_root)/'data/raw/eta_promise_version.jsonl'), cutoff))
        eta_rows, probability_rows = [], []
        for pred in sorted(raw,key=lambda r:(_trip(r),r['snapshot_time'],r['snapshot_id'])):
            key = _key(pred)
            p = probability[key]
            member = members[_trip(pred)]
            meta = {name: pred[name] for name in ('tenant_id','trip_id','snapshot_id','snapshot_time','fold_id',
                                                 'route_id','position_validity','elapsed_trip_seconds','deadline_remaining_seconds')}
            meta.update(calibration_role=role, departure_time=member['departure_time'],
                        variant='raw', calibration_applied=False, calibration_reason='raw_forecast', calibration_status='not_applied')
            eta_status = 'available' if pred['status'] == 'available' else 'unavailable'
            source = {'candidate':'native','baseline_fallback':'baseline_fallback','unavailable':'unavailable'}.get(pred['prediction_source'], pred['prediction_source'])
            eta_rows.append({**meta, **eta_targets[key], 'status':eta_status,'prediction_source':source,
                             'raw_model_sha256':pred['model_sha256'],
                             'lower_seconds':pred['p10_remaining_seconds'],
                             'median_seconds':pred['p50_remaining_seconds'],
                             'upper_seconds':pred['p90_remaining_seconds']})
            probability_rows.append({**meta, **delay_targets[key], 'status':p['status'],
                                     'prediction_source':p['prediction_source'], 'probability':p['probability'],
                                     'raw_model_sha256':p['model_sha256']})
        result[role] = {'eta':eta_rows,'probability':probability_rows}
    return result


def source_bindings(artifacts, settings):
    return {'protocol_sha256':PROTOCOL_SHA256,
            'source_files_sha256':canonical_digest(settings['expected_source_sha256']),
            'eta_models_sha256':canonical_digest(artifacts['eta_models']),
            'delay_models_sha256':canonical_digest(artifacts['delay_models']),
            'transformers_sha256':canonical_digest(artifacts['transformers']),
            'history_sha256':canonical_digest(artifacts['frozen_history'])}


def fit_calibrators(artifacts, role_rows, settings):
    members = artifacts['memberships']
    tenants = sorted({r['tenant_id'] for r in members if r['role']=='train'})
    states = {'eta': [], 'probability': []}
    bindings = source_bindings(artifacts,settings)
    for tenant in tenants:
        metadata = dict(snapshot_start_at=settings['training_cutoff_at'],
                        snapshot_end_at=settings['calibration_fit_cutoff_at'],
                        label_cutoff_at=settings['calibration_fit_cutoff_at'],
                        fit_trip_keys=[list(_trip(r)) for r in members if r['tenant_id']==tenant and r['role']=='calibration_fit'])
        for family, fit in (('eta',fit_interval_calibrator), ('probability',fit_probability_calibrator)):
            rows=[r for r in role_rows['calibration_fit'][family] if r['tenant_id']==tenant
                  and r['eligibility']=='eligible' and r['status']=='available']
            states[family].append(fit(rows,tenant_id=tenant,fit_metadata=metadata,source_bindings=bindings))
    return states


def apply_assessment(assessment, states):
    out = {}
    for family, apply in (('eta',apply_interval_calibrator), ('probability',apply_probability_calibrator)):
        raw=assessment[family]
        calibrators={state['tenant_id']:state for state in states[family]}
        if set(calibrators) != {r['tenant_id'] for r in raw}:
            raise ValueError('Calibrators do not cover exactly the assessment tenants')
        calibrated=[]
        for tenant,state in sorted(calibrators.items()):
            calibrated.extend(apply(state,[r for r in raw if r['tenant_id']==tenant]))
        for r in calibrated:
            r['variant']='calibrated'
            r['calibration_status']='fitted' if r['calibration_applied'] else 'unavailable' if r['status']=='unavailable' else 'unsupported'
        out[family]=sorted([*raw,*calibrated],key=lambda r:(r['variant'],r['tenant_id'],r['trip_id'],r['snapshot_time'],r['snapshot_id']))
    return out


def _write_json(path,value):
    path.write_text(json.dumps(value,indent=2,sort_keys=True,allow_nan=False)+'\n')


def _write_rows(path,rows):
    with path.open('w') as f:
        for row in rows:
            f.write(json.dumps(row,sort_keys=True,allow_nan=False)+'\n')


def write_refit(path, artifacts):
    for name in ARTIFACT_LISTS:
        _write_rows(path/(name+'.jsonl'),artifacts[name])
    for name in ARTIFACT_JSON:
        _write_json(path/('refit_'+name+'.json'),artifacts[name])


def read_refit(path):
    path=Path(path)
    return {**{name:list(read_jsonl(path/(name+'.jsonl'))) for name in ARTIFACT_LISTS},
            **{name:json.loads((path/('refit_'+name+'.json')).read_text()) for name in ARTIFACT_JSON}}


def run(root, input_root, output):
    root,input_root,output=map(lambda p:Path(p).resolve(),(root,input_root,output))
    if output.exists():
        raise ValueError('Use a new output directory; retain previous experiment evidence')
    source,settings=verify_settings(root,root/'config/m3d.json')
    verify_source_inputs(input_root,settings)
    start=datetime.now(timezone.utc).isoformat()
    code_before={name:digest_file(root/'src/ds_m01'/name) for name in CODE_MODULES}
    artifacts=refit_and_predict(prepare_refit_inputs(input_root,source,settings),source,settings)
    output.parent.mkdir(parents=True,exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix='.m3d-',dir=output.parent))
    try:
        write_refit(staging,artifacts)
        raw_saved_at=datetime.now(timezone.utc).isoformat()
        roles=prepare_role_rows(artifacts,input_root,settings,source)
        states=fit_calibrators(artifacts,roles,settings)
        paired=apply_assessment(roles['assessment'],states)
        metrics=build_metrics(paired['eta'],paired['probability'])
        for family in ('eta','probability'):
            _write_rows(staging/f'calibration_fit_{family}_rows.jsonl',roles['calibration_fit'][family])
            _write_rows(staging/f'assessment_{family}_rows.jsonl',paired[family])
        _write_json(staging/'calibrators.json',states)
        _write_json(staging/'metrics.json',metrics)
        summary={'refit':artifacts['summary'],
                 'role_trip_counts':dict(sorted(Counter(r['role'] for r in artifacts['memberships']).items())),
                 'calibrators':{family:[{k:s[k] for k in ('tenant_id','status','reason','fit_trip_count','fit_snapshot_count')} for s in states[family]] for family in states},
                 'assessment':{family:metrics[family]['assessment']['overall'] for family in metrics if family in ('eta','probability')},
                 'fit_diagnostics':'Retained fitting rows, trip scores and objective are in-sample diagnostics. No calibrated forecasts are applied to fit trips.',
                 'final_test_evaluated':False,'policy_threshold_selected':False}
        _write_json(staging/'summary.json',summary)
        verify_source_inputs(input_root,settings)
        verify_settings(root,root/'config/m3d.json')
        if code_before!={name:digest_file(root/'src/ds_m01'/name) for name in CODE_MODULES}:
            raise ValueError('Execution source changed during the experiment')
        receipt={'status':'completed_calibration_assessment','protocol_sha256':PROTOCOL_SHA256,
                 'base_commit_sha':settings['base_commit_sha'],'started_at_utc':start,
                 'raw_predictions_saved_before_july_target_join_at_utc':raw_saved_at,
                 'completed_at_utc':datetime.now(timezone.utc).isoformat(),
                 'base_training_executed_in_this_command':True,
                 'calibrator_fitting_executed':True,
                 'base_training_cutoff_at':settings['training_cutoff_at'],
                 'calibration_fit_cutoff_at':settings['calibration_fit_cutoff_at'],
                 'assessment_label_cutoff_at':settings['assessment_label_cutoff_at'],
                 'input_sha256':settings['expected_source_sha256'],
                 'parent_config_sha256':settings['expected_parent_config_sha256'],
                 'code_sha256':{name:digest_file(root/'src/ds_m01'/name) for name in CODE_MODULES},
                 'output_sha256':{p.name:digest_file(p) for p in sorted(staging.iterdir())},
                 'buffer_labels_used':False,'assessment_labels_used_for_fitting':False,
                 'july_labels_used_for_base_training':False,'final_test_evaluated':False,
                 'source_access':'All shared source bytes hashed/decoded; only declared train/fit/assessment keys and cutoff-known outcome payloads used. Reading bytes is not evidence of target use.',
                 'execution_scope':'Synthetic retrospective experiment; cutoffs are simulated information clocks. Portable retained-model inference is distinct from cross-platform training reproduction.'}
        _write_json(staging/'run_receipt.json',receipt)
        staging.rename(output)
        return receipt
    except BaseException:
        shutil.rmtree(staging)
        raise


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--input-root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    print(json.dumps(run(a.root,a.input_root,a.output),indent=2))


if __name__=='__main__':
    main()
