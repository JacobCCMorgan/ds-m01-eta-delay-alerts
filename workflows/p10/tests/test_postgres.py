"""Pure adapter regressions; native SQL probes run in postgres.check itself."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from workflows.p10 import postgres


ROOT = Path(__file__).resolve().parents[3]


class NativeTypeTests(unittest.TestCase):
    def test_exact_decimal_is_not_quantized_to_declared_scale(self):
        self.assertNotEqual(postgres.normalize('12.1234', 'DECIMAL(12,3)'),
                            postgres.normalize(Decimal('12.123'), 'DECIMAL(12,3)'))
        self.assertEqual(postgres.normalize(Decimal('12.1200'), 'DECIMAL(12,3)'), '12.12')
        value = '1234567890123456789012345678901234567890.12345678901234567890'
        self.assertEqual(postgres.normalize(Decimal(value), 'DECIMAL(60,20)'), value[:-1])

    def test_double_retains_one_ulp_and_signed_zero_differences(self):
        self.assertNotEqual(postgres.normalize(0.1, 'DOUBLE'),
                            postgres.normalize(0.10000000000000002, 'DOUBLE'))
        self.assertNotEqual(postgres.normalize(-0.0, 'DOUBLE'), postgres.normalize(0.0, 'DOUBLE'))

    def test_numeric_boolean_fraction_and_nonfinite_are_rejected(self):
        for value, kind in [(True, 'DOUBLE'), (False, 'INTEGER'), (True, 'DECIMAL(12,3)'),
                            (1.1, 'INTEGER'), (float('inf'), 'DOUBLE'),
                            (float('nan'), 'DOUBLE'), ('NaN', 'DECIMAL(12,3)')]:
            with self.subTest(value=value, kind=kind), self.assertRaises(ValueError):
                postgres.normalize(value, kind)

    def test_timestamp_normalizes_timezone_but_preserves_microseconds(self):
        self.assertEqual(postgres.normalize('2026-11-14T01:02:03.123456-08:00', 'TIMESTAMPTZ'),
                         '2026-11-14T09:02:03.123456Z')
        with self.assertRaises(ValueError):
            postgres.normalize('2026-11-14T01:02:03', 'TIMESTAMPTZ')
        with self.assertRaisesRegex(ValueError, 'Sub-microsecond'):
            postgres.normalize('2026-11-14T01:02:03.1234567Z', 'TIMESTAMPTZ')
        self.assertEqual(postgres.normalize('2026-11-14T01:02:03.123456000Z', 'TIMESTAMPTZ'),
                         '2026-11-14T01:02:03.123456Z')

    def test_json_distinguishes_boolean_integer_float_and_rejects_nan(self):
        self.assertNotEqual(postgres.normalize({'x': True}, 'JSON'), postgres.normalize({'x': 1}, 'JSON'))
        self.assertNotEqual(postgres.normalize({'x': 1.0}, 'JSON'), postgres.normalize({'x': 1}, 'JSON'))
        with self.assertRaises(ValueError):
            postgres.normalize({'x': float('nan')}, 'JSON')

    def test_decimal_export_is_exact_numeric_json_not_a_string(self):
        result = postgres.json_text({'value': Decimal('123456789.123456789123456789')})
        self.assertEqual(json.loads(result, parse_float=Decimal)['value'], Decimal('123456789.123456789123456789'))
        self.assertNotIn('"123', result)

    def test_boolean_and_text_fields_are_not_coerced(self):
        for value, kind in [('false', 'BOOLEAN'), (0, 'BOOLEAN'), (123, 'VARCHAR')]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                postgres.normalize(value, kind)

    def test_unsafe_identifiers_and_type_sql_are_rejected(self):
        for name in ('x;DROP TABLE y', 'A', 'x.y', 'x"', 'x' * 64):
            with self.subTest(name=name), self.assertRaises(ValueError):
                postgres.quote(name)
        for kind in ('TEXT;DROP SCHEMA public', 'DECIMAL(1,3)', 'DECIMAL(1001,0)', 'FLOAT'):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                postgres.pg_type(kind)


class TypedInventoryTests(unittest.TestCase):
    def test_row_shape_nullability_and_duplicate_keys_are_strict(self):
        fields = [('id', 'VARCHAR', False), ('value', 'INTEGER', True)]
        for rows in ([{'id': 'a'}], [{'id': 'a', 'value': 1, 'extra': 2}],
                     [{'id': None, 'value': 1}], [{'id': 'a', 'value': 1}, {'id': 'a', 'value': 2}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                postgres.normal_rows(rows, fields, ('id',))
        self.assertEqual(postgres.normal_rows([{'id': 'a', 'value': None}], fields, ('id',)),
                         [{'id': 'a', 'value': None}])

    def test_fixed_parent_ddl_parser_retains_nested_checks(self):
        ddl = 'CREATE TABLE x ("id" VARCHAR NOT NULL,PRIMARY KEY (id),UNIQUE (id),CHECK (greatest(1,2)=2),CHECK (id IN (\'a\',\'b\')));'
        self.assertEqual(postgres._split_clauses(ddl)[-2:],
                         ['CHECK (greatest(1,2)=2)', "CHECK (id IN ('a','b'))"])

    def test_scopes_are_separate_and_all_parent_foreign_keys_are_retained(self):
        specs = postgres.specifications(ROOT)
        self.assertEqual(len(specs['operational']), 11)
        self.assertEqual(len(specs['p09']), 35)
        self.assertEqual(sum(len(t['fks']) for t in specs['operational'].values()), 25)
        self.assertEqual(sum(len(t['fks']) for t in specs['p09'].values()), 59)
        self.assertEqual(sum(len(t['checks']) for t in specs['p09'].values()), 34)
        self.assertEqual(specs['p09']['eta_action_followup']['fks'], postgres.p09.FKS['eta_action_followup'])
        self.assertNotIn('telemetry_event', specs['operational'])
        self.assertNotIn('eta_history_reference', specs['operational'])

    def test_actual_native_values_export_without_decimal_or_clock_loss(self):
        specs = {'sample': dict(fields=[('id', 'VARCHAR', False), ('amount', 'DECIMAL(12,3)', False),
            ('clock', 'TIMESTAMPTZ', False), ('number', 'DOUBLE', False)], keys=('id',))}
        rows = {'sample': [dict(id='a', amount=Decimal('123.450'),
            clock=datetime(2026, 11, 14, 9, 2, 3, 123456, tzinfo=timezone.utc), number=0.10000000000000002)]}
        with tempfile.TemporaryDirectory() as directory:
            report = postgres._export(Path(directory) / 'out', rows, specs)
            path = Path(directory) / 'out/data/raw/sample.jsonl'
            value = json.loads(path.read_text())
            self.assertEqual(value['amount'], 123.45)
            self.assertEqual(value['clock'], '2026-11-14T09:02:03.123456Z')
            self.assertEqual(value['number'], 0.10000000000000002)
            self.assertEqual(report['data/raw/sample.jsonl']['rows'], 1)
            with self.assertRaises(FileExistsError):
                postgres._export(Path(directory) / 'out', rows, specs)

    def test_cross_tenant_witness_is_an_existing_foreign_parent_not_an_unknown_id(self):
        edge = dict(columns=['tenant_id', 'parent_id'], referenced_columns=['tenant_id', 'id'])
        child = dict(tenant_id='a', parent_id='same')
        parents = [dict(tenant_id='a', id='same'), dict(tenant_id='b', id='same'), dict(tenant_id='b', id='other')]
        self.assertEqual(postgres._cross_tenant_witness([child], parents, edge), (child, parents[-1]))
        self.assertIsNone(postgres._cross_tenant_witness([child], parents[:-1], edge))


if __name__ == '__main__':
    unittest.main()
