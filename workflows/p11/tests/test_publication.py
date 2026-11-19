"""Real DuckDB/Parquet publication, abrupt process failure and recovery checks."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import duckdb
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from workflows.p11 import publication
from workflows.p11.recovery import TABLES, fixture_builder, file_hashes, child_command, verify_recovery

class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='p11 publication ')
        self.root = Path(self.temp.name).resolve()
        self.store = self.root/'store'

    def tearDown(self):
        self.temp.cleanup()

    def baseline(self):
        return publication.publish(self.store, fixture_builder(1), expected_tables=TABLES)

    def test_two_complete_versions_and_old_artifacts_survive(self):
        first = self.baseline()
        old = self.store/'generations'/first['generation_id']
        hashes = file_hashes(old)
        second = publication.publish(self.store, fixture_builder(2), expected_tables=TABLES,
                                     expected_current=first['generation_id'])
        self.assertEqual(file_hashes(old), hashes)
        self.assertEqual(publication.read_current(self.store, verify_logical=True), second)
        self.assertEqual(second['previous_generation_id'], first['generation_id'])
        self.assertEqual(second['tables']['source_event']['row_count'], 3)

    def test_empty_table_schema_and_microsecond_decimal_types_retained(self):
        manifest = self.baseline()
        self.assertEqual(manifest['tables']['reserved_action']['row_count'], 0)
        fields = {r['name']:r for r in manifest['tables']['source_event']['columns']}
        self.assertEqual(fields['amount']['type'], 'DECIMAL(12,3)')
        self.assertEqual(fields['known_at']['type'], 'TIMESTAMP WITH TIME ZONE')
        self.assertFalse(fields['tenant_id']['nullable'])
        self.assertTrue(fields['amount']['nullable'])

    def test_explicit_bounded_comparison_policy_retains_full_rows(self):
        manifest=self.baseline()
        self.assertEqual(manifest['comparison_runtime']['memory_limit'],'2GB')
        self.assertEqual(manifest['comparison_runtime']['threads'],1)
        self.assertTrue(manifest['comparison_runtime']['preserve_insertion_order'])
        self.assertEqual(manifest['tables']['source_event']['comparison_order'],['tenant_id','event_id','revision'])
        self.assertEqual(len(manifest['tables']['source_event']['columns']),7)
        self.assertTrue(manifest['all_rows_compared_exactly'])

    def test_stale_expected_current_rejects_before_builder(self):
        first = self.baseline()
        called = []
        with self.assertRaisesRegex(ValueError, 'stale publication'):
            publication.publish(self.store, lambda p: called.append(p), expected_tables=TABLES)
        self.assertFalse(called)
        self.assertEqual(publication.read_current(self.store), first)

    def reject_builder(self, mutation, message):
        first = self.baseline()
        old_hashes = file_hashes(self.store/'generations'/first['generation_id'])
        def altered(stage):
            result = fixture_builder(2)(stage)
            mutation(stage)
            return result
        with self.assertRaisesRegex(ValueError, message):
            publication.publish(self.store, altered, expected_tables=TABLES,
                                expected_current=first['generation_id'])
        self.assertEqual(publication.read_current(self.store), first)
        self.assertEqual(file_hashes(self.store/'generations'/first['generation_id']), old_hashes)

    def test_missing_parquet_rejected_preserving_old_generation(self):
        self.reject_builder(lambda stage: (stage/'parquet/source_event.parquet').unlink(), 'Parquet catalog')

    def test_extra_parquet_rejected_preserving_old_generation(self):
        self.reject_builder(lambda stage: (stage/'parquet/extra.parquet').write_bytes(b'x'), 'Parquet catalog')

    def test_changed_row_rejected_preserving_old_generation(self):
        def alter(stage):
            con=duckdb.connect()
            path=stage/'parquet/derived_snapshot.parquet'
            con.execute('CREATE TABLE changed AS SELECT * FROM read_parquet(?)',[str(path)])
            con.execute('UPDATE changed SET score=0.333 WHERE tenant_id=\'a\'')
            path.unlink()
            con.execute('COPY changed TO ? (FORMAT PARQUET)',[str(path)])
            con.close()
        self.reject_builder(alter, 'rows differ')

    def test_duplicate_multiplicity_rejected(self):
        def alter(stage):
            con=duckdb.connect()
            path=stage/'parquet/derived_snapshot.parquet'
            con.execute('CREATE TABLE changed AS SELECT * FROM read_parquet(?)',[str(path)])
            con.execute('INSERT INTO changed SELECT * FROM changed LIMIT 1')
            path.unlink()
            con.execute('COPY changed TO ? (FORMAT PARQUET)',[str(path)])
            con.close()
        self.reject_builder(alter, 'rows differ')

    def test_changed_column_type_rejected(self):
        def alter(stage):
            con=duckdb.connect()
            path=stage/'parquet/reserved_action.parquet';path.unlink()
            con.execute('COPY (SELECT 1::INTEGER AS tenant_id, NULL::VARCHAR AS action_id WHERE false) TO ? (FORMAT PARQUET)',[str(path)])
            con.close()
        self.reject_builder(alter, 'schema differs')

    def test_extra_database_table_rejected(self):
        def alter(stage):
            con=duckdb.connect(str(stage/'warehouse.duckdb'))
            con.execute('CREATE TABLE extra(i INTEGER)');con.close()
        self.reject_builder(alter, 'Database catalog')

    def test_non_json_builder_metadata_fails_before_reference_change(self):
        first=self.baseline()
        def builder(stage):
            fixture_builder(2)(stage)
            return {'bad': object()}
        with self.assertRaises(TypeError):
            publication.publish(self.store,builder,expected_tables=TABLES,expected_current=first['generation_id'])
        self.assertEqual(publication.read_current(self.store),first)

    def test_database_tampering_fails_closed(self):
        first=self.baseline()
        path=self.store/'generations'/first['generation_id']/'warehouse.duckdb'
        with path.open('ab') as stream:stream.write(b'tampered')
        with self.assertRaisesRegex(ValueError,'artifact changed'):
            publication.read_current(self.store)
        with self.assertRaisesRegex(ValueError,'artifact changed'):
            publication.recover(self.store)

    def test_parquet_tampering_fails_closed(self):
        first=self.baseline()
        path=self.store/'generations'/first['generation_id']/'parquet/source_event.parquet'
        data=bytearray(path.read_bytes());data[8]^=1;path.write_bytes(data)
        with self.assertRaisesRegex(ValueError,'artifact changed'):
            publication.read_current(self.store)

    def test_manifest_tampering_fails_closed(self):
        first=self.baseline()
        path=self.store/'generations'/first['generation_id']/'manifest.json'
        path.write_text(path.read_text()+' ')
        with self.assertRaisesRegex(ValueError,'manifest hash differs'):
            publication.read_current(self.store)

    def test_pointer_path_traversal_rejected(self):
        self.baseline()
        pointer=self.store/'current.json';value=json.loads(pointer.read_text())
        value['generation_id']='../staging/escaped';pointer.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'generation identity'):
            publication.read_current(self.store)

    def test_symlink_artifact_rejected(self):
        first=self.baseline()
        path=self.store/'generations'/first['generation_id']/'parquet/source_event.parquet'
        outside=self.root/'outside.parquet';path.rename(outside);path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError,'symlinks'):
            publication.read_current(self.store)

    def test_unknown_nonempty_store_not_adopted(self):
        self.store.mkdir();(self.store/'other.txt').write_text('preserve')
        with self.assertRaisesRegex(ValueError,'nonempty unregistered'):
            self.baseline()
        self.assertEqual((self.store/'other.txt').read_text(),'preserve')

    def test_concurrent_writer_rejected_and_old_reader_stays_complete(self):
        first=self.baseline()
        marker=self.root/'writer-ready'
        process=subprocess.Popen(child_command(self.store,first['generation_id'],'hold',marker),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        try:
            deadline=time.monotonic()+20
            while not marker.exists() and process.poll() is None and time.monotonic()<deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(),'Writer did not reach hold point')
            self.assertEqual(publication.read_current(self.store),first)
            with self.assertRaisesRegex(ValueError,'Another writer'):
                publication.publish(self.store,fixture_builder(2),expected_tables=TABLES,expected_current=first['generation_id'])
        finally:
            process.kill();process.communicate(timeout=10)
        self.assertEqual(publication.read_current(self.store),first)
        recovered=publication.recover(self.store)
        self.assertTrue(any(r['state']=='incomplete_or_invalid' for r in recovered['entries']))

    def test_five_real_abrupt_exit_points_and_clean_rebuild(self):
        receipt=verify_recovery(self.root/'fault-matrix')
        self.assertEqual(receipt['faults_executed'],5)
        self.assertTrue(all(c['old_database_and_all_parquet_preserved'] for c in receipt['cases']))

    def test_invalid_and_duplicate_table_names_rejected(self):
        for names in ([],['source_event','source_event'],['../outside']):
            with self.subTest(names=names),self.assertRaises(ValueError):
                publication.publish(self.store,fixture_builder(1),expected_tables=names)

    def test_interrupted_validation_spill_is_retained_without_promotion(self):
        first=self.baseline()
        spill=self.store/'staging'/('.validation-'+'a'*32)
        spill.mkdir();(spill/'spill.bin').write_bytes(b'partial validation temporary')
        before=file_hashes(self.store)
        result=publication.recover(self.store)
        self.assertEqual(file_hashes(self.store),before)
        self.assertEqual(result['current_generation_id'],first['generation_id'])
        self.assertTrue(any(r['state']=='interrupted_validation_temporary' for r in result['entries']))



if __name__ == "__main__":
    unittest.main()
