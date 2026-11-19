"""Resource-bounded execution of the frozen M2 warehouse checks.

The build body below preserves all 27 SQL/streaming invariants from
src/ds_m01/warehouse.py. Materialization, JSON row-shape validation, Parquet
export and provenance-edge checks reuse that frozen implementation directly.
Only connection resource settings change: a 2 GB engine budget, one worker
thread and a private spill directory capped at 16 GB. Original files remain
unchanged; this is an additive execution adapter, not a new scientific result.
"""
from collections import defaultdict
import json
from pathlib import Path

import duckdb

from ds_m01.common import parse_ts, verify_source_manifest
from ds_m01.pipeline import RAW_TABLES, DERIVED_TABLES
from ds_m01.warehouse import (
    materialize_tables, provenance_violations, iter_jsonl, POSITION_AGE_VIOLATIONS,
)

def build_warehouse(root,config):
    root=Path(root)
    if config.get('verify_source_manifest',False):verify_source_manifest(root)
    contract=defaultdict(list)
    for f in json.loads((root/'contracts/fields.json').read_text()):contract[f['table']].append(f)
    filename=config.get('database_filename','ds_m01_m1.duckdb')
    if Path(filename).name!=filename or not filename.endswith('.duckdb'):
        raise ValueError('database_filename must be a .duckdb basename')
    dbpath=root/'data'/filename
    if dbpath.exists():dbpath.unlink()
    con=duckdb.connect(str(dbpath));con.execute("SET TimeZone='UTC'")
    # All settings affect resources only; every retained invariant is unchanged.
    spill=root/'data/p11_duckdb_spill'
    spill.mkdir(exist_ok=True)
    con.execute("SET memory_limit='2GB'")
    con.execute("SET threads=1")
    con.execute("SET max_temp_directory_size='16GB'")
    con.execute('SET temp_directory=?',[str(spill)])
    active=list(RAW_TABLES)+list(DERIVED_TABLES)
    required=config.get('required_tables',active)
    include_reserved=config.get('materialize_reserved_tables',False)
    ddl,counts,statuses,shape_count=materialize_tables(con,root,contract,active,required,include_reserved)
    (root/'sql/duckdb_schema.sql').write_text('\n\n'.join(ddl)+'\n')
    checks=[]
    def check(name,sql):
        bad=con.execute(sql).fetchone()[0]
        checks.append({'name':name,'violations':bad,'passed':bad==0,'sql':sql})
    check('snapshot_plan_known_at', '''SELECT count(*) FROM eta_snapshot s LEFT JOIN eta_trip_plan_version p ON s.tenant_id=p.tenant_id AND s.trip_version_id=p.trip_version_id WHERE p.trip_version_id IS NULL OR p.plan_known_at>s.snapshot_time OR p.trip_id<>s.trip_id OR p.vehicle_id<>s.vehicle_id''')
    check('snapshot_promise_known_at', '''SELECT count(*) FROM eta_snapshot s LEFT JOIN eta_promise_version p ON s.tenant_id=p.tenant_id AND s.promise_version_id=p.promise_version_id WHERE p.promise_version_id IS NULL OR p.known_at>s.snapshot_time OR p.trip_id<>s.trip_id''')
    check('snapshot_position_known_at', '''SELECT count(*) FROM eta_snapshot s LEFT JOIN telemetry_event p ON s.tenant_id=p.tenant_id AND s.selected_position_event_id=p.canonical_event_id WHERE s.selected_position_event_id IS NOT NULL AND (p.canonical_event_id IS NULL OR p.available_time>s.snapshot_time OR p.event_time>s.snapshot_time OR p.vehicle_id<>s.vehicle_id OR p.device_id<>s.device_id)''')
    check('position_age_calculation', POSITION_AGE_VIOLATIONS)
    check('snapshot_position_validity', '''SELECT count(*) FROM eta_snapshot WHERE (position_validity='no_position' AND (latitude_deg IS NOT NULL OR selected_position_event_id IS NOT NULL)) OR latitude_deg NOT BETWEEN -90 AND 90 OR longitude_deg NOT BETWEEN -180 AND 180 OR remaining_distance_km<0''')
    check('snapshots_have_one_label', '''SELECT count(*) FROM eta_snapshot s LEFT JOIN eta_snapshot_label l USING(tenant_id,snapshot_id) WHERE l.snapshot_id IS NULL''')
    check('labels_match_snapshot_promise', '''SELECT count(*) FROM eta_snapshot_label l JOIN eta_snapshot s USING(tenant_id,snapshot_id) WHERE l.promise_version_id<>s.promise_version_id''')
    check('label_cutoff_is_exclusive', '''SELECT count(*) FROM eta_snapshot_label l JOIN eta_snapshot s USING(tenant_id,snapshot_id) JOIN eta_split_assignment sp USING(tenant_id,trip_id) JOIN eta_trip_outcome o ON l.tenant_id=o.tenant_id AND l.outcome_version_id=o.outcome_version_id WHERE o.label_available_time>=sp.label_cutoff_at''')
    check('latest_outcome_before_cutoff', '''SELECT count(*) FROM eta_snapshot_label l JOIN eta_snapshot s USING(tenant_id,snapshot_id) JOIN eta_split_assignment sp USING(tenant_id,trip_id) JOIN eta_trip_outcome o ON l.tenant_id=o.tenant_id AND l.outcome_version_id=o.outcome_version_id WHERE EXISTS(SELECT 1 FROM eta_trip_outcome later WHERE later.tenant_id=s.tenant_id AND later.trip_id=s.trip_id AND later.label_available_time<sp.label_cutoff_at AND later.label_available_time>o.label_available_time)''')
    check('eligible_targets_use_arrival_not_service', '''SELECT count(*) FROM eta_snapshot_label l JOIN eta_snapshot s USING(tenant_id,snapshot_id) JOIN eta_trip_outcome o ON l.tenant_id=o.tenant_id AND l.outcome_version_id=o.outcome_version_id WHERE l.label_eligibility='eligible' AND (o.label_status<>'verified' OR o.arrival_time_precision NOT IN ('second','minute') OR l.remaining_duration_seconds<>(date_diff('microsecond',s.snapshot_time,o.actual_arrival_time) // 1000000) OR l.remaining_duration_seconds<=0 OR l.delay_outcome IS NULL)''')
    check('delay_uses_frozen_promise', '''SELECT count(*) FROM eta_snapshot_label l JOIN eta_promise_version p USING(tenant_id,promise_version_id) JOIN eta_trip_outcome o ON l.tenant_id=o.tenant_id AND l.outcome_version_id=o.outcome_version_id WHERE l.label_eligibility='eligible' AND l.delay_outcome<>(o.actual_arrival_time>p.window_end_at)''')
    check('unknown_not_zero_or_on_time', '''SELECT count(*) FROM eta_snapshot_label WHERE label_eligibility<>'eligible' AND (remaining_duration_seconds IS NOT NULL OR delay_outcome IS NOT NULL)''')
    check('history_training_only_no_self', '''SELECT count(*) FROM eta_history_reference h JOIN eta_snapshot s USING(tenant_id,snapshot_id) JOIN eta_split_assignment sp ON h.tenant_id=sp.tenant_id AND h.historical_trip_id=sp.trip_id WHERE sp.split_role<>'train' OR h.historical_trip_id=s.trip_id OR h.historical_label_known_at>=s.snapshot_time OR h.historical_label_known_at>=TIMESTAMPTZ '2026-07-01T07:00:00Z' ''')
    check('history_independent_departure_duration', '''SELECT count(*) FROM eta_history_reference h JOIN eta_trip_outcome o ON h.tenant_id=o.tenant_id AND h.historical_outcome_version_id=o.outcome_version_id WHERE o.arrival_time_precision NOT IN ('second','minute') OR h.historical_duration_seconds<>(SELECT (date_diff('microsecond',min(e.occurred_at),o.actual_arrival_time) // 1000000) FROM eta_trip_event e WHERE e.tenant_id=h.tenant_id AND e.trip_id=h.historical_trip_id AND e.event_type='departure')''')
    check('provenance_fk_snapshots_labels_history', '''SELECT count(*) FROM (SELECT provenance_id FROM eta_snapshot UNION ALL SELECT provenance_id FROM eta_snapshot_label UNION ALL SELECT provenance_id FROM eta_history_reference) d LEFT JOIN provenance_record p USING(provenance_id) WHERE p.provenance_id IS NULL''')
    check('snapshot_derived_availability', '''SELECT count(*) FROM eta_snapshot s JOIN provenance_record p USING(provenance_id) WHERE p.available_time<>s.snapshot_time OR p.origin_type<>'derived' ''')
    check('source_availability_after_event', '''SELECT count(*) FROM telemetry_event WHERE available_time<event_time''')
    check('split_groups_are_trip_unique', '''SELECT count(*) FROM (SELECT tenant_id,trip_id,count(DISTINCT split_role) n FROM eta_split_assignment GROUP BY tenant_id,trip_id HAVING n<>1)''')
    check('arrival_service_delivery_chronology', '''SELECT count(*) FROM eta_trip_outcome WHERE service_start_time<actual_arrival_time OR delivery_completed_time<service_start_time''')
    # Nested provenance is checked in SQL; only the small snapshot clock index is in Python.
    bad_parent,bad_clock=provenance_violations(con)
    checks.extend([{'name':'all_provenance_parents_exist','violations':bad_parent,'passed':bad_parent==0},
                   {'name':'derived_available_after_all_parents','violations':bad_clock,'passed':bad_clock==0}])
    bad=sum(r['split_role'] in ('train','calibration') and (r['unseen_vehicle'] or r['unseen_route']) for r in iter_jsonl(root/'data/processed/trip_membership.jsonl'))
    checks.append({'name':'heldout_excluded_from_fit_and_calibration','violations':bad,'passed':bad==0})
    snapshot_clocks={(r['tenant_id'],r['snapshot_id']):parse_ts(r['snapshot_time']) for r in iter_jsonl(root/'data/processed/eta_snapshot.jsonl')}
    leaks=0
    for row in iter_jsonl(root/'data/processed/feature_lineage.jsonl'):
        clock=snapshot_clocks.get((row['tenant_id'],row['snapshot_id']))
        if clock is None or parse_ts(row['max_source_available_time'])>clock:leaks+=1
    checks.append({'name':'feature_parent_asof_allowlist','violations':leaks,'passed':leaks==0})
    checks.append({'name':'canonical_row_shapes_and_not_null','rows_checked':shape_count,'violations':0,'passed':True})
    physical={row[0] for row in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main' AND table_type='BASE TABLE'").fetchall()}
    expected=set(required) if include_reserved else set(active)
    missing=len(expected-physical)
    checks.append({'name':'required_canonical_tables_present','expected_count':len(expected),'violations':missing,'passed':missing==0})
    reserved=[t for t in counts if t not in active]
    pending_rows=sum(counts[t] for t in reserved)
    checks.append({'name':'future_stage_tables_explicitly_empty','tables_checked':len(reserved),'violations':pending_rows,'passed':pending_rows==0})
    bad_fields=sum(con.execute('SELECT count(*) FROM information_schema.columns WHERE table_schema=? AND table_name=?',['main',t]).fetchone()[0]!=len(contract[t]) for t in counts)
    checks.append({'name':'required_schema_field_counts','tables_checked':len(counts),'violations':bad_fields,'passed':bad_fields==0})
    report={'engine':'DuckDB','engine_version':duckdb.__version__,'active_canonical_tables':len(active),'counts':counts,
            'checks':checks,'passed':all(c['passed'] for c in checks),'database_path':f'data/{filename}',
            'materialized_canonical_tables':len(counts),'required_table_count':len(required),
            'all_required_schemas_created':set(required).issubset(physical),
            'populated_canonical_tables':sum(count>0 for count in counts.values()),
            'reserved_empty_tables':reserved,'table_status':statuses,
            'parquet_export_count':len(counts),'full_project_28_tables_complete':False,
            'scope_note':'Schema creation is not population or model/alert validation; future-stage tables remain explicitly empty.'}
    con.close()
    (root/'reports/quality_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report
