import copy
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ds_m01.calibration_run import prepare_role_rows, verify_settings, run

SETTINGS={'final_training_id':'R01','calibration_fit_cutoff_at':'2026-07-15T07:00:00Z','assessment_label_cutoff_at':'2026-08-01T07:00:00Z'}

def fixture():
    identity=dict(tenant_id='T',trip_id='A',snapshot_id='S',snapshot_time='2026-07-16T08:00:00Z',promise_version_id='P')
    member=dict(tenant_id='T',trip_id='A',role='assessment',departure_time=identity['snapshot_time'])
    feature={**identity,'categorical':{'route_id':'R','position_validity':'fresh'},'numeric':{'elapsed_trip_seconds':0}}
    promise={**identity,'promise_seconds_remaining':100,'deadline_at':'2026-07-16T08:01:40Z'}
    common={**identity,'fold_id':'R01','route_id':'R','position_validity':'fresh','elapsed_trip_seconds':0,
            'deadline_remaining_seconds':100,'deadline_at':promise['deadline_at'],'model_sha256':'a'*64,
            'status':'available'}
    eta={**common,'prediction_source':'candidate','p10_remaining_seconds':1,'p50_remaining_seconds':5,'p90_remaining_seconds':10}
    p={**common,'method_id':'logistic_delay','prediction_source':'model','probability':.2}
    artifacts={'memberships':[member],'features':[feature],'promise_features':[promise],'eta_predictions':[eta],'delay_predictions':[p]}
    inputs={'memberships':[copy.deepcopy(member)],'features':[copy.deepcopy(feature)],'snapshots':[identity]}
    return artifacts,inputs

class CalibrationRunTests(unittest.TestCase):
    def invoke(self, artifacts, inputs):
        calls=[]
        def labels(opps, keys, *args):
            cutoff=args[-1]; calls.append((keys,cutoff))
            return [{**r,'eligibility':'eligible','label_status':'verified','reason':'verified',
                     'target_seconds':5,'delay_label':False,'label_known_at':'2026-07-17T00:00:00Z'} for r in opps]
        with ExitStack() as stack:
            stack.enter_context(patch('ds_m01.calibration_run.verify_source_inputs',return_value={}))
            stack.enter_context(patch('ds_m01.calibration_run.prepare_refit_inputs',return_value=inputs))
            stack.enter_context(patch('ds_m01.calibration_run.read_jsonl',return_value=[]))
            stack.enter_context(patch('ds_m01.calibration_run.build_eta_targets',side_effect=labels))
            stack.enter_context(patch('ds_m01.calibration_run.build_delay_targets',side_effect=labels))
            result=prepare_role_rows(artifacts,Path('/unused'),SETTINGS,{})
        return result,calls
    def test_joins_only_declared_role_at_its_cutoff(self):
        a,i=fixture(); result,calls=self.invoke(a,i)
        self.assertEqual(result['calibration_fit']['eta'],[])
        self.assertEqual(len(result['assessment']['eta']),1)
        self.assertEqual(calls,[ (set(),SETTINGS['calibration_fit_cutoff_at'])]*2+[( {('T','A')},SETTINGS['assessment_label_cutoff_at'])]*2)
    def test_consistently_dropped_feature_and_predictions_rejected(self):
        a,i=fixture(); a['features']=[];a['eta_predictions']=[];a['delay_predictions']=[]
        with self.assertRaisesRegex(ValueError,'complete source inventory'):self.invoke(a,i)
    def test_changed_probability_trip_or_clock_rejected(self):
        for field,value in [('trip_id','OTHER'),('snapshot_time','2026-07-17T08:00:00Z'),('promise_version_id','OTHER'),('route_id','OTHER'),('deadline_remaining_seconds',999)]:
            a,i=fixture();a['delay_predictions'][0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):self.invoke(a,i)
    def test_changed_common_covariate_cannot_hide_source_difference(self):
        a,i=fixture()
        for r in (a['eta_predictions'][0],a['delay_predictions'][0]):r['position_validity']='stale'
        with self.assertRaises(ValueError):self.invoke(a,i)
    def test_buffer_opportunity_retained_but_no_target_join(self):
        a,i=fixture()
        a['memberships'][0]['role']=i['memberships'][0]['role']='calibration_buffer'
        result,calls=self.invoke(a,i)
        self.assertTrue(all(not keys for keys,cutoff in calls))
        self.assertFalse(result['assessment']['eta'])
    def test_existing_output_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker=Path(tmp)/'keep'; marker.write_text('original')
            with self.assertRaisesRegex(ValueError,'new output'):run(tmp,tmp,tmp)
            self.assertEqual(marker.read_text(),'original')
    def test_protocol_mutation_rejected_before_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'protocol.json';p.write_text('{}')
            with self.assertRaisesRegex(ValueError,'pre-outcome freeze'):verify_settings(tmp,p)
