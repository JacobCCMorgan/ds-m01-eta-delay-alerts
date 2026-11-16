"""Native PostgreSQL 17 boundary checks for 11 sources and 35 P09 tables.

The caller supplies a newly owned local cluster.  Two unique schemas are
committed, read from a new connection, and removed.  This is a bounded adapter
check, not a production migration or the complete 28-table M2 warehouse.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import uuid

from ds_m01 import postgres_check as original
from workflows.p09 import store as p09


VERSION = "p10-native-postgres-v1"
KEYS = original.TABLE_KEYS
OPERATIONAL_FKS = {
    table: [(('tenant_id',), 'core_tenant', ('tenant_id',))]
    for table in KEYS if table != 'core_tenant'
}
OPERATIONAL_FKS.update({
    'core_vehicle_device_assignment': [
        (('tenant_id',), 'core_tenant', ('tenant_id',)),
        (('tenant_id', 'vehicle_id'), 'core_vehicle', ('tenant_id', 'vehicle_id')),
        (('tenant_id', 'device_id'), 'core_device', ('tenant_id', 'device_id'))],
    'eta_route_version': [
        (('tenant_id',), 'core_tenant', ('tenant_id',)),
        (('tenant_id', 'origin_facility_version_id'), 'eta_facility_version', ('tenant_id', 'facility_version_id')),
        (('tenant_id', 'destination_facility_version_id'), 'eta_facility_version', ('tenant_id', 'facility_version_id'))],
    'eta_trip_plan_version': [
        (('tenant_id',), 'core_tenant', ('tenant_id',)),
        (('tenant_id', 'trip_id'), 'eta_trip', ('tenant_id', 'trip_id')),
        (('tenant_id', 'vehicle_id'), 'core_vehicle', ('tenant_id', 'vehicle_id')),
        (('tenant_id', 'route_version_id'), 'eta_route_version', ('tenant_id', 'route_version_id')),
        (('tenant_id', 'origin_facility_version_id'), 'eta_facility_version', ('tenant_id', 'facility_version_id')),
        (('tenant_id', 'destination_facility_version_id'), 'eta_facility_version', ('tenant_id', 'facility_version_id')),
        (('tenant_id', 'supersedes_trip_version_id'), 'eta_trip_plan_version', ('tenant_id', 'trip_version_id'))],
    'eta_promise_version': [
        (('tenant_id',), 'core_tenant', ('tenant_id',)),
        (('tenant_id', 'trip_id'), 'eta_trip', ('tenant_id', 'trip_id')),
        (('tenant_id', 'supersedes_promise_version_id'), 'eta_promise_version', ('tenant_id', 'promise_version_id'))],
    'eta_trip_event': [
        (('tenant_id',), 'core_tenant', ('tenant_id',)),
        (('tenant_id', 'trip_id'), 'eta_trip', ('tenant_id', 'trip_id'))],
    'eta_trip_outcome': [
        (('tenant_id',), 'core_tenant', ('tenant_id',)),
        (('tenant_id', 'trip_id'), 'eta_trip', ('tenant_id', 'trip_id')),
        (('tenant_id', 'supersedes_outcome_version_id'), 'eta_trip_outcome', ('tenant_id', 'outcome_version_id'))],
})
OPERATIONAL_CHECKS = {
    'eta_promise_version': ['window_start_at<=window_end_at'],
    'eta_route_version': ['planned_distance_km>=0'],
    'eta_facility_version': ['latitude_deg BETWEEN -90 AND 90', 'longitude_deg BETWEEN -180 AND 180'],
}


def require(value, message):
    if not value:
        raise ValueError(message)


def quote(name):
    require(isinstance(name, str) and re.fullmatch(r'[a-z_][a-z_0-9]{0,62}', name), 'Unsafe SQL identifier')
    return '"' + name + '"'


def pg_type(kind):
    mapped = {'VARCHAR': 'TEXT', 'TIMESTAMPTZ': 'TIMESTAMPTZ', 'INTEGER': 'INTEGER',
              'BIGINT': 'BIGINT', 'DOUBLE': 'DOUBLE PRECISION', 'BOOLEAN': 'BOOLEAN',
              'JSON': 'JSONB', 'DATE': 'DATE'}
    if kind in mapped:
        return mapped[kind]
    match = re.fullmatch(r'DECIMAL\(([1-9][0-9]*),([0-9]+)\)', kind)
    require(match and 0 <= int(match[2]) <= int(match[1]) <= 1000, 'Unsupported PostgreSQL contract type')
    return 'NUMERIC(' + match[1] + ',' + match[2] + ')'


def json_text(value):
    """Emit Decimal as an exact JSON number, including SELECT NUMERIC values."""
    if isinstance(value, Decimal):
        require(value.is_finite(), 'Non-finite decimal')
        return format(value, 'f')
    if isinstance(value, dict):
        require(all(isinstance(k, str) for k in value), 'JSON object keys must be strings')
        return '{' + ','.join(json.dumps(k, ensure_ascii=False) + ':' + json_text(value[k]) for k in sorted(value)) + '}'
    if isinstance(value, (list, tuple)):
        return '[' + ','.join(map(json_text, value)) + ']'
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def normalize(value, kind):
    if value is None:
        return None
    if kind == 'TIMESTAMPTZ':
        require(isinstance(value, (str, datetime)), 'Invalid timestamp type')
        if isinstance(value, str):
            fraction = re.search(r'[T ]\d{2}:\d{2}:\d{2}[.,]([0-9]+)', value)
            require(fraction is None or not any(c != '0' for c in fraction[1][6:]),
                    'Sub-microsecond timestamp precision cannot be preserved')
        return original._timestamp(value)
    if kind.startswith('DECIMAL'):
        require(not isinstance(value, bool), 'Boolean is not a decimal')
        number = Decimal(str(value))
        require(number.is_finite(), 'Non-finite decimal')
        # Never round to the declared scale: a lossy PG cast must fail equality.
        rendered = format(number, 'f')
        return rendered.rstrip('0').rstrip('.') if '.' in rendered else rendered
    if kind == 'DOUBLE':
        require(not isinstance(value, bool), 'Boolean is not a double')
        number = float(value)
        require(math.isfinite(number), 'Non-finite double')
        return format(number, '.17g')
    if kind in ('INTEGER', 'BIGINT'):
        require(not isinstance(value, bool), 'Boolean is not an integer')
        number = Decimal(str(value))
        require(number.is_finite() and number == number.to_integral_value(), 'Fractional integer')
        return int(number)
    if kind == 'BOOLEAN':
        require(isinstance(value, bool), 'Boolean column requires a JSON boolean')
        return value
    if kind == 'JSON':
        parsed = json.loads(value) if isinstance(value, str) else value
        return json_text(parsed)
    if kind == 'DATE':
        require(not isinstance(value, datetime), 'DATE cannot contain a timestamp')
        return value.isoformat() if isinstance(value, date) else date.fromisoformat(value).isoformat()
    require(kind == 'VARCHAR' and isinstance(value, str), 'Text column requires a string')
    return value


def normal_rows(rows, fields, keys):
    names = {f[0] for f in fields}
    result, seen = [], set()
    for row in rows:
        require(set(row) == names, 'Typed row column inventory differs')
        value = {}
        for name, kind, nullable in fields:
            require(row[name] is not None or nullable, 'NULL in a required field: ' + name)
            value[name] = normalize(row[name], kind)
        key = tuple(value[k] for k in keys)
        require(None not in key and key not in seen, 'Duplicate or NULL primary key')
        seen.add(key)
        result.append(value)
    return sorted(result, key=lambda row: tuple(str(row[k]) for k in keys))


def digest(value):
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def _split_clauses(ddl):
    """Parse the fixed generated P09 CREATE TABLE body, not arbitrary SQL."""
    body = ddl[ddl.index('(') + 1:ddl.rindex(')')]
    clauses, start, depth, quoted = [], 0, 0, None
    for i, char in enumerate(body):
        if quoted:
            if char == quoted:
                quoted = None
        elif char in ('"', "'"):
            quoted = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif char == ',' and not depth:
            clauses.append(body[start:i].strip()); start = i + 1
    require(not depth and quoted is None, 'Unbalanced parent DDL')
    clauses.append(body[start:].strip())
    return clauses


def specifications(root):
    operational = {}
    for table, fields in original._fields(root).items():
        operational[table] = dict(fields=[(f['name'], f['type'], original._bool(f['nullable'])) for f in fields],
            keys=KEYS[table], fks=OPERATIONAL_FKS.get(table, []), uniques=[],
            checks=OPERATIONAL_CHECKS.get(table, []))
    schema = p09._schema(root, {})
    workflow = {}
    for table, ddl in zip(p09.ORDER, p09._ddl(schema), strict=True):
        clauses = _split_clauses(ddl)
        workflow[table] = dict(fields=schema[table], keys=p09.KEYS[table],
            fks=(p09.parent.FKS if table in p09.PRESERVED else p09.FKS).get(table, []),
            uniques=[tuple(x.strip() for x in c[8:-1].split(',')) for c in clauses if c.startswith('UNIQUE (')],
            checks=[c[7:-1] for c in clauses if c.startswith('CHECK (')])
    for group in (operational, workflow):
        for table, spec in group.items():
            quote(table)
            for name, kind, nullable in spec['fields']:
                quote(name); pg_type(kind)
                require(isinstance(nullable, bool), 'Invalid nullability contract')
            names = {f[0] for f in spec['fields']}
            require(set(spec['keys']) <= names, 'Invalid primary key')
            for columns, other, refs in spec['fks']:
                require(other in group and len(columns) == len(refs), 'Foreign key leaves its scope')
                require(set(columns) <= names and set(refs) <= {f[0] for f in group[other]['fields']}, 'Invalid foreign key')
    require(len(operational) == 11 and len(workflow) == 35, 'Bounded table inventory changed')
    return {'operational': operational, 'p09': workflow}


def _read_rows(path):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    require(rows, 'Required native table has no rows: ' + path.name)
    return rows


def _connection(dsn):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    require(psycopg.__version__ == '3.2.10', 'P10 requires psycopg 3.2.10')
    require(isinstance(dsn, str) and bool(dsn.strip()), 'Explicit owned-cluster connection required')
    config = conninfo_to_dict(dsn)
    require(config.get('host') in ('127.0.0.1', '::1'), 'Only explicit loopback TCP clusters are allowed')
    require(not config.get('service') and config.get('hostaddr') in (None, '', config['host']),
            'Connection indirection is not allowed')
    conn = psycopg.connect(dsn, connect_timeout=10)
    try:
        require(170000 <= conn.info.server_version < 180000, 'P10 requires PostgreSQL 17')
        conn.execute("SET TIME ZONE 'UTC'")
        conn.execute("SET statement_timeout='120s'")
        conn.execute("SET lock_timeout='10s'")
        conn.commit()
        return conn
    except BaseException:
        conn.close()
        raise


def _adapt(value, kind):
    from psycopg.types.json import Jsonb
    if value is None:
        return None
    if kind == 'JSON':
        return Jsonb(json.loads(value) if isinstance(value, str) else value, dumps=json_text)
    if kind.startswith('DECIMAL'):
        return Decimal(str(value))
    if kind == 'DOUBLE':
        return float(value)
    if kind in ('INTEGER', 'BIGINT'):
        return int(value)
    return value


def _create(conn, schema, specs):
    conn.execute('CREATE SCHEMA ' + quote(schema))
    for table, spec in specs.items():
        columns = [quote(n) + ' ' + pg_type(k) + ('' if null else ' NOT NULL') for n, k, null in spec['fields']]
        columns += ['PRIMARY KEY (' + ','.join(map(quote, spec['keys'])) + ')']
        columns += ['UNIQUE (' + ','.join(map(quote, cols)) + ')' for cols in spec['uniques']]
        columns += ['CHECK (' + expression + ')' for expression in spec['checks']]
        conn.execute('CREATE TABLE ' + quote(schema) + '.' + quote(table) + ' (' + ','.join(columns) + ')')


def _insert(conn, schema, table, spec, rows, *, repeat=False):
    names = [f[0] for f in spec['fields']]
    query = 'INSERT INTO ' + quote(schema) + '.' + quote(table) + ' (' + ','.join(map(quote, names)) + ') VALUES (' + ','.join(['%s'] * len(names)) + ')'
    if repeat:
        query += ' ON CONFLICT (' + ','.join(map(quote, spec['keys'])) + ') DO NOTHING'
    with conn.cursor() as cur:
        cur.executemany(query, [tuple(_adapt(row[n], kind) for n, kind, _ in spec['fields']) for row in rows])


def _foreign_keys(conn, schema, specs):
    for table, spec in specs.items():
        for i, (columns, other, refs) in enumerate(spec['fks']):
            conn.execute('ALTER TABLE ' + quote(schema) + '.' + quote(table) + ' ADD CONSTRAINT ' + quote('fk_' + str(i)) +
                ' FOREIGN KEY (' + ','.join(map(quote, columns)) + ') REFERENCES ' + quote(schema) + '.' + quote(other) +
                ' (' + ','.join(map(quote, refs)) + ')')


def _select(conn, schema, table, spec):
    names = [f[0] for f in spec['fields']]
    return [dict(zip(names, row, strict=True)) for row in conn.execute('SELECT ' + ','.join(map(quote, names)) +
        ' FROM ' + quote(schema) + '.' + quote(table)).fetchall()]


def _compare(conn, schema, specs, sources):
    returned, report = {}, {}
    for table, spec in specs.items():
        returned[table] = _select(conn, schema, table, spec)
        before = normal_rows(sources[table], spec['fields'], spec['keys'])
        after = normal_rows(returned[table], spec['fields'], spec['keys'])
        require(before == after, 'PostgreSQL typed row mismatch: ' + table)
        report[table] = dict(input_rows=len(before), returned_rows=len(after),
            input_normalized_sha256=digest(before), returned_normalized_sha256=digest(after), exact=True)
    return returned, report


def _catalog(conn, schema, specs):
    fields = conn.execute('''SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY c.relname,a.attnum''', (schema,)).fetchall()
    aliases = {'TEXT': 'text', 'TIMESTAMPTZ': 'timestamp with time zone', 'INTEGER': 'integer',
        'BIGINT': 'bigint', 'DOUBLE PRECISION': 'double precision', 'BOOLEAN': 'boolean', 'JSONB': 'jsonb', 'DATE': 'date'}
    expected = []
    for table in sorted(specs):
        for name, kind, nullable in specs[table]['fields']:
            typ = pg_type(kind)
            expected.append((table, name, aliases.get(typ, typ.lower()), not nullable or name in specs[table]['keys']))
    require(fields == expected, 'Native PostgreSQL column catalog differs')
    constraints = conn.execute('''SELECT c.relname,k.conname,k.contype,k.convalidated,
        ARRAY(SELECT a.attname FROM unnest(k.conkey) WITH ORDINALITY x(id,ord)
              JOIN pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=x.id ORDER BY x.ord),
        p.relname, ARRAY(SELECT a.attname FROM unnest(k.confkey) WITH ORDINALITY x(id,ord)
              JOIN pg_attribute a ON a.attrelid=k.confrelid AND a.attnum=x.id ORDER BY x.ord),
        pg_get_constraintdef(k.oid)
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_class p ON p.oid=k.confrelid WHERE n.nspname=%s ORDER BY c.relname,k.conname''', (schema,)).fetchall()
    for table, spec in specs.items():
        actual = [r for r in constraints if r[0] == table]
        require(all(r[3] for r in actual), 'Unvalidated native constraint')
        require([tuple(r[4]) for r in actual if r[2] == 'p'] == [tuple(spec['keys'])], 'Primary key catalog differs')
        require(Counter((tuple(r[4]), r[5], tuple(r[6])) for r in actual if r[2] == 'f') ==
                Counter((tuple(a), b, tuple(c)) for a, b, c in spec['fks']), 'Foreign key catalog differs')
        require(Counter(tuple(r[4]) for r in actual if r[2] == 'u') == Counter(spec['uniques']), 'Unique catalog differs')
        require(sum(r[2] == 'c' for r in actual) == len(spec['checks']), 'CHECK catalog count differs')
        if spec['checks']:
            # Let this server normalize the independently supplied expected
            # expressions.  Text heuristics would mishandle PG casts/quoting.
            conn.execute('SAVEPOINT p10_check_definition')
            try:
                reference = '_p10_expected_checks'
                columns = [quote(n) + ' ' + pg_type(k) for n, k, _ in spec['fields']]
                columns += ['CHECK (' + expression + ')' for expression in spec['checks']]
                conn.execute('CREATE TABLE ' + quote(schema) + '.' + quote(reference) + ' (' + ','.join(columns) + ')')
                expected_checks = conn.execute('''SELECT pg_get_constraintdef(k.oid) FROM pg_constraint k
                    JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname=%s AND c.relname=%s AND k.contype='c' ''', (schema, reference)).fetchall()
                require(Counter(r[7] for r in actual if r[2] == 'c') == Counter(r[0] for r in expected_checks),
                        'Native CHECK expression differs from the declared contract')
            finally:
                conn.execute('ROLLBACK TO SAVEPOINT p10_check_definition')
                conn.execute('RELEASE SAVEPOINT p10_check_definition')
    return dict(column_count=len(fields), counts=dict(Counter(r[2] for r in constraints)),
        check_definitions_exact=True,
        columns=[dict(table=t, column=n, type=k, not_null=v) for t, n, k, v in fields],
        constraints=[dict(table=r[0], name=r[1], kind=r[2], validated=r[3], columns=r[4],
                         referenced_table=r[5], referenced_columns=r[6], definition=r[7]) for r in constraints])


def _expect_error(conn, code, operation, *, constraint=None):
    import psycopg
    conn.execute('SAVEPOINT p10_negative')
    result = None
    try:
        operation()
    except psycopg.Error as exc:
        require(exc.sqlstate == code, 'Negative probe returned the wrong SQLSTATE')
        if constraint:
            require(exc.diag.constraint_name == constraint, 'Negative probe rejected by a different constraint')
        result = dict(sqlstate=exc.sqlstate, constraint=exc.diag.constraint_name, rejected=True)
    finally:
        conn.execute('ROLLBACK TO SAVEPOINT p10_negative')
        conn.execute('RELEASE SAVEPOINT p10_negative')
    require(result is not None, 'Invalid native write was accepted')
    return result


def _update(conn, schema, table, spec, row, field, value):
    where = ' AND '.join(quote(k) + '=%s' for k in spec['keys'])
    conn.execute('UPDATE ' + quote(schema) + '.' + quote(table) + ' SET ' + quote(field) + '=%s WHERE ' + where,
                 (value, *(row[k] for k in spec['keys'])))


def _isolate_fk(conn, schema, table, edge, all_fks):
    for other in all_fks:
        if other is edge:
            continue
        if other['table'] == table or other['referenced_table'] == table:
            conn.execute('ALTER TABLE ' + quote(schema) + '.' + quote(other['table']) +
                         ' DROP CONSTRAINT ' + quote(other['name']))


def _cross_tenant_witness(children, parents, edge):
    columns, refs = edge['columns'], edge['referenced_columns']
    if len(columns) < 2 or columns[0] != 'tenant_id' or refs[0] != 'tenant_id':
        return None
    parent_keys = {tuple(r[k] for k in refs) for r in parents}
    for child in children:
        for parent in parents:
            if child['tenant_id'] == parent['tenant_id']:
                continue
            foreign_key = (child['tenant_id'], *(parent[r] for r in refs[1:]))
            if foreign_key not in parent_keys:
                return child, parent
    return None


def _negative_probes(conn, schema, specs, sources, catalog):
    results = []
    all_fks = [r for r in catalog['constraints'] if r['kind'] == 'f']
    for table, spec in specs.items():
        row = sources[table][0]
        pk_name = next(x['name'] for x in catalog['constraints'] if x['table'] == table and x['kind'] == 'p')
        item = _expect_error(conn, '23505', lambda: _insert(conn, schema, table, spec, [row]), constraint=pk_name)
        results.append(dict(table=table, probe='duplicate_primary_key', **item))
        for field, _, nullable in spec['fields']:
            if nullable:
                continue
            item = _expect_error(conn, '23502', lambda f=field: _update(conn, schema, table, spec, row, f, None))
            results.append(dict(table=table, probe='not_null', field=field, **item))
        for fk in [x for x in all_fks if x['table'] == table]:
            # Isolate this existing native FK inside a savepoint so overlapping
            # tenant keys / incoming references cannot mask its own rejection.
            def violate(edge=fk):
                _isolate_fk(conn, schema, table, edge, all_fks)
                field = edge['columns'][-1]
                _update(conn, schema, table, spec, row, field, 'p10_absent_' + uuid.uuid4().hex)
            item = _expect_error(conn, '23503', violate, constraint=fk['name'])
            results.append(dict(table=table, probe='isolated_foreign_key', fields=fk['columns'], **item))
            witness = _cross_tenant_witness(sources[table], sources[fk['referenced_table']], fk)
            if witness is not None:
                child, parent = witness
                def cross_tenant():
                    _isolate_fk(conn, schema, table, fk, all_fks)
                    changes = list(zip(fk['columns'][1:], fk['referenced_columns'][1:], strict=True))
                    conn.execute('UPDATE ' + quote(schema) + '.' + quote(table) + ' SET ' +
                        ','.join(quote(c) + '=%s' for c, _ in changes) + ' WHERE ' +
                        ' AND '.join(quote(k) + '=%s' for k in spec['keys']),
                        (*(parent[r] for _, r in changes), *(child[k] for k in spec['keys'])))
                item = _expect_error(conn, '23503', cross_tenant, constraint=fk['name'])
                results.append(dict(table=table, probe='existing_other_tenant_parent',
                    fields=fk['columns'], referenced_table=fk['referenced_table'],
                    existing_parent_confirmed=True, child_tenant_unchanged=True, **item))
    # Explicit value-level checks complement full CHECK catalog inspection.
    samples = {
        'eta_promise_version': [('window_start_at', '2099-01-01T00:00:00Z')],
        'eta_route_version': [('planned_distance_km', -1)],
        'eta_facility_version': [('latitude_deg', 91), ('longitude_deg', 181)],
        'eta_model_version': [('nominal_interval_coverage', 1.5)],
        'eta_prediction': [('delay_probability', 1.1), ('remaining_eta_seconds', -1)],
        'eta_alert': [('alert_status', 'p10_invalid')],
        'eta_alert_episode': [('delivered_alert_count', -1)],
        'eta_notification_delivery': [('delivery_status', 'p10_invalid'), ('recipient_role', 'p10_invalid'),
                                      ('received_or_opened_at', '2026-01-01T00:00:00Z')],
        'eta_adoption': [('adoption_status', 'p10_invalid')],
        'eta_action': [('action_type', 'p10_invalid'), ('handling_seconds', -1)],
        'eta_action_followup': [('review_outcome', 'p10_invalid'), ('result_origin', 'p10_invalid'),
                                ('observed_delay_seconds', -1), ('original_promise_delay_seconds', -1)],
        'p09_run_audit': [('quality_accepted', True), ('operational_approval', True), ('scenario_id', 'p10_invalid')],
        'p09_provenance_reference': [('result_origin', 'p10_invalid')],
        'p09_promise_reference': [('window_start_at', '2099-01-01T00:00:00Z'), ('reference_origin', 'p10_invalid')],
        'p08_run_audit': [('quality_accepted', True), ('operational_approval', True), ('target_rows_read', True),
                          ('population_role', 'p10_invalid')],
    }
    for table in specs:
        if table.startswith('p08_delivery_'):
            samples[table] = [('result_origin', 'p10_invalid'), ('clock_domain', 'p10_invalid')]
    for table, changes in samples.items():
        if table not in specs:
            continue
        for field, value in changes:
            item = _expect_error(conn, '23514', lambda: _update(conn, schema, table, specs[table], sources[table][0], field, value))
            results.append(dict(table=table, probe='check_constraint', field=field, **item))
    malformed = {'INTEGER': ('not_an_integer', '22P02'), 'BIGINT': ('not_an_integer', '22P02'),
        'DOUBLE': ('not_a_float', '22P02'), 'BOOLEAN': ('not_a_boolean', '22P02'),
        'JSON': ('{invalid_json', '22P02'), 'TIMESTAMPTZ': ('not_a_timestamp', '22007'),
        'DATE': ('not_a_date', '22007')}
    tested_kinds = set()
    for table, spec in specs.items():
        for field, kind, _ in spec['fields']:
            if kind in tested_kinds:
                continue
            if kind.startswith('DECIMAL'):
                invalid, code = '1e1001', '22003'
            elif kind in malformed:
                invalid, code = malformed[kind]
            else:
                continue
            item = _expect_error(conn, code, lambda: _update(conn, schema, table, spec, sources[table][0], field, invalid))
            results.append(dict(table=table, probe='native_type_rejection', field=field, declared_type=kind, **item))
            tested_kinds.add(kind)
    expected_cross_tenant_tables = {'eta_trip_plan_version', 'eta_promise_version', 'eta_action', 'eta_action_followup'} & specs.keys()
    observed_cross_tenant_tables = {r['table'] for r in results if r['probe'] == 'existing_other_tenant_parent'}
    require(expected_cross_tenant_tables <= observed_cross_tenant_tables, 'Cross-tenant foreign-key witness missing')
    return results


def reload_exact(conn, schema, table, spec, rows):
    """Idempotent retry only; a changed payload under the same key is an error."""
    actual = normal_rows(_select(conn, schema, table, spec), spec['fields'], spec['keys'])
    require(actual == normal_rows(rows, spec['fields'], spec['keys']), 'Same-key payload or key inventory conflict')
    _insert(conn, schema, table, spec, rows, repeat=True)


def _revision_evidence(conn, schema, specs):
    definitions = [('eta_trip_plan_version', 'trip_id', 'trip_version_id'),
        ('eta_promise_version', 'trip_id', 'promise_version_id'),
        ('eta_trip_outcome', 'trip_id', 'outcome_version_id'),
        ('p09_promise_reference', 'trip_id', 'promise_version_id')]
    result = {}
    for table, entity, version in definitions:
        if table not in specs:
            continue
        rows = conn.execute('SELECT tenant_id,' + quote(entity) + ',count(*),count(DISTINCT ' + quote(version) +
            ') FROM ' + quote(schema) + '.' + quote(table) + ' GROUP BY tenant_id,' + quote(entity) + ' HAVING count(*)>1').fetchall()
        require(all(r[2] == r[3] for r in rows), 'Duplicate source revision identifiers: ' + table)
        result[table] = dict(entities_with_multiple_versions=len(rows), retained_version_rows=sum(r[2] for r in rows),
                             duplicate_version_ids=0, older_versions_preserved=True)
        if table != 'p09_promise_reference':
            # A bounded native storage probe supplies a second version even if
            # this frozen synthetic source happens to contain no revisions.
            # It is rolled back and never enters evaluation or exported data.
            before = _select(conn, schema, table, specs[table])
            revision = dict(before[0])
            original_version = revision[version]
            revision[version] = 'p10_revision_' + uuid.uuid4().hex
            predecessor = {'eta_trip_plan_version': 'supersedes_trip_version_id',
                'eta_promise_version': 'supersedes_promise_version_id',
                'eta_trip_outcome': 'supersedes_outcome_version_id'}[table]
            revision[predecessor] = original_version
            conn.execute('SAVEPOINT p10_revision')
            try:
                _insert(conn, schema, table, specs[table], [revision])
                after = _select(conn, schema, table, specs[table])
                require(normal_rows(after, specs[table]['fields'], specs[table]['keys']) ==
                    normal_rows(before + [revision], specs[table]['fields'], specs[table]['keys']),
                    'Appending a version changed an existing record')
                result[table]['synthetic_append_probe'] = dict(new_version_inserted=True,
                    prior_rows_unchanged=True, predecessor_foreign_key_enforced=True, rolled_back=True)
            finally:
                conn.execute('ROLLBACK TO SAVEPOINT p10_revision')
                conn.execute('RELEASE SAVEPOINT p10_revision')
            require(normal_rows(_select(conn, schema, table, specs[table]), specs[table]['fields'], specs[table]['keys']) ==
                    normal_rows(before, specs[table]['fields'], specs[table]['keys']), 'Revision probe rollback changed source rows')
    return result


def _export(root, tables, specs):
    destination = Path(root) / 'data/raw'
    destination.mkdir(parents=True, exist_ok=False)
    report = {}
    for table, rows in tables.items():
        spec = specs[table]
        output = []
        for row in rows:
            converted = {}
            for field, kind, _ in spec['fields']:
                value = row[field]
                if value is not None and kind == 'TIMESTAMPTZ':
                    value = normalize(value, kind)
                elif value is not None and kind == 'DATE':
                    value = normalize(value, kind)
                converted[field] = value
            output.append(converted)
        output.sort(key=lambda row: tuple(str(row[k]) for k in spec['keys']))
        path = destination / (table + '.jsonl')
        path.write_text(''.join(json_text(row) + '\n' for row in output))
        # Export must preserve the just-observed native values as well.
        reread = [json.loads(line, parse_float=Decimal) for line in path.read_text().splitlines()]
        require(normal_rows(rows, spec['fields'], spec['keys']) == normal_rows(reread, spec['fields'], spec['keys']),
                'PostgreSQL export changed typed values')
        report['data/raw/' + path.name] = dict(rows=len(rows), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    return report


def check(root, dsn, *, operational_root, export_root=None):
    """Run native tests, commit/reconnect, export actual SELECT rows, clean up.

    Connection credentials are never returned.  Callers must additionally bind
    the fresh-cluster lifecycle and the immutable input inventory in their run.
    Any failure propagates; no partial result is labelled a pass.
    """
    root, operational_root = Path(root).resolve(), Path(operational_root).resolve()
    if export_root is not None:
        output = Path(export_root).resolve()
        require(not output.is_relative_to(root) and not output.is_relative_to(operational_root),
                'Native export must be outside the repository and source root')
    specs = specifications(root)
    sources = {
        'operational': {t: _read_rows(operational_root / 'data/raw' / (t + '.jsonl')) for t in specs['operational']},
        'p09': {t: _read_rows(root / 'workflows/p09/evidence/store/jsonl' / (t + '.jsonl')) for t in specs['p09']},
    }
    for group, tables in sources.items():
        for table, rows in tables.items():
            normal_rows(rows, specs[group][table]['fields'], specs[group][table]['keys'])
    names = {group: 'p10_' + group + '_' + uuid.uuid4().hex[:20] for group in specs}
    report = dict(version=VERSION, status='running', scope='11 operational source tables and 35 P09 typed tables; separate native schemas',
        started_at_utc=datetime.now(timezone.utc).isoformat(), credentials_recorded=False, original_evidence_modified=False,
        full_m2_warehouse_claimed=False, production_acceptance_claimed=False, groups={})
    conn = _connection(dsn)
    owned = []
    cleanup = False
    try:
        report['postgresql_version'] = conn.execute('SELECT version()').fetchone()[0]
        report['psycopg_version'] = '3.2.10'
        for group, schema in names.items():
            require(conn.execute('SELECT to_regnamespace(%s)', (schema,)).fetchone()[0] is None, 'Unique schema already exists')
            _create(conn, schema, specs[group]); owned.append(schema)
            for table, rows in sources[group].items():
                _insert(conn, schema, table, specs[group][table], rows)
            _foreign_keys(conn, schema, specs[group])
            _, before = _compare(conn, schema, specs[group], sources[group])
            catalog = _catalog(conn, schema, specs[group])
            probes = _negative_probes(conn, schema, specs[group], sources[group], catalog)
            for table, rows in sources[group].items():
                reload_exact(conn, schema, table, specs[group][table], rows)
            _, after = _compare(conn, schema, specs[group], sources[group])
            require(before == after, 'Failed probes or retries changed the stored rows')
            require(_catalog(conn, schema, specs[group]) == catalog, 'Probe rollback failed to restore native constraints')
            report['groups'][group] = dict(table_count=len(specs[group]), tables=after, catalog=catalog,
                negative_probes=probes, failed_writes_rolled_back=True, exact_retry_preserved=True,
                revision_retention=_revision_evidence(conn, schema, specs[group]))
        # Reject altered same-key rows before an ON CONFLICT retry could hide them.
        conflicting = [dict(row) for row in sources['operational']['core_tenant']]
        conflicting[0]['tenant_name'] += ' p10_changed_payload'
        rejected = False
        try:
            reload_exact(conn, names['operational'], 'core_tenant', specs['operational']['core_tenant'], conflicting)
        except ValueError as exc:
            require(str(exc) == 'Same-key payload or key inventory conflict', 'Wrong payload-conflict rejection')
            rejected = True
        require(rejected, 'Changed same-key payload was silently ignored')
        report['same_key_changed_payload_rejected'] = True
        with _connection(dsn) as observer:
            require(all(observer.execute('SELECT to_regnamespace(%s)', (schema,)).fetchone()[0] is None for schema in names.values()),
                    'Uncommitted schemas visible to a second connection')
        report['uncommitted_changes_hidden_from_second_connection'] = True
        conn.commit()
        report['commit_completed'] = True
        with _connection(dsn) as reader:
            for group, schema in names.items():
                returned, rows_report = _compare(reader, schema, specs[group], sources[group])
                require(rows_report == report['groups'][group]['tables'], 'Committed readback differs')
                _catalog(reader, schema, specs[group])
                if group == 'operational' and export_root is not None:
                    report['operational_export'] = _export(export_root, returned, specs[group])
            reader.execute('SET LOCAL search_path TO ' + quote(names['p09']) + ',pg_catalog')
            report['p09_native_sql_invariants'] = p09._sql_checks(reader)
        report['fresh_connection_exact_readback'] = True
    finally:
        conn.rollback()
        try:
            for schema in reversed(owned):
                # These exact names were successfully CREATEd by this call; no
                # caller-provided object names or pre-existing schemas are used.
                if conn.execute('SELECT to_regnamespace(%s)', (schema,)).fetchone()[0] is not None:
                    conn.execute('DROP SCHEMA ' + quote(schema) + ' CASCADE')
            conn.commit()
            require(all(conn.execute('SELECT to_regnamespace(%s)', (schema,)).fetchone()[0] is None for schema in owned),
                    'Native schema cleanup failed')
            cleanup = True
        finally:
            conn.close()
    report['owned_schema_cleanup_verified'] = cleanup
    report['status'] = 'passed'
    report['completed_at_utc'] = datetime.now(timezone.utc).isoformat()
    return report
