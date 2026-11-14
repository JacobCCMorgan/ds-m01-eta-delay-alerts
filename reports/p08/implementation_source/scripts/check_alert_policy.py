"""Independent P06 reference audit, using only the Python standard library.

No ds_m01 imports, model fitting, threshold tuning, deliveries or final-test
targets. Risk segmentation and capacity admission are separate reference passes.
Raw promises/outcomes are selected with independent SQLite as-of queries.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_FLOOR, ROUND_CEILING
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

PROTOCOL_SHA = '7b7dbe70ec1d44199bd971607eeec9a029fdee5567a3c4fa1f42f7fac9ee0470'
DEFINITION = 'p06-chronological-candidate-policy-v1'
ZONE = ZoneInfo('America/Los_Angeles')
CUTOFF = '2026-08-01T07:00:00Z'
TERMINAL = {'arrived','cancelled','incomplete','lost'}
IDENTITY = ('tenant_id','trip_id','snapshot_id','snapshot_time','promise_version_id','deadline_at','prediction_version')
TARGET_FIELDS = ('eligibility','delay_label','reason','actual_arrival_time','label_known_at','label_cutoff_at',
                 'label_status','arrival_time_precision','outcome_version_id','outcome_provenance_id')
OPPORTUNITY_FIELDS = set('''tenant_id trip_id snapshot_id snapshot_time route_id vehicle_id departure_time
trip_status_as_of promise_version_id deadline_at promise_known_at snapshot_provenance_id promise_provenance_id
prediction_version position_validity elapsed_trip_seconds unseen_route unseen_vehicle
eta_status eta_reason eta_source eta_calibration_status lower_seconds median_seconds upper_seconds
lower_seconds_int median_seconds_int upper_seconds_int probability_status probability_reason probability_source
probability_calibration_status raw_probability calibrated_probability probability fallback_reason'''.split())


def read(path):
    return json.loads(Path(path).read_text())


def rows(path):
    with Path(path).open() as stream:
        return [json.loads(line) for line in stream if line.strip()]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def ident(*parts):
    return hashlib.sha256('\x1f'.join(map(str,parts)).encode()).hexdigest()[:24]


def at(value):
    value = datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
    if value.tzinfo is None:
        raise ValueError('Naive audit timestamp')
    return value.astimezone(timezone.utc)


def utc(value):
    return at(value).isoformat().replace('+00:00','Z')


def micros(value):
    return (at(value)-datetime(1970,1,1,tzinfo=timezone.utc))//timedelta(microseconds=1)


def key(row):
    return row['tenant_id'],row['snapshot_id']


def trip(row):
    return row['tenant_id'],row['trip_id']


def ordered(values):
    return sorted(values,key=lambda r:(at(r['snapshot_time']),r['tenant_id'],r['trip_id'],r['snapshot_id']))


def index(values, fields=('tenant_id','snapshot_id')):
    result = {}
    for row in values:
        k=tuple(row[f] for f in fields)
        if k in result:
            raise ValueError('Duplicate reference identity: '+str(k))
        result[k]=row
    return result


def probability(value):
    return format(Decimal(str(value)).quantize(Decimal('0.000001'),rounding=ROUND_HALF_EVEN),'.6f')


class Audit:
    def __init__(self):
        self.comparisons=0
        self.sections=Counter()
        self.failures=[]
        self.maximum_numeric_difference=0.0

    def compare(self, actual, expected, path):
        self.comparisons+=1
        self.sections[path.split('/')[0]]+=1
        if isinstance(expected,dict):
            if not isinstance(actual,dict):
                self.failures.append({'path':path,'actual':actual,'expected_type':'dict'});return
            self.compare(sorted(map(str,actual)),sorted(map(str,expected)),path+'/keys')
            for k in expected.keys() & actual.keys():
                self.compare(actual[k],expected[k],path+'/'+str(k))
            return
        if isinstance(expected,list):
            if not isinstance(actual,list):
                self.failures.append({'path':path,'actual':actual,'expected_type':'list'});return
            self.compare(len(actual),len(expected),path+'/length')
            for i,(a,b) in enumerate(zip(actual,expected)):
                self.compare(a,b,path+'/'+str(i))
            return
        if isinstance(expected,float):
            valid=type(actual) in (int,float) and math.isfinite(actual) and math.isclose(actual,expected,rel_tol=1e-12,abs_tol=1e-12)
            if type(actual) in (int,float):
                self.maximum_numeric_difference=max(self.maximum_numeric_difference,abs(actual-expected))
        else:
            valid=type(actual) is type(expected) and actual==expected
        if not valid:
            self.failures.append({'path':path,'actual':actual,'expected':expected})


def rule(q):
    values={'enabled':q is not None,'threshold':None if q is None else format(Decimal(q),'.6f'),
            'minimum_action_lead_seconds':1800,'repeat_suppression_seconds':1800,
            'daily_candidate_cap_per_tenant':2,'timezone':'America/Los_Angeles'}
    return dict(policy_version='P06-RULE-'+digest({'definition_version':DEFINITION,'protocol_sha256':PROTOCOL_SHA,**values})[:24],**values)


def reference_simulation(values,q):
    """First segment risk per trip; separately admit eligible rows globally."""
    original_count=len(values)
    unique={}
    for row in values:
        if set(row)-OPPORTUNITY_FIELDS:
            raise ValueError('Reference policy cannot consume outcome or undeclared fields')
        if key(row) in unique and unique[key(row)]!=row:
            raise ValueError('Conflicting opportunity duplicate')
        unique[key(row)]=row
    values=ordered(unique.values())
    r=rule(q); version=r['policy_version'];threshold=None if q is None else Decimal(q)
    episodes=[]; association={}; base_state={}; grouped=defaultdict(list)
    for row in values:grouped[trip(row)].append(row)
    for t,stream in sorted(grouped.items()):
        active=None
        for row in stream:
            now=utc(row['snapshot_time']);k=key(row);state=row['trip_status_as_of']
            old=active
            if active is not None:active['episode_last_observed_at']=now
            close_reason=None
            if q is None:category,reason='disabled','policy_disabled'
            elif state in TERMINAL:
                category,reason='terminal','known_'+state;close_reason=reason
            elif state!='in_transit':category,reason='inactive','trip_not_in_transit'
            elif row['probability_status']!='available':category,reason='unavailable','probability_unavailable'
            elif row['probability_calibration_status']!='fitted':category,reason='unavailable','probability_calibration_unsupported'
            elif Decimal(row['probability'])<threshold:
                category,reason='below_threshold','supported_probability_below_threshold';close_reason=reason
            else:
                if active is None:
                    active={'tenant_id':t[0],'trip_id':t[1],'policy_version':version,
                            'episode_id':'P06-EP-'+digest([version,*t,row['snapshot_id']])[:24],
                            'episode_started_at':now,'start_snapshot_id':row['snapshot_id'],
                            'start_promise_version_id':row['promise_version_id'],'episode_last_observed_at':now,
                            'episode_closed_at':None,'closure_reason':None,'episode_status':'open',
                            'censored_at':None,'candidate_count':0}
                    episodes.append(active)
                lead=(at(row['deadline_at'])-at(row['snapshot_time'])).total_seconds()
                if row['eta_status']!='available':category,reason='unavailable','eta_unavailable'
                elif lead<1800:category,reason='insufficient_lead','promise_lead_below_minimum'
                else:category,reason='ready',None
            if close_reason and active is not None:
                active.update(episode_closed_at=now,closure_reason=close_reason,episode_status='closed')
                active=None
            association[k]=(active or old)
            base_state[k]=(category,reason)
        if active is not None:
            active.update(episode_status='censored',censored_at=utc(stream[-1]['snapshot_time']))
    last_admitted={};day_counts=Counter();decisions=[];candidates=[]
    for row in values:
        k=key(row);t=trip(row);now=at(row['snapshot_time']);day=(t[0],now.astimezone(ZONE).date().isoformat())
        category,reason=base_state[k]
        if category=='ready':
            if t in last_admitted and now-last_admitted[t]<timedelta(seconds=1800):
                category,reason='cooldown','trip_candidate_cooldown'
            elif day_counts[day]>=2:category,reason='capacity_exhausted','tenant_local_day_candidate_cap'
            else:
                category,reason='admitted','threshold_support_lead_cooldown_capacity_passed'
                last_admitted[t]=now;day_counts[day]+=1;association[k]['candidate_count']+=1
        episode=association[k]
        candidate_id=('P06-CAND-'+digest([version,*t,row['snapshot_id'],row['prediction_version'],row['promise_version_id']])[:24]
                      if category=='admitted' else None)
        decision={**{field:row[field] for field in IDENTITY},'policy_version':version,'candidate_status':category,
                  'candidate_id':candidate_id,'episode_id':episode['episode_id'] if episode else None,
                  'lead_seconds':(at(row['deadline_at'])-now).total_seconds(),'probability':row.get('probability'),'reason':reason}
        decisions.append(decision)
        if candidate_id:candidates.append(dict(decision,simulated_issue_at=utc(now)))
    episodes.sort(key=lambda e:(at(e['episode_started_at']),e['tenant_id'],e['trip_id'],e['start_snapshot_id']))
    summary={'definition_version':DEFINITION,'policy_version':version,'enabled':q is not None,'input_records':original_count,
             'opportunities':len(values),'duplicate_records_ignored':original_count-len(values),'monitored_trips':len(grouped),
             'admitted_candidates':len(candidates),'episodes':len(episodes),
             'closed_episodes':sum(e['episode_status']=='closed' for e in episodes),
             'censored_episodes':sum(e['episode_status']=='censored' for e in episodes),
             'decision_status_counts':dict(sorted(Counter(x['candidate_status'] for x in decisions).items())),
             'daily_candidate_counts':[{'tenant_id':t,'local_date':d,'candidates':n} for (t,d),n in sorted(day_counts.items())],
             'scope':'research_candidates_not_deliveries','labels_used':False,'delivery_simulated':False}
    return {'rule':r,'decisions':decisions,'candidates':candidates,'episodes':episodes,'summary':summary}


def classification(target):
    if target['eligibility']=='eligible':return 'true' if target['delay_label'] else 'false'
    return 'post_arrival' if target['reason']=='already_arrived_at_snapshot' else 'unresolved'


def ratio(n,d):
    return n/d if d else None


def metric_reference(opportunities,targets,simulation,tenant=None):
    o={key(r):r for r in opportunities if tenant is None or r['tenant_id']==tenant}
    labels={key(r):r for r in targets if key(r) in o}
    candidates=[r for r in simulation['candidates'] if key(r) in o]
    decisions=[r for r in simulation['decisions'] if key(r) in o]
    episodes=[r for r in simulation['episodes'] if tenant is None or r['tenant_id']==tenant]
    eligible={k for k,r in labels.items() if r['eligibility']=='eligible'}
    positive={trip(o[k]) for k in eligible if labels[k]['delay_label'] is True}
    negative={trip(o[k]) for k in eligible if labels[k]['delay_label'] is False}
    actionable={trip(o[k]) for k in eligible if labels[k]['delay_label'] is True and at(o[k]['deadline_at'])-at(o[k]['snapshot_time'])>=timedelta(seconds=1800)}
    usable={k for k,r in o.items() if r['eta_status']=='available' and r['probability_status']=='available' and r['probability_calibration_status']=='fitted'}
    klass=Counter(classification(labels[key(r)]) for r in candidates)
    true_trips={trip(r) for r in candidates if classification(labels[key(r)])=='true'}
    first={}
    for candidate in ordered(candidates):first.setdefault(trip(candidate),candidate)
    first_classes=Counter(classification(labels[key(r)]) for r in first.values())
    ep_counts=Counter(r['episode_id'] for r in candidates)
    all_trips={trip(r) for r in o.values()};nc=len(candidates);nf=len(first)
    mature=first_classes['true']+first_classes['false']
    counts={'monitored_trips':len(all_trips),'opportunities':len(o),'eligible_target_snapshots':len(eligible),
            'excluded_target_snapshots':len(o)-len(eligible),'scored_trips':len({trip(o[k]) for k in eligible}),
            'usable_forecast_label_trips':len({trip(o[k]) for k in eligible & usable}),
            'observed_positive_trips':len(positive),'actionable_positive_trips':len(actionable),'negative_trips':len(negative),
            'mixed_promise_label_trips':len(positive & negative),'usable_forecast_opportunities':len(usable),
            'probability_unavailable_opportunities':sum(r['probability_status']=='unavailable' for r in o.values()),
            'unsupported_calibration_opportunities':sum(r['probability_calibration_status']=='unsupported' for r in o.values()),
            'eta_unavailable_opportunities':sum(r['eta_status']=='unavailable' for r in o.values()),
            'all_admitted_candidates':nc,'alerted_trips':nf,'risk_episodes':len(episodes),'episodes_with_candidates':len(ep_counts),
            'repeated_candidates_within_episode':sum(n-1 for n in ep_counts.values()),'additional_candidates_per_trip':nc-nf,
            'confirmed_true_candidates':klass['true'],'known_false_candidates':klass['false'],'unresolved_candidates':klass['unresolved'],
            'retrospectively_post_arrival_candidates':klass['post_arrival'],'confirmed_actionable_true_alert_trips':len(true_trips),
            'first_candidates':nf,'confirmed_true_first_candidates':first_classes['true'],'known_false_first_candidates':first_classes['false'],
            'unresolved_first_candidates':first_classes['unresolved'],'retrospectively_post_arrival_first_candidates':first_classes['post_arrival']}
    return {'counts':counts,'denominators':{'actionable_trip_recall':len(actionable),'all_observed_positive_trip_recall':len(positive),
            'conditional_first_candidate_precision':mature,'candidate_target_coverage':nc,'first_candidate_target_coverage':nf,
            'first_candidate_precision_bounds':nf,'all_candidates_per_100_monitored_trips':len(all_trips)},
            'metrics':{'actionable_trip_recall':ratio(len(true_trips),len(actionable)),
                       'all_observed_positive_trip_recall':ratio(len(true_trips),len(positive)),
                       'conditional_first_candidate_precision':ratio(first_classes['true'],mature),
                       'candidate_target_coverage':ratio(klass['true']+klass['false'],nc),'first_candidate_target_coverage':ratio(mature,nf),
                       'first_candidate_precision_bounds':{'lower':ratio(first_classes['true'],nf),'upper':ratio(first_classes['true']+first_classes['unresolved'],nf)},
                       'all_candidates_per_100_monitored_trips':ratio(100*nc,len(all_trips)),'alerted_trip_fraction':ratio(nf,len(all_trips))},
            'decision_status_counts':dict(sorted(Counter(r['candidate_status'] for r in decisions).items())),
            'decision_reason_counts':dict(sorted(Counter(r['reason'] for r in decisions).items())),
            'target_exclusion_reason_counts':dict(sorted(Counter(r['reason'] for r in labels.values() if r['eligibility']=='excluded').items())),
            'candidate_unresolved_reason_counts':dict(sorted(Counter(labels[key(r)]['reason'] for r in candidates if classification(labels[key(r)])=='unresolved').items())),
            'sparse':counts['scored_trips']<20}


def feasibility(counts,enabled):
    support={'minimum_scored_trips':counts['scored_trips']>=10,'minimum_actionable_positive_trips':counts['actionable_positive_trips']>=2,
             'minimum_negative_trips':counts['negative_trips']>=2}
    budget=100*counts['all_admitted_candidates']<=50*counts['monitored_trips']
    captured=counts['confirmed_actionable_true_alert_trips']>0
    return {'label_support':all(support.values()),'support_checks':support,'global_budget':budget,
            'confirmed_positive_capture':captured,'selectable':enabled and all(support.values()) and budget and captured}


def choose(trials):
    eligible=[t for t in trials if t['feasibility']['selectable']]
    if not eligible:return trials[-1]
    return sorted(eligible,key=lambda t:(-t['metrics']['counts']['confirmed_actionable_true_alert_trips'],
                                         t['metrics']['counts']['all_admitted_candidates'],-Decimal(t['rule']['threshold'])))[0]


def target_from_outcome(snapshot,promise,outcome):
    result={name:None for name in TARGET_FIELDS}
    result.update(eligibility='excluded',reason='no_outcome_known_before_cutoff',label_cutoff_at=CUTOFF,
                  tenant_id=snapshot['tenant_id'],trip_id=snapshot['trip_id'],snapshot_id=snapshot['snapshot_id'],
                  promise_version_id=promise['promise_version_id'],deadline_at=utc(promise['window_end_at']))
    if outcome is None:return result
    result.update(label_known_at=utc(outcome['label_available_time']),label_status=outcome['label_status'],
                  arrival_time_precision=outcome.get('arrival_time_precision'),outcome_version_id=outcome['outcome_version_id'],
                  outcome_provenance_id=outcome.get('provenance_id'))
    failures=[(outcome['label_status']!='verified','latest_outcome_'+outcome['label_status']),
              (outcome.get('arrival_time_precision')!='second','insufficient_arrival_precision'),
              (outcome.get('actual_arrival_time') is None,'missing_gate_arrival'),
              (not isinstance(outcome.get('arrival_evidence_ref'),str) or not outcome['arrival_evidence_ref'].strip(),'missing_arrival_evidence'),
              (outcome.get('cancellation_time') is not None,'conflicting_cancellation_evidence')]
    for failed,reason in failures:
        if failed:result['reason']=reason;return result
    arrival=at(outcome['actual_arrival_time'])
    if arrival>at(outcome['label_available_time']):raise ValueError('Arrival after its knowledge clock')
    result['actual_arrival_time']=utc(arrival)
    if arrival<=at(snapshot['snapshot_time']):result['reason']='already_arrived_at_snapshot'
    else:result.update(eligibility='eligible',reason='verified_gate_arrival',delay_label=arrival>at(promise['window_end_at']))
    return result


def registry_reference(source,settings,split_version):
    states=read(source/'calibrators.json');transforms=read(source/'refit_transformers.json')
    eta=read(source/'refit_eta_models.json');delay=read(source/'refit_delay_models.json');result=[]
    for transform in sorted(transforms,key=lambda r:r['tenant_id']):
        tenant=transform['tenant_id']
        def one(values):
            found=[r for r in values if r['tenant_id']==tenant]
            if len(found)!=1:raise ValueError('Ambiguous registry source')
            return found[0]
        bindings={'tenant_id':tenant,'p06_protocol_sha256':PROTOCOL_SHA,'p05_report_sha256':settings['source_report_sha256'],
                  'transformer_sha256':digest(transform),'eta_model_sha256':digest(one(eta)),
                  'probability_model_sha256':digest(one([r for r in delay if r['method_id']=='logistic_delay'])),
                  'interval_calibrator_sha256':digest(one(states['eta'])),'probability_calibrator_sha256':digest(one(states['probability'])),
                  'quality_accepted':False,'release_mode':'offline_replay'}
        result.append({'tenant_id':tenant,'prediction_version':'p06-r01-'+tenant+'-'+digest(bindings)[:16],
                       'training_cutoff_at':'2026-07-01T07:00:00Z','feature_definition_version':transform['feature_definition_version'],
                       'nominal_interval_coverage':'0.800000','split_version':split_version,
                       'interval_calibration_version':'p05-interval-'+bindings['interval_calibrator_sha256'][:20],
                       'probability_calibration_version':'p05-probability-'+bindings['probability_calibrator_sha256'][:20],
                       'calibration_cutoff_at':'2026-07-15T07:00:00Z','source_bindings':bindings,
                       'baseline_definition':'Frozen original-training historical route/departure-band, distance-group and tenant fallback; state SHA256 '+settings['source_report_sha256']['refit_frozen_history.json']})
    return result


def source_reference(root,input_root,settings,audit):
    source=root/settings['source_report']
    members=index([r for r in rows(source/'memberships.jsonl') if r['role']=='assessment'],('tenant_id','trip_id'))
    audit.compare(len(members),42,'source/trip_population')
    snapshots=rows(input_root/'data/processed/eta_snapshot.jsonl')
    selected=[r for r in snapshots if trip(r) in members]
    audit.compare(len(selected),341,'source/opportunity_population')
    splits=rows(input_root/'data/processed/eta_split_assignment.jsonl')
    split_index=index(splits,('tenant_id','trip_id'));versions={r['split_version'] for r in splits}
    audit.compare(len(versions),1,'source/split_version_count')
    registry=registry_reference(source,settings,next(iter(versions)));model={r['tenant_id']:r for r in registry}
    eta=index([r for r in rows(source/'assessment_eta_rows.jsonl') if r['variant']=='calibrated'])
    probability_rows=index([r for r in rows(source/'assessment_probability_rows.jsonl') if r['variant']=='calibrated'])
    audit.compare(sorted(eta),sorted(key(r) for r in selected),'source/eta_key_inventory')
    audit.compare(sorted(probability_rows),sorted(eta),'source/probability_key_inventory')
    db=sqlite3.connect(':memory:')
    db.execute('CREATE TABLE promise(tenant TEXT,trip TEXT,known INTEGER,payload TEXT, UNIQUE(tenant,trip,known))')
    db.execute('CREATE TABLE outcome(tenant TEXT,trip TEXT,known INTEGER,payload TEXT, UNIQUE(tenant,trip,known))')
    for p in rows(input_root/'data/raw/eta_promise_version.jsonl'):
        if trip(p) in members:db.execute('INSERT INTO promise VALUES(?,?,?,?)',(*trip(p),micros(p['known_at']),json.dumps(p)))
    outcome_payloads=0
    for o in rows(input_root/'data/raw/eta_trip_outcome.jsonl'):
        if trip(o) not in members:continue
        if micros(o['label_available_time'])>=micros(CUTOFF):continue
        db.execute('INSERT INTO outcome VALUES(?,?,?,?)',(*trip(o),micros(o['label_available_time']),json.dumps(o)));outcome_payloads+=1
    opportunities=[];targets=[]
    for s in ordered(selected):
        m=members[trip(s)];k=key(s);a=eta[k];b=probability_rows[k]
        audit.compare(split_index[trip(s)]['split_role'],'calibration','source/original_role/'+str(k))
        audit.compare(m['unseen_route'] or m['unseen_vehicle'],False,'source/heldout/'+str(k))
        audit.compare('2026-07-15'<=at(m['departure_time']).astimezone(ZONE).date().isoformat()<='2026-07-24',True,'source/calendar/'+str(k))
        chosen=db.execute('SELECT payload FROM promise WHERE tenant=? AND trip=? AND known<=? ORDER BY known DESC LIMIT 1',(*trip(s),micros(s['snapshot_time']))).fetchone()
        if not chosen:raise ValueError('No snapshot-effective promise')
        p=json.loads(chosen[0]);audit.compare(s['promise_version_id'],p['promise_version_id'],'source/promise_version/'+str(k))
        latest=db.execute('SELECT payload FROM outcome WHERE tenant=? AND trip=? ORDER BY known DESC LIMIT 1',trip(s)).fetchone()
        target=target_from_outcome(s,p,json.loads(latest[0]) if latest else None);targets.append(target)
        for family,row in [('eta',a),('probability',b)]:
            audit.compare({f:row[f] for f in ('tenant_id','trip_id','snapshot_id')},{f:s[f] for f in ('tenant_id','trip_id','snapshot_id')},'source/'+family+'/identity/'+str(k))
            audit.compare(utc(row['snapshot_time']),utc(s['snapshot_time']),'source/'+family+'/clock/'+str(k))
        item={f:s[f] for f in ('tenant_id','trip_id','snapshot_id','vehicle_id','trip_status_as_of','position_validity')}
        item.update(snapshot_time=utc(s['snapshot_time']),route_id=m['route_id'],departure_time=utc(m['departure_time']),
                    promise_version_id=p['promise_version_id'],deadline_at=utc(p['window_end_at']),promise_known_at=utc(p['known_at']),
                    snapshot_provenance_id=s['provenance_id'],promise_provenance_id=p['provenance_id'],
                    prediction_version=model[s['tenant_id']]['prediction_version'],elapsed_trip_seconds=a['elapsed_trip_seconds'],
                    unseen_route=m['unseen_route'],unseen_vehicle=m['unseen_vehicle'],eta_status=a['status'],eta_source=a['prediction_source'],
                    eta_calibration_status=a['calibration_status'],eta_reason=a.get('calibration_reason') or ('available' if a['status']=='available' else 'eta_forecast_unavailable'),
                    probability_status=b['status'],probability_source=b['prediction_source'],probability_calibration_status=b['calibration_status'],
                    probability_reason=b.get('calibration_reason') or ('available' if b['status']=='available' else 'probability_forecast_unavailable'))
        for f in ('lower_seconds','median_seconds','upper_seconds'):item[f]=a[f] if a['status']=='available' else None
        if a['status']=='available':
            lo=int(Decimal(str(a['lower_seconds'])).to_integral_value(rounding=ROUND_FLOOR))
            mid=int(Decimal(str(a['median_seconds'])).to_integral_value(rounding=ROUND_HALF_EVEN))
            hi=int(Decimal(str(a['upper_seconds'])).to_integral_value(rounding=ROUND_CEILING))
            projected=(min(lo,mid),mid,max(hi,mid))
        else:projected=(None,None,None)
        item.update(zip(('lower_seconds_int','median_seconds_int','upper_seconds_int'),projected))
        item.update(raw_probability=b.get('raw_probability') if b['status']=='available' else None,
                    calibrated_probability=b.get('probability') if b['status']=='available' else None,
                    probability=probability(b['probability']) if b['status']=='available' else None,
                    fallback_reason='eta='+a['prediction_source']+';probability='+b['prediction_source']+';interval_calibration='+a['calibration_status']+';probability_calibration='+b['calibration_status'])
        opportunities.append(item)
    db.close()
    return opportunities,targets,registry,{'assessment_trips':len(members),'opportunities':len(selected),'cutoff_known_outcome_versions':outcome_payloads,
                                         'method':'SQLite latest-known promise/outcome joins; only assessment trip outcomes strictly before cutoff are considered.'}


def hand_fixtures():
    """Literal expected results, evaluated before opening real-run artifacts."""
    checks=0
    def eq(actual,expected):
        nonlocal checks
        checks+=1
        if actual!=expected:raise AssertionError(f'Independent hand fixture differs: {actual!r} != {expected!r}')
    def opportunity(i,seconds=0,trip_id='A',lead=7200,p='0.500000',**extra):
        time=at('2026-07-20T17:00:00Z')+timedelta(seconds=seconds)
        return dict(tenant_id='T',trip_id=trip_id,snapshot_id=str(i),snapshot_time=utc(time),deadline_at=utc(time+timedelta(seconds=lead)),
                    promise_version_id='P1',prediction_version='MODEL',trip_status_as_of='in_transit',
                    probability_status='available',probability_calibration_status='fitted',eta_status='available',probability=p,**extra)
    eq(probability(.4999995),'0.500000');eq(probability(.5000005),'0.500000');eq(probability(.5000015),'0.500002')
    sim=reference_simulation([opportunity(1,lead=1800),opportunity(2,1,'B',lead=1799.999)],'.50')
    eq([r['candidate_status'] for r in sim['decisions']],['admitted','insufficient_lead'])
    sequence=[opportunity(1),opportunity(2,300,p='.1'),opportunity(3,1799),opportunity(4,1800)]
    sequence[2]['promise_version_id']='P2';sequence[3]['promise_version_id']='P2'
    sim=reference_simulation(sequence,'.5');eq([r['candidate_status'] for r in sim['decisions']],['admitted','below_threshold','cooldown','admitted']);eq(len(sim['episodes']),2)
    sim=reference_simulation([opportunity(1,trip_id='A'),opportunity(2,trip_id='B'),opportunity(3,trip_id='C',p='.99')],'.5')
    eq([r['trip_id'] for r in sim['candidates']],['A','B']);eq(sim['decisions'][-1]['candidate_status'],'capacity_exhausted')
    midnight=[opportunity(1),opportunity(2,60),opportunity(3,1800)]
    for row,seconds in zip(midnight,(0,60,1800)):
        row['snapshot_time']=utc(at('2026-07-21T06:59:00Z')+timedelta(seconds=seconds));row['deadline_at']=utc(at(row['snapshot_time'])+timedelta(hours=2))
    eq([r['candidate_status'] for r in reference_simulation(midnight,'.5')['decisions']],['admitted','cooldown','admitted'])
    missing=[opportunity(1),opportunity(2,600),opportunity(3,1200,p='.1')];missing[1].update(probability_status='unavailable',probability=None)
    sim=reference_simulation(missing,'.5');eq(len(sim['episodes']),1);eq(sim['episodes'][0]['episode_status'],'closed')
    eq(reference_simulation(missing[:2],'.5')['episodes'][0]['censored_at'],missing[1]['snapshot_time'])
    future=opportunity(4,1800,p='1');future['trip_status_as_of']='arrived';eq(reference_simulation([future],'.5')['decisions'][0]['candidate_status'],'terminal')
    eq(reference_simulation(sequence[::-1],'.5'),reference_simulation(sequence,'.5'))
    eq(reference_simulation([sequence[0],sequence[0]],'.5')['summary']['duplicate_records_ignored'],1)
    unknown_rows=[opportunity(1,trip_id='A'),opportunity(2,trip_id='B')]
    targets=[dict(tenant_id='T',trip_id='A',snapshot_id='1',eligibility='excluded',delay_label=None,reason='latest_outcome_pending'),
             dict(tenant_id='T',trip_id='B',snapshot_id='2',eligibility='excluded',delay_label=None,reason='already_arrived_at_snapshot')]
    measured=metric_reference(unknown_rows,targets,reference_simulation(unknown_rows,'.5'))
    eq(measured['counts']['all_admitted_candidates'],2);eq(measured['counts']['known_false_candidates'],0)
    eq(measured['metrics']['conditional_first_candidate_precision'],None);eq(measured['metrics']['first_candidate_precision_bounds'],{'lower':0.0,'upper':0.5})
    s=opportunity(1);p={'promise_version_id':'P1','window_end_at':s['deadline_at']}
    outcome={'label_status':'verified','arrival_time_precision':'second','actual_arrival_time':s['deadline_at'],
             'label_available_time':'2026-07-21T17:00:00Z','arrival_evidence_ref':'gate','cancellation_time':None,'outcome_version_id':'O1','provenance_id':'REF'}
    eq(target_from_outcome(s,p,outcome)['delay_label'],False)
    eq(target_from_outcome(s,p,dict(outcome,actual_arrival_time=s['snapshot_time']))['reason'],'already_arrived_at_snapshot')
    return {'cases':8,'assertions':checks,'status':'passed'}


def canonical_checks(report,opportunities,registry,simulation,selected,status,selection_manifest,settings,generated_at,audit):
    directory=report/'store/jsonl';o=index(opportunities);models={r['tenant_id']:r for r in registry};generated=utc(generated_at)
    expected_predictions=[];expected_memberships=[];expected_releases=[]
    grouped=defaultdict(list)
    for r in opportunities:grouped[trip(r)].append(r)
    weights={}
    for values in grouped.values():
        values=ordered(values);regular=(Decimal(1)/len(values)).quantize(Decimal('0.000000001'),rounding=ROUND_HALF_EVEN)
        for i,r in enumerate(values):weights[key(r)]=regular if i<len(values)-1 else Decimal(1)-regular*(len(values)-1)
        audit.compare(sum(weights[key(r)] for r in values)==Decimal(1),True,'canonical/exact_trip_weight_sum/'+str(trip(values[0])))
    for k,r in sorted(o.items()):
        model=models[r['tenant_id']];time=at(r['snapshot_time']);lead=(at(r['deadline_at'])-time).total_seconds()
        lead_slice='departure' if r['elapsed_trip_seconds']==0 else 'past_promise' if lead<0 else 'lt_30m' if lead<1800 else '30_to_120m' if lead<=7200 else 'gt_120m'
        expected_memberships.append({'tenant_id':r['tenant_id'],'snapshot_id':r['snapshot_id'],'split_version':model['split_version'],
                    'lead_time_slice':lead_slice,'unseen_route':r['unseen_route'],'trip_evaluation_weight':float(weights[k]),
                    'evaluation_slice':[{'dimension':'vehicle_seen','value':'unseen' if r['unseen_vehicle'] else 'seen'},
                                        {'dimension':'period','value':'policy_development'},{'dimension':'position_coverage','value':r['position_validity']}]})
        output=ident('P06-output',r['tenant_id'],r['snapshot_id'],r['prediction_version'])
        expected_releases.append({'tenant_id':r['tenant_id'],'provenance_id':ident('P06-output-provenance',r['tenant_id'],output),
                                  'output_id':output,'prediction_version':r['prediction_version'],'released_at':generated,'release_mode':'offline_replay'})
        if r['eta_status']=='available':
            fallback=('none' if r['eta_source'] in ('native','model','candidate') else 'unseen_route' if r['unseen_route'] else
                      'no_position' if r['position_validity']=='no_position' else 'low_confidence' if r['position_validity']=='low_confidence' else
                      'invalid_inputs' if 'invalid' in str(r['eta_reason']) else 'insufficient_history')
            expected_predictions.append({'tenant_id':r['tenant_id'],'snapshot_id':r['snapshot_id'],'prediction_version':r['prediction_version'],
                'eta_median':utc(time+timedelta(seconds=r['median_seconds_int'])),'eta_lower':utc(time+timedelta(seconds=r['lower_seconds_int'])),
                'eta_upper':utc(time+timedelta(seconds=r['upper_seconds_int'])),'delay_probability':float(r['probability']) if r['probability'] is not None else None,
                'fallback_reason':fallback,'remaining_eta_seconds':r['median_seconds_int'],'generated_at':generated,
                'value_states':{} if r['probability'] is not None else {'delay_probability':'unknown'}})
    for name,expected,fields in [('eta_prediction',expected_predictions,('tenant_id','snapshot_id')),
                                  ('eta_evaluation_membership',expected_memberships,('tenant_id','snapshot_id')),
                                  ('eta_output_release',expected_releases,('tenant_id','output_id'))]:
        audit.compare(index(rows(directory/(name+'.jsonl')),fields),index(expected,fields),'canonical/'+name)
    model_fields=('prediction_version','training_cutoff_at','feature_definition_version','split_version','interval_calibration_version',
                  'probability_calibration_version','calibration_cutoff_at','baseline_definition')
    expected_models=[dict({f:r[f] for f in model_fields},nominal_interval_coverage=float(r['nominal_interval_coverage'])) for r in registry]
    audit.compare(index(rows(directory/'eta_model_version.jsonl'),('prediction_version',)),index(expected_models,('prediction_version',)),'canonical/model_registry')
    audit.compare(index(rows(directory/'p06_model_binding.jsonl'),('tenant_id',)),index([{f:r[f] for f in ('tenant_id','prediction_version','source_bindings')} for r in registry],('tenant_id',)),'canonical/model_bindings')
    stored_rule=rows(directory/'eta_alert_rule_version.jsonl');audit.compare(len(stored_rule),1,'canonical/rule_count')
    public_rule=stored_rule[0]
    for name,expected in [('decision_threshold_version',selected['policy_version']),('delay_alert_threshold',float(selected['threshold']) if selected['enabled'] else 1.0),
                           ('minimum_action_lead_seconds',1800),('repeat_suppression_seconds',1800)]:
        audit.compare(public_rule[name],expected,'canonical/rule/'+name)
    reference=public_rule['threshold_selection_ref'].split('#',1)
    audit.compare((report/reference[0]).is_file(),True,'canonical/selection_reference_exists')
    if len(reference)==2:audit.compare(reference[1],selected['policy_version'],'canonical/selection_reference_rule')
    rule_audit=rows(directory/'p06_rule_audit.jsonl')[0]
    for name,expected in [('rule',selected),('selection_manifest',selection_manifest),('enabled',selected['enabled']),('quality_limits',settings['quality_limits']),
                           ('selection_status',status),('experimental',True),('threshold',float(selected['threshold']) if selected['enabled'] else None)]:
        audit.compare(rule_audit[name],expected,'canonical/rule_audit/'+name)
    for table,field,values,fields in [('p06_opportunity_audit','opportunity',opportunities,('tenant_id','snapshot_id')),
        ('p06_decision_audit','decision',simulation['decisions'],('tenant_id','snapshot_id')),
        ('p06_candidate_audit','research_candidate',simulation['candidates'],('tenant_id','candidate_id')),
        ('p06_episode_audit','research_episode',simulation['episodes'],('tenant_id','episode_id'))]:
        retained=rows(directory/(table+'.jsonl'))
        audit.compare(index([r[field] for r in retained],fields),index(values,fields),'canonical/'+table+'/embedded_records')
        for entry in retained:
            reference_row=entry[field]
            for name in entry.keys() & reference_row.keys()-{'probability'}:
                audit.compare(entry[name],reference_row[name],'canonical/'+table+'/projection/'+name+'/'+str(tuple(entry[f] for f in fields)))
            if 'probability' in entry:
                audit.compare(entry['probability'],float(reference_row['probability']) if reference_row['probability'] is not None else None,'canonical/'+table+'/probability/'+str(tuple(entry[f] for f in fields)))
    references=index(rows(directory/'p06_provenance_reference.jsonl'),('tenant_id','provenance_id'))
    for release in expected_releases:
        reference=references[(release['tenant_id'],release['provenance_id'])]
        audit.compare(reference['source_sha256'],digest(release),'canonical/release_provenance/'+release['output_id'])
        audit.compare(reference['source_reference'],'eta_output_release#'+release['output_id'],'canonical/release_reference/'+release['output_id'])
    for name in settings['release']['remaining_reserved_tables']:
        audit.compare(rows(directory/(name+'.jsonl')),[],'canonical/reserved_empty/'+name)


def run(root,report,input_root,strict):
    fixtures=hand_fixtures()
    audit=Audit();settings=read(root/'config/p06.json');receipt=read(report/'run_receipt.json')
    audit.compare(sha(root/'config/p06.json'),PROTOCOL_SHA,'binding/protocol')
    if sha(root/'config/p06.json')!=PROTOCOL_SHA:raise ValueError('Frozen P06 protocol bytes changed')
    audit.compare(receipt['protocol_sha256'],PROTOCOL_SHA,'binding/receipt_protocol')
    tracked={root/'config/p06.json':PROTOCOL_SHA,report/'run_receipt.json':sha(report/'run_receipt.json')}
    for relative,expected in settings['input_sha256'].items():
        path=input_root/relative;observed=sha(path);audit.compare(observed,expected,'binding/input/'+relative);tracked[path]=observed
        if observed!=expected:raise ValueError('Frozen input bytes changed: '+relative)
    for relative,expected in settings['source_report_sha256'].items():
        path=root/settings['source_report']/relative;observed=sha(path);audit.compare(observed,expected,'binding/P05/'+relative);tracked[path]=observed
        if observed!=expected:raise ValueError('Frozen P05 source changed: '+relative)
    for relative,expected in receipt['output_sha256'].items():
        path=report/relative;observed=sha(path);audit.compare(observed,expected,'binding/output/'+relative);tracked[path]=observed
    if strict:
        for relative,expected in receipt['code_sha256'].items():
            path=root/'src/ds_m01'/relative;observed=sha(path);audit.compare(observed,expected,'binding/code/'+relative);tracked[path]=observed
    opportunities,targets,registry,source_info=source_reference(root,input_root,settings,audit)
    actual_opportunities=rows(report/'opportunities.jsonl');actual_targets=rows(report/'targets.jsonl')
    audit.compare(index(actual_opportunities),index(opportunities),'opportunities/full_source_projection')
    audit.compare(index(actual_targets),index(targets),'targets/independent_asof_labels')
    audit.compare(index(read(report/'model_registry.json'),('tenant_id',)),index(registry,('tenant_id',)),'registry/full_source_bindings')
    for row in actual_opportunities:
        audit.compare(sorted(row),sorted(OPPORTUNITY_FIELDS),'opportunities/outcome_free_fields/'+str(key(row)))
    retained=read(report/'policy_trials.json');audit.compare(len(retained),22,'trials/count')
    actual_trials={t['trial_id']:t for t in retained};references=[];ledger_evidence=[];simulations={}
    for q in [f'{Decimal(i)/20:.2f}' for i in range(21)]+[None]:
        trial_id='disabled_reference' if q is None else 'threshold_'+q
        sim=reference_simulation(opportunities,q);measured=metric_reference(opportunities,targets,sim)
        feasible=feasibility(measured['counts'],q is not None)
        reference={'trial_id':trial_id,'rule':sim['rule'],'metrics':measured,'feasibility':feasible}
        references.append(reference);simulations[trial_id]=sim;published=actual_trials[trial_id]
        audit.compare(published['rule'],sim['rule'],'trials/'+trial_id+'/rule')
        audit.compare(published['feasibility'],feasible,'trials/'+trial_id+'/feasibility')
        audit.compare({k:published['metrics'][k] for k in measured},measured,'trials/'+trial_id+'/overall')
        for flag in ('independent_policy_test','final_test_evaluated','operational_benefit_established'):
            audit.compare(published['metrics'][flag],False,'trials/'+trial_id+'/'+flag)
        tenant_expected=[{'tenant_id':tenant,**metric_reference(opportunities,targets,sim,tenant)} for tenant in sorted({r['tenant_id'] for r in opportunities})]
        audit.compare(published['metrics']['tenant_slices'],tenant_expected,'trials/'+trial_id+'/tenant_slices')
        ledger_evidence.append({'trial_id':trial_id,'candidate_ids':[r['candidate_id'] for r in sim['candidates']],
                               'decisions_sha256':digest(sim['decisions']),'episodes_sha256':digest(sim['episodes']),
                               'candidates_sha256':digest(sim['candidates']),'daily_candidate_counts':sim['summary']['daily_candidate_counts']})
    chosen=choose(references);selected=chosen['rule'];sim=simulations[chosen['trial_id']]
    status='selected' if selected['enabled'] else 'disabled_insufficient_support' if not chosen['feasibility']['label_support'] else 'disabled_no_feasible_capture'
    audit.compare(read(report/'selected_policy.json'),selected,'selection/selected_rule')
    audit.compare([t['trial_id'] for t in retained if t['selected']],[chosen['trial_id']],'selection/selected_flag')
    selection_manifest={'protocol_version':settings['protocol_version'],'settings_content_sha256':digest(settings),
        'opportunity_records_sha256':digest(sorted(opportunities,key=key)),'target_records_sha256':digest(sorted(targets,key=key)),
        'selected_trial_id':chosen['trial_id'],'trials_retained':22,'threshold_grid':settings['policy']['threshold_grid'],
        'selection_ordering':settings['selection']['ordering'],'support_checks':chosen['feasibility']['support_checks'],
        'budget_max_candidates_per_100_monitored_trips':50,'daily_candidate_cap_per_tenant':2,'budget_basis':settings['policy']['budget_basis'],
        'assessment_reused_for_policy_development':True,'model_or_calibrator_reselected':False,'final_test_evaluated':False,
        'operational_approval':False,'no_post_sweep_grid_or_budget_changes':True}
    audit.compare(read(report/'selection_manifest.json'),selection_manifest,'selection/evidence_manifest')
    for name in ('decisions','candidates','episodes'):
        audit.compare(rows(report/(name+'.jsonl')),sim[name],'selected_ledger/'+name)
    audit.compare(read(report/'simulation_summary.json'),sim['summary'],'selected_ledger/simulation_summary')
    actual_metrics=read(report/'metrics.json')
    audit.compare(actual_metrics,actual_trials[chosen['trial_id']]['metrics'],'selection/retained_selected_metrics')
    canonical_checks(report,opportunities,registry,sim,selected,status,selection_manifest,settings,receipt['canonical_generated_at'],audit)
    for path,expected in tracked.items():audit.compare(sha(path),expected,'stability/'+str(path.relative_to(path.anchor)))
    return {'status':'passed' if not audit.failures else 'failed','comparisons':audit.comparisons,'comparisons_by_section':dict(audit.sections),
        'failure_count':len(audit.failures),'failures':audit.failures[:100],'absolute_tolerance':1e-12,'relative_tolerance':1e-12,
        'maximum_absolute_numeric_difference':audit.maximum_numeric_difference,'hand_fixtures':fixtures,'source_target_audit':source_info,
        'trial_count':22,'selected_trial_id':chosen['trial_id'],'selected_rule':selected,'selected_metrics':chosen['metrics'],
        'independently_reconstructed_trial_ledgers':ledger_evidence,'protocol_sha256':PROTOCOL_SHA,'strict_current_code_binding':strict,
        'auditor_source_sha256':sha(Path(__file__)),'input_sha256':settings['input_sha256'],'source_report_sha256':settings['source_report_sha256'],
        'audited_output_sha256':receipt['output_sha256'],'receipt_sha256':sha(report/'run_receipt.json'),
        'implementation':'Standard-library independent reference: per-trip risk segmentation, separate global admission pass, set-based metrics, Decimal canonical projections, SQLite as-of labels; no ds_m01 imports.',
        'limits':['Nonselected ledgers are independently reconstructed and bound here; only their retained metrics/rules are directly compared because public per-trial ledgers are not stored.',
                  'Canonical JSONL projection and bindings are checked; DuckDB/Parquet serialization is covered by the separate store replay.',
                  'No base-model or calibrator fitting, threshold tuning, notification delivery, buffer targets or final-test targets.'],
        'buffer_targets_used':False,'final_test_evaluated':False,'notification_delivery_executed':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,required=True);p.add_argument('--report',type=Path,required=True)
    p.add_argument('--input-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--strict',action='store_true',help='Also bind current code to the immutable execution receipt.')
    a=p.parse_args()
    if a.output.exists() or a.output.is_symlink():raise SystemExit('Refusing to overwrite audit output: '+str(a.output))
    try:result=run(a.root.resolve(),a.report.resolve(),a.input_root.resolve(),a.strict)
    except Exception as exc:
        result={'status':'failed','comparisons':0,'failure_count':1,'failures':[{'fatal_error':type(exc).__name__+': '+str(exc)}],
                'auditor_source_sha256':sha(Path(__file__))}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open('x') as stream:json.dump(result,stream,indent=2,sort_keys=True,allow_nan=False,default=str);stream.write('\n')
    print(json.dumps({k:result[k] for k in ('status','comparisons','failure_count')}))
    if result['failure_count']:
        print(json.dumps(result['failures'][:10],indent=2,default=str));raise SystemExit(1)


if __name__=='__main__':main()
