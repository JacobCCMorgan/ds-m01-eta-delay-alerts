"""Typed, separate P09 workflow evidence with immutable P08 parent rows.

Opening observations live in P09 audit tables. Historical service-delivery rows,
model evidence and final-test outcomes are not rewritten by this extension.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile

import duckdb

from ds_m01.common import digest_file, parse_ts, read_jsonl, utc, write_jsonl
from ds_m01 import final_store as parent
from ds_m01 import release_store as base
from ds_m01.delivery_store import _constraint_signatures

VERSION = 'p09-workflow-store-v1'
PARENT_COMMIT = '12b904696b87d90369d85dabeee01eadadc854cd'
PROMOTED = ('eta_adoption', 'eta_action', 'eta_action_followup')
PRESERVED = tuple(name for name in parent.ORDER if name not in PROMOTED)
AUDITS = {'p09_opening_audit': 'opening_events', 'p09_decision_audit': 'decision_audit',
          'p09_followup_audit': 'followup_audit', 'p09_event_audit': 'events'}
AUX = {
    'p09_run_audit': [('store_version', 'VARCHAR', False), ('parent_commit', 'VARCHAR', False),
        ('scenario_id', 'VARCHAR', False), ('materialized_at', 'TIMESTAMPTZ', False),
        ('settings_sha256', 'VARCHAR', False), ('workflow_settings', 'JSON', False),
        ('simulation_sha256', 'VARCHAR', False), ('simulation_summary', 'JSON', False),
        ('parent_table_sha256', 'JSON', False), ('quality_accepted', 'BOOLEAN', False),
        ('operational_approval', 'BOOLEAN', False)],
    'p09_promise_reference': [('tenant_id', 'VARCHAR', False), ('promise_version_id', 'VARCHAR', False),
        ('trip_id', 'VARCHAR', False), ('known_at', 'TIMESTAMPTZ', False),
        ('window_start_at', 'TIMESTAMPTZ', False), ('window_end_at', 'TIMESTAMPTZ', False),
        ('reference_origin', 'VARCHAR', False), ('record_sha256', 'VARCHAR', False), ('record', 'JSON', False)],
    'p09_outcome_reference': [('tenant_id', 'VARCHAR', False), ('outcome_version_id', 'VARCHAR', False),
        ('trip_id', 'VARCHAR', False), ('label_available_time', 'TIMESTAMPTZ', False),
        ('actual_arrival_time', 'TIMESTAMPTZ', True), ('followup_through', 'TIMESTAMPTZ', False),
        ('label_status', 'VARCHAR', False), ('record_sha256', 'VARCHAR', False), ('record', 'JSON', False)],
    'p09_provenance_reference': [('tenant_id', 'VARCHAR', False), ('provenance_id', 'VARCHAR', False),
        ('entity_table', 'VARCHAR', False), ('entity_key', 'JSON', False), ('record_sha256', 'VARCHAR', False),
        ('result_origin', 'VARCHAR', False)],
}
for name in AUDITS:
    AUX[name] = [('tenant_id', 'VARCHAR', False), ('record_id', 'VARCHAR', False),
                 ('record_sha256', 'VARCHAR', False), ('record', 'JSON', False)]
ORDER = PRESERVED + ('p09_run_audit', 'p09_promise_reference', 'p09_outcome_reference',
                    'p09_provenance_reference') + PROMOTED + tuple(AUDITS)
KEYS = {**parent.KEYS, 'p09_run_audit': ('store_version',),
        'p09_promise_reference': ('tenant_id', 'promise_version_id'),
        'p09_outcome_reference': ('tenant_id', 'outcome_version_id'),
        'p09_provenance_reference': ('tenant_id', 'provenance_id'),
        **{name: ('tenant_id', 'record_id') for name in AUDITS}}
FKS = {
    'eta_adoption': [(('tenant_id', 'delivery_id'), 'eta_notification_delivery', ('tenant_id', 'delivery_id'))],
    'eta_action': [(('tenant_id', 'adoption_id'), 'eta_adoption', ('tenant_id', 'adoption_id')),
                   (('tenant_id', 'alert_id'), 'eta_alert', ('tenant_id', 'alert_id'))],
    'eta_action_followup': [(('tenant_id', 'action_id'), 'eta_action', ('tenant_id', 'action_id')),
        (('tenant_id', 'outcome_version_id'), 'p09_outcome_reference', ('tenant_id', 'outcome_version_id')),
        (('tenant_id', 'original_promise_version_id'), 'p09_promise_reference', ('tenant_id', 'promise_version_id')),
        (('tenant_id', 'followup_promise_version_id'), 'p09_promise_reference', ('tenant_id', 'promise_version_id'))],
    'p09_promise_reference': [(('tenant_id', 'trip_id'), 'p08_source_trip', ('tenant_id', 'trip_id'))],
    'p09_outcome_reference': [(('tenant_id', 'trip_id'), 'p08_source_trip', ('tenant_id', 'trip_id'))],
}
for name in PROMOTED:
    FKS[name].append((('tenant_id', 'provenance_id'), 'p09_provenance_reference', ('tenant_id', 'provenance_id')))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _settings(root):
    return json.loads((Path(root) / 'config/p08_evaluation.json').read_text())


def _schema(root, settings):
    schema = parent._schema(root, _settings(root))
    schema.update(deepcopy(AUX))
    _require(set(schema) == set(ORDER), 'P09 table schema inventory differs')
    return schema


def _sort(tables):
    for name in ORDER:
        base._index(tables[name], KEYS[name])
        tables[name].sort(key=lambda r: tuple(r[k] for k in KEYS[name]))
    return tables


def _reference_records(inputs, simulation):
    promises = []
    for rows, origin in ((inputs['promises'], 'retained_p08_promise'),
                         (simulation['promise_versions'], 'synthetic_p09_revision')):
        for row in rows:
            promises.append({**{k: row[k] for k in ('tenant_id', 'promise_version_id', 'trip_id', 'known_at',
                                                  'window_start_at', 'window_end_at')},
                             'reference_origin': origin, 'record_sha256': digest(row), 'record': deepcopy(row)})
    outcomes = [{**{k: row[k] for k in ('tenant_id', 'outcome_version_id', 'trip_id', 'label_available_time',
                                      'actual_arrival_time', 'followup_through', 'label_status')},
                 'record_sha256': digest(row), 'record': deepcopy(row)} for row in simulation['outcome_references']]
    return promises, outcomes


def build_tables(root, inputs, simulation, generated_at, settings):
    """Promote only the three workflow contracts in a separate exact store."""
    generated_at = utc(generated_at)
    source = inputs['parent_tables']
    _require(set(source) == set(parent.ORDER) and all(not source[n] for n in PROMOTED),
             'P09 requires the full original P08 store with empty workflow tables')
    tables = {name: deepcopy(source[name]) if name in PRESERVED else [] for name in ORDER}
    for name in PROMOTED:
        tables[name] = deepcopy(simulation[name])
    tables['p09_run_audit'] = [dict(store_version=VERSION, parent_commit=PARENT_COMMIT,
        scenario_id='S00_unmodified', materialized_at=generated_at, settings_sha256=digest(settings),
        workflow_settings=deepcopy(settings), simulation_sha256=digest(_ordered_simulation(simulation)),
        simulation_summary=deepcopy(simulation['summary']),
        parent_table_sha256={name: digest(source[name]) for name in parent.ORDER},
        quality_accepted=False, operational_approval=False)]
    promises, outcomes = _reference_records(inputs, simulation)
    tables['p09_promise_reference'] = promises
    tables['p09_outcome_reference'] = outcomes
    for name in PROMOTED:
        for row in tables[name]:
            tables['p09_provenance_reference'].append(dict(tenant_id=row['tenant_id'],
                provenance_id=row['provenance_id'], entity_table=name,
                entity_key={key: row[key] for key in KEYS[name]}, record_sha256=digest(row),
                result_origin='synthetic_scenario'))
    for name, key in AUDITS.items():
        tables[name] = [dict(tenant_id=row['tenant_id'], record_id=digest(row),
                            record_sha256=digest(row), record=deepcopy(row)) for row in simulation[key]]
    _sort(tables)
    _validate(root, tables, _schema(root, settings), settings)
    return tables


def _simulation(tables):
    return {**{name: deepcopy(tables[name]) for name in PROMOTED},
            **{key: [deepcopy(r['record']) for r in tables[name]] for name, key in AUDITS.items()},
            'promise_versions': [deepcopy(r['record']) for r in tables['p09_promise_reference']
                                 if r['reference_origin'] == 'synthetic_p09_revision'],
            'outcome_references': [deepcopy(r['record']) for r in tables['p09_outcome_reference']],
            'summary': deepcopy(tables['p09_run_audit'][0]['simulation_summary'])}


def _ordered_simulation(value):
    """Audit transport ordering is immaterial; event clocks and IDs remain exact."""
    return {k: sorted(v, key=digest) if isinstance(v, list) else v for k, v in value.items()}


def _validate(root, tables, schema, settings):
    _require(set(tables) == set(ORDER), 'P09 store table inventory differs')
    parent._check_types({n: tables[n] for n in parent.ORDER}, {n: schema[n] for n in parent.ORDER})
    for name in AUX:
        base._index(tables[name], KEYS[name])
        for row in tables[name]:
            _require(set(row) == {f for f, _, _ in schema[name]}, 'P09 auxiliary columns differ: ' + name)
            for field, kind, nullable in schema[name]:
                value = row[field]
                if value is None:
                    _require(nullable, 'Unexpected NULL: ' + name + '.' + field); continue
                if kind == 'VARCHAR': _require(isinstance(value, str) and bool(value.strip()), 'Nonempty text required')
                if kind == 'BOOLEAN': _require(type(value) is bool, 'Boolean required')
                if kind == 'TIMESTAMPTZ': parse_ts(value)
                if kind == 'JSON':
                    _require(isinstance(value, (dict, list)), 'Structured JSON required')
                    json.dumps(value, allow_nan=False)
    _require(len(tables['p09_run_audit']) == 1, 'One P09 run audit is required')
    run = tables['p09_run_audit'][0]
    _require(run['store_version'] == VERSION and run['parent_commit'] == PARENT_COMMIT
             and run['scenario_id'] == 'S00_unmodified' and run['quality_accepted'] is False
             and run['operational_approval'] is False, 'P09 run audit overclaims scope or changes parent')
    _require(run['settings_sha256'] == digest(settings) and digest(run['workflow_settings']) == digest(settings),
             'P09 store settings differ')
    projection = {name: deepcopy(tables[name]) if name in PRESERVED else [] for name in parent.ORDER}
    parent._validate(projection, {name: schema[name] for name in parent.ORDER}, _settings(root))
    _require(run['parent_table_sha256'] == {name: digest(projection[name]) for name in parent.ORDER},
             'Immutable parent table digest differs')
    directory = Path(root) / 'reports/p08/scenarios/S00_unmodified/store/jsonl'
    for name in parent.ORDER:
        _require(digest(read_jsonl(directory / (name + '.jsonl'))) == digest(projection[name]),
                 'P09 has rewritten an immutable parent table: ' + name)
    for name in AUDITS:
        for row in tables[name]:
            _require(row['tenant_id'] == row['record'].get('tenant_id')
                     and row['record_id'] == row['record_sha256'] == digest(row['record']),
                     'P09 audit payload or identity differs: ' + name)
    for name in ('p09_promise_reference', 'p09_outcome_reference'):
        for row in tables[name]:
            _require(row['record_sha256'] == digest(row['record']), 'P09 reference payload hash differs')
            for key in row.keys() - {'reference_origin', 'record_sha256', 'record'}:
                _require(row[key] == row['record'].get(key), 'P09 typed reference differs from source record')
    provenance = base._index(tables['p09_provenance_reference'], ('tenant_id', 'provenance_id'))
    expected_refs = set()
    for name in PROMOTED:
        for row in tables[name]:
            key = (row['tenant_id'], row['provenance_id']); expected_refs.add(key)
            ref = provenance.get(key)
            _require(ref is not None and ref['entity_table'] == name
                     and ref['entity_key'] == {k: row[k] for k in KEYS[name]}
                     and ref['record_sha256'] == digest(row) and ref['result_origin'] == 'synthetic_scenario',
                     'Canonical P09 provenance differs')
    _require(set(provenance) == expected_refs, 'P09 provenance has extra or omitted rows')
    from .inputs import load_parent
    from .engine import simulate_workflow
    reconstructed = simulate_workflow(load_parent(root), settings)
    _require(run['simulation_sha256'] == digest(_ordered_simulation(reconstructed))
             == digest(_ordered_simulation(_simulation(tables))),
             'P09 workflow tables differ from source-bound deterministic reconstruction')
    _validate_workflow(tables, parse_ts(run['materialized_at']))


def _validate_workflow(tables, materialized_at):
    deliveries = base._index(tables['p08_delivery_attempt_audit'], ('tenant_id', 'delivery_id'))
    adoptions = base._index(tables['eta_adoption'], ('tenant_id', 'adoption_id'))
    actions = base._index(tables['eta_action'], ('tenant_id', 'action_id'))
    outcomes = base._index(tables['p09_outcome_reference'], ('tenant_id', 'outcome_version_id'))
    promises = base._index(tables['p09_promise_reference'], ('tenant_id', 'promise_version_id'))
    used_deliveries = set()
    allowed = {'accepted', 'partially_accepted', 'rejected', 'not_reviewed', 'overridden'}
    for row in adoptions.values():
        dkey = row['tenant_id'], row['delivery_id']; delivery = deliveries.get(dkey)
        _require(delivery is not None and delivery['delivery_status'] == 'delivered'
                 and delivery['receipt_observed'] is True and delivery['delivered_at'] is not None,
                 'Adoption must reference a confirmed delivery in the same tenant')
        _require(dkey not in used_deliveries, 'Duplicate adoption decision for delivery')
        used_deliveries.add(dkey)
        _require(row['adoption_status'] in allowed, 'Unsupported adoption status')
        if row['adoption_status'] == 'not_reviewed':
            _require(row['decided_at'] is None, 'Unreviewed delivery cannot have a decision time')
        else:
            _require(row['decided_at'] is not None and parse_ts(delivery['receipt_known_at']) <=
                     parse_ts(row['decided_at']) <= materialized_at, 'Adoption decision clock differs')
        if row['adoption_status'] in {'rejected', 'partially_accepted', 'overridden'}:
            _require(isinstance(row['adoption_reason'], str) and bool(row['adoption_reason'].strip()),
                     'Partial adoption, rejection and override require a reason')
    for row in actions.values():
        adoption = adoptions.get((row['tenant_id'], row['adoption_id']))
        _require(adoption is not None, 'Action adoption identity crosses tenants or is absent')
        delivery = deliveries[row['tenant_id'], adoption['delivery_id']]
        _require(row['alert_id'] == delivery['alert_id'], 'Action and adoption alert differ')
        _require(row['action_type'] in {'contact_driver', 'contact_customer', 'reschedule', 'reassign', 'no_action', 'other'},
                 'Unsupported action type')
        if row['handling_seconds'] is not None:
            _require(type(row['handling_seconds']) is int and row['handling_seconds'] >= 0,
                     'Handling time must be a nonnegative integer or unknown')
        if row['action_time'] is not None:
            _require(adoption['decided_at'] is not None and parse_ts(adoption['decided_at']) <=
                     parse_ts(row['action_time']) <= materialized_at, 'Action precedes its decision')
        if row['action_type'] != 'no_action':
            _require(adoption['adoption_status'] in {'accepted', 'partially_accepted', 'overridden'}
                     and row['action_time'] is not None, 'Executed action lacks an executable adoption decision')
    for row in tables['eta_action_followup']:
        action = actions.get((row['tenant_id'], row['action_id']))
        outcome = outcomes.get((row['tenant_id'], row['outcome_version_id']))
        old = promises.get((row['tenant_id'], row['original_promise_version_id']))
        new = promises.get((row['tenant_id'], row['followup_promise_version_id']))
        _require(action is not None and action['action_type'] != 'no_action' and outcome is not None
                 and old is not None and new is not None, 'Follow-up requires action, source outcome and both promises')
        adoption = adoptions[row['tenant_id'], action['adoption_id']]
        delivery = deliveries[row['tenant_id'], adoption['delivery_id']]
        _require(outcome['trip_id'] == old['trip_id'] == new['trip_id'] == delivery['trip_id'],
                 'Follow-up references a different trip')
        _require(row['original_promise_version_id'] == delivery['promise_version_id'],
                 'Original alert promise was rewritten')
        cutoff = parse_ts(row['outcome_observation_end'])
        _require(parse_ts(action['action_time']) <= cutoff <= materialized_at
                 and parse_ts(outcome['label_available_time']) <= cutoff
                 and parse_ts(new['known_at']) <= cutoff, 'Follow-up uses future or pre-action observations')
        _require(row['review_outcome'] in {'confirmed', 'rejected', 'disputed', 'not_reported', 'open', 'lost'}
                 and row['result_origin'] == 'synthetic_scenario', 'Follow-up origin or review status differs')
        values = [row['observed_delay_seconds'], row['original_promise_delay_seconds']]
        _require(all(v is None or type(v) is int and v >= 0 for v in values),
                 'Follow-up delay must be a nonnegative integer or unknown')
        _require((values[0] is None) == (values[1] is None), 'Promise comparison must share the same observed arrival')
        if values[0] is not None:
            _require(row['review_outcome'] == 'confirmed' and outcome['actual_arrival_time'] is not None,
                     'Unresolved follow-up cannot be reported as a known delay')


def _ddl(schema):
    old = dict(zip(parent.ORDER, parent._ddl({name: schema[name] for name in parent.ORDER})))
    statements = []
    for name in ORDER:
        if name in PRESERVED:
            statements.append(old[name]); continue
        columns = ['"' + f + '" ' + kind + ('' if nullable else ' NOT NULL') for f, kind, nullable in schema[name]]
        columns.append('PRIMARY KEY (' + ','.join(KEYS[name]) + ')')
        for fields, other, refs in FKS.get(name, []):
            columns.append('FOREIGN KEY (' + ','.join(fields) + ') REFERENCES ' + other + ' (' + ','.join(refs) + ')')
        if name == 'eta_adoption':
            columns += ['UNIQUE (tenant_id,delivery_id)', "CHECK (adoption_status IN ('accepted','partially_accepted','rejected','not_reviewed','overridden'))",
                        "CHECK ((adoption_status='not_reviewed' AND decided_at IS NULL) OR (adoption_status<>'not_reviewed' AND decided_at IS NOT NULL))"]
        if name == 'eta_action':
            columns += ["CHECK (action_type IN ('contact_driver','contact_customer','reschedule','reassign','no_action','other'))",
                        'CHECK (handling_seconds IS NULL OR handling_seconds>=0)']
        if name == 'eta_action_followup':
            columns += ["CHECK (review_outcome IN ('confirmed','rejected','disputed','not_reported','open','lost'))",
                        "CHECK (result_origin='synthetic_scenario')", 'CHECK (observed_delay_seconds IS NULL OR observed_delay_seconds>=0)',
                        'CHECK (original_promise_delay_seconds IS NULL OR original_promise_delay_seconds>=0)']
        if name == 'p09_run_audit':
            columns += ['CHECK (NOT quality_accepted AND NOT operational_approval)', "CHECK (scenario_id='S00_unmodified')"]
        if name == 'p09_provenance_reference':
            columns += ["CHECK (result_origin='synthetic_scenario')"]
        if name == 'p09_promise_reference':
            columns += ['CHECK (window_start_at<=window_end_at)',
                        "CHECK (reference_origin IN ('retained_p08_promise','synthetic_p09_revision'))"]
        statements.append('CREATE TABLE ' + name + ' (' + ','.join(columns) + ');')
    return statements


def _sql_checks(con):
    queries = {
        'adoption_after_confirmed_delivery': '''SELECT count(*) FROM eta_adoption a JOIN p08_delivery_attempt_audit d
            USING(tenant_id,delivery_id) WHERE d.delivery_status<>'delivered' OR NOT d.receipt_observed
            OR (a.decided_at IS NOT NULL AND a.decided_at<d.receipt_known_at)''',
        'action_after_matching_adoption': '''SELECT count(*) FROM eta_action x JOIN eta_adoption a USING(tenant_id,adoption_id)
            JOIN p08_delivery_attempt_audit d USING(tenant_id,delivery_id)
            WHERE x.alert_id<>d.alert_id OR x.action_time<a.decided_at OR (x.action_type<>'no_action' AND
            (x.action_time IS NULL OR a.adoption_status NOT IN ('accepted','partially_accepted','overridden')))''',
        'followup_source_clocks_and_trip': '''SELECT count(*) FROM eta_action_followup f JOIN eta_action x USING(tenant_id,action_id)
            JOIN eta_adoption a USING(tenant_id,adoption_id) JOIN p08_delivery_attempt_audit d USING(tenant_id,delivery_id)
            JOIN p09_outcome_reference o USING(tenant_id,outcome_version_id)
            JOIN p09_promise_reference p ON f.tenant_id=p.tenant_id AND f.original_promise_version_id=p.promise_version_id
            JOIN p09_promise_reference q ON f.tenant_id=q.tenant_id AND f.followup_promise_version_id=q.promise_version_id
            WHERE d.trip_id<>o.trip_id OR d.trip_id<>p.trip_id OR d.trip_id<>q.trip_id
            OR f.original_promise_version_id<>d.promise_version_id OR o.label_available_time>f.outcome_observation_end
            OR q.known_at>f.outcome_observation_end OR x.action_time>f.outcome_observation_end''',
        'retained_service_rows_not_recipient_openings': 'SELECT count(*) FROM eta_notification_delivery WHERE received_or_opened_at IS NOT NULL',
        'known_delay_requires_confirmed_followup': '''SELECT count(*) FROM eta_action_followup WHERE
            (observed_delay_seconds IS NULL)<>(original_promise_delay_seconds IS NULL)
            OR (observed_delay_seconds IS NOT NULL AND review_outcome<>'confirmed')''',
    }
    checks = []
    for name, sql in queries.items():
        n = con.execute(sql).fetchone()[0]
        _require(n == 0, 'P09 SQL invariant failed: ' + name)
        checks.append(dict(name=name, violations=n, passed=True))
    return checks


def _claims(counts, checks):
    return dict(status='passed', store_version=VERSION, scenario_id='S00_unmodified',
        parent_commit=PARENT_COMMIT, canonical_tables=list(parent.CANONICAL) + list(PROMOTED),
        reserved_empty_tables=[],
        preserved_parent_tables=list(PRESERVED), auxiliary_tables=list(AUX), counts=counts, checks=checks,
        foreign_keys_enforced=True, parquet_roundtrip_verified=True, release_mode='offline_replay',
        synthetic_workflow_simulated=True, historical_service_rows_unchanged=True,
        real_notifications_sent=False, real_recipient_behaviour_observed=False, causal_benefit_estimated=False,
        quality_accepted=False, operational_approval=False)


def _logical(value, kind):
    if kind == 'JSON':
        parsed = json.loads(value) if isinstance(value, str) else value
        return json.dumps(parsed, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return base._logical(value, kind)


def write_store(root, destination, tables, settings):
    """Publish fresh JSONL, DuckDB and Parquet atomically after validation."""
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Use a fresh P09 store directory; retained evidence is never overwritten')
    schema = _schema(root, settings)
    _validate(root, tables, schema, settings)
    statements = _ddl(schema)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.p09-store-', dir=destination.parent) as temporary:
        stage = Path(temporary) / 'complete'; (stage / 'jsonl').mkdir(parents=True); (stage / 'parquet').mkdir()
        con = duckdb.connect(str(stage / 'experiment.duckdb'))
        try:
            con.execute("SET TimeZone='UTC'"); con.execute('BEGIN')
            for name, statement in zip(ORDER, statements):
                con.execute(statement)
                rows = [[json.dumps(row[f], sort_keys=True, allow_nan=False) if kind == 'JSON' else row[f]
                         for f, kind, _ in schema[name]] for row in tables[name]]
                if rows:
                    con.executemany('INSERT INTO ' + name + ' VALUES (' + ','.join('?' for _ in schema[name]) + ')', rows)
                write_jsonl(stage / 'jsonl' / (name + '.jsonl'), tables[name])
            checks = _sql_checks(con); con.execute('COMMIT'); counts = {}
            for name in ORDER:
                counts[name] = con.execute('SELECT count(*) FROM ' + name).fetchone()[0]
                path = str(stage / 'parquet' / (name + '.parquet'))
                con.execute('COPY (SELECT * FROM ' + name + ' ORDER BY ' + ','.join(KEYS[name]) + ') TO ? (FORMAT PARQUET,COMPRESSION ZSTD)', [path])
                delta = con.execute('SELECT count(*) FROM ((SELECT * FROM ' + name + ' EXCEPT ALL SELECT * FROM read_parquet(?)) UNION ALL (SELECT * FROM read_parquet(?) EXCEPT ALL SELECT * FROM ' + name + '))', [path, path]).fetchone()[0]
                _require(delta == 0, 'P09 Parquet roundtrip differs: ' + name)
            checks.append(dict(name='parquet_roundtrip_all_tables', violations=0, passed=True))
        finally:
            con.close()
        (stage / 'schema.sql').write_text('\n\n'.join(statements) + '\n')
        receipt = {**_claims(counts, checks), 'engine': 'DuckDB', 'engine_version': duckdb.__version__,
                   'output_sha256': {str(p.relative_to(stage)): digest_file(p) for p in sorted(stage.rglob('*')) if p.is_file()}}
        (stage / 'verification.json').write_text(json.dumps(receipt, sort_keys=True, indent=2) + '\n')
        if destination.exists() or destination.is_symlink():
            raise FileExistsError('P09 store destination appeared during execution')
        stage.rename(destination)
    return receipt


def verify_store(root, destination, expected_tables, settings):
    """Verify exact inventory, hashes, types, constraints and logical rows read-only."""
    destination = Path(destination)
    schema = _schema(root, settings); _validate(root, expected_tables, schema, settings)
    receipt = json.loads((destination / 'verification.json').read_text())
    paths = {'experiment.duckdb', 'schema.sql'} | {folder + '/' + name + '.' + extension
        for folder, extension in (('jsonl', 'jsonl'), ('parquet', 'parquet')) for name in ORDER}
    actual = {str(p.relative_to(destination)) for p in destination.rglob('*') if p.is_file()}
    _require(not any(p.is_symlink() for p in destination.rglob('*')), 'P09 evidence contains a symlink')
    _require(actual == paths | {'verification.json'} and set(receipt.get('output_sha256', {})) == paths,
             'P09 store file inventory differs')
    for name, sha in receipt['output_sha256'].items():
        _require(digest_file(destination / name) == sha, 'P09 store file hash changed: ' + name)
    statements = _ddl(schema)
    _require((destination / 'schema.sql').read_text() == '\n\n'.join(statements) + '\n', 'P09 physical schema differs')
    reference = duckdb.connect(':memory:')
    try:
        for sql in statements: reference.execute(sql)
        constraints = _constraint_signatures(reference)
    finally:
        reference.close()
    con = duckdb.connect(str(destination / 'experiment.duckdb'), read_only=True)
    counts, compared = {}, 0
    try:
        con.execute("SET TimeZone='UTC'")
        _require({r[0] for r in con.execute('SHOW TABLES').fetchall()} == set(ORDER)
                 and _constraint_signatures(con) == constraints, 'P09 physical constraints or table inventory differ')
        for name in ORDER:
            actual_schema = [(r[0], r[1].replace('TIMESTAMP WITH TIME ZONE', 'TIMESTAMPTZ'), r[2] == 'YES')
                             for r in con.execute('DESCRIBE ' + name).fetchall()]
            _require(actual_schema == schema[name], 'P09 DuckDB schema differs: ' + name)
            expected = sorted(expected_tables[name], key=lambda r: tuple(r[k] for k in KEYS[name]))
            _require(digest(read_jsonl(destination / 'jsonl' / (name + '.jsonl'))) == digest(expected),
                     'P09 JSONL differs from reconstructed rows: ' + name)
            desired = [tuple(_logical(row[f], kind) for f, kind, _ in schema[name]) for row in expected]
            projection = ','.join('CAST(' + f + ' AS VARCHAR) AS ' + f if kind == 'TIMESTAMPTZ' else f for f, kind, _ in schema[name])
            order = ' ORDER BY ' + ','.join(KEYS[name]); path = str(destination / 'parquet' / (name + '.parquet'))
            database = con.execute('SELECT ' + projection + ' FROM ' + name + order).fetchall()
            parquet = con.execute('SELECT ' + projection + ' FROM read_parquet(?)' + order, [path]).fetchall()
            kinds = [(r[0], r[1].replace('TIMESTAMP WITH TIME ZONE', 'TIMESTAMPTZ'))
                     for r in con.execute('DESCRIBE SELECT * FROM read_parquet(?)', [path]).fetchall()]
            _require(kinds == [(f, k) for f, k, _ in schema[name]], 'P09 Parquet schema differs: ' + name)
            for records in (database, parquet):
                normalized = [tuple(_logical(v, kind) for v, (_, kind, _) in zip(row, schema[name])) for row in records]
                _require(normalized == desired, 'P09 typed logical rows differ: ' + name)
            counts[name] = len(expected); compared += 3 * len(expected)
        checks = _sql_checks(con); checks.append(dict(name='parquet_roundtrip_all_tables', violations=0, passed=True))
    finally:
        con.close()
    claims = _claims(counts, checks)
    _require({k: receipt.get(k) for k in claims} == claims, 'P09 store verification claims differ')
    return {**claims, 'differences': 0, 'logical_rows_compared': compared, 'read_only': True,
            'foreign_keys_verified': True}
