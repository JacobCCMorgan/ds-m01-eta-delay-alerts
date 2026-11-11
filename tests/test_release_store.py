"""Offline export contract, complete denominators and transaction boundaries."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import duckdb

from ds_m01.alert_policy import make_rule, simulate_policy
from ds_m01.common import parse_ts, utc
from ds_m01.release_store import build_tables, write_store, verify_store, CANONICAL, RESERVED, ORDER

PROJECT = Path(__file__).resolve().parents[1]
GENERATED = '2026-11-07T12:00:00Z'


def fixture():
    settings = json.loads((PROJECT/'config/p06.json').read_text())
    rows, registry = [], []
    for tenant in ('T01','T02'):
        version = 'MODEL-'+tenant
        registry.append(dict(tenant_id=tenant,prediction_version=version,training_cutoff_at='2026-07-01T07:00:00Z',
            calibration_cutoff_at='2026-07-15T07:00:00Z',feature_definition_version='features-v1',nominal_interval_coverage='0.800000',
            split_version='original-split',interval_calibration_version='interval-'+tenant,
            probability_calibration_version='probability-'+tenant,baseline_definition='Frozen training only',
            source_bindings={'tenant_id':tenant,'model_sha256':'a'*64}))
        for i in range(3):
            rows.append(dict(tenant_id=tenant,trip_id='TRIP',snapshot_id=f'S{i}',
                snapshot_time=utc(parse_ts('2026-07-16T16:00:00Z')+timedelta(minutes=20*i)),
                route_id='ROUTE',vehicle_id='VEHICLE',departure_time='2026-07-16T15:00:00Z',
                trip_status_as_of='in_transit',promise_version_id='PROMISE',deadline_at='2026-07-16T18:00:00Z',
                promise_known_at='2026-07-16T14:00:00Z',snapshot_provenance_id=f'{tenant}-SNAP-PROV-{i}',
                promise_provenance_id=f'{tenant}-PROMISE-PROV',prediction_version=version,position_validity='valid',
                elapsed_trip_seconds=3600+i*1200,unseen_route=False,unseen_vehicle=False,
                eta_status='available',eta_reason='available',eta_source='candidate',eta_calibration_status='fitted',
                lower_seconds=0.59,median_seconds=0.6,upper_seconds=0.61,
                lower_seconds_int=0,median_seconds_int=1,upper_seconds_int=1,
                probability_status='available',probability_reason='available',probability_source='model',
                probability_calibration_status='fitted',raw_probability=0.55,calibrated_probability=0.4999995,
                probability='0.500000',fallback_reason='eta=candidate;probability=model;interval=fitted'))
    return rows,registry,settings


def selection(rows,settings,enabled=True):
    rule = make_rule(settings,'0.5' if enabled else None,enabled=enabled)
    return {'rule':rule,'status':'selected' if enabled else 'disabled','selection_manifest':{'research_only':True},
            'simulation':simulate_policy(rows,rule)}


class ReleaseStoreTests(unittest.TestCase):
    def setUp(self):
        self.rows,self.registry,self.settings = fixture()
        self.selection = selection(self.rows,self.settings)
        self.tables = build_tables(self.rows,self.selection,self.registry,GENERATED,self.settings)
        self.temporary = tempfile.TemporaryDirectory()
        self.destination = Path(self.temporary.name)/'store'

    def tearDown(self):
        self.temporary.cleanup()

    def test_typed_store_has_real_foreign_keys_and_roundtrips_all_exports(self):
        result = write_store(PROJECT,self.destination,self.tables,self.settings)
        self.assertEqual(result['status'],'passed')
        self.assertEqual(result['opportunities'],6)
        self.assertEqual(result['canonical_predictions'],6)
        self.assertEqual(len(list((self.destination/'parquet').glob('*.parquet'))),len(ORDER))
        self.assertEqual(result['canonical_tables'],list(CANONICAL))
        self.assertTrue(all(result['counts'][name]==0 for name in RESERVED))
        con = duckdb.connect(str(self.destination/'experiment.duckdb'))
        try:
            with self.assertRaises(duckdb.ConstraintException):
                con.execute("INSERT INTO p06_model_binding VALUES ('FOREIGN','UNKNOWN','{}')")
            self.assertEqual(con.execute('SELECT DISTINCT delay_probability FROM eta_prediction').fetchall(),[(Decimal('0.500000'),)])
            weights = con.execute('SELECT sum(e.trip_evaluation_weight) FROM eta_evaluation_membership e JOIN p06_source_snapshot s USING(tenant_id,snapshot_id) GROUP BY s.tenant_id,s.trip_id').fetchall()
            self.assertEqual(weights,[(Decimal('1.000000000'),)]*2)
        finally:
            con.close()

    def test_read_only_replay_checks_three_formats_and_detects_changed_evidence(self):
        write_store(PROJECT,self.destination,self.tables,self.settings)
        from ds_m01.common import digest_file
        database=self.destination/'experiment.duckdb'
        before=digest_file(database)
        result=verify_store(PROJECT,self.destination,self.tables,self.settings)
        self.assertEqual(result['differences'],0)
        self.assertTrue(result['read_only'])
        self.assertEqual(digest_file(database),before)
        changed=deepcopy(self.tables)
        changed['eta_prediction'][0]['remaining_eta_seconds']=2
        with self.assertRaisesRegex(ValueError,'JSONL differs'):
            verify_store(PROJECT,self.destination,changed,self.settings)
        path=self.destination/'jsonl/eta_prediction.jsonl'
        path.write_text(path.read_text()+'\n')
        with self.assertRaisesRegex(ValueError,'file changed'):
            verify_store(PROJECT,self.destination,self.tables,self.settings)

    def test_rounding_matches_policy_input_and_retains_precise_source(self):
        canonical = self.tables['eta_prediction'][0]
        self.assertEqual(canonical['delay_probability'],0.5)
        self.assertEqual(canonical['remaining_eta_seconds'],1)
        self.assertEqual(canonical['eta_lower'],'2026-07-16T16:00:00Z')
        self.assertEqual(canonical['eta_median'],canonical['eta_upper'])
        audit = self.tables['p06_opportunity_audit'][0]
        self.assertEqual(audit['opportunity']['calibrated_probability'],0.4999995)
        self.assertEqual(audit['upper_seconds'],0.61)
        self.assertEqual(self.tables['p06_decision_audit'][0]['candidate_status'],'admitted')

    def test_missing_eta_and_probability_keep_opportunities_without_invented_values(self):
        rows = deepcopy(self.rows)
        rows[0].update(eta_status='unavailable',eta_reason='insufficient_support',
                       lower_seconds=None,median_seconds=None,upper_seconds=None,
                       lower_seconds_int=None,median_seconds_int=None,upper_seconds_int=None)
        rows[1].update(probability_status='unavailable',probability_reason='unsupported',
                       probability_calibration_status='unavailable',raw_probability=None,
                       calibrated_probability=None,probability=None)
        tables = build_tables(rows,selection(rows,self.settings),self.registry,GENERATED,self.settings)
        result = write_store(PROJECT,self.destination,tables,self.settings)
        self.assertEqual(result['opportunities'],6)
        self.assertEqual(result['canonical_predictions'],5)
        self.assertEqual(len(tables['eta_output_release']),6)
        null = next(r for r in tables['eta_prediction'] if r['tenant_id']=='T01' and r['snapshot_id']=='S1')
        self.assertIsNone(null['delay_probability'])
        self.assertEqual(null['value_states'],{'delay_probability':'unknown'})

    def test_disabled_threshold_one_is_only_placeholder(self):
        disabled = selection(self.rows,self.settings,False)
        tables = build_tables(self.rows,disabled,self.registry,GENERATED,self.settings)
        self.assertEqual(tables['eta_alert_rule_version'][0]['delay_alert_threshold'],1)
        self.assertIsNone(tables['p06_rule_audit'][0]['threshold'])
        self.assertFalse(tables['p06_rule_audit'][0]['enabled'])
        self.assertEqual(tables['p06_candidate_audit'],[])
        write_store(PROJECT,self.destination,tables,self.settings)
        broken = deepcopy(tables);broken['p06_rule_audit'][0]['enabled']=True
        with self.assertRaisesRegex(ValueError,'explicit experimental policy'):
            write_store(PROJECT,self.destination.parent/'bad',broken,self.settings)

    def test_cross_tenant_model_and_snapshot_binding_fail_before_publication(self):
        for table,field,value in [('eta_prediction','prediction_version','MODEL-T02'),
                                  ('eta_evaluation_membership','snapshot_id','FOREIGN')]:
            broken = deepcopy(self.tables)
            broken[table][0][field]=value
            with self.subTest(table=table),self.assertRaises((ValueError,duckdb.ConstraintException)):
                write_store(PROJECT,self.destination,broken,self.settings)
            self.assertFalse(self.destination.exists())

    def test_omitted_decision_or_canonical_support_does_not_shrink_denominator(self):
        for table in ('p06_decision_audit','eta_prediction','eta_evaluation_membership','eta_output_release'):
            broken = deepcopy(self.tables);broken[table].pop()
            with self.subTest(table=table),self.assertRaises((ValueError,duckdb.ConstraintException)):
                write_store(PROJECT,self.destination,broken,self.settings)
            self.assertFalse(self.destination.exists())

    def test_clock_null_enum_and_source_hash_corruption_fail(self):
        cases=[]
        broken=deepcopy(self.tables);broken['eta_prediction'][0]['eta_upper']='2026-07-16T15:00:00Z';cases.append(broken)
        broken=deepcopy(self.tables);broken['eta_prediction'][0]['fallback_reason']='selected';cases.append(broken)
        broken=deepcopy(self.tables);broken['eta_prediction'][0]['delay_probability']=None;cases.append(broken)
        broken=deepcopy(self.tables)
        next(r for r in broken['p06_provenance_reference'] if r['reference_kind']=='source_snapshot')['source_sha256']='b'*64
        cases.append(broken)
        for broken in cases:
            with self.subTest(case=len(cases)),self.assertRaises(ValueError):
                write_store(PROJECT,self.destination,broken,self.settings)
            self.assertFalse(self.destination.exists())

    def test_failed_export_leaves_no_partial_destination_and_fresh_output_is_required(self):
        with patch('ds_m01.release_store.write_jsonl',side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError,'disk full'):
                write_store(PROJECT,self.destination,self.tables,self.settings)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.destination.parent.iterdir()),[])
        self.destination.mkdir()
        (self.destination/'keep').write_text('original')
        with self.assertRaises(FileExistsError):
            write_store(PROJECT,self.destination,self.tables,self.settings)
        self.assertEqual((self.destination/'keep').read_text(),'original')

    def test_targets_conflicting_keys_and_changed_projection_are_rejected(self):
        for mutate in (lambda rows:rows[0].update(delay_label=True),
                       lambda rows:rows.append(dict(rows[0])),
                       lambda rows:rows[0].update(probability='0.499999'),
                       lambda rows:rows[0].update(median_seconds_int=0)):
            rows=deepcopy(self.rows);mutate(rows)
            with self.assertRaises(ValueError):
                build_tables(rows,self.selection,self.registry,GENERATED,self.settings)

    def test_canonical_rule_cannot_disagree_with_policy_lead_or_suppression(self):
        for field in ('minimum_action_lead_seconds','repeat_suppression_seconds','threshold_selection_ref'):
            broken=deepcopy(self.tables)
            broken['eta_alert_rule_version'][0][field] = 'other-selection.json' if field=='threshold_selection_ref' else 0
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'Canonical rule differs'):
                write_store(PROJECT,self.destination,broken,self.settings)
            self.assertFalse(self.destination.exists())

    def test_selection_reference_resolves_to_retained_manifest_without_fake_fragment(self):
        # The enclosing run writes this evidence file before publishing store/.
        manifest_path=self.destination.parent/'selection_manifest.json'
        manifest_path.write_text(json.dumps(self.selection['selection_manifest']))
        write_store(PROJECT,self.destination,self.tables,self.settings)
        rules=json.loads((self.destination/'jsonl/eta_alert_rule_version.jsonl').read_text())
        reference=rules['threshold_selection_ref']
        self.assertNotIn('#',reference)
        self.assertEqual((self.destination.parent/reference).resolve(),manifest_path.resolve())
        retained=json.loads((self.destination.parent/reference).read_text())
        self.assertEqual(retained,self.tables['p06_rule_audit'][0]['selection_manifest'])
        broken=deepcopy(self.tables)
        broken['eta_alert_rule_version'][0]['threshold_selection_ref']='selection.json#'+self.selection['rule']['policy_version']
        with self.assertRaisesRegex(ValueError,'Canonical rule differs'):
            write_store(PROJECT,self.destination.parent/'dangling',broken,self.settings)

    def test_membership_is_reconstructed_including_residual_weights_and_slices(self):
        originals=[row for row in self.tables['eta_evaluation_membership'] if row['tenant_id']=='T01']
        self.assertEqual([row['trip_evaluation_weight'] for row in originals],[0.333333333,0.333333333,0.333333334])
        mutations=[]
        broken=deepcopy(self.tables);broken['eta_evaluation_membership'][0]['lead_time_slice']='past_promise';mutations.append(broken)
        broken=deepcopy(self.tables);broken['eta_evaluation_membership'][0]['evaluation_slice']=[];mutations.append(broken)
        broken=deepcopy(self.tables);broken['eta_evaluation_membership'][0]['unseen_route']=True;mutations.append(broken)
        broken=deepcopy(self.tables)
        for row,weight in zip(broken['eta_evaluation_membership'][:3],[0.2,0.3,0.5]):
            row['trip_evaluation_weight']=weight
        mutations.append(broken)
        for index,broken in enumerate(mutations):
            with self.subTest(mutation=index),self.assertRaisesRegex(ValueError,'membership differs'):
                write_store(PROJECT,self.destination,broken,self.settings)
            self.assertFalse(self.destination.exists())

    def test_source_reference_requires_complete_correct_record_key(self):
        for key in ({},{'tenant_id':None,'snapshot_id':'S0'},{'tenant_id':'FOREIGN','snapshot_id':'S0'}):
            broken=deepcopy(self.tables)
            reference=next(row for row in broken['p06_provenance_reference'] if row['reference_kind']=='source_snapshot')
            reference['record_key']=key
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'provenance record key'):
                write_store(PROJECT,self.destination,broken,self.settings)
            self.assertFalse(self.destination.exists())

    def test_nominal_coverage_is_a_probability_inside_open_unit_interval(self):
        for value in (0.0,1.0,1.1,-0.1):
            broken=deepcopy(self.tables);broken['eta_model_version'][0]['nominal_interval_coverage']=value
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'Nominal interval coverage'):
                write_store(PROJECT,self.destination,broken,self.settings)

    def test_excess_decimal_precision_is_rejected_before_database_cast(self):
        for name,field in (('eta_prediction','delay_probability'),('p06_opportunity_audit','probability'),
                           ('eta_evaluation_membership','trip_evaluation_weight')):
            broken=deepcopy(self.tables)
            broken[name][0][field]=0.5000004 if field!='trip_evaluation_weight' else 0.3333333334
            with self.subTest(table=name),self.assertRaisesRegex(ValueError,'declared decimal precision'):
                write_store(PROJECT,self.destination,broken,self.settings)
            self.assertFalse(self.destination.exists())


if __name__=='__main__':
    unittest.main()
