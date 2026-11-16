"""Native PostgreSQL lifecycle checks; missing PostgreSQL is a failure, not a skip."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from workflows.p10 import cluster as lifecycle


class NativeClusterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name).resolve()

    def assert_stopped(self, native, status):
        self.assertEqual(native.metadata['status'], status)
        self.assertTrue(native.metadata['graceful_shutdown'])
        self.assertTrue(native.metadata['listener_closed'])
        self.assertFalse((native.directory / 'data/postmaster.pid').exists())
        self.assertFalse((native.directory / 'bootstrap-password').exists())
        self.assertEqual(json.loads((native.directory / 'lifecycle.json').read_text()), native.metadata)
        with self.assertRaisesRegex(RuntimeError, 'not running'):
            _ = native.dsn

    def test_real_scram_roundtrip_environment_isolation_and_shutdown(self):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        hostile = {'PGHOST': '192.0.2.1', 'PGHOSTADDR': '192.0.2.1', 'PGPORT': '1',
                   'PGDATABASE': 'not_our_database', 'PGUSER': 'not_our_user',
                   'PGPASSWORD': 'not_our_password', 'PGSERVICE': 'missing_service',
                   'PGSERVICEFILE': '/nonexistent/p10-service', 'PGPASSFILE': '/nonexistent/p10-pass',
                   'PGOPTIONS': '-c default_transaction_read_only=on',
                   'PGTARGETSESSIONATTRS': 'read-only', 'PGCHANNELBINDING': 'require'}
        with ExitStack() as scope:
            with patch.dict(os.environ, hostile):
                native = scope.enter_context(lifecycle.ownedcluster(self.parent / 'native'))
                self.assertTrue(native.metadata['readiness_verified'])
                self.assertTrue(native.metadata['owned_data_directory_confirmed'])
                self.assertEqual(native.metadata['authentication'], 'scram-sha-256')
                self.assertEqual(stat.S_IMODE(native.directory.stat().st_mode), 0o700)
            private = conninfo_to_dict(native.dsn)
            with psycopg.connect(native.dsn) as connection:
                self.assertEqual(connection.execute('SHOW server_version_num').fetchone()[0],
                                 str(native.metadata['server_version_num']))
                connection.execute('CREATE TABLE lifecycle_probe(id integer PRIMARY KEY, label text)')
                connection.execute('INSERT INTO lifecycle_probe VALUES (7, %s)', ('native PostgreSQL',))
            with psycopg.connect(native.dsn) as connection:
                self.assertEqual(connection.execute('SELECT * FROM lifecycle_probe').fetchall(),
                                 [(7, 'native PostgreSQL')])
            wrong = dict(private, password='wrong_private_password')
            with self.assertRaises(psycopg.OperationalError):
                psycopg.connect(**wrong)
            self.assertNotIn(private['password'], repr(native))
            self.assertNotIn(private['password'], json.dumps(native.metadata))
        self.assert_stopped(native, 'passed_owned_cluster_lifecycle')
        for log in native.directory.glob('*.log'):
            self.assertNotIn(private['password'], log.read_text())
            self.assertNotIn(str(native.directory), log.read_text())

    def test_body_exception_stops_cluster_without_swallowing_failure(self):
        with self.assertRaisesRegex(ValueError, 'fixture body failure'):
            with lifecycle.ownedcluster(self.parent / 'exception') as native:
                raise ValueError('fixture body failure')
        self.assert_stopped(native, 'failed_body')
        self.assertEqual(native.metadata['failure_type'], 'ValueError')

    def test_keyboard_interrupt_stops_private_cluster(self):
        with self.assertRaises(KeyboardInterrupt):
            with lifecycle.ownedcluster(self.parent / 'interrupt') as native:
                raise KeyboardInterrupt()
        self.assert_stopped(native, 'failed_body')
        self.assertEqual(native.metadata['failure_type'], 'KeyboardInterrupt')

    def test_occupied_port_fails_preserves_logs_and_does_not_touch_listener(self):
        output = self.parent / 'occupied'
        with socket.socket() as other:
            other.bind(('127.0.0.1', 0))
            other.listen()
            address = other.getsockname()
            with patch.object(lifecycle, '_port', return_value=address[1]):
                with self.assertRaisesRegex(RuntimeError, 'command failed'):
                    with lifecycle.ownedcluster(output):
                        self.fail('An occupied port must prevent readiness')
            with socket.socket() as probe:
                probe.settimeout(1)
                self.assertEqual(probe.connect_ex(address), 0)
        record = json.loads((output / 'lifecycle.json').read_text())
        self.assertEqual(record['status'], 'failed_start')
        self.assertFalse(record['readiness_verified'])
        self.assertFalse(record['external_service_touched'])
        self.assertFalse((output / 'data/postmaster.pid').exists())
        self.assertIn('could not bind', (output / 'server.log').read_text())
        self.assertFalse((output / 'bootstrap-password').exists())

    def test_symlink_parent_is_canonicalized_before_readiness(self):
        canonical = self.parent / 'canonical'
        canonical.mkdir()
        alias = self.parent / 'alias'
        alias.symlink_to(canonical, target_is_directory=True)
        with lifecycle.ownedcluster(alias / 'native') as native:
            self.assertEqual(native.directory, canonical / 'native')
            self.assertTrue(native.metadata['owned_data_directory_confirmed'])
        self.assert_stopped(native, 'passed_owned_cluster_lifecycle')

    def test_failed_readiness_stops_started_server_and_records_failure(self):
        output = self.parent / 'readiness-failure'
        original = lifecycle._logged
        def rejected(directory, name, *args, **kwargs):
            if name == 'readiness.log':
                raise RuntimeError('fixture readiness failure')
            return original(directory, name, *args, **kwargs)
        with patch.object(lifecycle, '_logged', side_effect=rejected):
            with self.assertRaisesRegex(RuntimeError, 'fixture readiness failure'):
                with lifecycle.ownedcluster(output):
                    self.fail('Failed readiness yielded a connection')
        record = json.loads((output / 'lifecycle.json').read_text())
        self.assertEqual(record['status'], 'failed_start')
        self.assertTrue(record['graceful_shutdown'])
        self.assertTrue(record['listener_closed'])
        self.assertFalse(record['readiness_verified'])
        self.assertFalse((output / 'data/postmaster.pid').exists())


class ClusterBoundaryTests(unittest.TestCase):
    def test_existing_directory_is_not_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            marker = target / 'existing-data'
            marker.write_text('preserve')
            with self.assertRaisesRegex(ValueError, 'already exists'):
                with lifecycle.ownedcluster(target):
                    self.fail('Existing directory accepted')
            self.assertEqual(list(target.iterdir()), [marker])
            self.assertEqual(marker.read_text(), 'preserve')

    def test_cluster_inside_checkout_is_rejected_without_creation(self):
        target = ROOT / 'p10-forbidden-cluster-fixture'
        self.assertFalse(target.exists())
        with self.assertRaisesRegex(ValueError, 'outside the checkout'):
            with lifecycle.ownedcluster(target):
                self.fail('Checkout cluster accepted')
        self.assertFalse(target.exists())

    def test_process_environment_is_cleaned_without_mutating_user_settings(self):
        additions = {'PGHOST': 'foreign', 'POSTGRES_PASSWORD': 'foreign', 'DATABASE_URL': 'foreign',
                     'P10_PG_BIN': 'preserve-this-setting'}
        with patch.dict(os.environ, additions):
            result = lifecycle._environment()
            for name in ('PGHOST', 'POSTGRES_PASSWORD', 'DATABASE_URL'):
                self.assertNotIn(name, result)
                self.assertEqual(os.environ[name], 'foreign')
            self.assertEqual(result['P10_PG_BIN'], 'preserve-this-setting')

    def test_foreign_pid_data_directory_is_not_owned(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory).resolve()
            (data / 'postmaster.pid').write_text('1\n/foreign/postgres/data\n')
            with self.assertRaisesRegex(ValueError, 'ownership'):
                lifecycle._owned_pid(data)

    def test_wrong_major_runtime_is_rejected_before_cluster_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            bins = Path(directory)
            for name in lifecycle.TOOLS:
                executable = bins / name
                executable.write_text('#!/bin/sh\nprintf "postgres (PostgreSQL) 18.1\\n"\n')
                executable.chmod(0o700)
            output = bins / 'must-not-exist'
            with self.assertRaisesRegex(ValueError, 'requires PostgreSQL 17.x'):
                with lifecycle.ownedcluster(output, pg_bin=bins):
                    self.fail('Wrong major version accepted')
            self.assertFalse(output.exists())

    def test_missing_runtime_is_failure_not_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'Missing PostgreSQL executable'):
                lifecycle._runtime(directory, None)


if __name__ == '__main__':
    unittest.main()
