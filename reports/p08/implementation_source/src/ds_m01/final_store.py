"""Fresh typed final-scenario evidence, reconstructed without outcome targets.

The stable canonical field contracts are shared with earlier phases. New P08
audits identify the final population and retain its complete opportunity set;
historical P06/P07 data and their population guards are never rewritten.
"""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_EVEN
import json
from pathlib import Path
import tempfile

import duckdb

from .common import digest_file, ident, parse_ts, read_jsonl, utc, write_jsonl
from . import release_store as base
from . import delivery_store as transport
from .final_policy import digest, policy_opportunities, run_policy_delivery, validate_settings


VERSION = 'p08-final-scenario-store-v1'
CANONICAL = transport.CANONICAL
RESERVED = transport.RESERVED
PROMOTED = transport.PROMOTED
ORIGIN, CLOCK = transport.ORIGIN, transport.CLOCK


def _name(name):
    if name.startswith('p06_'):
        return 'p08_' + name[4:]
    if name.startswith('p07_'):
        return 'p08_delivery_' + name[4:]
    return name


AUX = {_name(name): deepcopy(columns) for name, columns in base.AUX.items()}
AUX.update({_name(name): deepcopy(columns) for name, columns in transport.AUX.items()
            if name != 'p07_run_audit'})
AUX.update({
    'p08_run_audit': [('scenario_id', 'VARCHAR', False), ('store_version', 'VARCHAR', False),
        ('protocol_version', 'VARCHAR', False), ('settings_sha256', 'VARCHAR', False),
        ('metadata_sha256', 'VARCHAR', False), ('registry', 'JSON', False),
        ('bundle_sha256', 'VARCHAR', False), ('bundle_header', 'JSON', False),
        ('policy_summary', 'JSON', False), ('delivery_summary', 'JSON', False),
        ('selection_manifest', 'JSON', False), ('counts_as_of', 'TIMESTAMPTZ', False),
        ('materialized_at', 'TIMESTAMPTZ', False), ('population_role', 'VARCHAR', False),
        ('quality_accepted', 'BOOLEAN', False), ('operational_approval', 'BOOLEAN', False),
        ('target_rows_read', 'BOOLEAN', False)],
    'p08_metadata_audit': [('tenant_id', 'VARCHAR', False), ('snapshot_id', 'VARCHAR', False),
        ('scenario_id', 'VARCHAR', False), ('opportunity', 'JSON', False)],
})
# The event ordinal preserves causal same-clock service/receipt ordering.
AUX['p08_delivery_event_audit'].append(('event_order', 'INTEGER', False))
ORDER = tuple(_name(name) for name in transport.PRESERVED) + (
    'p08_run_audit', 'p08_metadata_audit', 'p08_delivery_provenance_reference', *PROMOTED,
    'p08_delivery_episode_audit', 'p08_delivery_alert_audit',
    'p08_delivery_attempt_audit', 'p08_delivery_event_audit')
KEYS = {_name(name): fields for name, fields in transport.KEYS.items()}
KEYS.update(p08_run_audit=('scenario_id',), p08_metadata_audit=('tenant_id', 'snapshot_id'))
FKS = {_name(name): [(fields, _name(parent), refs) for fields, parent, refs in edges]
       for name, edges in transport.FKS.items()}
FKS['p08_metadata_audit'] = [(('tenant_id', 'snapshot_id'), 'p08_source_snapshot', ('tenant_id', 'snapshot_id'))]


def _projection_settings(settings):
    """Use the existing pure projector with its original, unchanged rule config."""
    path = Path(__file__).resolve().parents[2] / 'config/p06.json'
    if digest_file(path) != settings['artifact_sha256']['config/p06.json']:
        raise ValueError('Pinned policy projection settings changed')
    return json.loads(path.read_text())


def _schema(root, settings):
    validate_settings(settings)
    path = Path(root) / 'contracts/fields.json'
    if digest_file(path) != settings['artifact_sha256']['contracts/fields.json']:
        raise ValueError('Canonical final field contract changed')
    schema = {name: [] for name in CANONICAL + RESERVED}
    for field in json.loads(path.read_text()):
        if field['table'] in schema:
            schema[field['table']].append((field['name'], field['type'], field['nullable']))
    schema.update(deepcopy(AUX))
    if set(schema) != set(ORDER) or any(not columns for columns in schema.values()):
        raise ValueError('Final store field inventory differs')
    return schema


def _registry(registry):
    rows = deepcopy(list(registry))
    fields = set(base.MODEL_FIELDS) | {'tenant_id', 'source_bindings'}
    for row in rows:
        if set(row) != fields or not isinstance(row['source_bindings'], dict) or not row['source_bindings']:
            raise ValueError('Complete target-free model registry fields are required')
        if row['source_bindings'].get('tenant_id') != row['tenant_id']:
            raise ValueError('Model source manifest crosses tenant ownership')
        try:
            coverage = Decimal(str(row['nominal_interval_coverage']))
        except Exception as error:
            raise ValueError('Invalid nominal interval coverage') from error
        if not coverage.is_finite() or coverage != Decimal('0.8'):
            raise ValueError('Frozen nominal interval coverage must remain 0.8')
        if (parse_ts(row['training_cutoff_at']) != parse_ts('2026-07-01T07:00:00Z')
                or parse_ts(row['calibration_cutoff_at']) != parse_ts('2026-07-15T07:00:00Z')):
            raise ValueError('Frozen model/calibration knowledge cutoffs changed')
    return sorted(rows, key=lambda r: (r['tenant_id'], r['prediction_version']))


def build_tables(opportunities, registry, bundle, generated_at, settings):
    """Build one complete final-scenario store from label-free evidence only."""
    validate_settings(settings)
    metadata = deepcopy(list(opportunities))
    base._index(metadata, ('tenant_id', 'snapshot_id'))
    metadata.sort(key=lambda r: (r['tenant_id'], r['snapshot_id']))
    scenario = bundle.get('scenario_id')
    if bundle != run_policy_delivery(metadata, settings, scenario):
        raise ValueError('Final policy/delivery bundle differs from frozen label-free replay')
    return _build(metadata, _registry(registry), bundle, utc(generated_at), settings)


def _build(metadata, registry, bundle, generated_at, settings):
    rows = policy_opportunities(metadata)
    cutoff = bundle['delivery']['summary']['counts_as_of']
    if parse_ts(generated_at) < parse_ts(cutoff):
        raise ValueError('Actual final materialization precedes scenario observation cutoff')
    selection_manifest = {
        'scope': 'Frozen P06 selection applied to final test without reselection',
        'source_reference': 'reports/p06/selection_manifest.json',
        'source_sha256': settings['artifact_sha256']['reports/p06/selection_manifest.json'],
        'selected_rule': deepcopy(bundle['rule']), 'threshold_reselected': False,
        'quality_accepted': False, 'operational_approval': False,
    }
    selection = dict(rule=bundle['rule'], status='frozen_final_test_no_reselection',
                     selection_manifest=selection_manifest, simulation=bundle['policy'])
    projected = base.build_tables(rows, selection, registry, generated_at, _projection_settings(settings))
    tables = {name: [] for name in ORDER}
    for name, values in projected.items():
        tables[_name(name)] = values
    for row in tables['eta_evaluation_membership']:
        for item in row['evaluation_slice']:
            if item['dimension'] == 'period':
                item['value'] = 'final_test'
    for row in tables['p08_provenance_reference']:
        if row['source_reference'].startswith('p06_model_binding#'):
            row['source_reference'] = row['source_reference'].replace('p06_model_binding#', 'p08_model_binding#', 1)
    tables['p08_metadata_audit'] = [dict(tenant_id=row['tenant_id'], snapshot_id=row['snapshot_id'],
        scenario_id=bundle['scenario_id'], opportunity=row) for row in metadata]
    tables['p08_run_audit'] = [dict(scenario_id=bundle['scenario_id'], store_version=VERSION,
        protocol_version=settings['protocol_version'], settings_sha256=digest(settings),
        metadata_sha256=digest(metadata), registry=registry, bundle_sha256=digest(bundle),
        bundle_header={k: deepcopy(v) for k, v in bundle.items() if k not in ('policy', 'delivery')},
        policy_summary=deepcopy(bundle['policy']['summary']), delivery_summary=deepcopy(bundle['delivery']['summary']),
        selection_manifest=selection_manifest, counts_as_of=cutoff, materialized_at=generated_at,
        population_role='final_test', quality_accepted=False, operational_approval=False, target_rows_read=False)]
    candidates = base._index(tables['p08_candidate_audit'], ('tenant_id', 'candidate_id'))
    outputs = base._index(tables['eta_output_release'], ('tenant_id', 'output_id'))
    for record in bundle['delivery']['episodes']:
        item = transport._typed_record(record, transport.EPISODE_FIELDS, generated_at)
        tables['p08_delivery_episode_audit'].append(item)
        tables['eta_alert_episode'].append({k: item[k] for k in transport.CANONICAL_FIELDS['eta_alert_episode']})
    for record in bundle['delivery']['alerts']:
        item = transport._typed_record(record, transport.ALERT_FIELDS, generated_at)
        item['output_id'] = candidates[record['tenant_id'], record['candidate_id']]['output_id']
        tables['p08_delivery_alert_audit'].append(item)
        tables['eta_alert'].append({k: item[k] for k in transport.CANONICAL_FIELDS['eta_alert']})
    for record in bundle['delivery']['attempts']:
        item = transport._typed_record(record, transport.ATTEMPT_FIELDS, generated_at)
        provenance = ident('P08-attempt-provenance', bundle['scenario_id'], record['tenant_id'], record['delivery_id'])
        item['provenance_id'] = provenance
        tables['p08_delivery_attempt_audit'].append(item)
        tables['eta_notification_delivery'].append({k: item[k] for k in transport.CANONICAL_FIELDS['eta_notification_delivery']})
        candidate = candidates[record['tenant_id'], record['candidate_id']]
        output = outputs[record['tenant_id'], candidate['output_id']]
        tables['p08_delivery_provenance_reference'].append(dict(tenant_id=record['tenant_id'], provenance_id=provenance,
            reference_kind='synthetic_delivery_attempt', source_reference='p08_delivery_attempt_audit#' + record['delivery_id'],
            source_sha256=base._digest(record), record_key={'tenant_id': record['tenant_id'], 'delivery_id': record['delivery_id'],
                'alert_id': record['alert_id'], 'candidate_id': record['candidate_id'], 'scenario_id': bundle['scenario_id']},
            parent_provenance_ids=[output['provenance_id']], recorded_at=generated_at,
            result_origin=ORIGIN, clock_domain=CLOCK, materialized_at=generated_at))
    for index, record in enumerate(bundle['delivery']['events']):
        item = transport._typed_record(record, transport.EVENT_FIELDS, generated_at)
        item['event_order'] = index
        tables['p08_delivery_event_audit'].append(item)
    for name, values in tables.items():
        base._index(values, KEYS[name])
        values.sort(key=lambda r: tuple(r[k] for k in KEYS[name]))
    return tables


def _check_types(tables, schema):
    for name, rows in tables.items():
        base._index(rows, KEYS[name])
        for row in rows:
            if set(row) != {field for field, _, _ in schema[name]}:
                raise ValueError('Final store column mismatch: ' + name)
            for field, kind, nullable in schema[name]:
                value = row[field]
                if value is None:
                    if not nullable:
                        raise ValueError('Unexpected NULL: ' + name + '.' + field)
                    continue
                if kind == 'VARCHAR' and (not isinstance(value, str) or not value.strip()):
                    raise ValueError('Nonempty text required: ' + name + '.' + field)
                if kind == 'BOOLEAN' and type(value) is not bool:
                    raise ValueError('Boolean required: ' + name + '.' + field)
                if kind in ('INTEGER', 'BIGINT') and type(value) is not int:
                    raise ValueError('Integer required: ' + name + '.' + field)
                if kind == 'TIMESTAMPTZ':
                    parse_ts(value)
                if kind == 'JSON' and not isinstance(value, (dict, list)):
                    raise ValueError('Structured JSON required: ' + name + '.' + field)
                if kind == 'DOUBLE' or kind.startswith('DECIMAL'):
                    number = base._finite(value)
                    if kind.startswith('DECIMAL'):
                        places = int(kind.split(',')[1].rstrip(')'))
                        if number != number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN):
                            raise ValueError('Value exceeds declared decimal precision: ' + name + '.' + field)
            json.dumps(row, allow_nan=False)


def _validate(tables, schema, settings):
    validate_settings(settings)
    if set(tables) != set(ORDER) or any(tables[name] for name in RESERVED):
        raise ValueError('Final store requires complete inventory and empty P09 workflows')
    _check_types(tables, schema)
    if len(tables['p08_run_audit']) != 1:
        raise ValueError('Exactly one final-scenario manifest is required')
    run = tables['p08_run_audit'][0]
    metadata = [r['opportunity'] for r in tables['p08_metadata_audit']]
    bundle = run_policy_delivery(metadata, settings, run['scenario_id'])
    rebuilt = _build(metadata, _registry(run['registry']), bundle, run['materialized_at'], settings)
    for name in ORDER:
        if digest(tables[name]) != digest(rebuilt[name]):
            raise ValueError('Final canonical/audit reconstruction differs: ' + name)


def _ddl(schema):
    statements = []
    for name in ORDER:
        columns = ['"' + field + '" ' + kind + ('' if nullable else ' NOT NULL') for field, kind, nullable in schema[name]]
        columns.append('PRIMARY KEY (' + ','.join(KEYS[name]) + ')')
        if name == 'p08_model_binding': columns.append('UNIQUE (prediction_version)')
        if name == 'p08_opportunity_audit': columns.append('UNIQUE (tenant_id,output_id)')
        if name == 'p08_delivery_alert_audit': columns.append('UNIQUE (tenant_id,candidate_id)')
        if name == 'p08_delivery_attempt_audit': columns.append('UNIQUE (tenant_id,alert_id,attempt_index)')
        if name == 'p08_delivery_event_audit': columns.append('UNIQUE (event_order)')
        for fields, parent, refs in FKS.get(name, []):
            columns.append('FOREIGN KEY (' + ','.join(fields) + ') REFERENCES ' + parent + ' (' + ','.join(refs) + ')')
        if name == 'eta_model_version': columns.append('CHECK (nominal_interval_coverage>0 AND nominal_interval_coverage<1)')
        if name == 'eta_prediction':
            columns.extend(['CHECK (delay_probability IS NULL OR delay_probability BETWEEN 0 AND 1)',
                'CHECK (remaining_eta_seconds>=0 AND eta_lower<=eta_median AND eta_median<=eta_upper)'])
        if name == 'eta_alert': columns.append("CHECK (alert_status IN ('queued','suppressed','no_action_lead','delivered','failed'))")
        if name == 'eta_alert_episode': columns.append('CHECK (delivered_alert_count>=0 AND duplicate_alert_count=greatest(delivered_alert_count-1,0))')
        if name == 'eta_notification_delivery':
            columns.extend(["CHECK (delivery_status IN ('queued','delivered','failed','unknown'))",
                "CHECK (recipient_role IN ('dispatcher','demo_reviewer'))", 'CHECK (received_or_opened_at IS NULL)',
                "CHECK ((delivery_status='delivered' AND delivered_at IS NOT NULL AND receipt_type='service_delivered') OR (delivery_status<>'delivered' AND delivered_at IS NULL AND receipt_type='none'))"])
        if name.startswith('p08_delivery_'):
            columns.extend(["CHECK (result_origin='synthetic_scenario')", "CHECK (clock_domain='synthetic_scenario_time')"])
        if name == 'p08_run_audit':
            columns.extend(["CHECK (population_role='final_test')", 'CHECK (NOT quality_accepted AND NOT operational_approval AND NOT target_rows_read)'])
        statements.append('CREATE TABLE ' + name + ' (' + ','.join(columns) + ');')
    return statements


def _sql_checks(con):
    queries = {
        'complete_final_opportunity_accounting': '''SELECT count(*) FROM p08_source_snapshot s
          FULL JOIN p08_opportunity_audit a USING(tenant_id,snapshot_id)
          FULL JOIN p08_metadata_audit m USING(tenant_id,snapshot_id)
          FULL JOIN eta_evaluation_membership e USING(tenant_id,snapshot_id)
          WHERE s.snapshot_id IS NULL OR a.snapshot_id IS NULL OR m.snapshot_id IS NULL OR e.snapshot_id IS NULL''',
        'full_trip_weights': '''SELECT count(*) FROM (SELECT s.tenant_id,s.trip_id,sum(e.trip_evaluation_weight) w
          FROM eta_evaluation_membership e JOIN p08_source_snapshot s USING(tenant_id,snapshot_id)
          GROUP BY s.tenant_id,s.trip_id HAVING w<>1)''',
        'unavailable_predictions_remain_audited': '''SELECT count(*) FROM p08_opportunity_audit a
          LEFT JOIN eta_prediction p USING(tenant_id,snapshot_id,prediction_version)
          WHERE a.canonical_prediction_written<>(p.snapshot_id IS NOT NULL)
          OR (p.snapshot_id IS NOT NULL AND (p.delay_probability IS DISTINCT FROM a.probability OR p.remaining_eta_seconds<>a.median_seconds_int))''',
        'every_opportunity_has_release': '''SELECT count(*) FROM p08_opportunity_audit a FULL JOIN eta_output_release r
          USING(tenant_id,output_id) WHERE a.output_id IS NULL OR r.output_id IS NULL OR r.release_mode<>'offline_replay' OR r.released_at<>a.generated_at''',
        'all_candidates_have_alerts': '''SELECT count(*) FROM p08_candidate_audit c FULL JOIN p08_delivery_alert_audit a
          USING(tenant_id,candidate_id) WHERE c.candidate_id IS NULL OR a.candidate_id IS NULL''',
        'empty_downstream_workflows': '''SELECT (SELECT count(*) FROM eta_adoption)+(SELECT count(*) FROM eta_action)+(SELECT count(*) FROM eta_action_followup)''',
        'provenance_parent_resolution': '''WITH refs AS (SELECT tenant_id,provenance_id FROM p08_provenance_reference
          UNION ALL SELECT tenant_id,provenance_id FROM p08_delivery_provenance_reference),
          edges AS (SELECT c.tenant_id,json_extract_string(j.value,'$') parent_id FROM p08_provenance_reference c,json_each(c.parent_provenance_ids) j
          UNION ALL SELECT c.tenant_id,json_extract_string(j.value,'$') parent_id FROM p08_delivery_provenance_reference c,json_each(c.parent_provenance_ids) j)
          SELECT count(*) FROM edges e LEFT JOIN refs p ON e.tenant_id=p.tenant_id AND e.parent_id=p.provenance_id WHERE p.provenance_id IS NULL''',
    }
    result = []
    for name, query in queries.items():
        violations = con.execute(query).fetchone()[0]
        result.append(dict(name=name, violations=violations, passed=violations == 0))
        if violations:
            raise ValueError('Final store invariant failed: ' + name)
    return result


def _claims(counts, checks, scenario):
    return dict(status='passed', store_version=VERSION, scenario_id=scenario, population_role='final_test',
        canonical_tables=list(CANONICAL), reserved_empty_tables=list(RESERVED), auxiliary_tables=list(AUX),
        counts=counts, checks=checks, foreign_keys_enforced=True, parquet_roundtrip_verified=True,
        release_mode='offline_replay', synthetic_delivery_simulated=True, notifications_sent=False,
        recipient_openings_generated=False, adoption_actions_generated=False, target_rows_read=False,
        quality_accepted=False, operational_approval=False, full_m2_warehouse_rebuilt=False)


def write_store(repo_root, destination, tables, settings):
    """Publish a complete fresh DB/JSONL/Parquet set only after all checks."""
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Use a fresh final store directory')
    schema = _schema(repo_root, settings)
    _validate(tables, schema, settings)
    statements = _ddl(schema)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.p08-store-', dir=destination.parent) as temporary:
        stage = Path(temporary) / 'complete'
        (stage / 'jsonl').mkdir(parents=True)
        (stage / 'parquet').mkdir()
        con = duckdb.connect(str(stage / 'experiment.duckdb'))
        try:
            con.execute("SET TimeZone='UTC'")
            con.execute('BEGIN')
            for name, statement in zip(ORDER, statements):
                con.execute(statement)
                values = [[json.dumps(row[field], sort_keys=True, allow_nan=False) if kind == 'JSON' else row[field]
                    for field, kind, _ in schema[name]] for row in tables[name]]
                if values:
                    con.executemany('INSERT INTO ' + name + ' VALUES (' + ','.join('?' for _ in schema[name]) + ')', values)
                write_jsonl(stage / 'jsonl' / (name + '.jsonl'), tables[name])
            checks = _sql_checks(con)
            con.execute('COMMIT')
            counts = {}
            for name in ORDER:
                counts[name] = con.execute('SELECT count(*) FROM ' + name).fetchone()[0]
                path = str(stage / 'parquet' / (name + '.parquet'))
                con.execute('COPY (SELECT * FROM ' + name + ' ORDER BY ' + ','.join(KEYS[name]) + ') TO ? (FORMAT PARQUET,COMPRESSION ZSTD)', [path])
                difference = con.execute('SELECT count(*) FROM ((SELECT * FROM ' + name + ' EXCEPT ALL SELECT * FROM read_parquet(?)) UNION ALL (SELECT * FROM read_parquet(?) EXCEPT ALL SELECT * FROM ' + name + '))', [path, path]).fetchone()[0]
                if difference:
                    raise ValueError('Final Parquet roundtrip differs: ' + name)
            checks.append(dict(name='parquet_roundtrip_all_tables', violations=0, passed=True))
        finally:
            con.close()
        (stage / 'schema.sql').write_text('\n\n'.join(statements) + '\n')
        run = tables['p08_run_audit'][0]
        (stage / 'selection_manifest.json').write_text(json.dumps(run['selection_manifest'], sort_keys=True, indent=2) + '\n')
        receipt = {**_claims(counts, checks, run['scenario_id']), 'engine': 'DuckDB', 'engine_version': duckdb.__version__,
            'output_sha256': {str(p.relative_to(stage)): digest_file(p) for p in sorted(stage.rglob('*')) if p.is_file()}}
        (stage / 'verification.json').write_text(json.dumps(receipt, sort_keys=True, indent=2) + '\n')
        if destination.exists() or destination.is_symlink():
            raise FileExistsError('Final store destination appeared during execution')
        stage.rename(destination)
    return receipt


def verify_store(repo_root, destination, expected_tables, settings):
    """Inspect stored evidence read-only against exact frozen reconstruction."""
    destination = Path(destination)
    schema = _schema(repo_root, settings)
    _validate(expected_tables, schema, settings)
    receipt = json.loads((destination / 'verification.json').read_text())
    paths = {'experiment.duckdb', 'schema.sql', 'selection_manifest.json'} | {
        folder + '/' + name + '.' + extension for folder, extension in (('jsonl', 'jsonl'), ('parquet', 'parquet')) for name in ORDER}
    actual = {str(p.relative_to(destination)) for p in destination.rglob('*') if p.is_file()}
    if actual != paths | {'verification.json'} or set(receipt.get('output_sha256', {})) != paths:
        raise ValueError('Final store inventory differs')
    for name, sha in receipt['output_sha256'].items():
        if digest_file(destination / name) != sha:
            raise ValueError('Final store file changed: ' + name)
    statements = _ddl(schema)
    if (destination / 'schema.sql').read_text() != '\n\n'.join(statements) + '\n':
        raise ValueError('Final physical schema differs')
    manifest = expected_tables['p08_run_audit'][0]['selection_manifest']
    if json.loads((destination / 'selection_manifest.json').read_text()) != manifest:
        raise ValueError('Final threshold-selection reference differs')
    reference = duckdb.connect(':memory:')
    try:
        for statement in statements:
            reference.execute(statement)
        constraints = transport._constraint_signatures(reference)
    finally:
        reference.close()
    con = duckdb.connect(str(destination / 'experiment.duckdb'), read_only=True)
    counts, compared = {}, 0
    try:
        con.execute("SET TimeZone='UTC'")
        if {r[0] for r in con.execute('SHOW TABLES').fetchall()} != set(ORDER) or transport._constraint_signatures(con) != constraints:
            raise ValueError('Final table or physical constraint inventory differs')
        for name in ORDER:
            description = con.execute('DESCRIBE ' + name).fetchall()
            actual_schema = [(r[0], r[1].replace('TIMESTAMP WITH TIME ZONE', 'TIMESTAMPTZ'), r[2] == 'YES') for r in description]
            if actual_schema != schema[name]:
                raise ValueError('Final DuckDB schema differs: ' + name)
            expected = sorted(expected_tables[name], key=lambda r: tuple(r[k] for k in KEYS[name]))
            if digest(read_jsonl(destination / 'jsonl' / (name + '.jsonl'))) != digest(expected):
                raise ValueError('Final JSONL differs from reconstructed rows: ' + name)
            desired = [tuple(base._logical(row[field], kind) for field, kind, _ in schema[name]) for row in expected]
            projection = ','.join('CAST(' + field + ' AS VARCHAR) AS ' + field if kind == 'TIMESTAMPTZ' else field for field, kind, _ in schema[name])
            order = ' ORDER BY ' + ','.join(KEYS[name])
            database = con.execute('SELECT ' + projection + ' FROM ' + name + order).fetchall()
            path = str(destination / 'parquet' / (name + '.parquet'))
            parquet = con.execute('SELECT ' + projection + ' FROM read_parquet(?)' + order, [path]).fetchall()
            kinds = [(r[0], r[1].replace('TIMESTAMP WITH TIME ZONE', 'TIMESTAMPTZ')) for r in con.execute('DESCRIBE SELECT * FROM read_parquet(?)', [path]).fetchall()]
            if kinds != [(field, kind) for field, kind, _ in schema[name]]:
                raise ValueError('Final Parquet schema differs: ' + name)
            for records in (database, parquet):
                normalized = [tuple(base._logical(value, kind) for value, (_, kind, _) in zip(row, schema[name])) for row in records]
                if normalized != desired:
                    raise ValueError('Final typed logical rows differ: ' + name)
            counts[name] = len(expected)
            compared += 3 * len(expected)
        checks = _sql_checks(con)
        checks.append(dict(name='parquet_roundtrip_all_tables', violations=0, passed=True))
    finally:
        con.close()
    claims = _claims(counts, checks, expected_tables['p08_run_audit'][0]['scenario_id'])
    if {k: receipt.get(k) for k in claims} != claims:
        raise ValueError('Final verification receipt claims differ')
    return {**claims, 'differences': 0, 'logical_rows_compared': compared, 'read_only': True, 'foreign_keys_verified': True}
