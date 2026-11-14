"""Apply frozen R01 states to final/stress features without fitting or target IO."""
from pathlib import Path
import json
from zoneinfo import ZoneInfo

from .alert_inputs import build_registry, eta_projection, probability_projection
from .calibration_models import (apply_interval_calibrator, apply_probability_calibrator,
                                 validate_interval_calibrator, validate_probability_calibrator)
from .candidate_model import canonical_digest, predict_candidate, validate_model
from .candidate_run import _prediction
from .common import parse_ts, utc
from .delay_model import validate_delay_model
from .delay_run import _prediction_rows
from .feature_transform import transform_rows, validate_transformer
from .final_inputs import index, key, trip, require, verify_settings
from .final_stress import apply_stress

STATE_FILES = {'transformers':'refit_transformers.json','eta_models':'refit_eta_models.json',
               'delay_models':'refit_delay_models.json','frozen_history':'refit_frozen_history.json',
               'calibrators':'calibrators.json'}
META = ('tenant_id','trip_id','snapshot_id','snapshot_time','vehicle_id','route_id','position_validity',
        'departure_time','departure_month','novelty_group','unseen_route','unseen_vehicle','elapsed_trip_seconds',
        'departure_time_bucket','deadline_remaining_seconds','promise_version_id','deadline_at',
        'promise_known_at','promise_provenance_id')
TARGET_FIELDS = {'actual_arrival_time','remaining_duration_seconds','target_seconds','delay_label',
                 'delay_seconds','eligibility','label_status','label_known_at','outcome_version_id',
                 'outcome_provenance_id','arrival_evidence_ref','arrival_time_precision'}


def _reject_targets(value):
    if isinstance(value,dict):
        require(not TARGET_FIELDS.intersection(value),'Final inference received target fields')
        for item in value.values():_reject_targets(item)
    elif isinstance(value,(list,tuple)):
        for item in value:_reject_targets(item)


def load_states(root,settings):
    root = Path(root)
    require(verify_settings(root)==settings,'Inference settings differ from frozen protocol')
    report = root/'reports/m3d'
    states = {key:json.loads((report/name).read_text()) for key,name in STATE_FILES.items()}
    p06 = json.loads((root/'config/p06.json').read_text())
    states['model_registry'] = build_registry(report,p06,'m1-split-v1')
    return states


def validate_states(states,source_config):
    cutoff = utc(source_config['split']['train_label_cutoff_exclusive'])
    transformers = index(states['transformers'],('tenant_id',))
    eta = index(states['eta_models'],('tenant_id','candidate_id'))
    delay = index(states['delay_models'],('tenant_id','method_id'))
    registry = index(states['model_registry'],('tenant_id',))
    tenants = {key[0] for key in transformers}
    require(set(eta)=={(t,'gradient_boosting_quantile') for t in tenants}
            and set(delay)=={(t,m) for t in tenants for m in ('fold_prevalence','logistic_delay')}
            and set(registry)==set(transformers),'Frozen final model inventory differs')
    all_fit = set()
    for (tenant,),transformer in transformers.items():
        validate_transformer(transformer)
        require(transformer['fold_id']=='R01' and utc(transformer['training_cutoff_at'])==cutoff,
                'Final inference must preserve R01 original training cutoff')
        all_fit.update(tuple(k) for k in transformer['fit_trip_keys'])
        validate_model(eta[tenant,'gradient_boosting_quantile'],transformer)
        for method in ('fold_prevalence','logistic_delay'):
            validate_delay_model(delay[tenant,method],transformer)
        bindings = registry[tenant,]['source_bindings']
        for field,state in (('transformer_sha256',transformer),('eta_model_sha256',eta[tenant,'gradient_boosting_quantile']),
                            ('probability_model_sha256',delay[tenant,'logistic_delay'])):
            require(bindings[field]==canonical_digest(state),'Registry differs from frozen state: '+field)
    for family,validator,binding in (('eta',validate_interval_calibrator,'interval_calibrator_sha256'),
                                      ('probability',validate_probability_calibrator,'probability_calibrator_sha256')):
        calibrators = index(states['calibrators'][family],('tenant_id',))
        require(set(calibrators)==set(transformers),'Final calibrator tenant inventory differs')
        for (tenant,),calibrator in calibrators.items():
            validator(calibrator)
            require(registry[tenant,]['source_bindings'][binding]==canonical_digest(calibrator),
                    'Registry differs from frozen calibrator')
    index(states['frozen_history'],('tenant_id','trip_id'))
    for row in states['frozen_history']:
        require(trip(row) in all_fit and utc(row['training_cutoff_at'])==cutoff
                and parse_ts(row['actual_arrival_time'])<=parse_ts(row['label_known_at'])<parse_ts(cutoff),
                'Final historical pool exceeds original training scope')
    return transformers,eta,registry


def _calibrated_rows(raw,calibrators,apply,family):
    result=[]
    for state in calibrators:
        calibrated=apply(state,[r for r in raw if r['tenant_id']==state['tenant_id']])
        for row in calibrated:
            row.update(variant='calibrated',calibration_status='fitted' if row['calibration_applied'] else
                       'unavailable' if row['status']=='unavailable' else 'unsupported')
            result.append(row)
            canonical=dict(row,variant='canonical')
            if family=='eta':
                values=eta_projection(*(row[f] for f in ('lower_seconds','median_seconds','upper_seconds'))) if row['status']=='available' else (None,None,None)
                canonical.update(zip(('lower_seconds','median_seconds','upper_seconds'),values))
            else:
                value=probability_projection(row['probability']) if row['status']=='available' else None
                canonical.update(probability=float(value) if value is not None else None,canonical_probability=value)
            result.append(canonical)
    return result


def predict(inputs,states,source_config,settings,scenario_id='S00_unmodified'):
    """Produce all forecasts and precise/canonical audits before any label join."""
    _reject_targets(inputs)
    transformers,models,registry = validate_states(states,source_config)
    members=index(inputs['memberships'],('tenant_id','trip_id'))
    context=index(inputs['snapshots'],('tenant_id','snapshot_id'))
    promises=index(inputs['promise_features'],('tenant_id','snapshot_id'))
    require(set(context)==set(promises)=={key(r) for r in inputs['features']},'Final feature/promise/context inventories differ')
    require({trip(r) for r in context.values()}==set(members),'Final inference lost declared trips')
    require({(r['tenant_id'],) for r in context.values()}<=set(transformers),'Final tenant lacks frozen state')
    zone=ZoneInfo(settings['population']['timezone'])
    start,end=settings['population']['departure_dates_inclusive']
    original_features=index(inputs['features'],('tenant_id','snapshot_id'))
    for member in members.values():
        require(member['original_split_role']=='test' and member['role']=='test','Only original final trips enter final inference')
        require(start<=parse_ts(member['departure_time']).astimezone(zone).date().isoformat()<=end,
                'Final inference departure lies outside frozen test dates')
    for k,row in context.items():
        promise=promises[k];feature=original_features[k];member=members[trip(row)]
        require(parse_ts(member['departure_time'])<=parse_ts(row['snapshot_time'])<parse_ts(settings['population']['label_cutoff_exclusive']),
                'Final inference snapshot lies outside frozen departure/cutoff')
        require(trip(feature)==trip(row) and parse_ts(feature['snapshot_time'])==parse_ts(row['snapshot_time'])
                and feature['route_version_id']==row['route_version_id']
                and feature['categorical']['route_id']==row['route_id']==member['route_id']
                and feature['categorical']['position_validity']==row['position_validity']
                and feature['numeric']['elapsed_trip_seconds']==row['elapsed_trip_seconds']
                and row['vehicle_id']==member['vehicle_id'] and row['novelty_group']==member['novelty_group'],
                'Original final features/context membership binding differs')
        require(trip(row)==trip(promise) and row['promise_version_id']==promise['promise_version_id']
                and parse_ts(row['snapshot_time'])==parse_ts(promise['snapshot_time'])
                and parse_ts(row['deadline_at'])==parse_ts(promise['deadline_at'])
                and parse_ts(row['promise_known_at'])==parse_ts(promise['promise_known_at'])
                and row['promise_provenance_id']==promise['promise_provenance_id']
                and row['deadline_remaining_seconds']==promise['promise_seconds_remaining']
                and parse_ts(promise['promise_known_at'])<=parse_ts(row['snapshot_time']), 'Final promise/context binding differs')
    stress=apply_stress(inputs['features'],scenario_id,settings,states['transformers'])
    features=index(stress['features'],('tenant_id','snapshot_id'))
    matrices=[]
    for (tenant,),state in sorted(transformers.items()):
        matrices.extend(dict(r,status='available',reason=None,calibration_role='test') for r in
                        transform_rows([f for f in features.values() if f['tenant_id']==tenant],state,role='validation'))
    eta_raw,eta_baseline=[],[]
    for (tenant,),state in sorted(transformers.items()):
        model=models[tenant,'gradient_boosting_quantile']
        rows=[r for r in matrices if r['tenant_id']==tenant]
        answers=index(predict_candidate(model,rows,state),('tenant_id','snapshot_id'))
        for row in rows:
            k=key(row); feature=features[k]; original=context[k]
            require(trip(row)==trip(original) and parse_ts(row['snapshot_time'])==parse_ts(original['snapshot_time']),
                    'Final features/context identity differs')
            meta={field:original[field] for field in META}
            meta.update(scenario_id=scenario_id,fold_id='R01',calibration_role='test',calibration_status='not_applied',
                        calibration_applied=False,calibration_reason='raw_forecast',variant='raw',
                        input_route_id=feature['categorical']['route_id'],input_position_validity=feature['categorical']['position_validity'])
            answer=dict(answers[k],model_sha256=canonical_digest(model))
            for output,candidate,forecast in ((eta_raw,'gradient_boosting_quantile',answer),
                    (eta_baseline,'historical_baseline',{'status':'unsupported','reason':'reference_baseline'})):
                pred=_prediction(row,forecast,feature,states['frozen_history'],
                                 {'fold_id':'R01','training_cutoff_at':state['training_cutoff_at']},candidate,state)
                source={'candidate':'native','baseline_fallback':'baseline_fallback','unavailable':'unavailable'}[pred['prediction_source']]
                if candidate=='historical_baseline' and pred['status']=='available':source='baseline'
                output.append(dict(meta,method_id='pipeline' if candidate=='gradient_boosting_quantile' else 'historical_baseline',
                    status=pred['status'],prediction_source=source,raw_model_sha256=pred['model_sha256'],
                    candidate_status=pred['candidate_status'],candidate_reason=pred['candidate_reason'],
                    fallback_reason=pred['fallback_reason'],fallback_pool=pred['fallback_pool'],
                    fallback_surviving_count=pred['fallback_surviving_count'],
                    lower_seconds=pred['p10_remaining_seconds'],median_seconds=pred['p50_remaining_seconds'],upper_seconds=pred['p90_remaining_seconds']))
    probability_raw,prevalence=[],[]
    for pred in _prediction_rows(states['delay_models'],matrices,stress['features'],states['transformers'],inputs['promise_features']):
        original=context[key(pred)]
        meta={field:original[field] for field in META}
        meta.update(scenario_id=scenario_id,fold_id='R01',calibration_role='test',calibration_status='not_applied',
                    calibration_applied=False,calibration_reason='raw_forecast',variant='raw',
                    input_route_id=pred['route_id'],input_position_validity=pred['position_validity'])
        output=probability_raw if pred['method_id']=='logistic_delay' else prevalence
        output.append(dict(meta,method_id='pipeline' if pred['method_id']=='logistic_delay' else 'fold_prevalence',
                           status=pred['status'],prediction_source=pred['prediction_source'],probability=pred['probability'],
                           raw_model_sha256=pred['model_sha256'],candidate_status=pred['candidate_status'],
                           candidate_reason=pred['candidate_reason'],fallback_model_sha256=pred['fallback_model_sha256']))
    eta_rows=[*eta_raw,*eta_baseline,*_calibrated_rows(eta_raw,states['calibrators']['eta'],apply_interval_calibrator,'eta')]
    probability_rows=[*probability_raw,*prevalence,*_calibrated_rows(probability_raw,states['calibrators']['probability'],apply_probability_calibrator,'probability')]
    eta=index((r for r in eta_rows if r['variant']=='calibrated'),('tenant_id','snapshot_id'))
    probability=index((r for r in probability_rows if r['variant']=='calibrated'),('tenant_id','snapshot_id'))
    require(set(eta)==set(probability)==set(context),'Final forecasts dropped opportunities')
    opportunities,projection=[] ,[]
    for k,original in sorted(context.items(),key=lambda p:(parse_ts(p[1]['snapshot_time']),*p[0])):
        a,b=eta[k],probability[k]
        item={field:original[field] for field in ('tenant_id','trip_id','snapshot_id','snapshot_time','vehicle_id','route_id',
              'departure_time','trip_status_as_of','promise_version_id','deadline_at','promise_known_at','promise_provenance_id',
              'position_validity','elapsed_trip_seconds','unseen_route','unseen_vehicle',
              'novelty_group','departure_month','deadline_remaining_seconds','departure_time_bucket')}
        item.update(scenario_id=scenario_id,snapshot_provenance_id=original['provenance_id'],
                    prediction_version=registry[original['tenant_id'],]['prediction_version'],
                    eta_status=a['status'],eta_reason=a.get('calibration_reason') or 'available',
                    eta_source=a['prediction_source'],eta_calibration_status=a['calibration_status'],
                    probability_status=b['status'],probability_reason=b.get('calibration_reason') or 'available',
                    probability_source=b['prediction_source'],probability_calibration_status=b['calibration_status'])
        values=tuple(a[field] for field in ('lower_seconds','median_seconds','upper_seconds'))
        projected=eta_projection(*values) if a['status']=='available' else (None,None,None)
        item.update(zip(('lower_seconds','median_seconds','upper_seconds'),values))
        item.update(zip(('lower_seconds_int','median_seconds_int','upper_seconds_int'),projected))
        item.update(raw_probability=b['raw_probability'],calibrated_probability=b['probability'],
                    probability=probability_projection(b['probability']) if b['status']=='available' else None,
                    fallback_reason='eta='+str(a['prediction_source'])+';probability='+str(b['prediction_source'])
                    +';interval_calibration='+str(a['calibration_status'])+';probability_calibration='+str(b['calibration_status']))
        opportunities.append(item)
        projection.append(dict(tenant_id=item['tenant_id'],trip_id=item['trip_id'],snapshot_id=item['snapshot_id'],
                               scenario_id=scenario_id,precise_interval=list(values),stored_interval=list(projected),
                               precise_probability=b['probability'],stored_probability=item['probability']))
    order=lambda r:(r['method_id'],r['variant'],r['tenant_id'],r['trip_id'],r['snapshot_time'],r['snapshot_id'])
    return {'eta_rows':sorted(eta_rows,key=order),'probability_rows':sorted(probability_rows,key=order),
            'opportunities':opportunities,'model_registry':states['model_registry'],'matrices':matrices,
            'features':stress['features'],'projection_audit':projection,'stress_audit':stress['audit'],
            'stress_summary':stress['summary']}
