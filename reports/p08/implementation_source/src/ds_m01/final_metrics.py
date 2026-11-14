"""Frozen final forecast scoring with complete support and shared trip resamples.

No fitting, threshold selection or outcome-based subgroup construction occurs
here. Missing intervals are misses for all-eligible-trip coverage. Paired losses
use a fixed common set of eligible available snapshots; whole-trip differences
retain the full, identical eligible target population, including missing outputs.
"""
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN
import hashlib
import json
import math
import random
from zoneinfo import ZoneInfo

import numpy as np

from .common import parse_ts
from .delay_metrics import _ranking, _reliability

VERSION='p08-final-forecast-metrics-v1'
GROUPS=('seen_vehicle_seen_route','unseen_vehicle_seen_route','seen_vehicle_unseen_route','unseen_vehicle_unseen_route')
ETA_METRICS=('median_mae_seconds','median_rmse_seconds','mean_width_seconds','mean_interval_score_seconds',
             'trip_weighted_snapshot_coverage','trip_all_eligible_snapshots_coverage')
PROB_METRICS=('brier_score','log_loss')
WHOLE='trip_all_eligible_snapshots_coverage'
SLICES=('novelty_group','tenant_id','departure_month','route_id','position_validity','early_trip',
        'elapsed_7200','deadline_advance','prediction_source','calibration_status')
EPSILON=1e-15
IDENTITY_FIELDS=('tenant_id','trip_id','snapshot_id','snapshot_time','departure_time','route_id','vehicle_id',
                 'novelty_group','position_validity','elapsed_trip_seconds','deadline_remaining_seconds')
TARGET_FIELDS=('eligibility','reason','label_status','label_cutoff_at','label_known_at','outcome_version_id',
               'arrival_time_precision','actual_arrival_time','target_seconds','delay_label',
               'promise_version_id','selected_promise_version_id','promise_window_end','deadline_at',
               'promise_known_at','promise_provenance_id','remaining_duration_seconds','delay_seconds')


def _key(row):return row['tenant_id'],row['snapshot_id']
def _trip(row):return row['tenant_id'],row['trip_id']
def _finite(x):return type(x) in (int,float) and math.isfinite(x)
def _json(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def attach_targets(forecast_rows,target_rows,kind):
    """Join only after the caller freezes forecasts; reject omissions and relabeling."""
    if kind not in ('eta','probability'):raise ValueError('Unknown target family')
    forecasts=list(forecast_rows);targets={}
    for target in target_rows:
        key=_key(target)
        if key in targets:raise ValueError('Duplicate final target opportunity')
        targets[key]=target
    if {_key(r) for r in forecasts}!=set(targets):raise ValueError('Forecast and target inventories differ')
    result=[]
    label_fields=('eligibility','label_status','label_known_at','outcome_version_id','target_seconds','delay_label','actual_arrival_time')
    for row in forecasts:
        if any(field in row for field in label_fields):raise ValueError('Forecast already contains target fields')
        target=targets[_key(row)]
        for field in ('tenant_id','trip_id','snapshot_id','snapshot_time'):
            if row[field]!=target[field]:raise ValueError('Target join identity/clock differs')
        for source_field,target_field in (('promise_version_id','selected_promise_version_id'),
                                          ('promise_version_id','promise_version_id'),('deadline_at','deadline_at'),
                                          ('deadline_at','promise_window_end'),('promise_known_at','promise_known_at'),
                                          ('promise_provenance_id','promise_provenance_id')):
            if source_field in row and target.get(target_field) is not None and row[source_field]!=target[target_field]:
                raise ValueError('Target join changes the forecast promise')
        joined={**row,**target}
        if 'reason' in row:joined['forecast_reason']=row['reason']
        result.append(joined)
    return result


def _settings(settings):
    boot=settings['uncertainty']['forecast_bootstrap']
    if (settings['protocol_version']!='p08-final-evaluation-v1' or boot['replicates']!=2000
            or boot['seed']!=20261111 or boot['minimum_scored_trips']!=20 or boot['quantiles']!=[.025,.975]
            or tuple(settings['population']['primary_novelty_groups'])!=GROUPS):
        raise ValueError('Final metric/bootstrap settings differ from the frozen protocol')
    return boot


def _validate(rows,family):
    seen=set();trip_metadata={}
    for row in rows:
        for field in ('tenant_id','trip_id','snapshot_id','snapshot_time','departure_time','route_id','vehicle_id'):
            if not isinstance(row.get(field),str) or not row[field]:raise ValueError('Missing metric identity: '+field)
        if _key(row) in seen:raise ValueError('Duplicate final metric opportunity')
        seen.add(_key(row))
        if row.get('novelty_group') not in GROUPS:raise ValueError('Unknown original novelty group')
        constant=tuple(row[field] for field in ('departure_time','route_id','vehicle_id','novelty_group'))
        if trip_metadata.setdefault(_trip(row),constant)!=constant:raise ValueError('Trip metadata changed between snapshots')
        for field in ('snapshot_time','departure_time'):parse_ts(row[field])
        if parse_ts(row['snapshot_time'])<parse_ts(row['departure_time']):raise ValueError('Snapshot precedes departure')
        if row.get('position_validity') not in ('valid','low_confidence','no_position'):raise ValueError('Unknown original GPS state')
        for field in ('elapsed_trip_seconds','deadline_remaining_seconds'):
            value=row.get(field)
            if value is not None and (not _finite(value) or field=='elapsed_trip_seconds' and value<0):raise ValueError('Invalid slice clock')
        if row.get('eligibility') not in ('eligible','excluded'):raise ValueError('Unknown target eligibility')
        if row['eligibility']=='eligible' and row.get('label_status')!='verified':raise ValueError('Eligible targets must be verified')
        if row.get('label_cutoff_at')!='2026-10-15T07:00:00Z':raise ValueError('Final target cutoff differs from protocol')
        cutoff=parse_ts(row['label_cutoff_at'])
        if parse_ts(row['snapshot_time'])>=cutoff:raise ValueError('Snapshot crosses the final cutoff')
        known=row.get('label_known_at')
        if known is not None and parse_ts(known)>=cutoff:raise ValueError('Outcome knowledge must strictly precede final cutoff')
        if row['eligibility']=='eligible' and known is None:raise ValueError('Eligible target lacks its knowledge clock')
        arrival=row.get('actual_arrival_time')
        if arrival is not None:
            if known is None or parse_ts(arrival)>parse_ts(known):raise ValueError('Arrival occurs after verification')
            if row['eligibility']=='eligible' and parse_ts(arrival)<=parse_ts(row['snapshot_time']):raise ValueError('Eligible target is already arrived')
        target=row.get('target_seconds') if family=='eta' else row.get('delay_label')
        valid=(_finite(target) and target>0) if family=='eta' else type(target) is bool
        if row['eligibility']=='eligible' and not valid or row['eligibility']=='excluded' and target is not None:
            raise ValueError('Target value differs from eligibility')
        values=[row.get(f) for f in ('lower_seconds','median_seconds','upper_seconds')] if family=='eta' else [row.get('probability')]
        if row.get('status')=='available':
            if not all(_finite(value) for value in values):raise ValueError('Available forecast must be finite')
            if family=='eta' and not 0<=values[0]<=values[1]<=values[2]:raise ValueError('Interval must be nonnegative and ordered')
            if family=='probability' and not 0<=values[0]<=1:raise ValueError('Probability outside unit interval')
            if row.get('prediction_source')=='unavailable':raise ValueError('Available source cannot be unavailable')
        elif row.get('status')!='unavailable' or any(value is not None for value in values) or row.get('prediction_source')!='unavailable':
            raise ValueError('Unavailable forecast must retain nulls and unavailable source')
        if not isinstance(row.get('prediction_source'),str):raise ValueError('Forecast source is required')
        if row.get('calibration_status') not in ('not_applied','fitted','unsupported','unavailable'):
            raise ValueError('Unknown calibration support state')


def _support(rows):
    eligible=[r for r in rows if r['eligibility']=='eligible']
    scored=[r for r in eligible if r['status']=='available']
    return eligible,scored,{
        'opportunities':len(rows),'trips':len({_trip(r) for r in rows}),
        'eligible_snapshots':len(eligible),'eligible_trips':len({_trip(r) for r in eligible}),
        'excluded_snapshots':len(rows)-len(eligible),
        'available_snapshots':sum(r['status']=='available' for r in rows),
        'available_trips':len({_trip(r) for r in rows if r['status']=='available'}),
        'scored_snapshots':len(scored),'scored_trips':len({_trip(r) for r in scored}),
        'unavailable_eligible_snapshots':len(eligible)-len(scored),
    }


def _common_report(rows,counts,metrics,reasons):
    def counter(field,condition=lambda r:True):return dict(sorted(Counter(str(r.get(field) or 'unknown') for r in rows if condition(r)).items()))
    def source_class(r):
        return 'unavailable' if r['status']=='unavailable' else 'fallback' if r['prediction_source'] in ('baseline_fallback','prevalence_fallback') else 'native'
    return {'counts':counts,'metrics':metrics,'undefined_reasons':reasons,'sparse':counts['scored_trips']<20,
            'exclusion_reason_counts':counter('reason',lambda r:r['eligibility']=='excluded'),
            'prediction_source_counts':counter('prediction_source'),
            'calibration_status_counts':{state:sum(r['calibration_status']==state for r in rows)
                                        for state in ('not_applied','fitted','unsupported','unavailable')},
            'native_fallback_unavailable_support':{source:{'opportunities':len(members),'trips':len({_trip(r) for r in members}),
                'eligible_opportunities':sum(r['eligibility']=='eligible' for r in members),
                'scored_opportunities':sum(r['eligibility']=='eligible' and r['status']=='available' for r in members)}
                for source in ('native','fallback','unavailable') for members in [[r for r in rows if source_class(r)==source]]},
            'prediction_source_support':{source:{'opportunities':len(members),'trips':len({_trip(r) for r in members}),
                'eligible_opportunities':sum(r['eligibility']=='eligible' for r in members),
                'scored_opportunities':sum(r['eligibility']=='eligible' and r['status']=='available' for r in members)}
                for source in sorted({r['prediction_source'] for r in rows})
                for members in [[r for r in rows if r['prediction_source']==source]]},
            'calibration_reason_counts':counter('calibration_reason'),
            'availability_fraction':counts['available_snapshots']/counts['opportunities'] if counts['opportunities'] else None,
            'availability_undefined_reason':None if counts['opportunities'] else 'no_opportunities',
            'weighting':'equal_scored_tenant_trip_then_equal_scored_snapshot_in_this_cohort_and_slice'}


def _contributions(rows,family):
    eligible,scored,_=_support(rows)
    by_trip=defaultdict(list);all_trip=defaultdict(list)
    for r in eligible:all_trip[_trip(r)].append(r)
    for r in scored:by_trip[_trip(r)].append(r)
    out={name:{} for name in (ETA_METRICS if family=='eta' else PROB_METRICS)}
    for key,members in sorted(by_trip.items()):
        def avg(fn):return math.fsum(fn(r) for r in members)/len(members)
        if family=='eta':
            out['median_mae_seconds'][key]=avg(lambda r:abs(r['median_seconds']-r['target_seconds']))
            # RMSE uses a trip mean squared error; take sqrt only after averaging trips.
            out['median_rmse_seconds'][key]=avg(lambda r:(r['median_seconds']-r['target_seconds'])**2)
            out['mean_width_seconds'][key]=avg(lambda r:r['upper_seconds']-r['lower_seconds'])
            out['mean_interval_score_seconds'][key]=avg(lambda r:r['upper_seconds']-r['lower_seconds']+
                10*max(r['lower_seconds']-r['target_seconds'],0)+10*max(r['target_seconds']-r['upper_seconds'],0))
            out['trip_weighted_snapshot_coverage'][key]=avg(lambda r:float(r['lower_seconds']<=r['target_seconds']<=r['upper_seconds']))
        else:
            out['brier_score'][key]=avg(lambda r:(r['probability']-r['delay_label'])**2)
            def logloss(r):
                p=min(1-EPSILON,max(EPSILON,r['probability']))
                return -math.log(p) if r['delay_label'] else -math.log1p(-p)
            out['log_loss'][key]=avg(logloss)
    if family=='eta':
        for key,members in sorted(all_trip.items()):
            out[WHOLE][key]=float(all(r['status']=='available' and r['lower_seconds']<=r['target_seconds']<=r['upper_seconds'] for r in members))
    return out


def _mean(values,metric):
    if not values:return None
    mean=math.fsum(values.values())/len(values)
    return math.sqrt(mean) if metric=='median_rmse_seconds' else mean


def summarize_eta(rows,*,raw_quantiles=False):
    rows=list(rows);_validate(rows,'eta')
    eligible,scored,counts=_support(rows)
    contributions=_contributions(rows,'eta')
    metrics={name:_mean(values,name) for name,values in contributions.items()}
    groups=defaultdict(list)
    for r in eligible:groups[_trip(r)].append(r)
    complete=[members for members in groups.values() if all(r['status']=='available' for r in members)]
    covered=sum(contributions[WHOLE].values())
    counts.update(fully_available_eligible_trips=len(complete),all_eligible_snapshots_covered_trips=int(covered))
    conditional='trip_all_snapshots_coverage_given_full_availability'
    metrics[conditional]=covered/len(complete) if complete else None
    reasons={name:('no_eligible_trips' if name==WHOLE else 'no_scored_snapshots') for name,value in metrics.items() if value is None}
    if metrics[conditional] is None:reasons[conditional]='no_fully_available_eligible_trips'
    if raw_quantiles:
        n=Counter(_trip(r) for r in scored)
        for q,field in ((.1,'lower_seconds'),(.5,'median_seconds'),(.9,'upper_seconds')):
            name=f'pinball_{q:.1f}_seconds'
            metrics[name]=math.fsum(max(q*(r['target_seconds']-r[field]),(q-1)*(r['target_seconds']-r[field]))/n[_trip(r)] for r in scored)/len(n) if n else None
            if not n:reasons[name]='no_scored_snapshots'
    return {**_common_report(rows,counts,metrics,reasons),'nominal_coverage':.8,'interval_score_alpha':.2,
            'whole_trip_denominator':'all_eligible_trips_missing_forecasts_are_misses',
            'eta_whole_trip_point_target_met':metrics[WHOLE]>=.8 if metrics[WHOLE] is not None else None,
            'endpoint_semantics':'raw_quantile_estimates' if raw_quantiles else 'interval_bounds_not_reestimated_quantiles'}


def _class_support(rows):
    pos=[r for r in rows if r['delay_label']];neg=[r for r in rows if not r['delay_label']]
    p={_trip(r) for r in pos};n={_trip(r) for r in neg}
    return {'positive_snapshots':len(pos),'negative_snapshots':len(neg),'positive_trips':len(p),
            'negative_trips':len(n),'mixed_label_trips':len(p&n)}


def summarize_probability(rows):
    rows=list(rows);_validate(rows,'probability')
    eligible,scored,counts=_support(rows)
    n=Counter(_trip(r) for r in scored)
    observations=[(float(r['probability']),int(r['delay_label']),1/n[_trip(r)],r) for r in sorted(scored,key=_key)]
    auc,ap,ranking_reason,_=_ranking(observations,False)
    reliability=_reliability(observations,tuple(i/10 for i in range(11)),20)
    for item in reliability:
        item['undefined_reason']=None if item['snapshots'] else 'empty_fixed_bin'
        if not n:item['cohort_weight_fraction']=None
    metrics={name:_mean(values,name) for name,values in _contributions(rows,'probability').items()}
    metrics.update(roc_auc=auc,average_precision=ap,
        weighted_event_rate=math.fsum(y*w for _,y,w,_ in observations)/len(n) if n else None,
        mean_probability=math.fsum(p*w for p,_,w,_ in observations)/len(n) if n else None,
        fixed_bin_ece=math.fsum(b['cohort_weight_fraction']*abs(b['mean_probability']-b['observed_event_rate']) for b in reliability if b['snapshots']) if n else None)
    reasons={name:'no_scored_snapshots' for name,value in metrics.items() if value is None}
    if ranking_reason:reasons.update(roc_auc=ranking_reason,average_precision=ranking_reason)
    counts['log_loss_clipped_snapshots']=sum(r['probability']<EPSILON or r['probability']>1-EPSILON for r in scored)
    return {**_common_report(rows,counts,metrics,reasons),'eligible_class_support':_class_support(eligible),
        'scored_class_support':_class_support(scored),'reliability':reliability,'log_loss_epsilon':EPSILON,
        'reliability_weighting':'full_scored_cohort_trip_weights_retained_within_each_fixed_bin',
        'probability_scope':'precise_retained_forecasts_unless_explicit_canonical_projection_variant'}


def bootstrap_plan(rows,settings):
    """Draw once from complete metadata inventory; share across methods/slices."""
    boot=_settings(settings);strata=defaultdict(set);membership={}
    for row in rows:
        key=_trip(row);stratum=(row['tenant_id'],row['novelty_group'])
        if membership.setdefault(key,stratum)!=stratum:raise ValueError('A trip crosses bootstrap strata')
        strata[stratum].add(key)
    keys=sorted(membership);index={key:i for i,key in enumerate(keys)}
    draws=np.zeros((boot['replicates'],len(keys)),dtype=np.int32)
    rng=random.Random(boot['seed'])
    groups=[sorted(values) for _,values in sorted(strata.items())]
    for replicate in range(boot['replicates']):
        for group in groups:
            for _ in group:draws[replicate,index[rng.choice(group)]]+=1
    serial=[{'tenant_id':key[0],'trip_id':key[1],'novelty_group':membership[key][1]} for key in keys]
    return {'keys':keys,'index':index,'draws':draws,'metadata':{
        'replicates':2000,'seed':20261111,'minimum_original_scored_trips':20,
        'inventory_trips':len(keys),'inventory_sha256':hashlib.sha256(_json(serial).encode()).hexdigest(),
        'draw_counts_sha256':hashlib.sha256(draws.astype('<i4').tobytes()).hexdigest(),
        'strata':[{'tenant_id':s[0],'novelty_group':s[1],'trips':len(v)} for s,v in sorted(strata.items())],
        'resampling':'one_complete_inventory_draw_plan_shared_by_all_methods_variants_slices_and_pairs',
        'auc_ap_bootstrapped':False,'interpretation':'descriptive_conditional_on_fixed_synthetic_trajectories'}}


def _percentile(values,p):
    values=sorted(float(v) for v in values);position=(len(values)-1)*p
    low=int(math.floor(position));high=int(math.ceil(position));fraction=position-low
    return values[low]*(1-fraction)+values[high]*fraction


def _interval(left,metric,plan,right=None,*,scored_trips=None):
    keys=sorted(left)
    if right is not None and set(keys)!=set(right):raise ValueError('Paired bootstrap contribution keys differ')
    if not set(keys)<=set(plan['index']):raise ValueError('Bootstrap row is outside complete inventory')
    sampled=plan['draws'][:,[plan['index'][key] for key in keys]]
    denominator=sampled.sum(axis=1,dtype=np.int64)
    undefined=int(np.count_nonzero(denominator==0))
    scored_trips=len(keys) if scored_trips is None else scored_trips
    reason='fewer_than_20_original_scored_trips' if scored_trips<20 else 'undefined_replicate_denominator' if undefined else None
    result={'lower':None,'upper':None,'confidence_level':.95,'undefined_reason':reason,
        'original_contributing_trips':len(keys),'original_scored_trips':scored_trips,'replicates_requested':2000,
        'defined_replicates':2000-undefined,'undefined_replicates':undefined,'quantile_interpolation':'linear_r_(n-1)*p'}
    if reason is not None:return result
    first=sampled@np.asarray([left[key] for key in keys],dtype=float)/denominator
    if metric=='median_rmse_seconds':first=np.sqrt(first)
    if right is not None:
        second=sampled@np.asarray([right[key] for key in keys],dtype=float)/denominator
        if metric=='median_rmse_seconds':second=np.sqrt(second)
        first=second-first
    result.update(lower=_percentile(first,.025),upper=_percentile(first,.975))
    return result


def bootstrap_summary(rows,family,settings,*,plan=None):
    rows=list(rows);_validate(rows,family);_settings(settings)
    plan=bootstrap_plan(rows,settings) if plan is None else plan
    scored_trips=_support(rows)[2]['scored_trips']
    return {name:_interval(values,name,plan,scored_trips=scored_trips) for name,values in _contributions(rows,family).items()}


def _pair_rows(left,right):
    a={_key(r):r for r in left};b={_key(r):r for r in right}
    if len(a)!=len(left) or len(b)!=len(right) or a.keys()!=b.keys():raise ValueError('Compared methods must retain identical opportunity inventories')
    for key,before in a.items():
        if _json({f:before.get(f) for f in (*IDENTITY_FIELDS,*TARGET_FIELDS)})!=_json({f:b[key].get(f) for f in (*IDENTITY_FIELDS,*TARGET_FIELDS)}):
            raise ValueError('Paired forecast target or original metadata differs')
    return a,b


def paired_comparison(left,right,family,settings,*,plan=None):
    left,right=list(left),list(right);_validate(left,family);_validate(right,family)
    a,b=_pair_rows(left,right);plan=bootstrap_plan(left,settings) if plan is None else plan
    eligible={key for key,r in a.items() if r['eligibility']=='eligible'}
    lkeys={key for key in eligible if a[key]['status']=='available'}
    rkeys={key for key in eligible if b[key]['status']=='available'}
    common=lkeys&rkeys
    la=_contributions([a[key] for key in sorted(common)],family)
    rb=_contributions([b[key] for key in sorted(common)],family)
    # Whole-trip misses use all original eligible snapshots, not the available intersection.
    if family=='eta':
        la[WHOLE]=_contributions(left,family)[WHOLE];rb[WHOLE]=_contributions(right,family)[WHOLE]
    changes={}
    for metric,values in la.items():
        first=_mean(values,metric);second=_mean(rb[metric],metric)
        changes[metric]={'left':first,'right':second,'right_minus_left':second-first if first is not None else None,
            'undefined_reason':None if first is not None else 'no_fixed_paired_support',
            'support':'all_original_eligible_trips_missing_forecasts_are_misses' if metric==WHOLE else 'fixed_common_eligible_available_snapshots',
            'interval':_interval(values,metric,plan,rb[metric],scored_trips=len({_trip(a[key]) for key in common}))}
    return {'counts':{'all_opportunities':len(a),'eligible_snapshots':len(eligible),
        'left_scored_snapshots':len(lkeys),'right_scored_snapshots':len(rkeys),'paired_scored_snapshots':len(common),
        'paired_scored_trips':len({_trip(a[key]) for key in common}),
        'left_scored_trips':len({_trip(a[key]) for key in lkeys}),'right_scored_trips':len({_trip(a[key]) for key in rkeys}),
        'left_trips_lost_from_paired_support':len({_trip(a[key]) for key in lkeys}-{_trip(a[key]) for key in common}),
        'right_trips_lost_from_paired_support':len({_trip(a[key]) for key in rkeys}-{_trip(a[key]) for key in common}),
        'left_only_scored_snapshots':len(lkeys-rkeys),'right_only_scored_snapshots':len(rkeys-lkeys),
        'neither_available_eligible_snapshots':len(eligible-lkeys-rkeys)},'metrics':changes,
        'direction':'right_minus_left','paired_support_sha256':hashlib.sha256(_json(sorted(common)).encode()).hexdigest()}


def _slice_value(row,field):
    if field=='departure_month':return parse_ts(row['departure_time']).astimezone(ZoneInfo('America/Los_Angeles')).strftime('%Y-%m')
    if field in ('early_trip','elapsed_7200'):
        value=row.get('elapsed_trip_seconds')
        if value is None:return 'unknown'
        cutoff=3600 if field=='early_trip' else 7200
        return 'below_'+str(cutoff) if value<cutoff else 'at_least_'+str(cutoff)
    if field=='deadline_advance':
        value=row.get('deadline_remaining_seconds')
        return 'unknown' if value is None else 'overdue' if value<0 else '[0,1800]' if value<=1800 else '(1800,7200]' if value<=7200 else '>7200'
    return str(row.get(field) or 'unknown')


def _domains(rows):
    defaults={'novelty_group':GROUPS,'departure_month':('2026-08','2026-09'),
        'position_validity':('valid','low_confidence','no_position'),
        'early_trip':('below_3600','at_least_3600'),'elapsed_7200':('below_7200','at_least_7200'),
        'deadline_advance':('overdue','[0,1800]','(1800,7200]','>7200'),
        'calibration_status':('not_applied','fitted','unsupported','unavailable')}
    return {field:sorted(set(defaults.get(field,()))|{_slice_value(row,field) for row in rows}) for field in SLICES}


def _projection_guard(raw,calibrated,canonical,family):
    a,b=_pair_rows(raw,calibrated);_,c=_pair_rows(raw,canonical)
    for key,row in a.items():
        after=b[key];projected=c[key]
        if row['status']!=after['status'] or row['prediction_source']!=after['prediction_source']:
            raise ValueError('Calibration changed forecast availability/source')
        if after['status']!=projected['status'] or after['prediction_source']!=projected['prediction_source']:
            raise ValueError('Canonical projection changed forecast availability/source')
        if row['calibration_status']!='not_applied':raise ValueError('Raw variant must declare calibration not applied')
        if after['calibration_status']!=projected['calibration_status']:
            raise ValueError('Canonical projection changed calibration support')
        if row['status']=='unavailable':continue
        if after['calibration_status'] not in ('fitted','unsupported'):
            raise ValueError('Available calibrated variant must declare fitted or unsupported')
        value_fields=('lower_seconds','median_seconds','upper_seconds') if family=='eta' else ('probability',)
        if after['calibration_status']=='unsupported' and any(row[f]!=after[f] for f in value_fields):
            raise ValueError('Unsupported calibrator must retain identity raw forecasts')
        if family=='eta':
            if row['median_seconds']!=after['median_seconds']:raise ValueError('Interval calibration changed median')
            median=int(Decimal(str(after['median_seconds'])).to_integral_value(rounding=ROUND_HALF_EVEN))
            lower=min(median,int(Decimal(str(after['lower_seconds'])).to_integral_value(rounding=ROUND_FLOOR)))
            upper=max(median,int(Decimal(str(after['upper_seconds'])).to_integral_value(rounding=ROUND_CEILING)))
            if [projected[f] for f in ('lower_seconds','median_seconds','upper_seconds')]!=[lower,median,upper]:
                raise ValueError('Canonical ETA projection differs from fixed outward rounding')
        else:
            expected=format(Decimal(str(after['probability'])).quantize(Decimal('.000001'),rounding=ROUND_HALF_EVEN),'.6f')
            if projected.get('canonical_probability')!=expected or projected['probability']!=float(expected):
                raise ValueError('Canonical probability differs from six-decimal half-even projection')


def build_metrics(interval_rows,probability_rows,settings):
    """Report every fixed method, required slice and paired difference; no tuning."""
    _settings(settings)
    inputs={'eta':list(interval_rows),'probability':list(probability_rows)}
    method_sets={};inventory=None
    for family,rows in inputs.items():
        baseline='historical_baseline' if family=='eta' else 'fold_prevalence'
        expected={('pipeline','raw'),('pipeline','calibrated'),('pipeline','canonical'),(baseline,'raw')}
        found={(r.get('method_id'),r.get('variant')) for r in rows}
        if found and found!=expected:raise ValueError('Final method/variant inventory differs: '+family)
        groups={key:[r for r in rows if (r['method_id'],r['variant'])==key] for key in sorted(expected)}
        for members in groups.values():_validate(members,family)
        first=groups['pipeline','raw']
        for members in groups.values():_pair_rows(first,members)
        _projection_guard(first,groups['pipeline','calibrated'],groups['pipeline','canonical'],family)
        current={_key(r):tuple(r.get(f) for f in IDENTITY_FIELDS) for r in first}
        if inventory is not None and current!=inventory:raise ValueError('ETA and probability inventories/context differ')
        inventory=current;method_sets[family]=groups
    master=method_sets['eta']['pipeline','raw']
    plan=bootstrap_plan(master,settings)
    result={'definition_version':VERSION,'scope':'original_August_September_final_test',
        'bootstrap_design':plan['metadata'],'quality_accepted':False,'operational_approval':False}
    for family,groups in method_sets.items():
        baseline='historical_baseline' if family=='eta' else 'fold_prevalence'
        output={'methods':{},'slices':[],'paired_comparisons':[]}
        domains=_domains([r for members in groups.values() for r in members])
        def summarize(members,variant):
            return summarize_eta(members,raw_quantiles=variant=='raw') if family=='eta' else summarize_probability(members)
        for (method,variant),members in groups.items():
            output['methods'][method+'/'+variant]={**summarize(members,variant),
                'bootstrap_intervals':bootstrap_summary(members,family,settings,plan=plan)}
            for field,values in domains.items():
                for value in values:
                    selected=[r for r in members if _slice_value(r,field)==value]
                    output['slices'].append({'method_id':method,'variant':variant,'field':field,'value':value,
                        **summarize(selected,variant),'bootstrap_intervals':bootstrap_summary(selected,family,settings,plan=plan)})
        pairs=[(('pipeline','raw'),('pipeline','calibrated')),
               (('pipeline','calibrated'),('pipeline','canonical')),
               ((baseline,'raw'),('pipeline','raw')),((baseline,'raw'),('pipeline','calibrated'))]
        for left,right in pairs:
            first,second=groups[left],groups[right]
            scopes=[('overall','all',first,second)]
            # Source/calibration slices can change by method: compare on the left's declared metadata support,
            # while retaining both methods' rows. They are separately reported in each method's own slices.
            for field in SLICES:
                if field in ('prediction_source','calibration_status'):continue
                for value in domains[field]:scopes.append((field,value,[r for r in first if _slice_value(r,field)==value],
                                                           [r for r in second if _slice_value(r,field)==value]))
            for field,value,a,b in scopes:
                output['paired_comparisons'].append({'left_method':'/'.join(left),'right_method':'/'.join(right),
                    'field':field,'value':value,**paired_comparison(a,b,family,settings,plan=plan)})
        result[family]=output
    return result
