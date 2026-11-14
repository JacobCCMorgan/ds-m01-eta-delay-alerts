"""Portable final inference is label-free, fixed-state and identity preserving."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ds_m01.calibration_refit import refit_and_predict
from ds_m01.calibration_models import fit_interval_calibrator,fit_probability_calibrator
from ds_m01.candidate_model import canonical_digest
from ds_m01.final_inference import predict
from ds_m01.final_stress import SCENARIOS
from test_calibration_refit import fixture as training_fixture
from test_calibration_models import rows as calibration_rows,metadata

ROOT=Path(__file__).resolve().parents[1]
SETTINGS=json.loads((ROOT/'config/p08_evaluation.json').read_text())


def inference_fixture():
    training,source,config=training_fixture()
    trained=refit_and_predict(training,source,config)
    states={name:trained[name] for name in ('transformers','eta_models','delay_models','frozen_history')}
    states['calibrators']={'eta':[],'probability':[]};states['model_registry']=[]
    for transformer in states['transformers']:
        tenant=transformer['tenant_id']
        toy=[dict(row,tenant_id=tenant,trip_id='CAL-'+row['trip_id']) for row in calibration_rows()]
        eta=fit_interval_calibrator(toy,tenant_id=tenant,fit_metadata=metadata(toy))
        probability=fit_probability_calibrator(toy,tenant_id=tenant,fit_metadata=metadata(toy))
        states['calibrators']['eta'].append(eta);states['calibrators']['probability'].append(probability)
        em=next(r for r in states['eta_models'] if r['tenant_id']==tenant)
        dm=next(r for r in states['delay_models'] if r['tenant_id']==tenant and r['method_id']=='logistic_delay')
        states['model_registry'].append(dict(tenant_id=tenant,prediction_version='FIXED-'+tenant,
            source_bindings={name:canonical_digest(value) for name,value in
                [('transformer_sha256',transformer),('eta_model_sha256',em),('probability_model_sha256',dm),
                 ('interval_calibrator_sha256',eta),('probability_calibrator_sha256',probability)]}))
    inputs={'features':[],'snapshots':[],'memberships':[],'promise_features':[]}
    future={ (r['tenant_id'],r['snapshot_id']) for r in trained['matrices'] if r['fold_role']=='validation'}
    for original in trained['features']:
        if (original['tenant_id'],original['snapshot_id']) not in future:continue
        feature=deepcopy(original)
        feature.update(trip_id='FINAL-'+feature['trip_id'],snapshot_time='2026-08-02T17:00:00Z',route_version_id='RV1')
        feature['numeric']['elapsed_trip_seconds']=3600.0
        inputs['features'].append(feature)
        tenant,tid,sid=feature['tenant_id'],feature['trip_id'],feature['snapshot_id']
        route=feature['categorical']['route_id']
        context=dict(tenant_id=tenant,trip_id=tid,snapshot_id=sid,snapshot_time=feature['snapshot_time'],
            route_version_id=feature['route_version_id'],vehicle_id='V1',route_id=route,
            position_validity=feature['categorical']['position_validity'],trip_status_as_of='in_transit',
            departure_time='2026-08-02T16:00:00Z',departure_month='2026-08',novelty_group='seen_vehicle_seen_route',
            unseen_route=False,unseen_vehicle=False,elapsed_trip_seconds=3600.0,
            departure_time_bucket=feature['categorical']['departure_time_bucket'],deadline_remaining_seconds=7200.0,
            promise_version_id='PROMISE-'+sid,deadline_at='2026-08-02T19:00:00Z',promise_known_at='2026-08-02T16:00:00Z',
            promise_provenance_id='PP-'+sid,provenance_id='SP-'+sid)
        inputs['snapshots'].append(context)
        inputs['promise_features'].append(dict(context,promise_seconds_remaining=7200.0))
        inputs['memberships'].append(dict(tenant_id=tenant,trip_id=tid,role='test',original_split_role='test',
            departure_time=context['departure_time'],vehicle_id='V1',route_id=route,novelty_group=context['novelty_group']))
    return inputs,states,source


class FinalInferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs,cls.states,cls.source=inference_fixture()

    def predict(self,inputs=None,states=None,scenario=SCENARIOS[0]):
        return predict(inputs or self.inputs,states or self.states,self.source,SETTINGS,scenario)

    def test_retained_json_inference_cannot_refit_or_open_files(self):
        before=deepcopy((self.inputs,self.states))
        with patch('builtins.open',side_effect=AssertionError('No inference file IO')), \
             patch('ds_m01.candidate_model.fit_candidate',side_effect=AssertionError('No fitting')), \
             patch('ds_m01.delay_model.fit_delay_model',side_effect=AssertionError('No fitting')):
            result=self.predict(states=json.loads(json.dumps(self.states)))
        self.assertEqual((self.inputs,self.states),before)
        n=len(self.inputs['snapshots'])
        self.assertEqual((len(result['eta_rows']),len(result['probability_rows']),len(result['opportunities'])),(4*n,4*n,n))
        self.assertEqual({(r['method_id'],r['variant']) for r in result['eta_rows']},
                         {('pipeline','raw'),('pipeline','calibrated'),('pipeline','canonical'),('historical_baseline','raw')})
        self.assertTrue(all('eligibility' not in r and 'delay_label' not in r for r in result['opportunities']))

    def test_calibration_and_projection_preserve_median_and_explicit_raw_values(self):
        result=self.predict()
        by={(r['tenant_id'],r['snapshot_id'],r['variant']):r for r in result['eta_rows'] if r['method_id']=='pipeline'}
        for opportunity in result['opportunities']:
            key=(opportunity['tenant_id'],opportunity['snapshot_id'])
            raw,calibrated,canonical=(by[*key,v] for v in ('raw','calibrated','canonical'))
            self.assertEqual(raw['median_seconds'],calibrated['median_seconds'])
            self.assertEqual(calibrated['raw_upper_seconds'],raw['upper_seconds'])
            self.assertEqual(calibrated['upper_seconds'],raw['upper_seconds']+8)
            self.assertLessEqual(canonical['lower_seconds'],calibrated['lower_seconds'])
            self.assertGreaterEqual(canonical['upper_seconds'],calibrated['upper_seconds'])
            self.assertRegex(opportunity['probability'],r'^\d\.\d{6}$')
            self.assertEqual(canonical['median_seconds'],opportunity['median_seconds_int'])

    def test_stress_preserves_source_identity_registry_and_calibration_states(self):
        baseline=self.predict()
        for scenario in ('S01_missing_position','S03_unseen_route_slot','S04_natural_entity_holdouts','S07_transport_degraded'):
            result=self.predict(scenario=scenario)
            self.assertEqual(result['model_registry'],baseline['model_registry'])
            for original,stressed in zip(baseline['opportunities'],result['opportunities']):
                for field in ('tenant_id','trip_id','snapshot_id','snapshot_time','route_id','position_validity','promise_version_id','deadline_at','prediction_version','novelty_group'):
                    self.assertEqual(original[field],stressed[field],field)
            if scenario=='S03_unseen_route_slot':
                self.assertTrue(all('route_id' in r['unknown_categorical_features'] for r in result['matrices']))
            if scenario=='S01_missing_position':
                self.assertTrue(all('remaining_distance_km' in r['missing_numeric_features'] for r in result['matrices']))

    def test_injected_target_fields_fail_before_prediction(self):
        for container in ('features','snapshots','promise_features','memberships'):
            inputs=deepcopy(self.inputs);inputs[container][0]['delay_label']=None
            with self.subTest(container=container),self.assertRaisesRegex(ValueError,'target fields'):
                self.predict(inputs=inputs)

    def test_foreign_model_history_promise_and_original_feature_binding_fail(self):
        states=deepcopy(self.states);states['eta_models'][0]['tenant_id']='OTHER'
        with self.assertRaisesRegex(ValueError,'inventory'):self.predict(states=states)
        states=deepcopy(self.states);states['frozen_history'][0]['label_known_at']='2026-08-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError,'historical pool'):self.predict(states=states)
        inputs=deepcopy(self.inputs);inputs['promise_features'][0]['promise_provenance_id']='FOREIGN'
        with self.assertRaisesRegex(ValueError,'promise/context'):self.predict(inputs=inputs)
        inputs=deepcopy(self.inputs);inputs['features'][0]['categorical']['route_id']='FOREIGN'
        with self.assertRaisesRegex(ValueError,'features/context'):self.predict(inputs=inputs)

    def test_state_registry_tampering_and_training_overlap_are_rejected(self):
        states=deepcopy(self.states);states['model_registry'][0]['source_bindings']['eta_model_sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'Registry'):self.predict(states=states)
        inputs=deepcopy(self.inputs)
        tenant=inputs['features'][0]['tenant_id']
        train=next(s for s in self.states['transformers'] if s['tenant_id']==tenant)['fit_trip_keys'][0][1]
        old=inputs['features'][0]['trip_id']
        for rows in inputs.values():
            for row in rows:
                if row['tenant_id']==tenant and row['trip_id']==old:row['trip_id']=train
        with self.assertRaisesRegex(ValueError,'overlaps fit groups'):self.predict(inputs=inputs)


if __name__=='__main__':unittest.main()
