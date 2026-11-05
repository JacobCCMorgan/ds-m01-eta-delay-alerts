"""Independent end-to-end fold isolation, promise timing and fallback fixtures."""

from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from ds_m01.delay_run import train_and_predict, run
from ds_m01.delay_targets import build_delay_targets
from ds_m01.common import parse_ts
from test_candidate_run import fixture as eta_fixture

PROJECT = Path(__file__).resolve().parents[1]


def fixture():
    inputs, _, source, outcomes = eta_fixture()
    config = json.loads((PROJECT / 'config/m3c.json').read_text())
    config['folds'] = config['folds'][:1]
    snapshots, promises = [], []
    for feature in inputs['features']:
        day = feature['snapshot_time'][:10]
        trip = feature['trip_id']
        index = int(trip[-1])
        snapshots.append({**{k:feature[k] for k in ('tenant_id','trip_id','snapshot_id','snapshot_time')}, 'promise_version_id': 'P-'+trip})
        promises.append(dict(tenant_id=feature['tenant_id'], trip_id=trip, promise_version_id='P-'+trip,
                             known_at=day+'T16:00:00Z', window_start_at=day+'T17:00:00Z',
                             window_end_at=day+('T17:30:00Z' if index%2 == 0 else 'T18:30:00Z'), provenance_id='PP-'+trip))
    for outcome in outcomes:
        outcome.update(arrival_evidence_ref='gate-observation', provenance_id='PO-'+outcome['trip_id'], cancellation_time=None)
    return inputs, snapshots, promises, config, source, outcomes


def execute(values, calls=None):
    inputs, snapshots, promises, config, source, outcomes = values
    def labels(opportunities, keys, cutoff):
        if calls is not None:
            calls.append((opportunities,keys,cutoff))
        return build_delay_targets(opportunities,keys,outcomes,promises,cutoff)
    return train_and_predict(inputs,snapshots,promises,config,source,labels)


class DelayRunnerTests(unittest.TestCase):
    def test_only_fit_keys_and_clocks_enter_training_and_every_validation_is_predicted(self):
        values=fixture(); calls=[]
        output=execute(values,calls)
        self.assertEqual(len(calls),1)
        opportunities,keys,cutoff=calls[0]
        self.assertEqual(len(keys),10)
        self.assertTrue(all(r['fold_role']=='fit' for r in opportunities))
        self.assertEqual(parse_ts(cutoff),parse_ts(values[3]['folds'][0]['training_cutoff_at']))
        self.assertEqual(len(output['training_rows']),10)
        self.assertEqual(len(output['predictions']),8)
        self.assertTrue(all(r['trip_id'] in ('TRIP-5','TRIP-6') for r in output['predictions']))
        self.assertTrue(all('delay_label' not in r for r in output['predictions']))
        self.assertEqual({r['sample_weight'] for r in output['training_rows']},{1.0})

    def test_validation_target_mutations_do_not_change_any_model_or_prediction(self):
        values=fixture(); expected=execute(values)
        changed=deepcopy(values)
        for row in changed[-1]:
            if row['trip_id'] in ('TRIP-5','TRIP-6'):
                row.update(label_status=object(),actual_arrival_time=object())
        actual=execute(changed)
        self.assertEqual(actual,expected)

    def test_single_class_is_recorded_and_classifier_uses_its_declared_prior(self):
        values=fixture()
        for promise in values[2]:
            promise['window_end_at']=promise['window_end_at'][:10]+'T20:30:00Z'
        output=execute(values)
        for row in output['predictions']:
            self.assertEqual(row['probability'],0.0)
            if row['method_id']=='logistic_delay':
                self.assertEqual(row['prediction_source'],'prevalence_fallback')
                self.assertEqual(row['candidate_reason'],'insufficient_class_trip_support')
                self.assertIsNotNone(row['fallback_model_sha256'])

    def test_missing_snapshot_or_changed_key_preserving_label_join_fails(self):
        values=fixture()
        with self.assertRaisesRegex(ValueError,'every retained'):
            train_and_predict(values[0],values[1][1:],values[2],values[3],values[4],lambda *args: [])
        def wrong(opportunities,keys,cutoff):
            targets=build_delay_targets(opportunities,keys,values[-1],values[2],cutoff)
            targets[0]['trip_id']='SWAPPED'
            return targets
        with self.assertRaisesRegex(ValueError,'identity or promise'):
            train_and_predict(*values[:5],wrong)

    def test_existing_output_and_changed_protocol_fail_before_fitting(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            marker=root/'marker'; marker.write_text('accepted')
            with self.assertRaises(FileExistsError):
                run(PROJECT,root,root)
            self.assertEqual(marker.read_text(),'accepted')
            (root/'config').mkdir()
            (root/'config/m3c.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'pre-fit'):
                run(root,root,root/'new')
            self.assertFalse((root/'new').exists())


if __name__=='__main__':
    unittest.main()
