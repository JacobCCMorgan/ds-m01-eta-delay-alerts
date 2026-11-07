"""Training/July separation and frozen-history tests with independent toy trips."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ds_m01.calibration_refit import refit_and_predict, replay_predictions, verify_source_inputs, SOURCE_FILES
from ds_m01.common import digest_file
from test_delay_run import fixture as delay_fixture

PROJECT = Path(__file__).resolve().parents[1]


def fixture():
    previous, snapshots, promises, _, source, outcomes = delay_fixture()
    features = deepcopy(previous['features'])
    members = []
    for feature, snapshot in zip(features, snapshots):
        index = int(feature['trip_id'][-1])
        role = 'train' if index < 5 else 'calibration_fit' if index == 5 else 'assessment'
        day = '2026-01-10' if index < 5 else '2026-07-05' if index == 5 else '2026-07-20'
        feature['snapshot_time'] = day + 'T17:00:00Z'
        snapshot.update(snapshot_time=feature['snapshot_time'], elapsed_trip_seconds=3600, route_version_id='RV1')
        members.append(dict(tenant_id=feature['tenant_id'], trip_id=feature['trip_id'], role=role,
                            original_split_role='train' if role == 'train' else 'calibration',
                            departure_time=day+'T16:00:00Z', route_id='R1', vehicle_id='V1',
                            unseen_vehicle=False, unseen_route=False))
    for promise in promises:
        index = int(promise['trip_id'][-1])
        if index >= 5:
            day = '2026-07-05' if index == 5 else '2026-07-20'
            for field in ('known_at','window_start_at','window_end_at'):
                promise[field] = day + promise[field][10:]
    settings = json.loads((PROJECT/'config/m3d.json').read_text())
    inputs = dict(features=features, snapshots=snapshots, promises=promises, memberships=members,
                  train_outcomes=[r for r in outcomes if int(r['trip_id'][-1]) < 5])
    return inputs, source, settings


class CalibrationRefitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs, cls.source, cls.settings = fixture()
        cls.result = refit_and_predict(cls.inputs, cls.source, cls.settings)

    def test_full_original_training_is_refit_and_every_july_opportunity_retained(self):
        result = self.result
        self.assertEqual(result['summary']['training_id'], 'R01')
        self.assertEqual(result['summary']['training_trips'], 10)
        self.assertEqual(result['summary']['training_history_trips'], 10)
        self.assertEqual(len(result['eta_predictions']), 4)
        self.assertEqual(len(result['delay_predictions']), 8)
        self.assertEqual(result['summary']['july_roles'], {'assessment':2,'calibration_fit':2})
        for rows in (result['eta_predictions'], result['delay_predictions']):
            self.assertTrue(all('delay_label' not in row and 'actual_arrival_time' not in row for row in rows))
            self.assertEqual({r['training_cutoff_at'] for r in rows}, {'2026-07-01T07:00:00Z'})
        self.assertTrue(all(r['training_cutoff_at']=='2026-07-01T07:00:00Z' for r in result['frozen_history']))
        self.assertEqual({r['sample_weight'] for r in result['delay_training_rows']},{1.0})

    def test_july_feature_values_never_refit_models_transformers_or_history(self):
        changed=deepcopy(self.inputs)
        for row in changed['features']:
            if row['snapshot_time'].startswith('2026-07'):
                row['numeric']['elapsed_trip_seconds']=1e6
                row['categorical']['route_id']='UNSEEN_JULY_ROUTE'
        actual=refit_and_predict(changed,self.source,self.settings)
        for name in ('eta_models','delay_models','transformers','frozen_history','eta_training_rows','delay_training_rows'):
            self.assertEqual(actual[name],self.result[name],name)

    def test_july_or_test_outcomes_cannot_enter_refit(self):
        for trip in ('TRIP-5','FINAL-TEST'):
            changed=deepcopy(self.inputs)
            changed['train_outcomes'].append(dict(tenant_id='T01',trip_id=trip,label_available_time=object()))
            with self.subTest(trip=trip),self.assertRaisesRegex(ValueError,'Non-training outcome'):
                refit_and_predict(changed,self.source,self.settings)

    def test_later_training_outcome_correction_cannot_change_base_models(self):
        changed=deepcopy(self.inputs)
        changed['train_outcomes'].append(dict(tenant_id='T01',trip_id='TRIP-0',label_available_time='2026-07-01T07:00:00Z',
                                             actual_arrival_time=object(),label_status=object()))
        self.assertEqual(refit_and_predict(changed,self.source,self.settings),self.result)

    def test_fit_history_rejects_ambiguous_versions_and_future_arrival_knowledge(self):
        changed=deepcopy(self.inputs)
        changed['train_outcomes'].append(dict(changed['train_outcomes'][0],outcome_version_id='SAME-CLOCK'))
        with self.assertRaisesRegex(ValueError,'simultaneous'):
            refit_and_predict(changed,self.source,self.settings)
        changed=deepcopy(self.inputs)
        changed['train_outcomes'][0]['label_available_time']='2026-01-10T17:30:00Z'
        with self.assertRaisesRegex(ValueError,'after it was known'):
            refit_and_predict(changed,self.source,self.settings)

    def test_model_family_cutoff_heldout_and_original_role_guards(self):
        cases=[]
        settings=deepcopy(self.settings); settings['training_cutoff_at']='2026-08-01T07:00:00Z'
        cases.append((self.inputs,settings,'original training label cutoff'))
        settings=deepcopy(self.settings); settings['eta']['base_candidate_id']='linear_quantile'
        cases.append((self.inputs,settings,'selected model families'))
        inputs=deepcopy(self.inputs); inputs['memberships'][0]['original_split_role']='calibration'
        cases.append((inputs,self.settings,'original frozen split'))
        inputs=deepcopy(self.inputs); inputs['memberships'][0]['unseen_vehicle']=True
        cases.append((inputs,self.settings,'Heldout'))
        for inputs,settings,message in cases:
            with self.subTest(message=message),self.assertRaisesRegex(ValueError,message):
                refit_and_predict(inputs,self.source,settings)

    def test_whole_trip_buffer_opportunities_are_emitted_without_training(self):
        changed=deepcopy(self.inputs)
        for member in changed['memberships']:
            if member['role']=='calibration_fit':
                member.update(role='calibration_buffer',departure_time='2026-07-12T16:00:00Z')
        for row in [*changed['features'], *changed['snapshots']]:
            if row['trip_id']=='TRIP-5':
                row['snapshot_time']='2026-07-12T17:00:00Z'
        for row in changed['promises']:
            if row['trip_id']=='TRIP-5':
                for field in ('known_at','window_start_at','window_end_at'):
                    row[field]='2026-07-12'+row[field][10:]
        result=refit_and_predict(changed,self.source,self.settings)
        self.assertEqual(result['summary']['july_roles'], {'assessment':2,'calibration_buffer':2})
        self.assertEqual(result['eta_models'],self.result['eta_models'])
        self.assertEqual(result['delay_models'],self.result['delay_models'])

    def test_portable_replay_matches_and_cannot_fit_or_read_targets(self):
        clean = json.loads(json.dumps(self.result))
        clean.pop('eta_training_rows')
        clean.pop('delay_training_rows')
        with patch('ds_m01.calibration_refit.fit_candidate', side_effect=AssertionError('fit')), \
             patch('ds_m01.calibration_refit.fit_delay_model', side_effect=AssertionError('fit')), \
             patch('ds_m01.calibration_refit.build_delay_targets', side_effect=AssertionError('labels')):
            actual = replay_predictions(clean, self.source, self.settings)
        for name, rows in actual.items():
            self.assertEqual(rows, self.result[name])
        self.assertEqual(len(actual['baseline_predictions']), 4)

    def test_replay_rejects_extra_models_changed_matrix_and_nontraining_history(self):
        cases = []
        changed = deepcopy(self.result)
        changed['delay_models'].append(dict(changed['delay_models'][0], tenant_id='FOREIGN'))
        cases.append((changed, 'model set'))
        changed = deepcopy(self.result)
        changed['matrices'][0]['vector'][0] += 1
        cases.append((changed, 'Retained matrices'))
        changed = deepcopy(self.result)
        changed['frozen_history'][0]['trip_id'] = 'TRIP-5'
        cases.append((changed, 'original training scope'))
        for changed, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                replay_predictions(changed, self.source, self.settings)

    def test_all_eight_input_hashes_are_mandatory_and_drift_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for relative in SOURCE_FILES:
                path=root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{}\n')
            pins={relative:digest_file(root/relative) for relative in SOURCE_FILES}
            self.assertEqual(verify_source_inputs(root,{'expected_source_sha256':pins}),pins)
            with self.assertRaisesRegex(ValueError,'all eight'):
                verify_source_inputs(root,{'expected_source_sha256':dict(list(pins.items())[:-1])})
            (root/SOURCE_FILES[0]).write_text('changed')
            with self.assertRaisesRegex(ValueError,'source changed'):
                verify_source_inputs(root,{'expected_source_sha256':pins})


if __name__=='__main__':
    unittest.main()
