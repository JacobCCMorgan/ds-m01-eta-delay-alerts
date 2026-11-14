"""Separate typed synthetic-delivery store preserving its historical P06 parent.

Canonical alert/delivery clocks describe the synthetic July scenario. Actual
materialization timestamps live in P07 audits; the imported November release
clocks are preserved and never compared as if they were July dispatch clocks.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_EVEN
import json
import hashlib
from pathlib import Path
import tempfile

import duckdb

from .common import digest_file, ident, parse_ts, read_jsonl, utc, write_jsonl
from . import release_store as parent


PROMOTED=('eta_alert_episode','eta_alert','eta_notification_delivery')
CANONICAL=parent.CANONICAL+PROMOTED
RESERVED=('eta_adoption','eta_action','eta_action_followup')
PRESERVED=tuple(name for name in parent.ORDER if name not in PROMOTED)
ORIGIN='synthetic_scenario'
CLOCK='synthetic_scenario_time'
VERSION='p07-typed-delivery-store-v1'
PARENT_PROTOCOL_SHA256='7b7dbe70ec1d44199bd971607eeec9a029fdee5567a3c4fa1f42f7fac9ee0470'
ALERT_FIELDS=('tenant_id','alert_id','candidate_id','alert_episode_id','trip_id','snapshot_id','prediction_version',
 'decision_threshold_version','created_at','promise_version_id','deadline_at','probability','alert_status','status_reason',
 'first_delivery_id','first_delivered_at','attempt_count','result_origin')
ATTEMPT_FIELDS=('tenant_id','delivery_id','alert_id','candidate_id','alert_episode_id','trip_id','snapshot_id','prediction_version',
 'decision_threshold_version','promise_version_id','deadline_at','attempt_index','sent_at','scheduled_service_at','service_at',
 'service_outcome','scheduled_receipt_at','receipt_known_at','delivered_at','delivery_status','attempt_status','error_reason',
 'retryable','retry_scheduled_at','receipt_observed','receipt_type','recipient_role','received_or_opened_at','result_origin')
EPISODE_FIELDS=('tenant_id','alert_episode_id','trip_id','episode_started_at','episode_closed_at','delivered_alert_count',
 'duplicate_alert_count','counts_as_of','value_states','policy_version','source_episode_status','source_closure_reason',
 'censored_at','original_candidate_count','alert_count','result_origin')
EVENT_FIELDS=('event_id','event_type','event_at','tenant_id','trip_id','alert_episode_id','alert_id','delivery_id',
 'candidate_id','attempt_index','event_status','event_reason','result_origin')
CANONICAL_FIELDS={
 'eta_alert_episode':EPISODE_FIELDS[:9],
 'eta_alert':('tenant_id','alert_id','alert_episode_id','output_id','trip_id','snapshot_id','prediction_version',
              'decision_threshold_version','created_at','alert_status'),
 'eta_notification_delivery':('tenant_id','provenance_id','delivery_id','alert_id','recipient_role','delivered_at',
                              'received_or_opened_at','receipt_type','delivery_status'),
}
NULLABLE=frozenset(('first_delivery_id','first_delivered_at','scheduled_service_at','service_at','service_outcome','scheduled_receipt_at',
 'receipt_known_at','delivered_at','error_reason','retry_scheduled_at','received_or_opened_at','episode_closed_at',
 'source_closure_reason','censored_at'))
INTEGERS=frozenset(('attempt_count','attempt_index','delivered_alert_count','duplicate_alert_count','original_candidate_count','alert_count'))
BOOLEANS=frozenset(('retryable','receipt_observed'))


def _columns(fields, *, events=False):
    result=[]
    for field in fields:
        kind=('TIMESTAMPTZ' if field.endswith('_at') or field=='counts_as_of' else 'INTEGER' if field in INTEGERS
              else 'BOOLEAN' if field in BOOLEANS else 'JSON' if field=='value_states'
              else 'DECIMAL(7,6)' if field=='probability' else 'VARCHAR')
        nullable=field in NULLABLE or events and field in ('alert_episode_id','alert_id','delivery_id','candidate_id','attempt_index','event_reason')
        result.append((field,kind,nullable))
    return result


STAMP_COLUMNS=[('clock_domain','VARCHAR',False),('materialized_at','TIMESTAMPTZ',False),('record','JSON',False)]
AUX={
 'p07_run_audit':[('store_version','VARCHAR',False),('protocol_version','VARCHAR',False),('settings_sha256','VARCHAR',False),
                  ('parent_table_sha256','JSON',False),('simulation_sha256','VARCHAR',False),('counts_as_of','TIMESTAMPTZ',False),
                  ('materialized_at','TIMESTAMPTZ',False),('result_origin','VARCHAR',False),('clock_domain','VARCHAR',False),
                  ('simulation_summary','JSON',False)],
 'p07_provenance_reference':parent.AUX['p06_provenance_reference']+[
                  ('result_origin','VARCHAR',False),('clock_domain','VARCHAR',False),('materialized_at','TIMESTAMPTZ',False)],
 'p07_episode_audit':_columns(EPISODE_FIELDS)+STAMP_COLUMNS,
 'p07_alert_audit':_columns(ALERT_FIELDS)+[('output_id','VARCHAR',False)]+STAMP_COLUMNS,
 'p07_attempt_audit':_columns(ATTEMPT_FIELDS)+[('provenance_id','VARCHAR',False)]+STAMP_COLUMNS,
 'p07_event_audit':_columns(EVENT_FIELDS,events=True)+STAMP_COLUMNS,
}
ORDER=PRESERVED+('p07_run_audit','p07_provenance_reference',*PROMOTED,'p07_episode_audit','p07_alert_audit','p07_attempt_audit','p07_event_audit')
KEYS={**parent.KEYS,'p07_run_audit':('store_version',),'p07_provenance_reference':('tenant_id','provenance_id'),
 'p07_episode_audit':('tenant_id','alert_episode_id'),'p07_alert_audit':('tenant_id','alert_id'),
 'p07_attempt_audit':('tenant_id','delivery_id'),'p07_event_audit':('tenant_id','event_id')}
FKS={**parent.FKS,
 'eta_alert_episode':[(('tenant_id','trip_id'),'p06_source_trip',('tenant_id','trip_id')),
                      (('tenant_id','alert_episode_id'),'p06_episode_audit',('tenant_id','episode_id'))],
 'eta_alert':[(('tenant_id','alert_episode_id'),'eta_alert_episode',('tenant_id','alert_episode_id')),
              (('tenant_id','output_id'),'eta_output_release',('tenant_id','output_id')),
              (('tenant_id','trip_id'),'p06_source_trip',('tenant_id','trip_id')),
              (('tenant_id','snapshot_id'),'p06_source_snapshot',('tenant_id','snapshot_id')),
              (('tenant_id','prediction_version'),'p06_model_binding',('tenant_id','prediction_version')),
              (('decision_threshold_version',),'eta_alert_rule_version',('decision_threshold_version',))],
 'eta_notification_delivery':[(('tenant_id','alert_id'),'eta_alert',('tenant_id','alert_id')),
                              (('tenant_id','provenance_id'),'p07_provenance_reference',('tenant_id','provenance_id'))],
 'p07_episode_audit':[(('tenant_id','alert_episode_id'),'eta_alert_episode',('tenant_id','alert_episode_id'))],
 'p07_alert_audit':[(('tenant_id','alert_id'),'eta_alert',('tenant_id','alert_id')),
                    (('tenant_id','candidate_id'),'p06_candidate_audit',('tenant_id','candidate_id'))],
 'p07_attempt_audit':[(('tenant_id','delivery_id'),'eta_notification_delivery',('tenant_id','delivery_id')),
                      (('tenant_id','alert_id'),'eta_alert',('tenant_id','alert_id')),
                      (('tenant_id','candidate_id'),'p06_candidate_audit',('tenant_id','candidate_id'))],
 'p07_event_audit':[(('tenant_id','trip_id'),'p06_source_trip',('tenant_id','trip_id')),
                    (('tenant_id','alert_episode_id'),'eta_alert_episode',('tenant_id','alert_episode_id')),
                    (('tenant_id','alert_id'),'eta_alert',('tenant_id','alert_id')),
                    (('tenant_id','delivery_id'),'eta_notification_delivery',('tenant_id','delivery_id')),
                    (('tenant_id','candidate_id'),'p06_candidate_audit',('tenant_id','candidate_id'))],
}


def _settings(settings):
    if (settings.get('protocol_version')!='p07-delivery-v1' or tuple(settings['release']['canonical_tables'])!=CANONICAL
            or tuple(settings['release']['remaining_reserved_tables'])!=RESERVED
            or settings['release']['release_mode']!='offline_replay' or settings['release']['delivery_mode']!=ORIGIN):
        raise ValueError('P07 release settings differ from the fixed synthetic table scope')
    return settings['delivery']


def _simulation_digest(simulation):
    keys={'alerts':('tenant_id','alert_id'),'attempts':('tenant_id','delivery_id'),
          'episodes':('tenant_id','alert_episode_id'),'events':('tenant_id','event_id')}
    return parent._digest({**{name:sorted(simulation[name],key=lambda r:tuple(r[k] for k in fields))
                              for name,fields in keys.items()},'summary':simulation['summary']})


def _engine_id(prefix,*parts):
    payload=json.dumps(list(parts),ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    return prefix+'-'+hashlib.sha256(payload.encode()).hexdigest()[:24]


def _parent_projection(tables):
    return {name:deepcopy(tables[name]) if name not in PROMOTED else [] for name in parent.ORDER}


def _schema(root,settings):
    schema=parent._schema(root,settings)
    schema.update(AUX)
    if set(schema)!=set(ORDER):
        raise ValueError('P07 schema inventory differs')
    for name,fields in CANONICAL_FIELDS.items():
        if tuple(field for field,_,_ in schema[name])!=fields:
            raise ValueError('Canonical delivery field contract changed: '+name)
    return schema


def _typed_record(record,fields,generated_at):
    if set(record)!=set(fields) or record.get('result_origin')!=ORIGIN:
        raise ValueError('Simulation record fields or synthetic provenance differ')
    item={name:deepcopy(record[name]) for name in fields}
    if 'probability' in item:
        item['probability']=float(Decimal(item['probability']))
    item.update(clock_domain=CLOCK,materialized_at=generated_at,record=deepcopy(record))
    return item


def build_tables(parent_tables,simulation,generated_at,settings):
    """Promote three contracts while retaining all imported P06 rows verbatim."""
    _settings(settings)
    if set(parent_tables)!=set(parent.ORDER) or any(parent_tables[name] for name in parent.RESERVED):
        raise ValueError('P07 requires the complete unpromoted P06 table population')
    generated_at=utc(generated_at)
    if set(simulation)!={'alerts','attempts','episodes','events','summary'}:
        raise ValueError('Incomplete delivery simulation')
    summary=simulation['summary']
    cutoff=utc(summary['counts_as_of'])
    if parse_ts(cutoff)>parse_ts(generated_at):
        raise ValueError('Scenario observation cutoff cannot follow materialization')
    tables={name:deepcopy(parent_tables[name]) if name in parent_tables else [] for name in ORDER}
    candidates=parent._index(parent_tables['p06_candidate_audit'],('tenant_id','candidate_id'))
    releases=parent._index(parent_tables['eta_output_release'],('tenant_id','output_id'))
    tables['p07_run_audit']=[dict(store_version=VERSION,protocol_version=settings['protocol_version'],
        settings_sha256=parent._digest(settings),parent_table_sha256={name:parent._digest(parent_tables[name]) for name in parent.ORDER},
        simulation_sha256=_simulation_digest(simulation),counts_as_of=cutoff,materialized_at=generated_at,
        result_origin=ORIGIN,clock_domain=CLOCK,simulation_summary=deepcopy(summary))]
    alert_source=parent._index(simulation['alerts'],('tenant_id','alert_id'))
    for record in simulation['episodes']:
        item=_typed_record(record,EPISODE_FIELDS,generated_at)
        tables['p07_episode_audit'].append(item)
        tables['eta_alert_episode'].append({name:item[name] for name in CANONICAL_FIELDS['eta_alert_episode']})
    for record in simulation['alerts']:
        item=_typed_record(record,ALERT_FIELDS,generated_at)
        candidate=candidates.get((record['tenant_id'],record['candidate_id']))
        if candidate is None:
            raise ValueError('Synthetic alert has no tenant-qualified parent candidate')
        item['output_id']=candidate['output_id']
        tables['p07_alert_audit'].append(item)
        tables['eta_alert'].append({name:item[name] for name in CANONICAL_FIELDS['eta_alert']})
    for record in simulation['attempts']:
        item=_typed_record(record,ATTEMPT_FIELDS,generated_at)
        alert=alert_source.get((record['tenant_id'],record['alert_id']))
        if alert is None:
            raise ValueError('Synthetic attempt has no tenant-qualified alert')
        candidate=candidates[record['tenant_id'],alert['candidate_id']]
        output=releases[record['tenant_id'],candidate['output_id']]
        provenance=ident('P07-attempt-provenance',record['tenant_id'],record['delivery_id'],settings['delivery']['version'])
        item['provenance_id']=provenance
        tables['p07_attempt_audit'].append(item)
        tables['eta_notification_delivery'].append({name:item[name] for name in CANONICAL_FIELDS['eta_notification_delivery']})
        tables['p07_provenance_reference'].append(dict(tenant_id=record['tenant_id'],provenance_id=provenance,
            reference_kind='synthetic_delivery_attempt',source_reference='p07_attempt_audit#'+record['delivery_id'],
            source_sha256=parent._digest(record),record_key={'tenant_id':record['tenant_id'],'delivery_id':record['delivery_id'],
                'alert_id':record['alert_id'],'candidate_id':record['candidate_id']},
            parent_provenance_ids=[output['provenance_id']],recorded_at=generated_at,
            result_origin=ORIGIN,clock_domain=CLOCK,materialized_at=generated_at))
    for record in simulation['events']:
        tables['p07_event_audit'].append(_typed_record(record,EVENT_FIELDS,generated_at))
    for name in ORDER:
        parent._index(tables[name],KEYS[name])
        if name not in PRESERVED:
            tables[name].sort(key=lambda r:tuple(r[k] for k in KEYS[name]))
    return tables


def _check_types(tables,schema):
    for name,rows in tables.items():
        parent._index(rows,KEYS[name])
        for row in rows:
            if set(row)!={field for field,_,_ in schema[name]}:
                raise ValueError('P07 strict column mismatch: '+name)
            for field,kind,nullable in schema[name]:
                value=row[field]
                if value is None:
                    if not nullable:
                        raise ValueError('Unexpected NULL: '+name+'.'+field)
                    continue
                if kind=='VARCHAR' and (not isinstance(value,str) or not value.strip()):
                    raise ValueError('Nonempty text required: '+name+'.'+field)
                if kind=='BOOLEAN' and type(value) is not bool:
                    raise ValueError('Boolean required: '+name+'.'+field)
                if kind in ('BIGINT','INTEGER') and type(value) is not int:
                    raise ValueError('Integer required: '+name+'.'+field)
                if kind=='TIMESTAMPTZ':
                    parse_ts(value)
                if kind=='JSON' and not isinstance(value,(dict,list)):
                    raise ValueError('Structured JSON required: '+name+'.'+field)
                if kind=='DOUBLE' or kind.startswith('DECIMAL'):
                    number=parent._finite(value)
                    if kind.startswith('DECIMAL'):
                        places=int(kind.split(',')[1].rstrip(')'))
                        if number!=number.quantize(Decimal(1).scaleb(-places),rounding=ROUND_HALF_EVEN):
                            raise ValueError('Decimal projection differs: '+name+'.'+field)
            json.dumps(row,allow_nan=False)


def _validate_payloads(root,tables,schema,settings):
    config=_settings(settings)
    if set(tables)!=set(ORDER) or any(tables[name] for name in RESERVED):
        raise ValueError('P07 requires its complete typed inventory and empty future workflows')
    _check_types(tables,schema)
    p06_path=Path(root)/'config/p06.json'
    if digest_file(p06_path)!=PARENT_PROTOCOL_SHA256:
        raise ValueError('Historical P06 protocol changed')
    p06_settings=json.loads(p06_path.read_text())
    original=_parent_projection(tables)
    parent._validate_payloads(original,parent._schema(root,p06_settings),p06_settings)
    if len(tables['p07_run_audit'])!=1:
        raise ValueError('P07 requires one materialization binding')
    run=tables['p07_run_audit'][0]
    generated=run['materialized_at']
    cutoff=parse_ts(run['counts_as_of'])
    if (run['store_version']!=VERSION or run['protocol_version']!=settings['protocol_version']
            or run['settings_sha256']!=parent._digest(settings)
            or run['parent_table_sha256']!={name:parent._digest(original[name]) for name in parent.ORDER}
            or run['result_origin']!=ORIGIN or run['clock_domain']!=CLOCK or cutoff>parse_ts(generated)):
        raise ValueError('P07 run/source binding or synthetic clock domain differs')
    if any(parse_ts(row['generated_at'])>parse_ts(generated) for row in tables['p06_opportunity_audit']):
        raise ValueError('P07 materialization precedes its historical P06 parent')
    simulation={'summary':run['simulation_summary']}
    for name,part,fields in (('p07_alert_audit','alerts',ALERT_FIELDS),('p07_attempt_audit','attempts',ATTEMPT_FIELDS),
                             ('p07_episode_audit','episodes',EPISODE_FIELDS),('p07_event_audit','events',EVENT_FIELDS)):
        simulation[part]=[]
        for row in tables[name]:
            record=row['record']
            projected=_typed_record(record,fields,generated)
            if parent._digest({field:row[field] for field in projected})!=parent._digest(projected):
                raise ValueError('Typed delivery audit differs from original simulation record: '+name)
            simulation[part].append(record)
    if run['simulation_sha256']!=_simulation_digest(simulation):
        raise ValueError('Stored simulation differs from its materialization binding')
    rebuilt=build_tables(original,simulation,generated,settings)
    for name in ORDER:
        if parent._digest(tables[name])!=parent._digest(rebuilt[name]):
            raise ValueError('Canonical or provenance projection differs from bound simulation: '+name)
    candidates=parent._index(original['p06_candidate_audit'],('tenant_id','candidate_id'))
    original_episodes=parent._index(original['p06_episode_audit'],('tenant_id','episode_id'))
    alerts=parent._index(tables['p07_alert_audit'],('tenant_id','alert_id'))
    episodes=parent._index(tables['p07_episode_audit'],('tenant_id','alert_episode_id'))
    attempts=parent._index(tables['p07_attempt_audit'],('tenant_id','delivery_id'))
    if {(r['tenant_id'],r['candidate_id']) for r in alerts.values()}!=set(candidates) or len(alerts)!=len(candidates):
        raise ValueError('Every parent candidate must map to exactly one synthetic alert')
    if set(episodes)!=set(original_episodes):
        raise ValueError('All parent episodes, including zero-delivery risks, must remain')
    by_alert=defaultdict(list)
    for key,row in attempts.items():
        akey=(row['tenant_id'],row['alert_id'])
        if akey not in alerts:
            raise ValueError('Attempt has no tenant-qualified alert')
        by_alert[akey].append(row)
    served=defaultdict(list)
    delivered=Counter()
    for key,alert in alerts.items():
        created=parse_ts(alert['created_at'])
        candidate=candidates[alert['tenant_id'],alert['candidate_id']]
        source=candidate['research_candidate']
        if alert['alert_id']!=_engine_id('P07-ALERT',alert['candidate_id']):
            raise ValueError('Alert identity does not bind its parent candidate')
        for field,ref in (('trip_id','trip_id'),('snapshot_id','snapshot_id'),('prediction_version','prediction_version'),
                          ('alert_episode_id','episode_id'),('decision_threshold_version','policy_version'),
                          ('promise_version_id','promise_version_id'),('deadline_at','deadline_at'),('created_at','simulated_issue_at')):
            expected=source[ref]
            value=alert[field]
            if (parse_ts(value)!=parse_ts(expected) if field in ('created_at','deadline_at') else value!=expected):
                raise ValueError('Alert differs from frozen candidate/promise/clock: '+field)
        if alert['probability']!=float(Decimal(source['probability'])) or alert['output_id']!=candidate['output_id']:
            raise ValueError('Alert probability or output differs from parent')
        if alert['alert_status'] not in ('queued','delivered','failed','suppressed','no_action_lead'):
            raise ValueError('Canonical alert status is outside contract')
        observations=sorted(by_alert[key],key=lambda r:r['attempt_index'])
        if (alert['attempt_count']!=len(observations) or len(observations)>config['max_attempts']
                or [r['attempt_index'] for r in observations]!=list(range(1,len(observations)+1))):
            raise ValueError('Attempt indices/counts violate bounded retries')
        successes=[]
        service_success=False
        for i,attempt in enumerate(observations):
            for field in ('candidate_id','alert_episode_id','trip_id','snapshot_id','prediction_version',
                          'decision_threshold_version','promise_version_id','deadline_at'):
                if attempt[field]!=alert[field]:
                    raise ValueError('Attempt differs from alert binding: '+field)
            if attempt['delivery_id']!=_engine_id('P07-DELIVERY',alert['alert_id'],i+1):
                raise ValueError('Attempt identity does not bind alert/index')
            sent=parse_ts(attempt['sent_at'])
            if not created<=sent<=cutoff or (sent-created).total_seconds()>config['ttl_seconds']:
                raise ValueError('Attempt dispatch crosses creation, observation or TTL boundary')
            if (parse_ts(alert['deadline_at'])-sent).total_seconds()<config['minimum_action_lead_seconds']:
                raise ValueError('Attempt dispatched without required prospective lead')
            if service_success:
                raise ValueError('An alert retried after successful service')
            if i:
                previous=observations[i-1]
                if (previous['service_outcome']!='transient_failure' or previous['delivery_status']!='failed'
                        or previous['receipt_known_at'] is None or not previous['retryable']
                        or previous['retry_scheduled_at'] is None or parse_ts(previous['retry_scheduled_at'])!=sent
                        or (sent-parse_ts(previous['receipt_known_at'])).total_seconds()!=config['retry_backoff_seconds'][i-1]):
                    raise ValueError('Retry is not bound to a confirmed transient receipt/backoff')
            if attempt['received_or_opened_at'] is not None or attempt['recipient_role'] not in ('dispatcher','demo_reviewer'):
                raise ValueError('P07 cannot manufacture recipient opening or adoption')
            if attempt['delivery_status'] not in ('queued','delivered','failed','unknown'):
                raise ValueError('Canonical delivery status is outside contract')
            if attempt['attempt_status'] not in ('dispatched','awaiting_receipt','blocked','unknown_resolution','delivered','transient_failure','permanent_failure'):
                raise ValueError('Unknown attempt state')
            if attempt['service_outcome'] not in (None,'delivered','transient_failure','permanent_failure','gate_blocked'):
                raise ValueError('Unknown observed service outcome')
            if attempt['scheduled_service_at'] is not None and parse_ts(attempt['scheduled_service_at'])<sent:
                raise ValueError('Scheduled service precedes dispatch')
            if attempt['scheduled_receipt_at'] is None or parse_ts(attempt['scheduled_receipt_at'])<sent:
                raise ValueError('Scheduled receipt precedes dispatch')
            service=None if attempt['service_at'] is None else parse_ts(attempt['service_at'])
            known=None if attempt['receipt_known_at'] is None else parse_ts(attempt['receipt_known_at'])
            if service is not None and (not sent<=service<=cutoff or attempt['scheduled_service_at'] is None or service!=parse_ts(attempt['scheduled_service_at'])):
                raise ValueError('Observed service clock differs from dispatch, schedule or cutoff')
            if known is not None and (not sent<=known<=cutoff or service is not None and known<service):
                raise ValueError('Receipt knowledge violates service or inclusive cutoff')
            if attempt['receipt_observed']!=(known is not None):
                raise ValueError('Receipt-observed flag differs from known clock')
            if attempt['service_outcome']=='delivered':
                if service is None or (service-created).total_seconds()>config['ttl_seconds']:
                    raise ValueError('Successful service violates TTL or lacks observed clock')
                if (parse_ts(alert['deadline_at'])-service).total_seconds()<config['minimum_action_lead_seconds']:
                    raise ValueError('Successful delivery lacks required notice')
                ep=episodes[alert['tenant_id'],alert['alert_episode_id']]
                if ep['episode_closed_at'] is not None and service>=parse_ts(ep['episode_closed_at']):
                    raise ValueError('Service success occurred after known episode closure')
                served[alert['tenant_id'],alert['trip_id']].append(service)
                service_success=True
            if attempt['delivery_status']=='delivered':
                if (attempt['service_outcome']!='delivered' or service is None or known is None
                        or attempt['delivered_at'] is None or parse_ts(attempt['delivered_at'])!=service
                        or attempt['receipt_type']!='service_delivered' or attempt['attempt_status']!='delivered'):
                    raise ValueError('Confirmed delivery lacks observed successful service/receipt')
                successes.append(attempt)
                delivered[alert['tenant_id'],alert['alert_episode_id']]+=1
            elif attempt['delivered_at'] is not None or attempt['receipt_type']!='none':
                raise ValueError('Unconfirmed attempt cannot claim delivery receipt')
            if attempt['delivery_status']=='queued' and known is not None:
                raise ValueError('Known terminal receipt cannot remain queued')
            if attempt['delivery_status'] in ('failed','unknown') and known is None:
                raise ValueError('Terminal attempt state lacks observed knowledge')
            if attempt['retryable']!=(attempt['delivery_status']=='failed' and attempt['service_outcome']=='transient_failure'):
                raise ValueError('Retryability differs from confirmed transient failure')
            if attempt['retry_scheduled_at'] is not None and (not attempt['retryable'] or known is None or parse_ts(attempt['retry_scheduled_at'])<=known):
                raise ValueError('Retry schedule lacks eligible prior receipt')
        if len(successes)>1:
            raise ValueError('More than one successful receipt exists for a deduplicated alert')
        expected_success=successes[0] if successes else None
        if (alert['alert_status']=='delivered')!=bool(expected_success):
            raise ValueError('Alert delivery state differs from confirmed attempt')
        if (alert['first_delivery_id']!=(expected_success['delivery_id'] if expected_success else None)
                or alert['first_delivered_at']!=(expected_success['delivered_at'] if expected_success else None)):
            raise ValueError('First-success binding differs from observed attempt')
        if created>cutoff and (observations or alert['alert_status']!='queued' or alert['status_reason']!='candidate_not_yet_observed'):
            raise ValueError('Future candidate acquired a premature scenario outcome')
    for clocks in served.values():
        clocks.sort()
        if any((b-a).total_seconds()<config['repeat_suppression_seconds'] for a,b in zip(clocks,clocks[1:])):
            raise ValueError('Successful services violate tenant/trip delivery cooldown')
    for key,row in episodes.items():
        source=original_episodes[key]['research_episode']
        closed=source['episode_closed_at']
        expected_closed=utc(closed) if closed is not None and parse_ts(closed)<=cutoff else None
        for field,expected in (('trip_id',source['trip_id']),('policy_version',source['policy_version']),
                              ('episode_started_at',utc(source['episode_started_at'])),('episode_closed_at',expected_closed),
                              ('source_episode_status',source['episode_status']),('source_closure_reason',source['closure_reason']),
                              ('censored_at',source['censored_at']),('original_candidate_count',source['candidate_count']),
                              ('alert_count',source['candidate_count']),('counts_as_of',utc(run['counts_as_of'])),
                              ('delivered_alert_count',delivered[key]),('duplicate_alert_count',max(delivered[key]-1,0)),
                              ('value_states',{} if expected_closed else {'episode_closed_at':'not_mature'})):
            if row[field]!=expected:
                raise ValueError('Episode source/censoring/delivery count differs: '+field)
    for row in tables['p07_event_audit']:
        if (parse_ts(row['event_at'])>cutoff or row['event_type'] not in ('closure','candidate','dispatch','service','receipt','timeout')):
            raise ValueError('Event lies beyond observation or has an unknown type')
        if row['event_status'] not in ('closed','queued','suppressed','failed','no_action_lead','delivered','transient_failure','permanent_failure','unknown'):
            raise ValueError('Unknown delivery event status')
    summary=run['simulation_summary']
    expected_counts={'candidates':len(candidates),'alerts':len(alerts),'episodes':len(episodes),'attempts':len(attempts),
        'delivered_alerts':sum(r['alert_status']=='delivered' for r in alerts.values()),
        'delivered_attempts':sum(r['delivery_status']=='delivered' for r in attempts.values()),
        'duplicate_deliveries':sum(r['duplicate_alert_count'] for r in episodes.values()),'event_count':len(tables['p07_event_audit']),
        'alert_status_counts':dict(sorted(Counter(r['alert_status'] for r in alerts.values()).items())),
        'delivery_status_counts':dict(sorted(Counter(r['delivery_status'] for r in attempts.values()).items())),
        'labels_used':False,'recipient_openings_generated':False,'notifications_sent':False,'synthetic_delivery_simulated':True}
    if parent._digest({name:summary.get(name) for name in expected_counts})!=parent._digest(expected_counts):
        raise ValueError('Simulation summary overstates or omits observed delivery evidence')


def _sql_checks(con):
    checks=parent._sql_checks(con)
    queries={
      'complete_parent_episode_promotion':'''SELECT count(*) FROM p06_episode_audit p FULL JOIN eta_alert_episode e
        ON p.tenant_id=e.tenant_id AND p.episode_id=e.alert_episode_id
        WHERE p.episode_id IS NULL OR e.alert_episode_id IS NULL OR p.trip_id<>e.trip_id''',
      'complete_parent_candidate_promotion':'''SELECT count(*) FROM p06_candidate_audit p FULL JOIN p07_alert_audit a
        USING(tenant_id,candidate_id) WHERE p.candidate_id IS NULL OR a.candidate_id IS NULL OR p.trip_id<>a.trip_id
        OR p.snapshot_id<>a.snapshot_id OR p.output_id<>a.output_id OR p.prediction_version<>a.prediction_version
        OR p.policy_version<>a.decision_threshold_version OR p.simulated_issue_at<>a.created_at''',
      'canonical_alert_binding':'''SELECT count(*) FROM eta_alert a FULL JOIN p07_alert_audit x USING(tenant_id,alert_id)
        WHERE a.alert_id IS NULL OR x.alert_id IS NULL OR a.alert_episode_id<>x.alert_episode_id OR a.output_id<>x.output_id
        OR a.trip_id<>x.trip_id OR a.snapshot_id<>x.snapshot_id OR a.prediction_version<>x.prediction_version
        OR a.decision_threshold_version<>x.decision_threshold_version OR a.created_at<>x.created_at OR a.alert_status<>x.alert_status''',
      'canonical_attempt_binding':'''SELECT count(*) FROM eta_notification_delivery d FULL JOIN p07_attempt_audit x USING(tenant_id,delivery_id)
        WHERE d.delivery_id IS NULL OR x.delivery_id IS NULL OR d.alert_id<>x.alert_id OR d.provenance_id<>x.provenance_id
        OR d.recipient_role<>x.recipient_role OR d.delivered_at IS DISTINCT FROM x.delivered_at
        OR d.received_or_opened_at IS NOT NULL OR x.received_or_opened_at IS NOT NULL OR d.receipt_type<>x.receipt_type
        OR d.delivery_status<>x.delivery_status''',
      'episode_success_counts_are_not_attempt_counts':'''WITH delivered AS (
        SELECT a.tenant_id,a.alert_episode_id,count(DISTINCT d.alert_id) n FROM eta_notification_delivery d
        JOIN eta_alert a USING(tenant_id,alert_id) WHERE d.delivery_status='delivered' GROUP BY a.tenant_id,a.alert_episode_id)
        SELECT count(*) FROM eta_alert_episode e LEFT JOIN delivered d USING(tenant_id,alert_episode_id)
        WHERE e.delivered_alert_count<>coalesce(d.n,0) OR e.duplicate_alert_count<>greatest(coalesce(d.n,0)-1,0)''',
      'known_receipts_and_synthetic_clock_domain':'''SELECT count(*) FROM p07_attempt_audit a CROSS JOIN p07_run_audit r
        WHERE a.result_origin<>'synthetic_scenario' OR a.clock_domain<>'synthetic_scenario_time' OR a.materialized_at<>r.materialized_at
        OR a.sent_at>r.counts_as_of OR a.service_at>r.counts_as_of OR a.receipt_known_at>r.counts_as_of
        OR (a.delivery_status='delivered' AND (a.receipt_known_at IS NULL OR a.delivered_at IS DISTINCT FROM a.service_at
        OR a.service_outcome<>'delivered'))''',
      'delivery_provenance_links_parent_output':'''SELECT count(*) FROM p07_attempt_audit a JOIN p07_provenance_reference p USING(tenant_id,provenance_id)
        JOIN p07_alert_audit x ON a.tenant_id=x.tenant_id AND a.alert_id=x.alert_id
        JOIN eta_output_release o ON x.tenant_id=o.tenant_id AND x.output_id=o.output_id
        WHERE p.reference_kind<>'synthetic_delivery_attempt' OR p.result_origin<>'synthetic_scenario'
        OR json_array_length(p.parent_provenance_ids)<>1 OR json_extract_string(p.parent_provenance_ids,'$[0]')<>o.provenance_id
        OR json_extract_string(p.record_key,'$.delivery_id')<>a.delivery_id OR p.recorded_at<>p.materialized_at''',
      'one_success_per_alert':'''SELECT count(*) FROM (SELECT tenant_id,alert_id FROM eta_notification_delivery
        WHERE delivery_status='delivered' GROUP BY tenant_id,alert_id HAVING count(*)>1)''',
      'no_future_adoption_action_workflows':'''SELECT (SELECT count(*) FROM eta_adoption)+(SELECT count(*) FROM eta_action)+(SELECT count(*) FROM eta_action_followup)''',
    }
    for name,query in queries.items():
        count=con.execute(query).fetchone()[0]
        checks.append(dict(name=name,violations=count,passed=count==0))
        if count:
            raise ValueError(f'P07 SQL invariant failed: {name} ({count})')
    return checks


def _ddl(schema):
    statements=[]
    for name in ORDER:
        columns=[f'"{field}" {kind}'+('' if nullable else ' NOT NULL') for field,kind,nullable in schema[name]]
        columns.append('PRIMARY KEY ('+','.join(KEYS[name])+')')
        if name=='p06_model_binding':columns.append('UNIQUE (prediction_version)')
        if name=='p06_opportunity_audit':columns.append('UNIQUE (tenant_id,output_id)')
        if name=='p07_alert_audit':columns.append('UNIQUE (tenant_id,candidate_id)')
        if name=='p07_attempt_audit':columns.append('UNIQUE (tenant_id,alert_id,attempt_index)')
        for fields,reference,refs in FKS.get(name,[]):
            columns.append('FOREIGN KEY ('+','.join(fields)+') REFERENCES '+reference+' ('+','.join(refs)+')')
        if name=='eta_alert':
            columns.append("CHECK (alert_status IN ('queued','suppressed','no_action_lead','delivered','failed'))")
        if name=='eta_alert_episode':
            columns.append('CHECK (delivered_alert_count>=0 AND duplicate_alert_count=greatest(delivered_alert_count-1,0))')
        if name=='eta_notification_delivery':
            columns.extend(["CHECK (delivery_status IN ('queued','delivered','failed','unknown'))",
                "CHECK (recipient_role IN ('dispatcher','demo_reviewer'))","CHECK (received_or_opened_at IS NULL)",
                "CHECK ((delivery_status='delivered' AND delivered_at IS NOT NULL AND receipt_type='service_delivered') OR (delivery_status<>'delivered' AND delivered_at IS NULL AND receipt_type='none'))"])
        if name in AUX:
            columns.extend(["CHECK (result_origin='synthetic_scenario')","CHECK (clock_domain='synthetic_scenario_time')"])
        if name=='p07_attempt_audit':columns.append('CHECK (attempt_index>=1)')
        statements.append('CREATE TABLE '+name+' ('+','.join(columns)+');')
    return statements


def _receipt_claims(counts,checks):
    return dict(status='passed',canonical_tables=list(CANONICAL),reserved_empty_tables=list(RESERVED),
        auxiliary_tables=list(parent.AUX)+list(AUX),counts=counts,checks=checks,foreign_keys_enforced=True,
        parquet_roundtrip_verified=True,parent_tables_preserved=list(PRESERVED),release_mode='offline_replay',
        result_origin=ORIGIN,synthetic_delivery_simulated=True,notifications_sent=False,
        recipient_openings_generated=False,adoption_actions_generated=False,final_test_evaluated=False,
        full_m2_warehouse_rebuilt=False)


def write_store(root,destination,tables,settings):
    """Validate all three physical formats and publish only a fresh directory."""
    destination=Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Use a fresh P07 store destination')
    schema=_schema(root,settings)
    _validate_payloads(root,tables,schema,settings)
    statements=_ddl(schema)
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.p07-store-',dir=destination.parent) as temporary:
        stage=Path(temporary)/'complete'
        (stage/'jsonl').mkdir(parents=True)
        (stage/'parquet').mkdir()
        con=duckdb.connect(str(stage/'experiment.duckdb'))
        try:
            con.execute("SET TimeZone='UTC'")
            con.execute('BEGIN')
            for name,statement in zip(ORDER,statements):
                con.execute(statement)
                values=[[json.dumps(row[field],sort_keys=True,allow_nan=False) if kind=='JSON' else row[field]
                         for field,kind,_ in schema[name]] for row in tables[name]]
                if values:
                    con.executemany(f'INSERT INTO {name} VALUES ('+','.join('?' for _ in schema[name])+')',values)
                write_jsonl(stage/'jsonl'/f'{name}.jsonl',tables[name])
            checks=_sql_checks(con)
            con.execute('COMMIT')
            counts={}
            for name in ORDER:
                counts[name]=con.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                path=stage/'parquet'/f'{name}.parquet'
                con.execute(f'COPY (SELECT * FROM {name} ORDER BY '+','.join(KEYS[name])+') TO ? (FORMAT PARQUET,COMPRESSION ZSTD)',[str(path)])
                difference=con.execute(f'''SELECT count(*) FROM ((SELECT * FROM {name} EXCEPT ALL SELECT * FROM read_parquet(?))
                  UNION ALL (SELECT * FROM read_parquet(?) EXCEPT ALL SELECT * FROM {name}))''',[str(path),str(path)]).fetchone()[0]
                if difference:
                    raise ValueError('Parquet roundtrip differs: '+name)
            checks.append(dict(name='parquet_roundtrip_all_tables',violations=0,passed=True))
        finally:
            con.close()
        (stage/'schema.sql').write_text('\n\n'.join(statements)+'\n')
        receipt={**_receipt_claims(counts,checks),'engine':'DuckDB','engine_version':duckdb.__version__,
                 'output_sha256':{str(path.relative_to(stage)):digest_file(path) for path in sorted(stage.rglob('*')) if path.is_file()}}
        (stage/'verification.json').write_text(json.dumps(receipt,indent=2,sort_keys=True)+'\n')
        if destination.exists() or destination.is_symlink():
            raise FileExistsError('P07 store destination appeared during execution')
        stage.rename(destination)
    return receipt


def _constraint_signatures(con):
    rows=con.execute('''SELECT table_name,constraint_type,constraint_column_names,referenced_table,referenced_column_names,expression
      FROM duckdb_constraints() WHERE database_name=current_database()''').fetchall()
    return sorted(json.dumps(row,sort_keys=True) for row in rows)


def verify_store(root,destination,expected_tables,settings):
    """Read-only file, schema, physical constraints and logical-row verification."""
    destination=Path(destination)
    schema=_schema(root,settings)
    _validate_payloads(root,expected_tables,schema,settings)
    receipt=json.loads((destination/'verification.json').read_text())
    paths={'experiment.duckdb','schema.sql'}|{f'{folder}/{name}.{extension}' for folder,extension in
                                           (('jsonl','jsonl'),('parquet','parquet')) for name in ORDER}
    actual={str(path.relative_to(destination)) for path in destination.rglob('*') if path.is_file()}
    if actual!=paths|{'verification.json'} or set(receipt.get('output_sha256',{}))!=paths:
        raise ValueError('P07 store inventory differs from complete typed export')
    for name,sha in receipt['output_sha256'].items():
        if digest_file(destination/name)!=sha:
            raise ValueError('P07 store file changed: '+name)
    statements=_ddl(schema)
    if (destination/'schema.sql').read_text()!='\n\n'.join(statements)+'\n':
        raise ValueError('P07 retained schema differs from required physical definition')
    reference=duckdb.connect(':memory:')
    try:
        for statement in statements:reference.execute(statement)
        wanted_constraints=_constraint_signatures(reference)
    finally:
        reference.close()
    con=duckdb.connect(str(destination/'experiment.duckdb'),read_only=True)
    counts,compared={},0
    try:
        con.execute("SET TimeZone='UTC'")
        if {row[0] for row in con.execute('SHOW TABLES').fetchall()}!=set(ORDER):
            raise ValueError('P07 DuckDB table inventory differs')
        if _constraint_signatures(con)!=wanted_constraints:
            raise ValueError('P07 physical primary/foreign/check/null constraints differ')
        for name in ORDER:
            description=con.execute(f'DESCRIBE {name}').fetchall()
            actual_schema=[(row[0],row[1].replace('TIMESTAMP WITH TIME ZONE','TIMESTAMPTZ'),row[2]=='YES') for row in description]
            if actual_schema!=schema[name]:
                raise ValueError('P07 DuckDB typed schema differs: '+name)
            expected=sorted(expected_tables[name],key=lambda r:tuple(r[k] for k in KEYS[name]))
            if parent._digest(read_jsonl(destination/'jsonl'/f'{name}.jsonl'))!=parent._digest(expected):
                raise ValueError('P07 retained JSONL differs from reconstructed rows: '+name)
            desired=[tuple(parent._logical(row[field],kind) for field,kind,_ in schema[name]) for row in expected]
            projection=','.join(f'CAST({field} AS VARCHAR) AS {field}' if kind=='TIMESTAMPTZ' else field for field,kind,_ in schema[name])
            order=' ORDER BY '+','.join(KEYS[name])
            database=con.execute(f'SELECT {projection} FROM {name}'+order).fetchall()
            path=str(destination/'parquet'/f'{name}.parquet')
            parquet=con.execute(f'SELECT {projection} FROM read_parquet(?)'+order,[path]).fetchall()
            types=[(row[0],row[1].replace('TIMESTAMP WITH TIME ZONE','TIMESTAMPTZ')) for row in con.execute('DESCRIBE SELECT * FROM read_parquet(?)',[path]).fetchall()]
            if types!=[(field,kind) for field,kind,_ in schema[name]]:
                raise ValueError('P07 Parquet typed schema differs: '+name)
            for source,records in (('DuckDB',database),('Parquet',parquet)):
                normalized=[tuple(parent._logical(value,kind) for value,(_,kind,_) in zip(row,schema[name])) for row in records]
                if normalized!=desired:
                    raise ValueError('P07 '+source+' logical rows differ: '+name)
            counts[name]=len(expected)
            compared+=len(expected)*3
        checks=_sql_checks(con)
        checks.append(dict(name='parquet_roundtrip_all_tables',violations=0,passed=True))
    finally:
        con.close()
    claims=_receipt_claims(counts,checks)
    if parent._digest({name:receipt.get(name) for name in claims})!=parent._digest(claims):
        raise ValueError('P07 verification receipt claims differ from inspected evidence')
    return {**claims,'differences':0,'logical_rows_compared':compared,'read_only':True,'foreign_keys_verified':True}


def verify_store_receipts(outer,inner,replayed):
    """Bind an enclosing run's copied receipt to the inspected inner store."""
    if parent._digest(outer)!=parent._digest(inner):
        raise ValueError('Outer and inner P07 store receipts differ')
    names=_receipt_claims(replayed['counts'],replayed['checks'])
    if parent._digest({name:inner.get(name) for name in names})!=parent._digest({name:replayed.get(name) for name in names}):
        raise ValueError('Retained P07 store receipt overstates replay verification')
    return True
