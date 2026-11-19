"""Executed abrupt-exit evidence for the single-filesystem publication protocol."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import duckdb

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from workflows.p11 import publication


TABLES = ('source_event', 'derived_snapshot', 'reserved_action')
FAULTS = ('after_database', 'after_first_parquet', 'after_validation',
          'after_generation_rename', 'after_pointer_replace')


def fixture_builder(version, fault=None, hold=None):
    def build(stage):
        database = stage / 'warehouse.duckdb'
        con = duckdb.connect(str(database))
        try:
            con.execute("SET TimeZone='UTC'")
            con.execute('CREATE TABLE source_event (tenant_id VARCHAR NOT NULL, event_id VARCHAR NOT NULL, revision INTEGER NOT NULL, amount DECIMAL(12,3), known_at TIMESTAMPTZ, valid BOOLEAN, payload JSON, PRIMARY KEY(tenant_id,event_id,revision))')
            con.execute("INSERT INTO source_event VALUES ('a','e',1,12.345,'2026-01-01T00:00:00.123456Z',true,'{\"x\":1}'),('b','e',1,NULL,'2026-01-01T00:00:00.123457Z',false,'{\"x\":2}')")
            if version == 2:
                con.execute("INSERT INTO source_event VALUES ('a','e',2,15.125,'2026-01-01T00:00:01.123456Z',true,'{\"x\":3}')")
            con.execute('CREATE TABLE derived_snapshot (tenant_id VARCHAR NOT NULL, snapshot_id VARCHAR NOT NULL, source_revision INTEGER NOT NULL, score DOUBLE, PRIMARY KEY(tenant_id,snapshot_id))')
            con.execute("INSERT INTO derived_snapshot VALUES ('a','s',?,?), ('b','s',1,NULL)", [version, version * 0.125])
            con.execute('CREATE TABLE reserved_action (tenant_id VARCHAR NOT NULL, action_id VARCHAR NOT NULL, PRIMARY KEY(tenant_id,action_id))')
            con.execute('CHECKPOINT')
        finally:
            con.close()
        if fault:
            fault('after_database')
        (stage / 'parquet').mkdir()
        con = duckdb.connect(str(database), read_only=True)
        try:
            for index, name in enumerate(TABLES):
                con.execute(f'COPY "{name}" TO ? (FORMAT PARQUET)', [str(stage/'parquet'/(name+'.parquet'))])
                if index == 0 and fault:
                    fault('after_first_parquet')
        finally:
            con.close()
        if hold:
            Path(hold).write_text('ready')
            while True:
                time.sleep(0.05)
        return {'fixture_version': version}
    return build


def file_hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()}


def child_command(root, expected, mode, marker=''):
    return [sys.executable, '-B', str(Path(__file__).resolve()), '--worker',
            str(root), expected, mode, str(marker)]


def verify_recovery(output):
    """Retain five abrupt-exit cases, old versions, recovery and clean parity."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    results = []
    for point in FAULTS:
        store = output / point
        baseline = publication.publish(store, fixture_builder(1), expected_tables=TABLES)
        old = store/'generations'/baseline['generation_id']
        prior_hashes = file_hashes(old)
        process = subprocess.run(child_command(store, baseline['generation_id'], point),
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=45)
        (output/(point+'.log')).write_text(process.stdout)
        if process.returncode != 86:
            raise ValueError('The fault process did not exit at its requested point: '+point+' '+process.stdout)
        observed = publication.read_current(store, expected_tables=TABLES, verify_logical=True)
        expected_version = 2 if point == 'after_pointer_replace' else 1
        if observed['metadata']['fixture_version'] != expected_version or file_hashes(old) != prior_hashes:
            raise ValueError('Interrupted publication changed or mixed the prior complete generation')
        before = file_hashes(store)
        inspection = publication.recover(store)
        if file_hashes(store) != before or inspection['automatic_promotion']:
            raise ValueError('Recovery changed or automatically promoted retained evidence')
        recovered = publication.publish(store, fixture_builder(2), expected_tables=TABLES,
                                        expected_current=observed['generation_id'])
        clean = publication.publish(output/(point+'-clean'), fixture_builder(2), expected_tables=TABLES)
        if recovered['tables'] != clean['tables']:
            raise ValueError('Recovered build differs from clean independent build')
        results.append({'fault': point, 'process_exit': process.returncode,
                        'active_version_after_exit': expected_version,
                        'old_database_and_all_parquet_preserved': True,
                        'recovery_mutated_artifacts': False,
                        'clean_and_recovered_logical_tables_equal': True,
                        'recovery': inspection})
    receipt = {'status': 'passed_abrupt_failure_and_recovery', 'faults_executed': len(results),
               'database_and_parquet_executed': True, 'subprocess_abrupt_exit': True,
               'cases': results, 'scope': 'Real small typed DuckDB/Parquet fixture; single-filesystem generation protocol, not PostgreSQL distributed commit or power-loss emulation.'}
    (output/'recovery.json').write_text(json.dumps(receipt, sort_keys=True, indent=2)+'\n')
    return receipt



if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--worker':
        root,expected,mode,marker=sys.argv[2:]
        def abrupt(point):
            if point==mode:
                os._exit(86)
        publication.publish(Path(root),fixture_builder(2,abrupt,marker if mode=='hold' else None),
                            expected_tables=TABLES,expected_current=expected,fault=abrupt)
        raise SystemExit('Requested fault was not reached')
    raise SystemExit("Use verify_recovery(output) or the P11 CLI")
