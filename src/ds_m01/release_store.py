"""Typed offline policy export with complete opportunity accounting.

This is a separate experiment store, not a rebuild of the M2 warehouse. Five
canonical output contracts are populated; research decisions and source
references live in explicitly named auxiliary tables. No notification is sent.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN
import hashlib
import json
import math
from pathlib import Path
import tempfile

import duckdb

from .common import digest_file, ident, parse_ts, read_jsonl, utc, write_jsonl

MODEL_FIELDS = ('prediction_version', 'training_cutoff_at', 'feature_definition_version',
                'nominal_interval_coverage', 'split_version', 'interval_calibration_version',
                'probability_calibration_version', 'calibration_cutoff_at', 'baseline_definition')
CANONICAL = ('eta_model_version', 'eta_evaluation_membership', 'eta_prediction',
             'eta_alert_rule_version', 'eta_output_release')
RESERVED = ('eta_alert_episode', 'eta_alert', 'eta_notification_delivery', 'eta_adoption',
            'eta_action', 'eta_action_followup')
# References in canonical rows resolve from the enclosing run/report directory.
SELECTION_REFERENCE = 'selection_manifest.json'
FORBIDDEN = {'delay_label', 'actual_arrival_time', 'eligibility', 'label_known_at',
             'outcome_version_id', 'target_seconds', 'remaining_duration_seconds'}
AUX = {
 'p06_model_binding': [('tenant_id','VARCHAR',False),('prediction_version','VARCHAR',False),('source_bindings','JSON',False)],
 'p06_source_trip': [('tenant_id','VARCHAR',False),('trip_id','VARCHAR',False),('vehicle_id','VARCHAR',False),('route_id','VARCHAR',False),('departure_time','TIMESTAMPTZ',False),('unseen_vehicle','BOOLEAN',False),('unseen_route','BOOLEAN',False)],
 'p06_provenance_reference': [('tenant_id','VARCHAR',False),('provenance_id','VARCHAR',False),('reference_kind','VARCHAR',False),('source_reference','VARCHAR',False),('source_sha256','VARCHAR',False),('record_key','JSON',False),('parent_provenance_ids','JSON',False),('recorded_at','TIMESTAMPTZ',False)],
 'p06_source_snapshot': [('tenant_id','VARCHAR',False),('snapshot_id','VARCHAR',False),('trip_id','VARCHAR',False),('snapshot_time','TIMESTAMPTZ',False),('promise_version_id','VARCHAR',False),('deadline_at','TIMESTAMPTZ',False),('promise_known_at','TIMESTAMPTZ',False),('snapshot_provenance_id','VARCHAR',False),('promise_provenance_id','VARCHAR',False)],
 'p06_rule_audit': [('policy_version','VARCHAR',False),('enabled','BOOLEAN',False),('threshold','DECIMAL(7,6)',True),('selection_status','VARCHAR',False),('experimental','BOOLEAN',False),('quality_limits','JSON',False),('rule','JSON',False),('selection_manifest','JSON',False)],
 'p06_opportunity_audit': [('tenant_id','VARCHAR',False),('trip_id','VARCHAR',False),('snapshot_id','VARCHAR',False),('snapshot_time','TIMESTAMPTZ',False),('prediction_version','VARCHAR',False),('output_id','VARCHAR',False),('eta_status','VARCHAR',False),('probability_status','VARCHAR',False),('probability','DECIMAL(7,6)',True),('lower_seconds','DOUBLE',True),('median_seconds','DOUBLE',True),('upper_seconds','DOUBLE',True),('lower_seconds_int','BIGINT',True),('median_seconds_int','BIGINT',True),('upper_seconds_int','BIGINT',True),('canonical_prediction_written','BOOLEAN',False),('generated_at','TIMESTAMPTZ',False),('opportunity','JSON',False)],
 'p06_episode_audit': [('tenant_id','VARCHAR',False),('episode_id','VARCHAR',False),('trip_id','VARCHAR',False),('policy_version','VARCHAR',False),('research_episode','JSON',False)],
 'p06_candidate_audit': [('tenant_id','VARCHAR',False),('candidate_id','VARCHAR',False),('episode_id','VARCHAR',False),('trip_id','VARCHAR',False),('snapshot_id','VARCHAR',False),('prediction_version','VARCHAR',False),('output_id','VARCHAR',False),('policy_version','VARCHAR',False),('simulated_issue_at','TIMESTAMPTZ',False),('probability','DECIMAL(7,6)',False),('created_at','TIMESTAMPTZ',False),('research_candidate','JSON',False)],
 'p06_decision_audit': [('tenant_id','VARCHAR',False),('trip_id','VARCHAR',False),('snapshot_id','VARCHAR',False),('snapshot_time','TIMESTAMPTZ',False),('prediction_version','VARCHAR',False),('policy_version','VARCHAR',False),('candidate_status','VARCHAR',False),('candidate_id','VARCHAR',True),('episode_id','VARCHAR',True),('lead_seconds','DOUBLE',False),('probability','DECIMAL(7,6)',True),('reason','VARCHAR',False),('decision','JSON',False)],
}
KEYS = {
 'eta_model_version': ('prediction_version',), 'p06_model_binding': ('tenant_id','prediction_version'),
 'p06_source_trip': ('tenant_id','trip_id'), 'p06_provenance_reference': ('tenant_id','provenance_id'),
 'p06_source_snapshot': ('tenant_id','snapshot_id'), 'eta_evaluation_membership': ('tenant_id','snapshot_id','split_version'),
 'eta_prediction': ('tenant_id','snapshot_id','prediction_version'), 'p06_rule_audit': ('policy_version',),
 'eta_alert_rule_version': ('decision_threshold_version',), 'p06_opportunity_audit': ('tenant_id','snapshot_id','prediction_version'),
 'eta_output_release': ('tenant_id','output_id'), 'p06_episode_audit': ('tenant_id','episode_id'),
 'p06_candidate_audit': ('tenant_id','candidate_id'), 'p06_decision_audit': ('tenant_id','snapshot_id','prediction_version','policy_version'),
 'eta_alert_episode': ('tenant_id','alert_episode_id'), 'eta_alert': ('tenant_id','alert_id'),
 'eta_notification_delivery': ('tenant_id','delivery_id'), 'eta_adoption': ('tenant_id','adoption_id'),
 'eta_action': ('tenant_id','action_id'), 'eta_action_followup': ('tenant_id','action_id','outcome_version_id'),
}
ORDER = ('eta_model_version','p06_model_binding','p06_source_trip','p06_provenance_reference',
         'p06_source_snapshot','eta_evaluation_membership','eta_prediction','p06_rule_audit',
         'eta_alert_rule_version','p06_opportunity_audit','eta_output_release','p06_episode_audit',
         'p06_candidate_audit','p06_decision_audit',*RESERVED)


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _key(row):
    return row['tenant_id'], row['snapshot_id']


def _index(rows, fields):
    result = {}
    for row in rows:
        key = tuple(row[name] for name in fields)
        if key in result or any(not isinstance(v,str) or not v.strip() for v in key):
            raise ValueError('Duplicate or invalid release identity')
        result[key] = row
    return result


def _finite(value):
    if isinstance(value,bool) or not isinstance(value,(int,float,Decimal)) or not math.isfinite(float(value)):
        raise ValueError('Release value must be finite numeric data')
    return Decimal(str(value))


def _check_opportunity(row):
    if FORBIDDEN.intersection(row):
        raise ValueError('Release opportunity cannot contain outcome targets')
    at = parse_ts(row['snapshot_time'])
    if parse_ts(row['departure_time']) > at or parse_ts(row['promise_known_at']) > at:
        raise ValueError('Release source context was unavailable at the snapshot')
    for field in ('unseen_route','unseen_vehicle'):
        if type(row[field]) is not bool:
            raise ValueError('Heldout flags must be explicit booleans')
    for field in ('eta_status','probability_status'):
        if row[field] not in ('available','unavailable'):
            raise ValueError('Release requires explicit availability status')
    precise = [row[k] for k in ('lower_seconds','median_seconds','upper_seconds')]
    projected = [row[k] for k in ('lower_seconds_int','median_seconds_int','upper_seconds_int')]
    if row['eta_status'] == 'unavailable':
        if any(v is not None for v in precise+projected):
            raise ValueError('Unavailable ETA must retain null values')
    else:
        lo, mid, hi = map(_finite,precise)
        if not 0 <= lo <= mid <= hi:
            raise ValueError('ETA endpoints must be nonnegative and ordered')
        median = int(mid.to_integral_value(rounding=ROUND_HALF_EVEN))
        expected = [min(int(lo.to_integral_value(rounding=ROUND_FLOOR)),median),median,
                    max(int(hi.to_integral_value(rounding=ROUND_CEILING)),median)]
        if any(type(v) is not int for v in projected) or projected != expected:
            raise ValueError('ETA integer projection differs from frozen rounding')
    if row['probability_status'] == 'unavailable':
        if any(row.get(k) is not None for k in ('probability','raw_probability','calibrated_probability')):
            raise ValueError('Unavailable probability must retain null values')
    else:
        raw, calibrated = _finite(row['raw_probability']), _finite(row['calibrated_probability'])
        if not 0 <= raw <= 1 or not 0 <= calibrated <= 1:
            raise ValueError('Probability must lie in [0,1]')
        expected = calibrated.quantize(Decimal('0.000001'),rounding=ROUND_HALF_EVEN)
        if not isinstance(row['probability'],str) or row['probability'] != format(expected,'.6f'):
            raise ValueError('Canonical probability differs from frozen six-decimal projection')


def _fallback(row):
    if row['eta_source'] in ('candidate','model','native'):
        return 'none'
    if row['unseen_route']:
        return 'unseen_route'
    if row['position_validity'] == 'no_position':
        return 'no_position'
    if row['position_validity'] == 'low_confidence':
        return 'low_confidence'
    return 'invalid_inputs' if 'invalid' in str(row.get('eta_reason')) else 'insufficient_history'


def build_tables(opportunities, selection, registry, generated_at, settings):
    """Project frozen forecasts and outcome-free policy decisions into contracts."""
    generated_at = utc(generated_at)
    opportunities = list(opportunities)
    by_key = _index(opportunities,('tenant_id','snapshot_id'))
    model_by_tenant = _index(registry,('tenant_id',))
    _index(registry,('prediction_version',))
    if set(model_by_tenant) != {(r['tenant_id'],) for r in opportunities}:
        raise ValueError('Registry must cover exactly the opportunity tenants')
    rule = selection['rule']
    from .alert_policy import make_rule
    if make_rule(settings,rule['threshold'],enabled=rule['enabled']) != rule:
        raise ValueError('Rule version does not bind frozen policy settings')
    if type(rule['enabled']) is not bool or (not rule['enabled'] and rule['threshold'] is not None):
        raise ValueError('Disabled policy requires an explicit null threshold')
    threshold = Decimal(rule['threshold']) if rule['enabled'] else None
    if threshold is not None and not 0 <= threshold <= 1:
        raise ValueError('Invalid rule threshold')
    if (rule['minimum_action_lead_seconds'] != settings['policy']['minimum_action_lead_seconds']
            or rule['repeat_suppression_seconds'] != settings['policy']['repeat_suppression_seconds']
            or rule['daily_candidate_cap_per_tenant'] != settings['policy']['daily_candidate_cap_per_tenant']):
        raise ValueError('Policy parameters differ from frozen configuration')
    simulation = selection['simulation']
    decisions = _index(simulation['decisions'],('tenant_id','snapshot_id'))
    if set(decisions) != set(by_key):
        raise ValueError('Policy decisions must cover every opportunity exactly once')
    tables = {name:[] for name in ORDER}
    tables['p06_rule_audit'] = [dict(policy_version=rule['policy_version'],enabled=rule['enabled'],
        threshold=float(threshold) if threshold is not None else None,selection_status=selection['status'],
        experimental=True,quality_limits=settings['quality_limits'],rule=rule,
        selection_manifest=selection['selection_manifest'])]
    tables['eta_alert_rule_version'] = [dict(decision_threshold_version=rule['policy_version'],
        delay_alert_threshold=float(threshold) if threshold is not None else 1.0,
        minimum_action_lead_seconds=rule['minimum_action_lead_seconds'],repeat_suppression_seconds=rule['repeat_suppression_seconds'],
        threshold_selection_ref=SELECTION_REFERENCE)]
    references, trips = {}, {}
    def reference(tenant, identifier, kind, path, digest, key, parents=()):
        item = dict(tenant_id=tenant,provenance_id=identifier,reference_kind=kind,source_reference=path,
                    source_sha256=digest,record_key=key,parent_provenance_ids=sorted(set(parents)),recorded_at=generated_at)
        ref = tenant,identifier
        if ref in references and references[ref] != item:
            raise ValueError('Conflicting provenance reference')
        references[ref] = item
    for model in registry:
        if not isinstance(model['source_bindings'],dict) or not model['source_bindings']:
            raise ValueError('Model registry needs retained source bindings')
        tables['eta_model_version'].append({**{name:model[name] for name in MODEL_FIELDS},
                                            'nominal_interval_coverage':float(Decimal(str(model['nominal_interval_coverage'])))})
        tables['p06_model_binding'].append({name:model[name] for name in ('tenant_id','prediction_version','source_bindings')})
        reference(model['tenant_id'],ident('P06-model',model['tenant_id'],model['prediction_version']),
                  'retained_model_bundle','p06_model_binding#'+model['prediction_version']+'/source_bindings',_digest(model['source_bindings']),
                  {'prediction_version':model['prediction_version']})
    grouped = defaultdict(list)
    for row in opportunities:
        grouped[row['tenant_id'],row['trip_id']].append(row)
    weights = {}
    for rows in grouped.values():
        rows.sort(key=lambda r:(parse_ts(r['snapshot_time']),r['snapshot_id']))
        regular = (Decimal(1)/len(rows)).quantize(Decimal('0.000000001'),rounding=ROUND_HALF_EVEN)
        for index,row in enumerate(rows):
            weights[_key(row)] = regular if index < len(rows)-1 else Decimal(1)-regular*(len(rows)-1)
    for key,row in sorted(by_key.items()):
        _check_opportunity(row)
        model = model_by_tenant[row['tenant_id'],]
        if row['prediction_version'] != model['prediction_version']:
            raise ValueError('Prediction version is not owned by opportunity tenant')
        if not parse_ts(model['training_cutoff_at']) <= parse_ts(model['calibration_cutoff_at']) <= parse_ts(row['snapshot_time']) <= parse_ts(generated_at):
            raise ValueError('Model, calibration, snapshot or execution clocks disagree')
        trip = {name:row[name] for name in ('tenant_id','trip_id','vehicle_id','route_id','departure_time','unseen_vehicle','unseen_route')}
        tkey = row['tenant_id'],row['trip_id']
        if tkey in trips and trips[tkey] != trip:
            raise ValueError('Conflicting source trip context')
        trips[tkey] = trip
        snapshot = {name:row[name] for name,_,_ in AUX['p06_source_snapshot']}
        tables['p06_source_snapshot'].append(snapshot)
        for kind,path,identifier,record_key in (
                ('source_snapshot','data/processed/eta_snapshot.jsonl',row['snapshot_provenance_id'],{'tenant_id':row['tenant_id'],'snapshot_id':row['snapshot_id']}),
                ('source_promise','data/raw/eta_promise_version.jsonl',row['promise_provenance_id'],{'tenant_id':row['tenant_id'],'trip_id':row['trip_id'],'promise_version_id':row['promise_version_id']})):
            reference(row['tenant_id'],identifier,kind,path,settings['input_sha256'][path],record_key)
        output = ident('P06-output',row['tenant_id'],row['snapshot_id'],row['prediction_version'])
        provenance = ident('P06-output-provenance',row['tenant_id'],output)
        release = dict(tenant_id=row['tenant_id'],provenance_id=provenance,output_id=output,
                       prediction_version=row['prediction_version'],released_at=generated_at,release_mode='offline_replay')
        reference(row['tenant_id'],provenance,'offline_output','eta_output_release#'+output,_digest(release),
                  {'tenant_id':row['tenant_id'],'output_id':output},
                  [row['snapshot_provenance_id'],row['promise_provenance_id'],ident('P06-model',row['tenant_id'],row['prediction_version'])])
        lead = (parse_ts(row['deadline_at'])-parse_ts(row['snapshot_time'])).total_seconds()
        lead_slice = ('departure' if row['elapsed_trip_seconds']==0 else 'past_promise' if lead<0 else
                      'lt_30m' if lead<1800 else '30_to_120m' if lead<=7200 else 'gt_120m')
        tables['eta_evaluation_membership'].append(dict(tenant_id=row['tenant_id'],snapshot_id=row['snapshot_id'],
            split_version=model['split_version'],lead_time_slice=lead_slice,unseen_route=row['unseen_route'],
            trip_evaluation_weight=float(weights[key]),evaluation_slice=[
                {'dimension':'vehicle_seen','value':'unseen' if row['unseen_vehicle'] else 'seen'},
                {'dimension':'period','value':'policy_development'},
                {'dimension':'position_coverage','value':row['position_validity']}]))
        probability = float(Decimal(row['probability'])) if row['probability'] is not None else None
        written = row['eta_status']=='available'
        if written:
            tables['eta_prediction'].append(dict(tenant_id=row['tenant_id'],snapshot_id=row['snapshot_id'],
                prediction_version=row['prediction_version'],eta_median=utc(parse_ts(row['snapshot_time'])+timedelta(seconds=row['median_seconds_int'])),
                eta_lower=utc(parse_ts(row['snapshot_time'])+timedelta(seconds=row['lower_seconds_int'])),
                eta_upper=utc(parse_ts(row['snapshot_time'])+timedelta(seconds=row['upper_seconds_int'])),
                delay_probability=probability,fallback_reason=_fallback(row),remaining_eta_seconds=row['median_seconds_int'],
                generated_at=generated_at,value_states={} if probability is not None else {'delay_probability':'unknown'}))
        tables['eta_output_release'].append(release)
        audit = {name:row[name] for name,_,_ in AUX['p06_opportunity_audit'] if name in row}
        audit.update(output_id=output,probability=probability,canonical_prediction_written=written,generated_at=generated_at,opportunity=row)
        tables['p06_opportunity_audit'].append(audit)
        decision = decisions[key]
        for field in ('tenant_id','trip_id','snapshot_id','snapshot_time','promise_version_id','deadline_at','prediction_version','probability'):
            if decision[field] != row[field]:
                raise ValueError('Decision differs from its source opportunity')
        if decision['policy_version'] != rule['policy_version'] or decision['lead_seconds'] != lead:
            raise ValueError('Decision rule or prospective lead differs')
        tables['p06_decision_audit'].append({**{name:decision[name] for name,_,_ in AUX['p06_decision_audit'] if name in decision},
                                           'probability':probability,'decision':decision})
    tables['p06_source_trip'] = list(trips.values())
    tables['p06_provenance_reference'] = list(references.values())
    for episode in simulation['episodes']:
        tables['p06_episode_audit'].append({**{name:episode[name] for name in ('tenant_id','episode_id','trip_id','policy_version')},
                                           'research_episode':episode})
    for candidate in simulation['candidates']:
        row = by_key[_key(candidate)]
        for field in ('tenant_id','trip_id','prediction_version','promise_version_id','deadline_at','probability'):
            if candidate[field] != row[field]:
                raise ValueError('Candidate differs from its frozen opportunity')
        if candidate['policy_version'] != rule['policy_version'] or parse_ts(candidate['simulated_issue_at']) != parse_ts(row['snapshot_time']):
            raise ValueError('Candidate policy or simulated clock differs')
        tables['p06_candidate_audit'].append({**{name:candidate[name] for name,_,_ in AUX['p06_candidate_audit'] if name in candidate},
            'output_id':ident('P06-output',row['tenant_id'],row['snapshot_id'],row['prediction_version']),
            'probability':float(Decimal(candidate['probability'])),'created_at':generated_at,'research_candidate':candidate})
    for name,rows in tables.items():
        _index(rows,KEYS[name])
        rows.sort(key=lambda r:tuple(r[field] for field in KEYS[name]))
    return tables


FKS = {
 'p06_model_binding': [(('prediction_version',),'eta_model_version',('prediction_version',))],
 'p06_source_snapshot': [(('tenant_id','trip_id'),'p06_source_trip',('tenant_id','trip_id')),
                         (('tenant_id','snapshot_provenance_id'),'p06_provenance_reference',('tenant_id','provenance_id')),
                         (('tenant_id','promise_provenance_id'),'p06_provenance_reference',('tenant_id','provenance_id'))],
 'eta_evaluation_membership': [(('tenant_id','snapshot_id'),'p06_source_snapshot',('tenant_id','snapshot_id'))],
 'eta_prediction': [(('tenant_id','snapshot_id'),'p06_source_snapshot',('tenant_id','snapshot_id')),
                    (('tenant_id','prediction_version'),'p06_model_binding',('tenant_id','prediction_version'))],
 'eta_alert_rule_version': [(('decision_threshold_version',),'p06_rule_audit',('policy_version',))],
 'p06_opportunity_audit': [(('tenant_id','snapshot_id'),'p06_source_snapshot',('tenant_id','snapshot_id')),
                          (('tenant_id','prediction_version'),'p06_model_binding',('tenant_id','prediction_version'))],
 'eta_output_release': [(('tenant_id','output_id'),'p06_opportunity_audit',('tenant_id','output_id')),
                       (('tenant_id','prediction_version'),'p06_model_binding',('tenant_id','prediction_version')),
                       (('tenant_id','provenance_id'),'p06_provenance_reference',('tenant_id','provenance_id'))],
 'p06_episode_audit': [(('tenant_id','trip_id'),'p06_source_trip',('tenant_id','trip_id')),
                      (('policy_version',),'p06_rule_audit',('policy_version',))],
 'p06_candidate_audit': [(('tenant_id','episode_id'),'p06_episode_audit',('tenant_id','episode_id')),
                        (('tenant_id','output_id'),'eta_output_release',('tenant_id','output_id')),
                        (('tenant_id','snapshot_id'),'p06_source_snapshot',('tenant_id','snapshot_id')),
                        (('tenant_id','prediction_version'),'p06_model_binding',('tenant_id','prediction_version')),
                        (('policy_version',),'p06_rule_audit',('policy_version',))],
 'p06_decision_audit': [(('tenant_id','snapshot_id'),'p06_source_snapshot',('tenant_id','snapshot_id')),
                       (('tenant_id','prediction_version'),'p06_model_binding',('tenant_id','prediction_version')),
                       (('policy_version',),'p06_rule_audit',('policy_version',)),
                       (('tenant_id','candidate_id'),'p06_candidate_audit',('tenant_id','candidate_id')),
                       (('tenant_id','episode_id'),'p06_episode_audit',('tenant_id','episode_id'))],
}


def _schema(repo_root, settings):
    contract_path = Path(repo_root)/'contracts/fields.json'
    if digest_file(contract_path) != settings['field_contract_sha256']:
        raise ValueError('Canonical field contract changed')
    schema = defaultdict(list)
    for field in json.loads(contract_path.read_text()):
        if field['table'] in CANONICAL+RESERVED:
            schema[field['table']].append((field['name'],field['type'],field['nullable']))
    schema.update(AUX)
    if set(schema) != set(ORDER):
        raise ValueError('Release schema inventory differs')
    return schema


def _validate_payloads(tables, schema, settings):
    if set(tables) != set(ORDER) or any(tables[name] for name in RESERVED):
        raise ValueError('Store must contain five canonical contracts and six empty future tables')
    if tuple(settings['release']['canonical_tables']) != CANONICAL or tuple(settings['release']['remaining_reserved_tables']) != RESERVED:
        raise ValueError('Frozen release table inventory differs')
    for name,rows in tables.items():
        _index(rows,KEYS[name])
        for row in rows:
            if set(row) != {field for field,_,_ in schema[name]}:
                raise ValueError('Release column mismatch: '+name)
            for field,kind,nullable in schema[name]:
                value = row[field]
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
                if kind.startswith('DECIMAL') or kind=='DOUBLE':
                    number = _finite(value)
                    if kind.startswith('DECIMAL'):
                        places = int(kind.split(',')[1].rstrip(')'))
                        if number != number.quantize(Decimal(1).scaleb(-places),rounding=ROUND_HALF_EVEN):
                            raise ValueError('Value exceeds declared decimal precision: '+name+'.'+field)
                if kind=='TIMESTAMPTZ':
                    parse_ts(value)
                if kind=='JSON' and not isinstance(value,(dict,list)):
                    raise ValueError('Structured JSON required: '+name+'.'+field)
            json.dumps(row,allow_nan=False)
    for row in tables['p06_opportunity_audit']:
        source = row['opportunity']
        _check_opportunity(source)
        for name in ('tenant_id','trip_id','snapshot_id','snapshot_time','prediction_version','eta_status','probability_status',
                     'lower_seconds','median_seconds','upper_seconds','lower_seconds_int','median_seconds_int','upper_seconds_int'):
            if row[name] != source[name]:
                raise ValueError('Opportunity audit columns differ from retained source')
        expected_probability = float(Decimal(source['probability'])) if source['probability'] is not None else None
        if row['probability'] != expected_probability or row['canonical_prediction_written'] != (source['eta_status']=='available'):
            raise ValueError('Opportunity audit availability or canonical probability differs')
        if row['output_id'] != ident('P06-output',row['tenant_id'],row['snapshot_id'],row['prediction_version']):
            raise ValueError('Output identity differs from its frozen opportunity')
    source_opportunities = {_key(row):row['opportunity'] for row in tables['p06_opportunity_audit']}
    for row in tables['p06_source_snapshot']:
        source = source_opportunities.get(_key(row))
        if source is None or any(row[name] != source[name] for name in row):
            raise ValueError('Source snapshot context differs from opportunity reference')
    for name,payload in (('p06_decision_audit','decision'),('p06_candidate_audit','research_candidate')):
        for row in tables[name]:
            retained = row[payload]
            source = source_opportunities.get(_key(row))
            if source is None:
                raise ValueError('Research decision has no source opportunity')
            for field in ('tenant_id','trip_id','snapshot_id','prediction_version','promise_version_id','deadline_at','probability'):
                if retained[field] != source[field]:
                    raise ValueError('Research decision differs from frozen promise or forecast')
            for field in row.keys() & retained.keys() - {'probability'}:
                if row[field] != retained[field]:
                    raise ValueError('Typed decision differs from retained decision payload')
    references = {(row['tenant_id'],row['provenance_id']):row for row in tables['p06_provenance_reference']}
    for model in tables['p06_model_binding']:
        identifier = ident('P06-model',model['tenant_id'],model['prediction_version'])
        ref = references.get((model['tenant_id'],identifier))
        if (model['source_bindings'].get('tenant_id') != model['tenant_id'] or ref is None
                or ref['source_sha256'] != _digest(model['source_bindings'])
                or ref['source_reference'] != 'p06_model_binding#'+model['prediction_version']+'/source_bindings'):
            raise ValueError('Model provenance reference differs from tenant-bound source manifest')
    for release in tables['eta_output_release']:
        ref = references.get((release['tenant_id'],release['provenance_id']))
        if ref is None or ref['source_sha256'] != _digest(release) or ref['source_reference'] != 'eta_output_release#'+release['output_id']:
            raise ValueError('Output provenance reference differs from actual release record')
    for row in tables['p06_rule_audit']:
        rule = row['rule']
        if (row['enabled'] != rule['enabled'] or row['policy_version'] != rule['policy_version']
                or row['quality_limits'] != settings['quality_limits'] or row['experimental'] is not True
                or row['threshold'] != (float(Decimal(rule['threshold'])) if rule['enabled'] else None)):
            raise ValueError('Rule audit differs from explicit experimental policy')
        from .alert_policy import make_rule
        if make_rule(settings,rule['threshold'],enabled=rule['enabled']) != rule:
            raise ValueError('Rule audit version does not bind frozen policy parameters')
    rules = {row['policy_version']:row['rule'] for row in tables['p06_rule_audit']}
    for row in tables['eta_alert_rule_version']:
        rule = rules.get(row['decision_threshold_version'])
        if (rule is None or row['minimum_action_lead_seconds'] != rule['minimum_action_lead_seconds']
                or row['repeat_suppression_seconds'] != rule['repeat_suppression_seconds']
                or row['threshold_selection_ref'] != SELECTION_REFERENCE):
            raise ValueError('Canonical rule differs from frozen policy binding')
    expected_references = {}
    for row in tables['p06_source_snapshot']:
        expected_references[row['tenant_id'],row['snapshot_provenance_id']] = (
            'source_snapshot', {'tenant_id':row['tenant_id'],'snapshot_id':row['snapshot_id']})
        expected_references[row['tenant_id'],row['promise_provenance_id']] = (
            'source_promise', {'tenant_id':row['tenant_id'],'trip_id':row['trip_id'],'promise_version_id':row['promise_version_id']})
    for row in tables['p06_provenance_reference']:
        if row['reference_kind'] in ('source_snapshot','source_promise'):
            path = 'data/processed/eta_snapshot.jsonl' if row['reference_kind']=='source_snapshot' else 'data/raw/eta_promise_version.jsonl'
            if row['source_reference'] != path or row['source_sha256'] != settings['input_sha256'][path]:
                raise ValueError('Source provenance reference is not bound to frozen input')
            if expected_references.get((row['tenant_id'],row['provenance_id'])) != (row['reference_kind'],row['record_key']):
                raise ValueError('Source provenance record key differs from referenced metadata')
    for row in tables['eta_prediction']:
        if row['fallback_reason'] not in ('none','unseen_route','stale_position','no_position','insufficient_history','invalid_inputs','low_confidence'):
            raise ValueError('Canonical fallback reason is outside contract values')
        expected = {} if row['delay_probability'] is not None else {'delay_probability':'unknown'}
        if row['value_states'] != expected:
            raise ValueError('Canonical probability null state is not explicit')
    for row in tables['eta_model_version']:
        if not 0 < row['nominal_interval_coverage'] < 1:
            raise ValueError('Nominal interval coverage must lie strictly between zero and one')
    groups = defaultdict(list)
    for row in source_opportunities.values():
        groups[row['tenant_id'],row['trip_id']].append(row)
    expected_weights = {}
    for rows in groups.values():
        ordered = sorted(rows,key=lambda row:(parse_ts(row['snapshot_time']),row['snapshot_id']))
        regular = (Decimal(1)/len(ordered)).quantize(Decimal('0.000000001'),rounding=ROUND_HALF_EVEN)
        for index,row in enumerate(ordered):
            expected_weights[_key(row)] = regular if index<len(ordered)-1 else Decimal(1)-regular*(len(ordered)-1)
    for row in tables['eta_evaluation_membership']:
        source = source_opportunities.get(_key(row))
        if source is None:
            raise ValueError('Canonical membership has no source opportunity')
        lead = (parse_ts(source['deadline_at'])-parse_ts(source['snapshot_time'])).total_seconds()
        expected_lead = ('departure' if source['elapsed_trip_seconds']==0 else 'past_promise' if lead<0
                         else 'lt_30m' if lead<1800 else '30_to_120m' if lead<=7200 else 'gt_120m')
        expected_slices = [
            {'dimension':'vehicle_seen','value':'unseen' if source['unseen_vehicle'] else 'seen'},
            {'dimension':'period','value':'policy_development'},
            {'dimension':'position_coverage','value':source['position_validity']}]
        if (row['lead_time_slice'] != expected_lead or row['unseen_route'] != source['unseen_route']
                or row['evaluation_slice'] != expected_slices
                or Decimal(str(row['trip_evaluation_weight'])) != expected_weights[_key(row)]):
            raise ValueError('Canonical membership differs from all-opportunity reconstruction')


def _sql_checks(con):
    queries = {
      'complete_opportunity_accounting': '''SELECT count(*) FROM p06_source_snapshot s
        FULL OUTER JOIN p06_opportunity_audit a USING(tenant_id,snapshot_id)
        FULL OUTER JOIN p06_decision_audit d USING(tenant_id,snapshot_id)
        FULL OUTER JOIN eta_evaluation_membership e USING(tenant_id,snapshot_id)
        WHERE s.snapshot_id IS NULL OR a.snapshot_id IS NULL OR d.snapshot_id IS NULL OR e.snapshot_id IS NULL''',
      'release_covers_every_opportunity': '''SELECT count(*) FROM p06_opportunity_audit a FULL OUTER JOIN eta_output_release r
        USING(tenant_id,output_id) WHERE a.output_id IS NULL OR r.output_id IS NULL OR a.prediction_version<>r.prediction_version
        OR r.release_mode<>'offline_replay' OR r.released_at<>a.generated_at''',
      'prediction_support_and_projection': '''SELECT count(*) FROM p06_opportunity_audit a LEFT JOIN eta_prediction p
        USING(tenant_id,snapshot_id,prediction_version) WHERE a.canonical_prediction_written<>(p.snapshot_id IS NOT NULL)
        OR (p.snapshot_id IS NOT NULL AND (p.remaining_eta_seconds<>a.median_seconds_int OR p.remaining_eta_seconds<0
        OR p.eta_median<>a.snapshot_time+a.median_seconds_int*INTERVAL '1 second'
        OR p.eta_lower<>a.snapshot_time+a.lower_seconds_int*INTERVAL '1 second'
        OR p.eta_upper<>a.snapshot_time+a.upper_seconds_int*INTERVAL '1 second'
        OR p.eta_lower>p.eta_median OR p.eta_median>p.eta_upper OR p.eta_lower<a.snapshot_time
        OR p.generated_at<>a.generated_at OR p.delay_probability IS DISTINCT FROM a.probability))''',
      'no_extra_prediction_rows': '''SELECT count(*) FROM eta_prediction p LEFT JOIN p06_opportunity_audit a
        USING(tenant_id,snapshot_id,prediction_version) WHERE a.snapshot_id IS NULL''',
      'membership_weights_and_model_split': '''SELECT count(*) FROM (
        SELECT s.tenant_id,s.trip_id,sum(e.trip_evaluation_weight) weight
        FROM eta_evaluation_membership e JOIN p06_source_snapshot s USING(tenant_id,snapshot_id)
        GROUP BY s.tenant_id,s.trip_id HAVING weight<>1
        UNION ALL SELECT a.tenant_id,a.trip_id,0 FROM eta_evaluation_membership e
        JOIN p06_opportunity_audit a USING(tenant_id,snapshot_id) JOIN eta_model_version m USING(prediction_version)
        WHERE e.split_version<>m.split_version)''',
      'tenant_trip_and_decision_bindings': '''SELECT count(*) FROM p06_decision_audit d
        JOIN p06_opportunity_audit a USING(tenant_id,snapshot_id,prediction_version)
        JOIN p06_source_snapshot s USING(tenant_id,snapshot_id) WHERE d.trip_id<>s.trip_id OR a.trip_id<>s.trip_id
        OR d.snapshot_time<>s.snapshot_time OR a.snapshot_time<>s.snapshot_time OR d.probability IS DISTINCT FROM a.probability
        OR d.lead_seconds<>date_diff('microsecond',s.snapshot_time,s.deadline_at)/1000000.0''',
      'model_and_source_clocks': '''SELECT count(*) FROM p06_opportunity_audit a JOIN eta_model_version m USING(prediction_version)
        JOIN p06_source_snapshot s USING(tenant_id,snapshot_id) JOIN p06_source_trip t ON s.tenant_id=t.tenant_id AND s.trip_id=t.trip_id
        WHERE m.training_cutoff_at>m.calibration_cutoff_at OR m.calibration_cutoff_at>s.snapshot_time
        OR s.promise_known_at>s.snapshot_time OR t.departure_time>s.snapshot_time OR a.generated_at<s.snapshot_time''',
      'disabled_rule_is_explicit': '''SELECT count(*) FROM eta_alert_rule_version c FULL OUTER JOIN p06_rule_audit a
        ON c.decision_threshold_version=a.policy_version WHERE c.decision_threshold_version IS NULL OR a.policy_version IS NULL
        OR (a.enabled AND (a.threshold IS NULL OR c.delay_alert_threshold<>a.threshold))
        OR (NOT a.enabled AND (a.threshold IS NOT NULL OR c.delay_alert_threshold<>1))
        OR c.delay_alert_threshold<0 OR c.delay_alert_threshold>1''',
      'candidate_and_decision_correspondence': '''SELECT count(*) FROM p06_candidate_audit c FULL OUTER JOIN p06_decision_audit d
        ON c.tenant_id=d.tenant_id AND c.candidate_id=d.candidate_id
        WHERE (c.candidate_id IS NOT NULL AND (d.candidate_id IS NULL OR d.candidate_status<>'admitted'
        OR c.snapshot_id<>d.snapshot_id OR c.trip_id<>d.trip_id OR c.prediction_version<>d.prediction_version
        OR c.policy_version<>d.policy_version OR c.episode_id IS DISTINCT FROM d.episode_id
        OR c.probability<>d.probability OR c.simulated_issue_at<>d.snapshot_time))
        OR (d.candidate_status='admitted' AND c.candidate_id IS NULL)
        OR (d.candidate_status<>'admitted' AND d.candidate_id IS NOT NULL)''',
      'candidate_scope_and_clock': '''SELECT count(*) FROM p06_candidate_audit c JOIN p06_rule_audit r USING(policy_version)
        JOIN p06_episode_audit e ON c.tenant_id=e.tenant_id AND c.episode_id=e.episode_id
        JOIN p06_opportunity_audit a ON c.tenant_id=a.tenant_id AND c.output_id=a.output_id
        WHERE NOT r.enabled OR c.trip_id<>e.trip_id OR c.policy_version<>e.policy_version
        OR c.snapshot_id<>a.snapshot_id OR c.prediction_version<>a.prediction_version OR c.created_at<>a.generated_at''',
      'probabilities_in_range': '''SELECT count(*) FROM p06_opportunity_audit WHERE probability<0 OR probability>1''',
      'provenance_reference_parents_resolve': '''WITH edges AS (SELECT child.tenant_id,child.recorded_at,json_extract_string(j.value,'$') parent_id
        FROM p06_provenance_reference child,json_each(child.parent_provenance_ids) j)
        SELECT count(*) FROM edges LEFT JOIN p06_provenance_reference parent
        ON edges.tenant_id=parent.tenant_id AND edges.parent_id=parent.provenance_id
        WHERE parent.provenance_id IS NULL OR parent.recorded_at>edges.recorded_at''',
      'source_provenance_kinds': '''SELECT count(*) FROM p06_source_snapshot s JOIN p06_provenance_reference sp
        ON s.tenant_id=sp.tenant_id AND s.snapshot_provenance_id=sp.provenance_id
        JOIN p06_provenance_reference pp ON s.tenant_id=pp.tenant_id AND s.promise_provenance_id=pp.provenance_id
        WHERE sp.reference_kind<>'source_snapshot' OR pp.reference_kind<>'source_promise'
        OR json_extract_string(sp.record_key,'$.snapshot_id') IS DISTINCT FROM s.snapshot_id
        OR json_extract_string(sp.record_key,'$.tenant_id') IS DISTINCT FROM s.tenant_id
        OR json_extract_string(pp.record_key,'$.promise_version_id') IS DISTINCT FROM s.promise_version_id
        OR json_extract_string(pp.record_key,'$.trip_id') IS DISTINCT FROM s.trip_id
        OR json_extract_string(pp.record_key,'$.tenant_id') IS DISTINCT FROM s.tenant_id''',
    }
    results = []
    for name,sql in queries.items():
        violations = con.execute(sql).fetchone()[0]
        results.append({'name':name,'violations':violations,'passed':violations==0})
        if violations:
            raise ValueError(f'Release invariant failed: {name} ({violations})')
    return results


def write_store(repo_root, destination, tables, settings):
    """Validate and atomically publish a new self-contained experiment store.

    Source-reference tables contain imported identities and hash references, not
    a claim that this subset recreates all 28 tables or full source provenance.
    """
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Use a fresh release-store directory')
    schema = _schema(repo_root,settings)
    _validate_payloads(tables,schema,settings)
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.p06-store-',dir=destination.parent) as temporary:
        stage = Path(temporary)/'complete'
        (stage/'jsonl').mkdir(parents=True)
        (stage/'parquet').mkdir()
        connection = duckdb.connect(str(stage/'experiment.duckdb'))
        ddl = []
        try:
            connection.execute("SET TimeZone='UTC'")
            connection.execute('BEGIN')
            for name in ORDER:
                columns = [f'"{field}" {kind}'+('' if nullable else ' NOT NULL') for field,kind,nullable in schema[name]]
                columns.append('PRIMARY KEY ('+','.join(KEYS[name])+')')
                if name=='p06_model_binding':
                    columns.append('UNIQUE (prediction_version)')
                if name=='p06_opportunity_audit':
                    columns.append('UNIQUE (tenant_id,output_id)')
                for fields,parent,parents in FKS.get(name,[]):
                    columns.append('FOREIGN KEY ('+','.join(fields)+') REFERENCES '+parent+' ('+','.join(parents)+')')
                statement = f'CREATE TABLE {name} ('+','.join(columns)+');'
                connection.execute(statement)
                ddl.append(statement)
                values = [[json.dumps(row[field],sort_keys=True,allow_nan=False) if kind=='JSON' else row[field]
                           for field,kind,_ in schema[name]] for row in tables[name]]
                if values:
                    connection.executemany(f'INSERT INTO {name} VALUES ('+','.join('?' for _ in schema[name])+')',values)
                write_jsonl(stage/'jsonl'/f'{name}.jsonl',tables[name])
            checks = _sql_checks(connection)
            connection.execute('COMMIT')
            counts = {}
            for name in ORDER:
                counts[name] = connection.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                output = stage/'parquet'/f'{name}.parquet'
                connection.execute(f'COPY (SELECT * FROM {name} ORDER BY '+','.join(KEYS[name])+') TO ? (FORMAT PARQUET,COMPRESSION ZSTD)',[str(output)])
                differences = connection.execute(f'''SELECT count(*) FROM (
                  (SELECT * FROM {name} EXCEPT ALL SELECT * FROM read_parquet(?))
                  UNION ALL (SELECT * FROM read_parquet(?) EXCEPT ALL SELECT * FROM {name}))''',[str(output),str(output)]).fetchone()[0]
                if differences:
                    raise ValueError('Parquet roundtrip differs: '+name)
            checks.append({'name':'parquet_roundtrip_all_tables','violations':0,'passed':True})
        finally:
            connection.close()
        (stage/'schema.sql').write_text('\n\n'.join(ddl)+'\n')
        verification = dict(status='passed',engine='DuckDB',engine_version=duckdb.__version__,
            canonical_tables=list(CANONICAL),reserved_empty_tables=list(RESERVED),auxiliary_tables=list(AUX),
            counts=counts,opportunities=len(tables['p06_opportunity_audit']),canonical_predictions=len(tables['eta_prediction']),
            checks=checks,foreign_keys_enforced=True,parquet_roundtrip_verified=True,release_mode='offline_replay',
            full_m2_warehouse_rebuilt=False,notifications_sent=False,final_test_evaluated=False,
            output_sha256={str(p.relative_to(stage)):digest_file(p) for p in sorted(stage.rglob('*')) if p.is_file()})
        (stage/'verification.json').write_text(json.dumps(verification,indent=2,sort_keys=True)+'\n')
        if destination.exists() or destination.is_symlink():
            raise FileExistsError('Release destination appeared during execution')
        stage.rename(destination)
    return verification


def _logical(value, kind):
    if value is None:
        return None
    if kind=='TIMESTAMPTZ':
        return parse_ts(value)
    if kind=='JSON':
        return json.loads(value) if isinstance(value,str) else value
    if kind.startswith('DECIMAL'):
        places = int(kind.split(',')[1].rstrip(')'))
        return Decimal(str(value)).quantize(Decimal(1).scaleb(-places),rounding=ROUND_HALF_EVEN)
    return value


def verify_store(repo_root, destination, expected_tables, settings):
    """Read-only hash, schema, constraint and logical-row verification of a store."""
    destination = Path(destination)
    schema = _schema(repo_root,settings)
    _validate_payloads(expected_tables,schema,settings)
    receipt = json.loads((destination/'verification.json').read_text())
    paths = {'experiment.duckdb','schema.sql'} | {f'{folder}/{name}.{extension}' for folder,extension in
                                               (('jsonl','jsonl'),('parquet','parquet')) for name in ORDER}
    actual = {str(p.relative_to(destination)) for p in destination.rglob('*') if p.is_file()}
    if actual != paths | {'verification.json'} or set(receipt.get('output_sha256',{})) != paths:
        raise ValueError('Release store inventory differs from complete typed export')
    for name,digest in receipt['output_sha256'].items():
        if digest_file(destination/name) != digest:
            raise ValueError('Release store file changed: '+name)
    connection = duckdb.connect(str(destination/'experiment.duckdb'),read_only=True)
    counts,compared = {},0
    try:
        connection.execute("SET TimeZone='UTC'")
        if {row[0] for row in connection.execute('SHOW TABLES').fetchall()} != set(ORDER):
            raise ValueError('DuckDB table inventory differs')
        constraints = connection.execute("SELECT table_name,constraint_type FROM duckdb_constraints() WHERE database_name=current_database()").fetchall()
        for name in ORDER:
            expected_schema = [(field,kind) for field,kind,_ in schema[name]]
            actual_schema = [(row[0],row[1].replace('TIMESTAMP WITH TIME ZONE','TIMESTAMPTZ')) for row in connection.execute(f'DESCRIBE {name}').fetchall()]
            if actual_schema != expected_schema:
                raise ValueError('DuckDB typed schema differs: '+name)
            if (name,'PRIMARY KEY') not in constraints or sum(t==name and kind=='FOREIGN KEY' for t,kind in constraints) != len(FKS.get(name,[])):
                raise ValueError('DuckDB physical constraints differ: '+name)
            expected = sorted(expected_tables[name],key=lambda r:tuple(r[k] for k in KEYS[name]))
            stored_json = read_jsonl(destination/'jsonl'/f'{name}.jsonl')
            if stored_json != expected:
                raise ValueError('Retained JSONL differs from replayed rows: '+name)
            wanted = [tuple(_logical(row[field],kind) for field,kind,_ in schema[name]) for row in expected]
            sql_order = ' ORDER BY '+','.join(KEYS[name])
            # Fetch UTC clock text to avoid DuckDB's optional pytz conversion.
            projection = ','.join(f'CAST({field} AS VARCHAR) AS {field}' if kind=='TIMESTAMPTZ' else field
                                  for field,kind,_ in schema[name])
            database = connection.execute(f'SELECT {projection} FROM {name}'+sql_order).fetchall()
            parquet = connection.execute(f'SELECT {projection} FROM read_parquet(?)'+sql_order,
                                         [str(destination/'parquet'/f'{name}.parquet')]).fetchall()
            parquet_schema = [(row[0],row[1].replace('TIMESTAMP WITH TIME ZONE','TIMESTAMPTZ')) for row in connection.execute('DESCRIBE SELECT * FROM read_parquet(?)',
                                        [str(destination/'parquet'/f'{name}.parquet')]).fetchall()]
            if parquet_schema != expected_schema:
                raise ValueError('Parquet typed schema differs: '+name)
            for origin,values in (('DuckDB',database),('Parquet',parquet)):
                normalized = [tuple(_logical(value,kind) for value,(_,kind,_) in zip(row,schema[name])) for row in values]
                if normalized != wanted:
                    raise ValueError(origin+' logical rows differ: '+name)
            counts[name] = len(expected)
            compared += len(expected)*3
        checks = _sql_checks(connection)
    finally:
        connection.close()
    if (receipt.get('status')!='passed' or receipt.get('counts')!=counts
            or receipt.get('release_mode')!='offline_replay' or receipt.get('notifications_sent') is not False
            or receipt.get('final_test_evaluated') is not False or receipt.get('full_m2_warehouse_rebuilt') is not False):
        raise ValueError('Retained store verification claims differ from inspected evidence')
    return dict(status='passed',differences=0,logical_rows_compared=compared,counts=counts,checks=checks,
                read_only=True,foreign_keys_verified=True,parquet_roundtrip_verified=True,notifications_sent=False)
