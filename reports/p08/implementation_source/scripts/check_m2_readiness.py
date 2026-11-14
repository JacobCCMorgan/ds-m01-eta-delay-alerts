"""Audit retained M2 data and report M3 support without fitting or tuning a model.

Read-only warehouse access; scenario truth is read only for engineering coverage.
Sparse-cohort warnings are descriptive, not statistical power guarantees.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import duckdb

FEATURE_ALLOWLIST = {
    'latitude_deg', 'longitude_deg', 'position_age_seconds', 'gnss_speed_kmh',
    'remaining_distance_km', 'departure_time_bucket', 'elapsed_trip_seconds',
    'position_validity',
}
FEATURE_PARENT_TABLES = {
    'eta_trip_plan_version', 'eta_promise_version', 'eta_route_version',
    'eta_trip_event', 'core_vehicle_device_assignment', 'eta_facility_version',
    'telemetry_event',
}
ROLES = ('train', 'calibration', 'test', 'excluded')


def _sql_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def _rows(con, sql, params=None):
    cursor = con.execute(sql, params or [])
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _json_rows(path):
    with Path(path).open() as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def _canonical(row):
    return json.dumps(row, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def check_continuity(m1_root, m2_root):
    """Compare actual row values, not counts; receipt provenance is intentionally excluded."""
    m1_root, m2_root = Path(m1_root), Path(m2_root)
    files = sorted((m1_root / 'data/raw').glob('*.jsonl'))
    files = [p for p in files if p.name != 'provenance_record.jsonl']
    for name in ('eta_snapshot.jsonl', 'eta_snapshot_label.jsonl'):
        p = m1_root / 'data/processed' / name
        if p.exists():
            files.append(p)
    comparisons = []
    for path in files:
        relative = path.relative_to(m1_root)
        old_rows = list(_json_rows(path))
        old_counter = Counter(_canonical(row) for row in old_rows)
        remaining = old_counter.copy()
        target = m2_root / relative
        if target.exists():
            for row in _json_rows(target):
                key = _canonical(row)
                if remaining[key] > 0:
                    remaining[key] -= 1
        missing = sum(n for n in remaining.values() if n > 0)
        comparisons.append({
            'path': str(relative), 'm1_row_count': len(old_rows),
            'matched_m1_rows': len(old_rows) - missing,
            'missing_or_changed_m1_rows': missing, 'passed': missing == 0,
        })
    return {
        'status': 'passed' if comparisons and all(x['passed'] for x in comparisons) else 'failed',
        'scope': 'Exact M1 business raw rows and snapshot/label rows must be contained in M2.',
        'excluded_from_comparison': [
            'provenance_record: new acquisition receipt and M2 dataset/version metadata',
            'eta_split_assignment.frozen_at: new actual execution receipt',
            'eta_history_reference: deliberately expanded eligible training pool',
        ],
        'files': comparisons,
    }


def support_assessment(role_counts, cohort_counts, minimum_eligible_trips=30):
    """A transparent diagnostic floor; never represent this as measured model performance."""
    blockers, cautions = [], []
    by_role = {row['split_role']: row for row in role_counts}
    for role in ('train', 'calibration', 'test'):
        row = by_role.get(role, {})
        if row.get('eligible_trips', 0) == 0:
            blockers.append(f'{role}: no eligible target trips')
        elif row['eligible_trips'] < minimum_eligible_trips:
            cautions.append(f'{role}: only {row["eligible_trips"]} eligible trips; below descriptive floor {minimum_eligible_trips}')
        if row.get('delayed_eligible_snapshots', 0) == 0 or row.get('on_time_eligible_snapshots', 0) == 0:
            blockers.append(f'{role}: both delay classes are not present among eligible snapshots')
    for row in cohort_counts:
        if row['eligible_trips'] < minimum_eligible_trips:
            cautions.append(f'test/{row["cohort"]}: {row["eligible_trips"]} eligible trips; below descriptive floor {minimum_eligible_trips}')
        if row['delayed_eligible_snapshots'] == 0 or row['on_time_eligible_snapshots'] == 0:
            cautions.append(f'test/{row["cohort"]}: both delay classes are not represented')
    return {
        'eligible_trip_diagnostic_floor': minimum_eligible_trips,
        'threshold_meaning': 'Descriptive sample-support flag only; not a statistical power calculation or deployment acceptance threshold.',
        'blockers': blockers, 'cautions': cautions,
        'ready_to_attempt_m3': not blockers,
        'constraints': [
            'Use trip-grouped temporal validation; snapshots within a trip are dependent.',
            'Do not tune features, models, calibration or thresholds using test-role outcomes.',
            'This audit examines coverage and validity only; no model accuracy, calibration or operational benefit is claimed.',
            'Synthetic scenario frequencies are engineering test design, not real fleet incidence.',
        ],
    }


def audit(root, config_path=None, m1_root=None, minimum_eligible_trips=30):
    root = Path(root).resolve()
    config_path = Path(config_path) if config_path else root / 'config/m2.json'
    config = json.loads(config_path.read_text())
    quality_path = root / 'reports/quality_report.json'
    quality = json.loads(quality_path.read_text())
    database = root / quality.get('database_path', 'data/' + config.get('database_filename', 'ds_m01_m2.duckdb'))
    started = time.perf_counter()
    con = duckdb.connect(str(database), read_only=True)
    con.execute("SET TimeZone='UTC'")
    membership = root / 'data/processed/trip_membership.jsonl'
    lineage_path = root / 'data/processed/feature_lineage.jsonl'
    con.execute('CREATE TEMP VIEW audit_membership AS SELECT * FROM read_json_auto(' + _sql_string(membership) + ')')
    con.execute('''CREATE TEMP VIEW audit_trip AS
        SELECT t.tenant_id, t.trip_id, sp.split_role, sp.label_cutoff_at,
               m.vehicle_id, m.route_id, m.unseen_vehicle, m.unseen_route,
               m.exclusion_reason, d.departure_time,
               CASE WHEN m.unseen_vehicle AND m.unseen_route THEN 'unseen_vehicle_and_route'
                    WHEN m.unseen_vehicle THEN 'unseen_vehicle_only'
                    WHEN m.unseen_route THEN 'unseen_route_only' ELSE 'seen_vehicle_and_route' END AS cohort
        FROM eta_trip t
        JOIN eta_split_assignment sp USING (tenant_id, trip_id)
        JOIN audit_membership m USING (tenant_id, trip_id)
        JOIN (SELECT tenant_id, trip_id, min(occurred_at) AS departure_time
              FROM eta_trip_event WHERE event_type IN ('departure','departed')
              GROUP BY tenant_id, trip_id) d USING (tenant_id, trip_id)''')
    con.execute('''CREATE TEMP VIEW audit_snapshot AS
        SELECT s.tenant_id, s.trip_id, s.snapshot_id, s.snapshot_time,
               s.position_validity, s.remaining_distance_km,
               t.split_role, t.cohort, t.label_cutoff_at,
               l.label_eligibility, l.remaining_duration_seconds, l.delay_outcome,
               l.outcome_version_id
        FROM eta_snapshot s JOIN audit_trip t USING (tenant_id, trip_id)
        JOIN eta_snapshot_label l USING (tenant_id, snapshot_id)''')

    summary_sql = '''count(*) AS snapshots,
        count(DISTINCT tenant_id || '/' || trip_id) AS trips_with_snapshots,
        count(*) FILTER (WHERE label_eligibility='eligible') AS eligible_snapshots,
        count(DISTINCT tenant_id || '/' || trip_id) FILTER (WHERE label_eligibility='eligible') AS eligible_trips,
        count(*) FILTER (WHERE label_eligibility='eligible' AND delay_outcome) AS delayed_eligible_snapshots,
        count(*) FILTER (WHERE label_eligibility='eligible' AND NOT delay_outcome) AS on_time_eligible_snapshots'''
    roles = _rows(con, '''SELECT t.split_role, count(*) AS trips,
        coalesce(s.snapshots,0) AS snapshots,
        coalesce(s.eligible_snapshots,0) AS eligible_snapshots,
        coalesce(s.eligible_trips,0) AS eligible_trips,
        coalesce(s.delayed_eligible_snapshots,0) AS delayed_eligible_snapshots,
        coalesce(s.on_time_eligible_snapshots,0) AS on_time_eligible_snapshots
        FROM audit_trip t LEFT JOIN (SELECT split_role, ''' + summary_sql + '''
        FROM audit_snapshot GROUP BY split_role) s USING (split_role)
        GROUP BY ALL ORDER BY t.split_role''')
    cohort_rows = _rows(con, '''SELECT t.cohort, count(*) AS trips,
        coalesce(s.snapshots,0) AS snapshots,
        coalesce(s.eligible_snapshots,0) AS eligible_snapshots,
        coalesce(s.eligible_trips,0) AS eligible_trips,
        coalesce(s.delayed_eligible_snapshots,0) AS delayed_eligible_snapshots,
        coalesce(s.on_time_eligible_snapshots,0) AS on_time_eligible_snapshots
        FROM audit_trip t LEFT JOIN (SELECT cohort, ''' + summary_sql + '''
        FROM audit_snapshot WHERE split_role='test' GROUP BY cohort) s USING (cohort)
        WHERE t.split_role='test' GROUP BY ALL ORDER BY t.cohort''')
    existing_cohorts = {r['cohort'] for r in cohort_rows}
    for cohort in ('seen_vehicle_and_route', 'unseen_vehicle_only', 'unseen_route_only', 'unseen_vehicle_and_route'):
        if cohort not in existing_cohorts:
            cohort_rows.append(dict(cohort=cohort, trips=0, snapshots=0, eligible_snapshots=0,
                                    eligible_trips=0, delayed_eligible_snapshots=0, on_time_eligible_snapshots=0))
    cohort_rows.sort(key=lambda r: r['cohort'])
    tz = _sql_string(config.get('timezone', 'America/Los_Angeles'))
    months = _rows(con, "SELECT strftime(timezone(" + tz + ", departure_time), '%Y-%m') AS month, count(*) AS trips FROM audit_trip GROUP BY month ORDER BY month")
    maturity = _rows(con, 'SELECT split_role, label_eligibility, count(*) AS snapshots, count(DISTINCT tenant_id || \'/\' || trip_id) AS trips FROM audit_snapshot GROUP BY split_role,label_eligibility ORDER BY split_role,label_eligibility')
    positions = _rows(con, 'SELECT split_role, position_validity, count(*) AS snapshots, count(*) FILTER (WHERE label_eligibility=\'eligible\') AS eligible_snapshots FROM audit_snapshot GROUP BY split_role,position_validity ORDER BY split_role,position_validity')
    tenant_role = _rows(con, 'SELECT tenant_id, split_role, count(*) AS trips, count(DISTINCT vehicle_id) AS vehicles, count(DISTINCT route_id) AS routes FROM audit_trip GROUP BY tenant_id,split_role ORDER BY tenant_id,split_role')
    heldout_ids = {
        'vehicles': _rows(con, 'SELECT vehicle_id, split_role, count(*) AS trips FROM audit_trip WHERE unseen_vehicle GROUP BY vehicle_id,split_role ORDER BY vehicle_id,split_role'),
        'routes': _rows(con, 'SELECT route_id, split_role, count(*) AS trips FROM audit_trip WHERE unseen_route GROUP BY route_id,split_role ORDER BY route_id,split_role'),
    }
    con.execute('''CREATE TEMP VIEW audit_history_support AS
        SELECT s.tenant_id, s.trip_id, s.snapshot_id, s.split_role, s.label_eligibility,
            count(h.historical_trip_id) AS historical_training_trips
        FROM audit_snapshot s LEFT JOIN eta_history_reference h USING (tenant_id,snapshot_id)
        GROUP BY s.tenant_id,s.trip_id,s.snapshot_id,s.split_role,s.label_eligibility''')
    history = _rows(con, '''SELECT split_role, count(*) AS snapshots,
        count(*) FILTER (WHERE historical_training_trips=0) AS no_history_snapshots,
        count(*) FILTER (WHERE label_eligibility='eligible' AND historical_training_trips=0) AS eligible_no_history_snapshots,
        min(historical_training_trips) AS minimum_training_trips,
        median(historical_training_trips) AS median_training_trips,
        max(historical_training_trips) AS maximum_training_trips
        FROM audit_history_support GROUP BY split_role ORDER BY split_role''')
    target_ranges = _rows(con, '''SELECT split_role,
        min(remaining_duration_seconds) AS minimum_remaining_seconds,
        median(remaining_duration_seconds) AS median_remaining_seconds,
        max(remaining_duration_seconds) AS maximum_remaining_seconds
        FROM audit_snapshot WHERE label_eligibility='eligible' GROUP BY split_role ORDER BY split_role''')

    checks = []
    def check(name, violations, detail=None):
        row = {'name': name, 'violations': int(violations), 'passed': violations == 0}
        if detail is not None:
            row['detail'] = detail
        checks.append(row)

    def sql_check(name, sql, params=None):
        check(name, con.execute(sql, params or []).fetchone()[0])

    expected_trips = config.get('scale', {}).get('main_trips', config.get('sample_size', 1800))
    total_trips = con.execute('SELECT count(*) FROM eta_trip').fetchone()[0]
    check('full_design_retained', abs(total_trips - expected_trips), {'expected': expected_trips, 'actual': total_trips})
    check('warehouse_quality_passed', 0 if quality.get('passed') else 1)
    sql_check('complete_trip_membership_and_departure', 'SELECT abs((SELECT count(*) FROM eta_trip)-(SELECT count(*) FROM audit_trip))')
    sql_check('complete_snapshot_label_membership', 'SELECT abs((SELECT count(*) FROM eta_snapshot)-(SELECT count(*) FROM audit_snapshot))')
    sql_check('heldouts_absent_from_train_and_calibration', "SELECT count(*) FROM audit_trip WHERE split_role IN ('train','calibration') AND (unseen_vehicle OR unseen_route)")
    sql_check('history_uses_seen_training_only', "SELECT count(*) FROM eta_history_reference h LEFT JOIN audit_trip t ON h.tenant_id=t.tenant_id AND h.historical_trip_id=t.trip_id WHERE t.trip_id IS NULL OR t.split_role<>'train' OR t.unseen_vehicle OR t.unseen_route")
    sql_check('history_before_snapshot_and_training_cutoff', '''SELECT count(*) FROM eta_history_reference h JOIN eta_snapshot s USING (tenant_id,snapshot_id)
        WHERE h.historical_label_known_at>=s.snapshot_time OR h.historical_label_known_at>=?::TIMESTAMPTZ''', [config['split']['train_label_cutoff_exclusive']])
    sql_check('eligible_targets_positive_and_binary', "SELECT count(*) FROM audit_snapshot WHERE label_eligibility='eligible' AND (remaining_duration_seconds IS NULL OR remaining_duration_seconds<=0 OR delay_outcome IS NULL)")
    sql_check('unavailable_targets_remain_null', "SELECT count(*) FROM audit_snapshot WHERE label_eligibility<>'eligible' AND (remaining_duration_seconds IS NOT NULL OR delay_outcome IS NOT NULL)")
    sql_check('selected_labels_available_before_role_cutoff', '''SELECT count(*) FROM audit_snapshot s JOIN eta_trip_outcome o ON s.tenant_id=o.tenant_id AND s.outcome_version_id=o.outcome_version_id
        WHERE o.label_available_time>=s.label_cutoff_at''')
    sql_check('source_available_before_extract', 'SELECT count(*) FROM telemetry_event WHERE available_time>=?::TIMESTAMPTZ', [config['extract_as_of']])

    # This inspects recorded feature lineage, not hidden simulation trajectories.
    lineage_count = 0
    bad_feature_sets = 0
    lineage_keys = Counter()
    snapshot_clocks = {(t, sid): clock for t, sid, clock in con.execute('SELECT tenant_id,snapshot_id,snapshot_time FROM eta_snapshot').fetchall()}
    future_or_unknown_lineage = 0
    for record in _json_rows(lineage_path):
        lineage_count += 1
        key = (record.get('tenant_id'), record.get('snapshot_id'))
        lineage_keys[key] += 1
        known_clock = datetime.fromisoformat(record['max_source_available_time'].replace('Z', '+00:00'))
        if key not in snapshot_clocks or known_clock > snapshot_clocks[key]:
            future_or_unknown_lineage += 1
        if set(record.get('feature_columns', [])) != FEATURE_ALLOWLIST:
            bad_feature_sets += 1
    check('feature_columns_match_explicit_allowlist', bad_feature_sets)
    check('one_lineage_record_per_snapshot_count', abs(lineage_count - len(snapshot_clocks)))
    check('lineage_identity_unique_and_complete', sum(n - 1 for n in lineage_keys.values() if n > 1) + len(set(snapshot_clocks) ^ set(lineage_keys)))
    check('lineage_max_availability_not_future', future_or_unknown_lineage)
    parent_tables = _rows(con, '''SELECT parent.canonical_table AS parent_table, count(*) AS parent_links
        FROM eta_snapshot s JOIN provenance_record p ON s.provenance_id=p.provenance_id,
        LATERAL json_each(p.parent_provenance_ids) j
        LEFT JOIN provenance_record parent ON parent.provenance_id=json_extract_string(j.value, '$')
        GROUP BY parent.canonical_table ORDER BY parent.canonical_table''')
    bad_parent_links = sum(row['parent_links'] for row in parent_tables if row['parent_table'] not in FEATURE_PARENT_TABLES)
    check('snapshot_parents_exclude_outcomes_and_simulation_truth', bad_parent_links)
    sql_check('snapshot_lineage_has_no_validation_file', '''SELECT count(*) FROM eta_snapshot s JOIN provenance_record p ON s.provenance_id=p.provenance_id,
        LATERAL json_each(p.parent_provenance_ids) j
        JOIN provenance_record parent ON parent.provenance_id=json_extract_string(j.value,'$')
        WHERE parent.raw_record_reference LIKE 'validation/%' ''')

    registry_path = root / 'validation/scenario_registry.json'
    registry = json.loads(registry_path.read_text())
    scenario_counts = registry.get('scenario_counts', {})
    check('scenario_registry_validation_only', 0 if registry.get('validation_only') is True else 1)
    check('scenario_counts_match_retained_trips', abs(sum(scenario_counts.values()) - total_trips))
    expected_months = []
    year, month = map(int, config['period']['main_start'][:7].split('-'))
    end = config['period']['main_end_inclusive'][:7]
    while f'{year:04d}-{month:02d}' <= end:
        expected_months.append(f'{year:04d}-{month:02d}')
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    check('all_configured_months_present', len(set(expected_months) ^ {r['month'] for r in months}))
    planned_per_month = registry.get('full_design_trips_per_month')
    if planned_per_month:
        check('configured_month_density_retained', sum(abs(r['trips'] - planned_per_month) for r in months), {'planned_trips_per_month': planned_per_month})

    continuity = check_continuity(m1_root, root) if m1_root else {'status': 'not_requested'}
    if m1_root:
        check('m1_business_rows_preserved', sum(x['missing_or_changed_m1_rows'] for x in continuity['files']))
    support = support_assessment(roles, cohort_rows, minimum_eligible_trips)
    for row in history:
        if row['eligible_no_history_snapshots']:
            support['cautions'].append(f'{row["split_role"]}: {row["eligible_no_history_snapshots"]} eligible snapshots have no historical training pool; define an explicit unsupported/fallback policy in M3')
    technical_passed = all(row['passed'] for row in checks)
    status = 'failed' if not technical_passed else ('blocked_for_m3' if support['blockers'] else 'ready_for_m3_with_cautions' if support['cautions'] else 'ready_for_m3')
    postgres_path = root / 'reports/postgres_check.json'
    postgres = json.loads(postgres_path.read_text()) if postgres_path.exists() else {'status': 'not_run'}
    report = {
        'project_id': 'DS-M01', 'milestone': 'M2', 'status': status,
        'technical_checks_passed': technical_passed,
        'ready_to_attempt_m3': technical_passed and support['ready_to_attempt_m3'],
        'source_kind': 'original_synthetic_2026_scenarios',
        'model_trained': False, 'metrics_are_model_performance': False,
        'test_use': 'Schema, validity and coverage audit only. No model or threshold is selected from test outcomes.',
        'database_path': str(database.relative_to(root)),
        'config_path': str(config_path.resolve().relative_to(root)),
        'input_hashes': {'config_sha256': _sha(config_path), 'quality_report_sha256': _sha(quality_path),
                         'membership_sha256': _sha(membership), 'feature_lineage_sha256': _sha(lineage_path),
                         'scenario_registry_sha256': _sha(registry_path)},
        'counts': quality.get('counts', {}), 'trips_by_role': roles,
        'trips_by_month': months, 'label_maturity_by_role': maturity,
        'test_cohorts': cohort_rows, 'position_quality_by_role': positions,
        'tenant_role_coverage': tenant_role, 'heldout_id_coverage': heldout_ids,
        'history_support_by_role': history, 'eligible_target_ranges_by_role': target_ranges,
        'synthetic_scenario_coverage': {
            'scope': 'Engineering scenario inventory only; never a predictor or real-world prevalence estimate.',
            'validation_only': registry.get('validation_only'), 'scenario_counts': scenario_counts,
        },
        'feature_separation': {'feature_allowlist': sorted(FEATURE_ALLOWLIST),
                               'allowed_parent_tables': sorted(FEATURE_PARENT_TABLES),
                               'observed_parent_tables': parent_tables,
                               'evidence_scope': 'Persisted feature column and direct provenance-parent audit; history may use strictly prior training outcomes.'},
        'checks': checks, 'm1_continuity': continuity, 'm3_support': support,
        'postgres_service_status': postgres.get('status', postgres.get('result', 'not_run')),
        'finished_at_utc': datetime.now(timezone.utc).isoformat(),
        'elapsed_seconds': round(time.perf_counter() - started, 3),
    }
    con.close()
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=Path('.'))
    p.add_argument('--config', type=Path)
    p.add_argument('--m1-root', type=Path)
    p.add_argument('--minimum-eligible-trips', type=int, default=30)
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    report = audit(args.root, args.config, args.m1_root, args.minimum_eligible_trips)
    output = args.output or args.root / 'reports/m2_readiness.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: report[k] for k in ['status', 'technical_checks_passed', 'ready_to_attempt_m3', 'trips_by_role', 'test_cohorts', 'm3_support', 'elapsed_seconds']}, ensure_ascii=False, indent=2))
    if not report['technical_checks_passed'] or not report['ready_to_attempt_m3']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
