"""Count SQL non-NULL values for the M2 field inventory in a verified P11 generation."""
import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
import sys

import duckdb


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from ds_m01.common import digest_file
from workflows.p11.publication import read_current
from workflows.p11.rebuild import compare_runs, require


BASELINE = ROOT / 'workflows/p11/evidence/rebuild.json'
BASELINE_SHA256 = '4cdf920a965ecb11af9a945c3760ba290cc07916c30da6952dfae1560197ebbe'
SUPPLEMENTAL = {'DS-M01-F006', 'DS-M01-P009'}


def profile(store, worker_result):
    require(digest_file(BASELINE) == BASELINE_SHA256, 'Published P11 reference receipt differs')
    baseline = json.loads(BASELINE.read_text())
    worker = json.loads(worker_result.read_text())
    require(worker['status'] == 'passed_full_frozen_m2_rebuild', 'Expected a completed full rebuild')
    require(len(baseline['runs']) == 2, 'Expected both published P11 reference runs')
    config_digest = digest_file(ROOT / 'config/m2.json')
    for reference in baseline['runs']:
        require(worker['config_sha256'] == reference['config_sha256'] == config_digest,
                'Rebuild was configured differently from the frozen M2 reference')
        compare_runs(reference, worker)
    # read_current pins the manifest and checks every stored artifact's size and
    # complete SHA-256 before any SQL query. Keep reading this same generation.
    manifest = read_current(store, expected_tables=worker['tables'])
    require(manifest is not None and manifest['generation_id'] == worker['generation_id'],
            'Active store and worker receipt identify different generations')
    generation = store / 'generations' / manifest['generation_id']
    for name, expected in worker['tables'].items():
        require(all(manifest['tables'][name][key] == value for key, value in expected.items()),
                'Generation logical contents differ from P11 reference: ' + name)

    with (ROOT / 'contracts/current_requirement_evidence.csv').open(newline='') as stream:
        selected = [r for r in csv.DictReader(stream)
                    if r['evidence_status'] == 'synthetic_data' or r['requirement_id'] in SUPPLEMENTAL]
    identities = {r['requirement_id'] for r in selected}
    require(len(identities) == len(selected) == 62 and SUPPLEMENTAL <= identities,
            'Expected 60 M2 synthetic-data fields and two supplemental provenance fields')
    by_table = defaultdict(list)
    for row in selected:
        by_table[row['table']].append(row)
    fields = {}
    with duckdb.connect() as db:
        for name, requirements in sorted(by_table.items()):
            table = worker['tables'][name]
            columns = sorted({r['column'] for r in requirements})
            require(set(columns) <= {c['name'] for c in table['columns']}, 'Missing field in ' + name)
            expressions = ['count("' + col.replace('"', '""') + '")' for col in columns]
            sql = 'SELECT count(*), ' + ', '.join(expressions) + ' FROM read_parquet(?)'
            count, *values = db.execute(sql, [str(generation / 'parquet' / (name + '.parquet'))]).fetchone()
            require(count == table['row_count'], 'Parquet row count differs for ' + name)
            counts = dict(zip(columns, values, strict=True))
            for r in requirements:
                non_null = counts[r['column']]
                fields[r['requirement_id']] = {
                    'table': name, 'column': r['column'], 'row_count': count,
                    'non_null_count': non_null, 'null_count': count - non_null,
                    'table_logical_sha256': table['logical_sha256'],
                }

    return {
        'status': 'measured_m2_field_presence', 'protocol_version': 1,
        'scope': 'SQL non-NULL counts in synthetic typed M2 columns. Counts include false and zero; field presence alone does not validate conditional or role-specific semantics.',
        'baseline_rebuild_sha256': BASELINE_SHA256,
        'worker_result_sha256': digest_file(worker_result),
        'generation_id': manifest['generation_id'],
        'generation_manifest_sha256': digest_file(generation / 'manifest.json'),
        'generation_artifacts_sha256': {p: a['sha256'] for p, a in manifest['artifacts'].items()},
        'all_28_tables_matched_both_published_rebuilds': True,
        'profiled_requirements': len(fields), 'profiled_relations': len(by_table),
        'fields': fields,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--worker-result', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = profile(args.store.resolve(), args.worker_result.resolve())
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n')


if __name__ == '__main__':
    main()
