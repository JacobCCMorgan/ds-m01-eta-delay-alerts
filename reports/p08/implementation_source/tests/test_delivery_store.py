"""Synthetic delivery projections, clock domains and physical integrity."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import duckdb

from ds_m01.common import digest_file, parse_ts
from ds_m01 import release_store as parent
from ds_m01.delivery_simulation import simulate_delivery
from ds_m01.delivery_store import (
    build_tables, write_store, verify_store, verify_store_receipts,
    ORDER, PRESERVED, RESERVED, CANONICAL,
)
from test_release_store import fixture, selection


PROJECT=Path(__file__).resolve().parents[1]
GENERATED='2026-11-11T19:55:00Z'


def transport(outcome='delivered',lag=0):
    return dict(outcome=outcome,latency_seconds=5,receipt_lag_seconds=lag)


class DeliveryStoreTests(unittest.TestCase):
    def setUp(self):
        self.rows,self.registry,self.parent_settings=fixture()
        self.selection=selection(self.rows,self.parent_settings)
        self.parent=parent.build_tables(self.rows,self.selection,self.registry,'2026-11-07T12:00:00Z',self.parent_settings)
        self.settings=json.loads((PROJECT/'config/p07.json').read_text())
        self.temporary=tempfile.TemporaryDirectory()
        self.destination=Path(self.temporary.name)/'store'
        self.simulation=self.simulate()
        self.tables=build_tables(self.parent,self.simulation,GENERATED,self.settings)

    def tearDown(self):
        self.temporary.cleanup()

    def simulate(self,draw=None,cutoff=None):
        with patch('ds_m01.delivery_simulation.transport_outcome',side_effect=draw or (lambda *args:transport())):
            return simulate_delivery(self.selection['simulation']['candidates'],self.selection['simulation']['episodes'],
                                     self.settings,counts_as_of=cutoff)

    def test_three_formats_roundtrip_all_rows_with_read_only_verification(self):
        receipt=write_store(PROJECT,self.destination,self.tables,self.settings)
        database=self.destination/'experiment.duckdb'
        before=digest_file(database)
        replay=verify_store(PROJECT,self.destination,self.tables,self.settings)
        self.assertEqual(replay['differences'],0)
        self.assertEqual(replay['logical_rows_compared'],3*sum(map(len,self.tables.values())))
        self.assertEqual(receipt['canonical_tables'],list(CANONICAL))
        self.assertEqual(len(ORDER),26)
        self.assertEqual(len(list((self.destination/'parquet').glob('*.parquet'))),26)
        self.assertTrue(replay['read_only'])
        self.assertEqual(digest_file(database),before)
        self.assertTrue(verify_store_receipts(receipt,receipt,replay))
        self.assertTrue(all(not self.tables[name] for name in RESERVED))

    def test_parent_rows_and_november_clocks_survive_july_scenario_promotion(self):
        for name in PRESERVED:
            self.assertEqual(self.tables[name],self.parent[name],name)
        self.assertEqual(len(self.tables['eta_alert']),4)
        self.assertEqual(len(self.tables['eta_alert_episode']),2)
        for alert in self.tables['eta_alert']:
            release=next(r for r in self.tables['eta_output_release'] if (r['tenant_id'],r['output_id'])==(alert['tenant_id'],alert['output_id']))
            self.assertLess(parse_ts(alert['created_at']),parse_ts(release['released_at']))
        self.assertTrue(all(r['materialized_at']==GENERATED and r['result_origin']=='synthetic_scenario'
                            for r in self.tables['p07_attempt_audit']))
        self.assertTrue(all(r['received_or_opened_at'] is None for r in self.tables['eta_notification_delivery']))

    def test_retry_attempts_are_not_duplicate_deliveries(self):
        simulation=self.simulate(lambda candidate,index,settings:transport('transient_failure' if index==1 else 'delivered'))
        tables=build_tables(self.parent,simulation,GENERATED,self.settings)
        self.assertEqual(len(tables['eta_notification_delivery']),8)
        self.assertEqual([r['delivered_alert_count'] for r in tables['eta_alert_episode']],[2,2])
        self.assertEqual([r['duplicate_alert_count'] for r in tables['eta_alert_episode']],[1,1])
        write_store(PROJECT,self.destination,tables,self.settings)
        verify_store(PROJECT,self.destination,tables,self.settings)

    def test_unknown_timeout_remains_unresolved_without_delivery_or_opening(self):
        tables=build_tables(self.parent,self.simulate(lambda *args:transport('unknown')),GENERATED,self.settings)
        self.assertEqual(len(tables['eta_notification_delivery']),4)
        self.assertTrue(all(r['delivery_status']=='unknown' and r['delivered_at'] is None and r['receipt_type']=='none'
                            for r in tables['eta_notification_delivery']))
        self.assertTrue(all(r['delivered_alert_count']==0 for r in tables['eta_alert_episode']))
        write_store(PROJECT,self.destination,tables,self.settings)

    def test_pending_receipt_and_future_zero_attempt_alerts_keep_all_parent_rows(self):
        simulation=self.simulate(lambda *args:transport(lag=15),cutoff='2026-07-16T16:00:10Z')
        tables=build_tables(self.parent,simulation,GENERATED,self.settings)
        self.assertEqual(len(tables['eta_alert']),4)
        self.assertEqual(len(tables['eta_notification_delivery']),2)
        self.assertTrue(all(r['delivery_status']=='queued' and r['delivered_at'] is None for r in tables['eta_notification_delivery']))
        self.assertTrue(all(r['delivered_alert_count']==0 for r in tables['eta_alert_episode']))
        self.assertEqual(sum(r['attempt_count']==0 for r in tables['p07_alert_audit']),2)
        write_store(PROJECT,self.destination,tables,self.settings)
        verify_store(PROJECT,self.destination,tables,self.settings)

    def test_cutoff_before_first_candidate_promotes_future_episodes_without_processing(self):
        tables=build_tables(self.parent,self.simulate(cutoff='2026-07-16T15:59:59Z'),GENERATED,self.settings)
        self.assertEqual(tables['eta_notification_delivery'],[])
        self.assertEqual(tables['p07_event_audit'],[])
        self.assertTrue(all(r['alert_status']=='queued' for r in tables['eta_alert']))
        write_store(PROJECT,self.destination,tables,self.settings)

    def test_risk_episode_without_admitted_candidates_is_preserved_and_censored(self):
        extra=deepcopy(self.rows[0])
        extra.update(trip_id='NO-CANDIDATE',snapshot_id='NO-CANDIDATE-SNAPSHOT',snapshot_provenance_id='NO-CANDIDATE-PROV',
                     promise_provenance_id='NO-CANDIDATE-PROMISE-PROV',promise_version_id='NO-CANDIDATE-PROMISE',
                     snapshot_time='2026-07-16T17:00:00Z',elapsed_trip_seconds=7200,deadline_at='2026-07-16T17:29:59Z')
        rows=self.rows+[extra]
        selected=selection(rows,self.parent_settings)
        parent_tables=parent.build_tables(rows,selected,self.registry,'2026-11-07T12:00:00Z',self.parent_settings)
        with patch('ds_m01.delivery_simulation.transport_outcome',side_effect=lambda *args:transport()):
            simulation=simulate_delivery(selected['simulation']['candidates'],selected['simulation']['episodes'],self.settings)
        tables=build_tables(parent_tables,simulation,GENERATED,self.settings)
        self.assertEqual(len(tables['eta_alert_episode']),3)
        episode=next(row for row in tables['eta_alert_episode'] if row['trip_id']=='NO-CANDIDATE')
        self.assertEqual(episode['delivered_alert_count'],0)
        self.assertIsNone(episode['episode_closed_at'])
        self.assertEqual(episode['value_states'],{'episode_closed_at':'not_mature'})
        self.assertFalse(any(row['trip_id']=='NO-CANDIDATE' for row in tables['eta_alert']))
        write_store(PROJECT,self.destination,tables,self.settings)

    def test_physical_foreign_keys_and_opening_constraints_are_enforced(self):
        write_store(PROJECT,self.destination,self.tables,self.settings)
        con=duckdb.connect(str(self.destination/'experiment.duckdb'))
        try:
            with self.assertRaises(duckdb.ConstraintException):
                con.execute("INSERT INTO eta_alert SELECT tenant_id,'FOREIGN',alert_episode_id,'MISSING',trip_id,snapshot_id,prediction_version,decision_threshold_version,created_at,alert_status FROM eta_alert LIMIT 1")
            with self.assertRaises(duckdb.ConstraintException):
                con.execute("UPDATE eta_notification_delivery SET received_or_opened_at=delivered_at")
            with self.assertRaises(duckdb.ConstraintException):
                con.execute("INSERT INTO eta_notification_delivery SELECT 'FOREIGN',provenance_id,'OTHER',alert_id,recipient_role,delivered_at,received_or_opened_at,receipt_type,delivery_status FROM eta_notification_delivery LIMIT 1")
        finally:
            con.close()

    def test_cross_tenant_identity_projection_and_provenance_corruption_fail_before_write(self):
        cases=[('eta_alert','snapshot_id','FOREIGN'),('eta_alert','prediction_version','MODEL-T02'),
               ('eta_notification_delivery','provenance_id','UNKNOWN'),('p07_provenance_reference','result_origin','real'),
               ('p07_attempt_audit','received_or_opened_at','2026-07-16T16:01:00Z'),
               ('p07_alert_audit','materialized_at','2026-07-16T16:00:00Z'),
               ('eta_alert_episode','delivered_alert_count',99)]
        for table,field,value in cases:
            broken=deepcopy(self.tables);broken[table][0][field]=value
            with self.subTest(table=table,field=field),self.assertRaises(ValueError):
                write_store(PROJECT,self.destination,broken,self.settings)
            self.assertFalse(self.destination.exists())

    def test_forged_engine_counts_future_receipt_and_recipient_opening_are_rejected(self):
        cases=[]
        broken=deepcopy(self.simulation);broken['episodes'][0]['delivered_alert_count']=99;cases.append(broken)
        broken=deepcopy(self.simulation);broken['attempts'][0]['receipt_known_at']='2026-08-01T07:00:00.000001Z';cases.append(broken)
        broken=deepcopy(self.simulation);broken['attempts'][0]['received_or_opened_at']=broken['attempts'][0]['delivered_at'];cases.append(broken)
        broken=deepcopy(self.simulation);broken['summary']['notifications_sent']=0;cases.append(broken)
        for index,broken in enumerate(cases):
            tables=build_tables(self.parent,broken,GENERATED,self.settings)
            with self.subTest(index=index),self.assertRaises(ValueError):
                write_store(PROJECT,self.destination,tables,self.settings)
            self.assertFalse(self.destination.exists())

    def test_no_synthetic_provenance_or_unknown_fields_can_be_silently_dropped(self):
        broken=deepcopy(self.simulation);broken['alerts'][0]['result_origin']='production'
        with self.assertRaisesRegex(ValueError,'synthetic provenance'):
            build_tables(self.parent,broken,GENERATED,self.settings)
        broken=deepcopy(self.tables);broken['eta_alert'][0]['private_field']='x'
        with self.assertRaises(ValueError):write_store(PROJECT,self.destination,broken,self.settings)
        broken=deepcopy(self.tables);broken['p06_candidate_audit'][0]['simulated_issue_at']='2026-07-16T16:01:00Z'
        with self.assertRaises(ValueError):write_store(PROJECT,self.destination,broken,self.settings)

    def test_receipt_boolean_claims_do_not_accept_integers(self):
        receipt=write_store(PROJECT,self.destination,self.tables,self.settings)
        replay=verify_store(PROJECT,self.destination,self.tables,self.settings)
        broken=deepcopy(receipt);broken['foreign_keys_enforced']=1
        with self.assertRaises(ValueError):verify_store_receipts(broken,receipt,replay)
        with self.assertRaises(ValueError):verify_store_receipts(broken,broken,replay)
        (self.destination/'verification.json').write_text(json.dumps(broken))
        with self.assertRaisesRegex(ValueError,'receipt claims'):
            verify_store(PROJECT,self.destination,self.tables,self.settings)

    def test_changed_exports_are_rejected_even_if_attacker_updates_hash(self):
        receipt=write_store(PROJECT,self.destination,self.tables,self.settings)
        path=self.destination/'jsonl/eta_alert.jsonl'
        rows=[json.loads(line) for line in path.read_text().splitlines()]
        rows[0]['alert_status']='failed'
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        with self.assertRaisesRegex(ValueError,'file changed'):
            verify_store(PROJECT,self.destination,self.tables,self.settings)
        receipt['output_sha256']['jsonl/eta_alert.jsonl']=digest_file(path)
        (self.destination/'verification.json').write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError,'JSONL differs'):
            verify_store(PROJECT,self.destination,self.tables,self.settings)

    def test_rehashed_jsonl_integer_cannot_impersonate_boolean(self):
        receipt=write_store(PROJECT,self.destination,self.tables,self.settings)
        path=self.destination/'jsonl/p07_attempt_audit.jsonl'
        rows=[json.loads(line) for line in path.read_text().splitlines()]
        self.assertIs(rows[0]['retryable'],False)
        rows[0]['retryable']=0
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        receipt['output_sha256']['jsonl/p07_attempt_audit.jsonl']=digest_file(path)
        (self.destination/'verification.json').write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError,'JSONL differs'):
            verify_store(PROJECT,self.destination,self.tables,self.settings)

    def test_missing_physical_constraints_fail_despite_matching_rows_and_file_hash(self):
        receipt=write_store(PROJECT,self.destination,self.tables,self.settings)
        database=self.destination/'experiment.duckdb'
        replacement=self.destination/'replacement.duckdb'
        con=duckdb.connect(str(replacement))
        try:
            con.execute("ATTACH '"+str(database).replace("'","''")+"' AS source_db (READ_ONLY)")
            for name in ORDER:con.execute(f'CREATE TABLE {name} AS SELECT * FROM source_db.{name}')
        finally:
            con.close()
        replacement.replace(database)
        receipt['output_sha256']['experiment.duckdb']=digest_file(database)
        (self.destination/'verification.json').write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError,'physical primary/foreign/check/null constraints'):
            verify_store(PROJECT,self.destination,self.tables,self.settings)

    def test_failed_export_is_atomic_and_existing_output_is_preserved(self):
        with patch('ds_m01.delivery_store.write_jsonl',side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError,'disk full'):
                write_store(PROJECT,self.destination,self.tables,self.settings)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.destination.parent.iterdir()),[])
        self.destination.mkdir();(self.destination/'keep').write_text('original')
        with self.assertRaises(FileExistsError):write_store(PROJECT,self.destination,self.tables,self.settings)
        self.assertEqual((self.destination/'keep').read_text(),'original')


if __name__=='__main__':unittest.main()
