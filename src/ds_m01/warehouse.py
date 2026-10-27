"""Typed DuckDB materialization, Parquet export, and independent SQL invariants."""
from collections import defaultdict
from pathlib import Path
import json
import re
import duckdb
from .common import read_jsonl, digest_file, verify_source_manifest
from .pipeline import RAW_TABLES, DERIVED_TABLES

KEYS={
 'core_tenant':['tenant_id'],'core_vehicle':['tenant_id','vehicle_id'],'core_device':['tenant_id','device_id'],
 'core_vehicle_device_assignment':['tenant_id','assignment_id'],'eta_trip':['tenant_id','trip_id'],
 'eta_facility_version':['tenant_id','facility_version_id'],'eta_route_version':['tenant_id','route_version_id'],
 'eta_trip_plan_version':['tenant_id','trip_version_id'],'eta_promise_version':['tenant_id','promise_version_id'],
 'eta_trip_event':['tenant_id','trip_event_id'],'eta_trip_outcome':['tenant_id','outcome_version_id'],
 'telemetry_event':['tenant_id','canonical_event_id'],'provenance_record':['provenance_id'],
 'eta_snapshot':['tenant_id','snapshot_id'],'eta_snapshot_label':['tenant_id','snapshot_id'],
 'eta_split_assignment':['tenant_id','trip_id','split_version'],
 'eta_history_reference':['tenant_id','snapshot_id','historical_trip_id']}


def quote(name):
    if not re.fullmatch(r'[a-z_][a-z_0-9]*',name):raise ValueError(name)
    return '"'+name+'"'


def build_warehouse(root,config):
    root=Path(root)
    if config.get('verify_source_manifest',False):verify_source_manifest(root)
    contract=defaultdict(list)
    for f in json.loads((root/'contracts/fields.json').read_text()):contract[f['table']].append(f)
    dbpath=root/'data/ds_m01_m1.duckdb'
    # A generated database can be regenerated; source bytes are never overwritten.
    if dbpath.exists():dbpath.unlink()
    con=duckdb.connect(str(dbpath));con.execute("SET TimeZone='UTC'")
    ddl=[];counts={};shape_count=0
    active=list(RAW_TABLES)+list(DERIVED_TABLES)
    parquet=root/'data/parquet';parquet.mkdir(parents=True,exist_ok=True)
    con.execute('BEGIN')
    for table in active:
        where='processed' if table in DERIVED_TABLES or table=='provenance_record' else 'raw'
        path=root/'data'/where/f'{table}.jsonl';fields=contract[table]
        types={f['name']:f['type'] for f in fields}
        if not types:raise ValueError(f'No contract for {table}')
        rows=read_jsonl(path)
        for row in rows:
            if set(row)!=set(types):raise ValueError(f'Column mismatch {table}: {set(row)^set(types)}')
            for f in fields:
                if not f['nullable'] and row[f['name']] is None:raise ValueError(f'NULL {table}.{f["name"]}')
            shape_count+=1
        columns=[f'{quote(f["name"])} {f["type"]}'+('' if f['nullable'] else ' NOT NULL') for f in fields]
        columns.append('PRIMARY KEY ('+','.join(map(quote,KEYS[table]))+')')
        sql=f'CREATE TABLE {quote(table)} ('+','.join(columns)+');'
        ddl.append(sql);con.execute(sql)
        if rows:
            con.read_json(str(path),columns=types,format='newline_delimited',maximum_object_size=16777216).create_view('_incoming',replace=True)
            con.execute(f'INSERT INTO {quote(table)} SELECT '+','.join(map(quote,types))+' FROM _incoming')
        counts[table]=con.execute(f'SELECT count(*) FROM {quote(table)}').fetchone()[0]
        con.execute(f'COPY (SELECT * FROM {quote(table)} ORDER BY '+','.join(map(quote,KEYS[table]))+') TO ? (FORMAT PARQUET, COMPRESSION ZSTD)',[str(parquet/f'{table}.parquet')])
    con.execute('COMMIT')
    (root/'sql/duckdb_schema.sql').write_text('\n\n'.join(ddl)+'\n')
    checks=[]
    def check(name,sql):
        bad=con.execute(sql).fetchone()[0]
        checks.append({'name':name,'violations':bad,'passed':bad==0,'sql':sql})
    check('snapshot_plan_known_at', '''SELECT count(*) FROM eta_snapshot s LEFT JOIN eta_trip_plan_version p ON s.tenant_id=p.tenant_id AND s.trip_version_id=p.trip_version_id WHERE p.trip_version_id IS NULL OR p.plan_known_at>s.snapshot_time OR p.trip_id<>s.trip_id OR p.vehicle_id<>s.vehicle_id''')
    check('snapshot_promise_known_at', '''SELECT count(*) FROM eta_snapshot s LEFT JOIN eta_promise_version p ON s.tenant_id=p.tenant_id AND s.promise_version_id=p.promise_version_id WHERE p.promise_version_id IS NULL OR p.known_at>s.snapshot_time OR p.trip_id<>s.trip_id''')
    check('snapshot_position_known_at', '''SELECT count(*) FROM eta_snapshot s LEFT JOIN telemetry_event p ON s.tenant_id=p.tenant_id AND s.selected_position_event_id=p.canonical_event_id WHERE s.selected_position_event_id IS NOT NULL AND (p.canonical_event_id IS NULL OR p.available_time>s.snapshot_time OR p.event_time>s.snapshot_time OR p.vehicle_id<>s.vehicle_id OR p.device_id<>s.device_id)''')
    check('position_age_calculation', '''SELECT count(*) FROM eta_snapshot WHERE position_age_seconds<>date_diff('second',last_position_time,snapshot_time) OR position_age_seconds<0''')
    check('snapshot_position_validity', '''SELECT count(*) FROM eta_snapshot WHERE (position_validity='no_position' AND (latitude_deg IS NOT NULL OR selected_position_event_id IS NOT NULL)) OR latitude_deg NOT BETWEEN -90 AND 90 OR longitude_deg NOT BETWEEN -180 AND 180 OR remaining_distance_km<0''')
    check('snapshots_have_one_label', '''SELECT count(*) FROM eta_snapshot s LEFT JOIN eta_snapshot_label l USING(tenant_id,snapshot_id) WHERE l.snapshot_id IS NULL''')
    check('labels_match_snapshot_promise', '''SELECT count(*) FROM eta_snapshot_label l JOIN eta_snapshot s USING(tenant_id,snapshot_id) WHERE l.promise_version_id<>s.promise_version_id''')
    check('label_cutoff_is_exclusive', '''SELECT count(*) FROM eta_snapshot_label l JOIN eta_snapshot s USING(tenant_id,snapshot_id) JOIN eta_split_assignment sp USING(tenant_id,trip_id) JOIN eta_trip_outcome o ON l.tenant_id=o.tenant_id AND l.outcome_version_id=o.outcome_version_id WHERE o.label_available_time>=sp.label_cutoff_at''')
    check('latest_outcome_before_cutoff', '''SELECT count(*) FROM eta_snapshot_label l JOIN eta_snapshot s USING(tenant_id,snapshot_id) JOIN eta_split_assignment sp USING(tenant_id,trip_id) JOIN eta_trip_outcome o ON l.tenant_id=o.tenant_id AND l.outcome_version_id=o.outcome_version_id WHERE EXISTS(SELECT 1 FROM eta_trip_outcome later WHERE later.tenant_id=s.tenant_id AND later.trip_id=s.trip_id AND later.label_available_time<sp.label_cutoff_at AND later.label_available_time>o.label_available_time)''')
    check('eligible_targets_use_arrival_not_service', '''SELECT count(*) FROM eta_snapshot_label l JOIN eta_snapshot s USING(tenant_id,snapshot_id) JOIN eta_trip_outcome o ON l.tenant_id=o.tenant_id AND l.outcome_version_id=o.outcome_version_id WHERE l.label_eligibility='eligible' AND (o.label_status<>'verified' OR o.arrival_time_precision NOT IN ('second','minute') OR l.remaining_duration_seconds<>date_diff('second',s.snapshot_time,o.actual_arrival_time) OR l.remaining_duration_seconds<=0 OR l.delay_outcome IS NULL)''')
    check('delay_uses_frozen_promise', '''SELECT count(*) FROM eta_snapshot_label l JOIN eta_promise_version p USING(tenant_id,promise_version_id) JOIN eta_trip_outcome o ON l.tenant_id=o.tenant_id AND l.outcome_version_id=o.outcome_version_id WHERE l.label_eligibility='eligible' AND l.delay_outcome<>(o.actual_arrival_time>p.window_end_at)''')
    check('unknown_not_zero_or_on_time', '''SELECT count(*) FROM eta_snapshot_label WHERE label_eligibility<>'eligible' AND (remaining_duration_seconds IS NOT NULL OR delay_outcome IS NOT NULL)''')
    check('history_training_only_no_self', '''SELECT count(*) FROM eta_history_reference h JOIN eta_snapshot s USING(tenant_id,snapshot_id) JOIN eta_split_assignment sp ON h.tenant_id=sp.tenant_id AND h.historical_trip_id=sp.trip_id WHERE sp.split_role<>'train' OR h.historical_trip_id=s.trip_id OR h.historical_label_known_at>=s.snapshot_time OR h.historical_label_known_at>=TIMESTAMPTZ '2026-07-01T07:00:00Z' ''')
    check('history_independent_departure_duration', '''SELECT count(*) FROM eta_history_reference h JOIN eta_trip_outcome o ON h.tenant_id=o.tenant_id AND h.historical_outcome_version_id=o.outcome_version_id WHERE o.arrival_time_precision NOT IN ('second','minute') OR h.historical_duration_seconds<>(SELECT date_diff('second',min(e.occurred_at),o.actual_arrival_time) FROM eta_trip_event e WHERE e.tenant_id=h.tenant_id AND e.trip_id=h.historical_trip_id AND e.event_type='departure')''')
    check('provenance_fk_snapshots_labels_history', '''SELECT count(*) FROM (SELECT provenance_id FROM eta_snapshot UNION ALL SELECT provenance_id FROM eta_snapshot_label UNION ALL SELECT provenance_id FROM eta_history_reference) d LEFT JOIN provenance_record p USING(provenance_id) WHERE p.provenance_id IS NULL''')
    check('snapshot_derived_availability', '''SELECT count(*) FROM eta_snapshot s JOIN provenance_record p USING(provenance_id) WHERE p.available_time<>s.snapshot_time OR p.origin_type<>'derived' ''')
    check('source_availability_after_event', '''SELECT count(*) FROM telemetry_event WHERE available_time<event_time''')
    check('split_groups_are_trip_unique', '''SELECT count(*) FROM (SELECT tenant_id,trip_id,count(DISTINCT split_role) n FROM eta_split_assignment GROUP BY tenant_id,trip_id HAVING n<>1)''')
    check('arrival_service_delivery_chronology', '''SELECT count(*) FROM eta_trip_outcome WHERE service_start_time<actual_arrival_time OR delivery_completed_time<service_start_time''')
    # Python checks complement relational invariants for nested provenance / allowlists.
    prov={p['provenance_id']:p for p in read_jsonl(root/'data/processed/provenance_record.jsonl')}
    bad_parent=0;bad_clock=0
    from .common import parse_ts
    for row in prov.values():
        for pid in row['parent_provenance_ids']:
            if pid not in prov:bad_parent+=1;continue
            if row['available_time'] and prov[pid]['available_time'] and parse_ts(prov[pid]['available_time'])>parse_ts(row['available_time']):bad_clock+=1
    checks.extend([{'name':'all_provenance_parents_exist','violations':bad_parent,'passed':bad_parent==0},
                   {'name':'derived_available_after_all_parents','violations':bad_clock,'passed':bad_clock==0}])
    member=read_jsonl(root/'data/processed/trip_membership.jsonl')
    bad=sum(r['split_role'] in ('train','calibration') and (r['unseen_vehicle'] or r['unseen_route']) for r in member)
    checks.append({'name':'heldout_excluded_from_fit_and_calibration','violations':bad,'passed':bad==0})
    leaks=sum(parse_ts(r['max_source_available_time'])>parse_ts(next(s['snapshot_time'] for s in read_jsonl(root/'data/processed/eta_snapshot.jsonl') if s['snapshot_id']==r['snapshot_id'])) for r in read_jsonl(root/'data/processed/feature_lineage.jsonl'))
    checks.append({'name':'feature_parent_asof_allowlist','violations':leaks,'passed':leaks==0})
    checks.append({'name':'canonical_row_shapes_and_not_null','rows_checked':shape_count,'violations':0,'passed':True})
    report={'engine':'DuckDB','engine_version':duckdb.__version__,'active_canonical_tables':len(active),'counts':counts,
            'checks':checks,'passed':all(c['passed'] for c in checks),'database_path':'data/ds_m01_m1.duckdb',
            'parquet_export_count':len(active),'full_project_28_tables_complete':False}
    con.close()
    (root/'reports/quality_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report
