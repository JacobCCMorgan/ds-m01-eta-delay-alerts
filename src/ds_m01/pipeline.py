"""ETA point-in-time snapshots. Validation truth is deliberately never opened here."""
from collections import defaultdict, Counter
from datetime import timedelta
from math import cos, radians
from pathlib import Path
from contextlib import ExitStack
import os
import tempfile
from zoneinfo import ZoneInfo
import json

from .common import parse_ts, utc, ident, read_jsonl, write_jsonl, verify_source_manifest

RAW_TABLES = ('core_tenant','core_vehicle','core_device','core_vehicle_device_assignment',
              'eta_trip','eta_facility_version','eta_route_version','eta_trip_plan_version',
              'eta_promise_version','eta_trip_event','eta_trip_outcome','telemetry_event','provenance_record')
DERIVED_TABLES = ('eta_snapshot','eta_snapshot_label','eta_split_assignment','eta_history_reference')
FEATURE_COLUMNS = ('latitude_deg','longitude_deg','position_age_seconds','gnss_speed_kmh',
                   'remaining_distance_km','departure_time_bucket','elapsed_trip_seconds','position_validity')


def _within(value, start, end):
    return (start is None or parse_ts(start) <= value) and (end is None or value < parse_ts(end))


def _latest(rows, field, cutoff, inclusive=True):
    candidates = [x for x in rows if x.get(field) and
                  (parse_ts(x[field]) <= cutoff if inclusive else parse_ts(x[field]) < cutoff)]
    return max(candidates, key=lambda x: (parse_ts(x[field]), json.dumps(x, sort_keys=True))) if candidates else None


def _tenant(rows, tenant):
    return [r for r in rows if r.get('tenant_id') == tenant]


def _trip(rows, tenant, trip):
    return [r for r in rows if r.get('tenant_id') == tenant and r.get('trip_id') == trip]


def _usable_assignment(row):
    return (row.get('resolution_status') == 'resolved'
            and (row.get('valid_to') is not None or row.get('valid_to_state') == 'open')
            and (row.get('known_to') is not None or row.get('known_to_state') == 'open'))


def _remaining_distance(route, facilities, latitude, longitude, at):
    """Projection onto an explicitly fictional straight route; no future trajectory."""
    selected = {}
    for kind in ('origin','destination'):
        fid = route.get(kind+'_facility_version_id')
        candidates = [f for f in facilities if f.get('facility_version_id') == fid
                      and _within(at, f.get('known_from'), f.get('known_to'))
                      and _within(at, f.get('valid_from'), f.get('valid_to'))]
        if len(candidates) != 1:
            return None
        selected[kind] = candidates[0]
    a,b = selected['origin'],selected['destination']
    factor = cos(radians((a['latitude_deg']+b['latitude_deg'])/2))
    dx=(b['longitude_deg']-a['longitude_deg'])*factor
    dy=b['latitude_deg']-a['latitude_deg']
    if dx*dx+dy*dy == 0:
        return None
    progress = ((longitude-a['longitude_deg'])*factor*dx+(latitude-a['latitude_deg'])*dy)/(dx*dx+dy*dy)
    return round(float(route['planned_distance_km'])*(1-max(0,min(1,progress))),3)


def snapshot_for_trip(tenant_id, trip_id, snapshot_time, tables):
    at=parse_ts(snapshot_time)
    plan=_latest(_trip(tables.get('eta_trip_plan_version',[]),tenant_id,trip_id),'plan_known_at',at)
    promise=_latest(_trip(tables.get('eta_promise_version',[]),tenant_id,trip_id),'known_at',at)
    if plan is None or promise is None:
        return None
    events=[e for e in _trip(tables.get('eta_trip_event',[]),tenant_id,trip_id)
            if parse_ts(e['available_at']) <= at and parse_ts(e['occurred_at']) <= at]
    # Status eligibility is based on knowledge, never an outcome-table lookup.
    status_event=max(events,key=lambda e:(parse_ts(e['available_at']),parse_ts(e['occurred_at']),e.get('trip_event_id',''))) if events else None
    status=status_event.get('trip_status','in_transit') if status_event else 'planned'
    if status in ('arrived','cancelled'):
        return None
    departures=[e for e in events if e.get('event_type') in ('departure','departed')]
    if not departures:
        return None
    depart=min(parse_ts(e['occurred_at']) for e in departures)
    if depart > at:
        return None
    routes=[r for r in _tenant(tables.get('eta_route_version',[]),tenant_id)
            if r['route_version_id']==plan['route_version_id'] and
            parse_ts(r['known_at'])<=at and _within(at,r.get('valid_from'),r.get('valid_to'))]
    route=routes[0] if len(routes)==1 else None
    if route is None:
        return None
    assignments=_tenant(tables.get('core_vehicle_device_assignment',[]),tenant_id)
    revisions={}
    for row in _tenant(tables.get('telemetry_event',[]),tenant_id):
        if row.get('vehicle_id') != plan['vehicle_id']:
            continue
        if not (depart <= parse_ts(row['event_time']) <= at and parse_ts(row['available_time']) <= at):
            continue
        key=(row.get('source_system_id'),row.get('source_event_id',row['canonical_event_id']))
        if key not in revisions or (parse_ts(row['available_time']),str(row.get('record_revision',''))) > (parse_ts(revisions[key]['available_time']),str(revisions[key].get('record_revision',''))):
            revisions[key]=row
    valid=[]
    for row in revisions.values():
        lat,lon=row.get('latitude_deg'),row.get('longitude_deg')
        if row.get('gnss_valid') not in ('valid',True,'true','True','1',1):
            continue
        if lat is None or lon is None or not (-90<=float(lat)<=90 and -180<=float(lon)<=180):
            continue
        mapping=[a for a in assignments if a['vehicle_id']==plan['vehicle_id']
                 and a['device_id']==row['device_id']
                 and _within(parse_ts(row['event_time']),a.get('valid_from'),a.get('valid_to'))
                 and _within(at,a.get('known_from'),a.get('known_to'))
                 and _usable_assignment(a)]
        if len(mapping)==1:
            valid.append(row)
    pos=max(valid,key=lambda r:(parse_ts(r['event_time']),parse_ts(r['available_time']),r['canonical_event_id'])) if valid else None
    age=int((at-parse_ts(pos['event_time'])).total_seconds()) if pos else None
    stale_seconds=tables.get('_settings',{}).get('position_stale_seconds',900)
    quality='no_position' if pos is None else ('low_confidence' if age>stale_seconds else 'valid')
    tz=next((x['report_timezone'] for x in _tenant(tables.get('core_tenant',[]),tenant_id)), 'America/Los_Angeles')
    local_dep=depart.astimezone(ZoneInfo(tz))
    sid='S-'+ident(tenant_id,trip_id,utc(at),'m1-features-v1')
    return dict(tenant_id=tenant_id,provenance_id='P-'+ident('eta_snapshot',sid),snapshot_id=sid,
                trip_id=trip_id,trip_version_id=plan['trip_version_id'],promise_version_id=promise['promise_version_id'],
                route_version_id=plan['route_version_id'],vehicle_id=plan['vehicle_id'],
                device_id=pos['device_id'] if pos else None,selected_position_event_id=pos['canonical_event_id'] if pos else None,
                snapshot_time=utc(at),trip_status_as_of=status,latitude_deg=pos['latitude_deg'] if pos else None,
                longitude_deg=pos['longitude_deg'] if pos else None,last_position_time=pos['event_time'] if pos else None,
                position_available_at=pos['available_time'] if pos else None,position_validity=quality,
                gnss_speed_kmh=pos.get('gnss_speed_kmh') if pos else None,position_age_seconds=age,
                remaining_distance_km=_remaining_distance(route,_tenant(tables.get('eta_facility_version',[]),tenant_id),pos['latitude_deg'],pos['longitude_deg'],at) if quality=='valid' else None,
                departure_time_bucket=f'{local_dep.isoweekday()}-{local_dep.hour:02d}@{tz}',
                elapsed_trip_seconds=int((at-depart).total_seconds()),feature_definition_version='m1-features-v1',
                forecast_id=None,routing_response_id=None)


def label_for_snapshot(snapshot, outcomes, promises, cutoff):
    """Cutoff is exclusive; unknown is never recoded to on-time/zero duration."""
    tenant,trip=snapshot['tenant_id'],snapshot['trip_id']
    latest=_latest(_trip(outcomes,tenant,trip),'label_available_time',parse_ts(cutoff),inclusive=False)
    promise=next((p for p in _trip(promises,tenant,trip) if p['promise_version_id']==snapshot['promise_version_id']),None)
    result=dict(tenant_id=tenant,provenance_id='P-'+ident('eta_snapshot_label',snapshot['snapshot_id'],cutoff),
                snapshot_id=snapshot['snapshot_id'],outcome_version_id=latest['outcome_version_id'] if latest else None,
                promise_version_id=snapshot['promise_version_id'],remaining_duration_seconds=None,
                delay_outcome=None,label_eligibility='not_mature')
    if latest is None:
        # Caller writes an explicit pending outcome if needed before persistence.
        return result
    status=latest['label_status']
    if status in ('cancelled','lost','conflict'):
        result['label_eligibility']=status
    elif status=='verified' and latest.get('actual_arrival_time'):
        if latest.get('arrival_time_precision') not in ('second','minute'):
            result['label_eligibility']='insufficient_precision'
        else:
            remaining=int((parse_ts(latest['actual_arrival_time'])-parse_ts(snapshot['snapshot_time'])).total_seconds())
            if remaining<=0:
                result['label_eligibility']='already_arrived'
            elif promise is None:
                result['label_eligibility']='conflict'
            else:
                result.update(label_eligibility='eligible',remaining_duration_seconds=remaining,
                              delay_outcome=parse_ts(latest['actual_arrival_time'])>parse_ts(promise['window_end_at']))
    return result


def assign_split(tenant_id,trip_id,departure_time,vehicle_id,route_id,config):
    local=parse_ts(departure_time).astimezone(ZoneInfo(config.get('timezone','America/Los_Angeles'))).date().isoformat()
    split=config.get('split',{})
    roles=[('train',split.get('train_departure_dates',['2026-01-01','2026-06-23'])),
           ('calibration',split.get('calibration_departure_dates',['2026-07-01','2026-07-24'])),
           ('test',split.get('test_departure_dates',['2026-08-01','2026-09-30']))]
    role=next((name for name,(start,end) in roles if start<=local<=end),'excluded')
    held=config.get('heldout',{})
    unseen=vehicle_id in held.get('vehicle_ids',[]) or route_id in held.get('route_ids',[])
    if unseen and role!='test':role='excluded'
    cutoffs={'train':split.get('train_label_cutoff_exclusive','2026-07-01T07:00:00Z'),
             'calibration':split.get('calibration_label_cutoff_exclusive','2026-08-01T07:00:00Z'),
             'test':config.get('extract_as_of','2026-10-15T07:00:00Z'),
             'excluded':config.get('extract_as_of','2026-10-15T07:00:00Z')}
    return dict(tenant_id=tenant_id,split_version='m1-split-v1',trip_id=trip_id,
                split_group_id=tenant_id+'/'+trip_id,split_role=role,
                frozen_at=config.get('generated_at','2026-10-27T00:00:00Z'),label_cutoff_at=utc(cutoffs[role]))


def _derived_provenance(table,key,row,available,parents,config,states=None):
    return dict(provenance_id=row['provenance_id'],source_system_id='DS-M01-PIPELINE',source_record_id=key,
                record_version=config.get('pipeline_version','0.1.0'),record_state='active',event_time=row.get('snapshot_time'),available_time=available,
                retrieved_at=config['generated_at'],extract_as_of=config['extract_as_of'],
                raw_record_reference=f'data/processed/{table}.jsonl#{key}',origin_type='derived',
                source_dataset_id=config.get('source_dataset_id','DS-M01-M1-2026'),synthetic_scenario_id=None,value_states=states or {},
                source_timezone='America/Los_Angeles',source_utc_offset_minutes=None,tenant_id=row['tenant_id'],
                field_origins={f'{table}.{c}':{'origin_type':'derived','source_dataset_ids':[config.get('source_dataset_id','DS-M01-M1-2026')],
                                            'reference':'src/ds_m01/pipeline.py@'+config.get('pipeline_version','0.1.0')} for c in row},
                availability_basis='simulated',availability_evidence='Point-in-time replay of generated source availability; actual execution time recorded separately.',
                canonical_table=table,canonical_record_key={'tenant_id':row['tenant_id'],'key':key},
                parent_provenance_ids=sorted(set(parents)))


def _append_jsonl(stream,row):
    """Write one canonical row without retaining the complete history pool."""
    stream.write(json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n')


def build(root,config):
    root=Path(root)
    if config.get('verify_source_manifest',False):verify_source_manifest(root)
    # Provenance can be gigabytes at full scale; copy its canonical JSONL bytes
    # into the staged output instead of retaining every nested record in RAM.
    tables={t:read_jsonl(root/'data/raw'/f'{t}.jsonl') for t in RAW_TABLES if t!='provenance_record'}
    if config.get('telemetry_mode','snapshot')=='http_snapshot':
        tables['telemetry_event']=read_jsonl(root/'data/ingested/telemetry_event.jsonl')
    elif config.get('telemetry_mode','snapshot')!='snapshot':
        raise ValueError('Unsupported telemetry mode')
    tables['_settings']=config
    snapshots=[];labels=[];assignments=[];lineage=[];memberships=[]
    history_count=0
    # Index by tenant and vehicle; raw telemetry is never joined across tenants.
    telem=defaultdict(list)
    for e in tables['telemetry_event']:telem[(e['tenant_id'],e['vehicle_id'])].append(e)
    plan_by_trip=defaultdict(list)
    for p in tables['eta_trip_plan_version']:plan_by_trip[(p['tenant_id'],p['trip_id'])].append(p)
    events_by_trip=defaultdict(list)
    promises_by_trip=defaultdict(list)
    assignments_by_vehicle=defaultdict(list)
    for e in tables['eta_trip_event']:events_by_trip[(e['tenant_id'],e['trip_id'])].append(e)
    for p in tables['eta_promise_version']:promises_by_trip[(p['tenant_id'],p['trip_id'])].append(p)
    for a in tables['core_vehicle_device_assignment']:assignments_by_vehicle[(a['tenant_id'],a['vehicle_id'])].append(a)
    trip_indexes={'eta_trip_plan_version':plan_by_trip,'eta_promise_version':promises_by_trip,'eta_trip_event':events_by_trip}
    split_by_trip={}
    for trip in tables['eta_trip']:
        key=(trip['tenant_id'],trip['trip_id'])
        plan=min(plan_by_trip[key],key=lambda x:parse_ts(x['plan_known_at']))
        departure_events=[e for e in events_by_trip[key] if e['event_type'] in ('departure','departed')]
        if not departure_events:
            raise ValueError(f'M1 requires explicit actual departure evidence for {key}')
        dep=min(departure_events,key=lambda e:parse_ts(e['occurred_at']))['occurred_at']
        route=next(x for x in tables['eta_route_version'] if x['tenant_id']==key[0] and x['route_version_id']==plan['route_version_id'])
        assignment=assign_split(*key,dep,plan['vehicle_id'],route['route_id'],config)
        assignments.append(assignment);split_by_trip[key]=assignment
        unseen_v=plan['vehicle_id'] in config['heldout']['vehicle_ids'];unseen_r=route['route_id'] in config['heldout']['route_ids']
        memberships.append(dict(tenant_id=key[0],trip_id=key[1],split_role=assignment['split_role'],
            vehicle_id=plan['vehicle_id'],route_id=route['route_id'],unseen_vehicle=unseen_v,unseen_route=unseen_r,
            exclusion_reason=('heldout_before_test' if unseen_v or unseen_r else 'label_buffer_or_outside_main') if assignment['split_role']=='excluded' else None))
        scoped={t:(trip_indexes[t][key] if t in trip_indexes else rows)
                for t,rows in tables.items() if t not in ('telemetry_event','eta_trip_outcome','provenance_record','_settings')}
        start=parse_ts(dep);end=min(start+timedelta(hours=config.get('max_snapshot_hours',12)),parse_ts(config['extract_as_of']))
        vehicles={p['vehicle_id'] for p in plan_by_trip[key]}
        scoped['telemetry_event']=[r for vehicle in vehicles for r in telem[(key[0],vehicle)] if start<=parse_ts(r['event_time'])<=end]
        scoped['core_vehicle_device_assignment']=[a for a in tables['core_vehicle_device_assignment'] if a['tenant_id']==key[0] and a['vehicle_id'] in vehicles]
        scoped['_settings']=config
        # The first prediction is triggered when departure is received. Starting
        # at occurrence time would skip short trips that finish before +30 min.
        at=max(start,min(parse_ts(e['available_at']) for e in departure_events))
        while at<=end:
            snap=snapshot_for_trip(*key,utc(at),scoped)
            if snap is not None:snapshots.append(snap)
            at+=timedelta(minutes=config['settings']['snapshot_interval_minutes'])
    # Labels are joined only AFTER feature construction. They never affect snapshot eligibility.
    by_promise={(r['tenant_id'],r['promise_version_id']):r for r in tables['eta_promise_version']}
    by_plan={(r['tenant_id'],r['trip_version_id']):r for r in tables['eta_trip_plan_version']}
    by_position={(r['tenant_id'],r['canonical_event_id']):r for r in tables['telemetry_event']}
    by_route={(r['tenant_id'],r['route_version_id']):r for r in tables['eta_route_version']}
    outcomes=defaultdict(list)
    for o in tables['eta_trip_outcome']:outcomes[(o['tenant_id'],o['trip_id'])].append(o)
    training_outcomes_by_tenant=defaultdict(list)
    # Preserve input outcome-group order so M1 bytes and deterministic replay
    # remain unchanged. No eligible history member is sampled or capped.
    for other,items in outcomes.items():
        if split_by_trip[other]['split_role']=='train':
            training_outcomes_by_tenant[other[0]].append((other,items))
    with tempfile.TemporaryDirectory(prefix='.pipeline-stage-',dir=root) as tmp:
        stage=Path(tmp)
        with ExitStack() as stack:
            provenance_stream=stack.enter_context((stage/'provenance_record.jsonl').open('w',encoding='utf-8'))
            history_stream=stack.enter_context((stage/'eta_history_reference.jsonl').open('w',encoding='utf-8'))
            with (root/'data/raw/provenance_record.jsonl').open(encoding='utf-8') as source:
                for line in source:
                    if line.strip():_append_jsonl(provenance_stream,json.loads(line))
            for snap in snapshots:
                key=(snap['tenant_id'],snap['trip_id']);split=split_by_trip[key]
                label=label_for_snapshot(snap,outcomes[key],promises_by_trip[key],split['label_cutoff_at'])
                if label['outcome_version_id'] is None:
                    raise ValueError(f'Missing pending outcome before label cutoff for {key}; do not fabricate FK')
                labels.append(label)
                p=by_plan[(key[0],snap['trip_version_id'])];promise=by_promise[(key[0],snap['promise_version_id'])]
                route=by_route[(key[0],snap['route_version_id'])]
                parents=[p['provenance_id'],promise['provenance_id'],route['provenance_id']]
                available=[p['plan_known_at'],promise['known_at'],route['known_at']]
                for event in events_by_trip[key]:
                    if event['event_type'] in ('departure','departed') and parse_ts(event['available_at'])<=parse_ts(snap['snapshot_time']):
                        parents.append(event['provenance_id']);available.append(event['available_at'])
                status_events=[e for e in events_by_trip[key] if parse_ts(e['available_at'])<=parse_ts(snap['snapshot_time']) and parse_ts(e['occurred_at'])<=parse_ts(snap['snapshot_time'])]
                if status_events:
                    status_event=max(status_events,key=lambda e:(parse_ts(e['available_at']),parse_ts(e['occurred_at']),e.get('trip_event_id','')))
                    parents.append(status_event['provenance_id']);available.append(status_event['available_at'])
                if snap['selected_position_event_id']:
                    pos=by_position[(key[0],snap['selected_position_event_id'])];parents.append(pos['provenance_id']);available.append(pos['available_time'])
                    for a in assignments_by_vehicle[(key[0],p['vehicle_id'])]:
                        if a['tenant_id']==key[0] and a['vehicle_id']==p['vehicle_id'] and a['device_id']==pos['device_id'] and _usable_assignment(a) and _within(parse_ts(pos['event_time']),a['valid_from'],a['valid_to']) and _within(parse_ts(snap['snapshot_time']),a['known_from'],a['known_to']):
                            parents.append(a['provenance_id']);available.append(a['known_from'])
                for f in tables['eta_facility_version']:
                    if f['tenant_id']==key[0] and f['facility_version_id'] in (route['origin_facility_version_id'],route['destination_facility_version_id']):
                        parents.append(f['provenance_id']);available.append(f['known_from'])
                max_available=utc(max(map(parse_ts,available)))
                assert parse_ts(max_available)<=parse_ts(snap['snapshot_time']), 'Future source in snapshot lineage'
                states={f'eta_snapshot.{c}':{'state':'not_applicable' if c in ('forecast_id','routing_response_id') else 'missing','reason':'Optional source disabled' if c in ('forecast_id','routing_response_id') else snap['position_validity']} for c,v in snap.items() if v is None}
                _append_jsonl(provenance_stream,_derived_provenance('eta_snapshot',snap['snapshot_id'],snap,snap['snapshot_time'],parents,config,states))
                lineage.append(dict(snapshot_id=snap['snapshot_id'],tenant_id=key[0],max_source_available_time=max_available,
                                    feature_columns=list(FEATURE_COLUMNS),parent_provenance_ids=sorted(set(parents))))
                selected=next(o for o in outcomes[key] if o['outcome_version_id']==label['outcome_version_id'])
                states={f'eta_snapshot_label.{c}':{'state':'not_mature' if label['label_eligibility']=='not_mature' else 'not_applicable','reason':label['label_eligibility']} for c,v in label.items() if v is None}
                label_available=utc(max(map(parse_ts,[snap['snapshot_time'],selected['label_available_time'],promise['known_at']])))
                _append_jsonl(provenance_stream,_derived_provenance('eta_snapshot_label',snap['snapshot_id'],label,label_available,[snap['provenance_id'],selected['provenance_id'],promise['provenance_id']],config,states))
                # Frozen eligible training pool for calibration/test; training snapshots additionally obey their own clock.
                history_cutoff=min(parse_ts(snap['snapshot_time']),parse_ts(config['split']['train_label_cutoff_exclusive']))
                for other,items in training_outcomes_by_tenant[key[0]]:
                    if other==key:continue
                    outcome=_latest(items,'label_available_time',history_cutoff,inclusive=False)
                    if outcome is None or outcome['label_status']!='verified' or not outcome.get('actual_arrival_time') or outcome.get('arrival_time_precision') not in ('second','minute'):continue
                    if parse_ts(outcome['actual_arrival_time'])>=parse_ts(snap['snapshot_time']):continue
                    departures=[e for e in events_by_trip[other] if e['event_type'] in ('departure','departed') and parse_ts(e['available_at'])<history_cutoff and parse_ts(e['occurred_at'])<parse_ts(outcome['actual_arrival_time'])]
                    if not departures:continue
                    departure=min(departures,key=lambda e:parse_ts(e['occurred_at']))
                    duration=int((parse_ts(outcome['actual_arrival_time'])-parse_ts(departure['occurred_at'])).total_seconds())
                    if duration<=0:continue
                    hr=dict(tenant_id=key[0],provenance_id='P-'+ident('history',snap['snapshot_id'],other[1]),snapshot_id=snap['snapshot_id'],
                            historical_trip_id=other[1],historical_outcome_version_id=outcome['outcome_version_id'],
                            historical_duration_seconds=duration,historical_label_known_at=outcome['label_available_time'],baseline_group='tenant_training_pool')
                    _append_jsonl(history_stream,hr)
                    history_count+=1
                    _append_jsonl(provenance_stream,_derived_provenance('eta_history_reference',snap['snapshot_id']+'/'+other[1],hr,snap['snapshot_time'],[outcome['provenance_id'],departure['provenance_id'],snap['provenance_id']],config))
            derived=dict(eta_snapshot=snapshots,eta_snapshot_label=labels,eta_split_assignment=assignments)
            for name,rows in derived.items():write_jsonl(stage/f'{name}.jsonl',rows)
            write_jsonl(stage/'feature_lineage.jsonl',lineage)
            write_jsonl(stage/'trip_membership.jsonl',memberships)
            for stream in (provenance_stream,history_stream):
                stream.flush()
                os.fsync(stream.fileno())
        # Every staged stream is closed before atomic per-file publication.
        destination=root/'data/processed'
        destination.mkdir(parents=True,exist_ok=True)
        for path in sorted(stage.iterdir()):
            with path.open('rb') as persisted:
                os.fsync(persisted.fileno())
            os.replace(path,destination/path.name)
        directory_fd=os.open(destination,os.O_RDONLY)
        try:os.fsync(directory_fd)
        finally:os.close(directory_fd)
    summary={'trip_count':len(tables['eta_trip']),'snapshot_count':len(snapshots),'history_reference_count':history_count,
             'label_eligibility':dict(Counter(r['label_eligibility'] for r in labels)),
             'position_quality':dict(Counter(r['position_validity'] for r in snapshots)),
             'trip_split_counts':dict(Counter(r['split_role'] for r in assignments)),
             'candidate_horizon_hours':config.get('max_snapshot_hours',12),
             'model_trained':False,'metrics_are_model_performance':False,
             'source_kind':'synthetic','feature_definition_version':'m1-features-v1'}
    (root/'reports').mkdir(exist_ok=True)
    (root/'reports'/config.get('summary_filename','m1_summary.json')).write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    return summary
