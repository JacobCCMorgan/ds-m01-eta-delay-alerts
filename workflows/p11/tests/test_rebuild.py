"""Historical evidence, full-scope gates, and real staged warehouse regressions."""
import copy
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import duckdb

from ds_m01.common import write_source_manifest
from ds_m01.generate import generate
from ds_m01.pipeline import build
from ds_m01.warehouse import build_warehouse as frozen_warehouse
from workflows.p11 import rebuild


ROOT = Path(__file__).resolve().parents[3]


class HistoricalOracleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo, self.source = self.root / 'repo', self.root / 'source'
        (self.repo / 'reports').mkdir(parents=True)
        records = []
        for index, name in enumerate(sorted(rebuild.EXPECTED_INPUT_PATHS)):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = (json.dumps({'fixture_identity': index}) + '\n').encode()
            path.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            records.append({'path': name, 'original_sha256': digest,
                            'rebuilt_sha256': digest, 'equal': True})
        self.receipt = {'status': 'passed', 'files': records}
        self.save()

    def save(self):
        (self.repo / 'reports/rebuild_report.json').write_text(json.dumps(self.receipt))

    def test_every_raw_and_processed_file_is_compared(self):
        result = rebuild.check_historical_files(self.repo, self.source)
        self.assertEqual(result['raw_files_compared'], 13)
        self.assertEqual(result['processed_files_compared'], 7)
        self.assertEqual(set(result['byte_exact_sha256']), rebuild.EXPECTED_INPUT_PATHS)

    def test_changed_history_bytes_are_rejected_even_when_counts_could_match(self):
        path = self.source / 'data/processed/eta_history_reference.jsonl'
        path.write_text('{"fixture_identity":-1}\n')
        with self.assertRaisesRegex(ValueError, 'bytes differ'):
            rebuild.check_historical_files(self.repo, self.source)

    def test_missing_or_extra_stream_is_rejected(self):
        path = self.source / 'data/raw/extra.jsonl'
        path.write_text('{}\n')
        with self.assertRaisesRegex(ValueError, 'inventory differs'):
            rebuild.check_historical_files(self.repo, self.source)
        path.unlink()
        (self.source / 'data/processed/feature_lineage.jsonl').unlink()
        with self.assertRaisesRegex(ValueError, 'inventory differs'):
            rebuild.check_historical_files(self.repo, self.source)

    def test_oracle_cannot_omit_or_duplicate_an_inconvenient_stream(self):
        original = copy.deepcopy(self.receipt)
        self.receipt['files'].pop()
        self.save()
        with self.assertRaisesRegex(ValueError, 'all 13 raw and 7 processed'):
            rebuild.check_historical_files(self.repo, self.source)
        self.receipt = original
        self.receipt['files'].append(self.receipt['files'][0])
        self.save()
        with self.assertRaisesRegex(ValueError, 'duplicate paths'):
            rebuild.check_historical_files(self.repo, self.source)

    def test_failed_historical_comparison_is_not_an_oracle(self):
        self.receipt['files'][0]['equal'] = False
        self.save()
        with self.assertRaisesRegex(ValueError, 'did not establish exact equality'):
            rebuild.check_historical_files(self.repo, self.source)


class RealWarehouseBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.fixture_temp.cleanup)
        cls.fixture = Path(cls.fixture_temp.name).resolve() / 'prepared'
        cls.fixture_config = json.loads((ROOT / 'config/m2.json').read_text())
        cls.fixture_config['sample_size'] = 120
        counts = generate(cls.fixture, cls.fixture_config)
        write_source_manifest(cls.fixture, counts)
        (cls.fixture / 'contracts').mkdir()
        shutil.copyfile(ROOT / 'contracts/fields.json', cls.fixture / 'contracts/fields.json')
        build(cls.fixture, cls.fixture_config)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source'
        self.stage = self.root / 'stage'
        self.stage.mkdir()
        self.config = copy.deepcopy(self.fixture_config)
        shutil.copytree(self.fixture, self.source)

    def test_original_materializer_emits_all_28_typed_relations_to_stage(self):
        metadata = rebuild.warehouse_builder(self.source, self.config)(self.stage)
        self.assertEqual(len(metadata['warehouse']['checks']), 27)
        self.assertTrue(all(c['passed'] for c in metadata['warehouse']['checks']))
        self.assertEqual({p.stem for p in (self.stage / 'parquet').glob('*.parquet')},
                         set(self.config['required_tables']))
        with duckdb.connect(str(self.stage / 'warehouse.duckdb'), read_only=True) as con:
            self.assertEqual(len(con.execute('SHOW TABLES').fetchall()), 28)
            self.assertEqual(con.execute('SELECT count(*) FROM eta_prediction').fetchone()[0], 0)
            self.assertGreater(con.execute('SELECT count(*) FROM eta_snapshot').fetchone()[0], 0)
        self.assertFalse((self.source / 'data' / self.config['database_filename']).exists())

    def test_database_fault_occurs_before_any_published_parquet(self):
        def fault(name):
            if name == 'after_database':
                raise RuntimeError(name)
        with self.assertRaisesRegex(RuntimeError, 'after_database'):
            rebuild.warehouse_builder(self.source, self.config, fault=fault)(self.stage)
        self.assertTrue((self.stage / 'warehouse.duckdb').exists())
        self.assertFalse((self.stage / 'parquet').exists())

    def test_first_parquet_fault_keeps_only_unpublished_staging_artifacts(self):
        def fault(name):
            if name == 'after_first_parquet':
                raise RuntimeError(name)
        with self.assertRaisesRegex(RuntimeError, 'after_first_parquet'):
            rebuild.warehouse_builder(self.source, self.config, fault=fault)(self.stage)
        self.assertEqual(len(list((self.stage / 'parquet').glob('*.parquet'))), 1)
        self.assertFalse((self.root / 'current.json').exists())

    def test_bounded_adapter_preserves_frozen_checks_and_all_typed_rows(self):
        reference = self.root / 'frozen_reference'
        shutil.copytree(self.fixture, reference)
        (reference / 'sql').mkdir()
        frozen = frozen_warehouse(reference, self.config)
        current = rebuild.warehouse_builder(self.source, self.config)(self.stage)['warehouse']
        self.assertEqual(current['checks'], frozen['checks'])
        self.assertEqual(current['counts'], frozen['counts'])
        self.assertEqual(current['table_status'], frozen['table_status'])
        with duckdb.connect(str(reference / 'data' / self.config['database_filename']), read_only=True) as left:
            with duckdb.connect(str(self.stage / 'warehouse.duckdb'), read_only=True) as right:
                for table in self.config['required_tables']:
                    with self.subTest(table=table):
                        schema = left.execute(f'DESCRIBE "{table}"').fetchall()
                        self.assertEqual(schema, right.execute(f'DESCRIBE "{table}"').fetchall())
                        columns = ','.join(f'epoch_us("{r[0]}") AS "{r[0]}"'
                                           if r[1] == 'TIMESTAMP WITH TIME ZONE' else f'"{r[0]}"'
                                           for r in schema)
                        left.execute(f'SELECT {columns} FROM "{table}" ORDER BY ALL')
                        right.execute(f'SELECT {columns} FROM "{table}" ORDER BY ALL')
                        self.assertEqual(left.description, right.description)
                        while True:
                            a, b = left.fetchmany(64), right.fetchmany(64)
                            self.assertEqual(a, b)
                            if not a:
                                break


class LosslessArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name).resolve()
        self.source = self.run / 'source'
        self.nonce = 'owned-test-run'
        (self.run / 'source_owner.json').write_text(json.dumps({'source': 'source', 'nonce': self.nonce}))
        self.originals = {}
        self.expected = {}
        for index, name in enumerate(sorted(rebuild.EXPECTED_INPUT_PATHS | {'validation/latent_truth.jsonl'})):
            data = (json.dumps({'stream': name, 'sequence': index}) + '\n').encode()
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self.originals[name] = data
            self.expected[name] = hashlib.sha256(data).hexdigest()

    def test_all_21_streams_restore_exact_bytes_before_plain_removal(self):
        result = rebuild._archive_owned_source(self.source, self.run, self.nonce, self.expected)
        self.assertEqual(len(result['files']), 21)
        self.assertTrue(result['all_archives_verified_before_any_plain_stream_removed'])
        self.assertTrue((self.source / 'jsonl_archive.json').is_file())
        for name, data in self.originals.items():
            self.assertFalse((self.source / name).exists())
            with gzip.open(self.source / result['files'][name]['archive_path'], 'rb') as stream:
                self.assertEqual(stream.read(), data)

    def test_late_archive_corruption_leaves_every_plain_original_intact(self):
        original_verify = rebuild._verify_gzip
        calls = 0
        def corrupt(path, expected_sha, expected_bytes):
            nonlocal calls
            calls += 1
            if calls == 10:
                Path(path).write_bytes(b'injected corrupt gzip')
            return original_verify(path, expected_sha, expected_bytes)
        with patch.object(rebuild, '_verify_gzip', side_effect=corrupt):
            with self.assertRaises((OSError, EOFError, ValueError)):
                rebuild._archive_owned_source(self.source, self.run, self.nonce, self.expected)
        self.assertEqual(calls, 10)
        self.assertFalse((self.source / 'jsonl_archive.json').exists())
        for name, data in self.originals.items():
            self.assertEqual((self.source / name).read_bytes(), data)

    def test_wrong_owner_or_changed_source_cannot_be_archived(self):
        with self.assertRaisesRegex(ValueError, 'ownership record differs'):
            rebuild._archive_owned_source(self.source, self.run, 'not-owner', self.expected)
        self.expected[sorted(self.expected)[0]] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'SHA changed'):
            rebuild._archive_owned_source(self.source, self.run, self.nonce, self.expected)
        self.assertTrue(all((self.source / name).exists() for name in self.originals))


class RepeatAndFailureTests(unittest.TestCase):
    def sample_result(self):
        return {'historical_oracle': {'byte_exact_sha256': {'data/processed/example': 'a'}},
                'generated_counts': {'eta_trip': 1800}, 'warehouse_counts': {'eta_trip': 1800},
                'tables': {f'table_{i}': {'row_count': 1, 'logical_sha256': 'a',
                           'columns': [{'name': 'tenant_id', 'type': 'VARCHAR', 'nullable': False}]}
                           for i in range(28)}}

    def test_repeat_requires_exact_schema_and_values_not_only_row_counts(self):
        first = self.sample_result()
        second = copy.deepcopy(first)
        self.assertEqual(rebuild.compare_runs(first, second)['canonical_relations_compared'], 28)
        second['tables']['table_2']['logical_sha256'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'logical digest differs'):
            rebuild.compare_runs(first, second)
        second = copy.deepcopy(first)
        second['tables']['table_3']['columns'][0]['nullable'] = True
        with self.assertRaisesRegex(ValueError, 'schema, count or logical digest'):
            rebuild.compare_runs(first, second)

    def test_worker_failure_never_writes_full_success_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp).resolve() / 'run'
            with patch.object(rebuild.subprocess, 'run', side_effect=subprocess.CalledProcessError(9, 'worker')):
                with self.assertRaises(subprocess.CalledProcessError):
                    rebuild.run_rebuild(ROOT, output)
            self.assertFalse((output / 'rebuild.json').exists())
            failure = json.loads((output / 'failure.json').read_text())
            self.assertEqual(failure['completed_runs'], 0)
            self.assertEqual(failure['status'], 'failed_no_success_receipt')

    def test_existing_or_checkout_output_is_rejected_before_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'fresh rebuild output'):
                rebuild.run_rebuild(ROOT, Path(tmp))
        with self.assertRaisesRegex(ValueError, 'outside the checkout'):
            rebuild.run_rebuild(ROOT, ROOT / 'must-not-be-created-by-test')
        self.assertFalse((ROOT / 'must-not-be-created-by-test').exists())

    def test_disk_measurement_rejects_links_and_reports_actual_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'file').write_bytes(b'12345')
            actual = rebuild.directory_bytes(root)
            self.assertEqual(actual['logical_bytes'], 5)
            self.assertEqual(actual['files'], 1)
            (root / 'link').symlink_to(root / 'file')
            with self.assertRaisesRegex(ValueError, 'symlink'):
                rebuild.directory_bytes(root)


if __name__ == '__main__':
    unittest.main()
