"""Inference-only replay stays label-free and rejects evidence or scope drift."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ds_m01.common import digest_file, write_jsonl
from ds_m01.delay_replay import verify, compare_values, _verify_report, _rebuild_training_rows
from ds_m01.delay_run import PROTOCOL_SHA256, OUTPUT_FILES, CODE_MODULES
from test_delay_run import fixture, execute, PROJECT


class DelayReplayTests(unittest.TestCase):
    def test_inference_only_reads_no_raw_outcomes_and_never_fits(self):
        values=fixture(); result=execute(values); settings=values[3]
        settings['expected_validation_opportunities_per_method']=4
        with tempfile.TemporaryDirectory() as directory:
            report=Path(directory)
            (report/'models.json').write_text(json.dumps(result['models']))
            write_jsonl(report/'predictions.jsonl',result['predictions'])
            write_jsonl(report/'promise_features.jsonl',result['promise_features'])
            with patch('ds_m01.delay_replay._verify_report',return_value=(settings,{'evaluation_executed':False})), \
                 patch('ds_m01.delay_replay.load_retained_inputs',return_value=values[0]), \
                 patch('ds_m01.delay_replay.load_sources',side_effect=AssertionError('raw input opened')), \
                 patch('ds_m01.delay_model.fit_delay_model',side_effect=AssertionError('refit')):
                receipt=verify(PROJECT,report)
                self.assertEqual(receipt['prediction_rows'],8)
                self.assertFalse(receipt['outcomes_opened'])
                self.assertFalse(receipt['training_executed'])
                self.assertFalse(receipt['targets_verified'])
                self.assertEqual(receipt['differences'],0)
                extra=deepcopy(result['models'][0]); extra['fold_id']='UNKNOWN'
                (report/'models.json').write_text(json.dumps(result['models']+[extra]))
                with self.assertRaisesRegex(ValueError,'model set'):
                    verify(PROJECT,report)

    def test_fit_target_and_weight_reconstruction_matches_generated_fixture(self):
        values=fixture(); expected=execute(values)
        settings=dict(values[3],_source_config=values[4])
        actual=_rebuild_training_rows(values[0],values[1],values[-1],values[2],settings)
        self.assertEqual(expected['training_rows'],actual)

    def test_exact_decisions_and_bounded_probabilities(self):
        tolerance={'absolute_tolerance':1e-12,'relative_tolerance':1e-10}
        compare_values({'probability':.5,'label':False},{'probability':.5+1e-13,'label':False},**tolerance)
        for expected,actual in [({'probability':.5},{'probability':.501}),({'label':False},{'label':0}),
                                ({'x':[1,2]},{'x':[1]}),({'x':1},{'x':1,'extra':2}),
                                ({'x':.5},{'x':float('nan')})]:
            with self.subTest(actual=actual),self.assertRaises(ValueError):
                compare_values(expected,actual,**tolerance)

    def test_scope_code_manifest_and_output_hash_are_mandatory(self):
        settings=json.loads((PROJECT/'config/m3c.json').read_text())
        receipt={'protocol_sha256':PROTOCOL_SHA256,'evaluation_executed':False,
                 'validation_targets_used_in_fitting':False,'calibration_used':False,'final_test_evaluated':False,
                 'input_sha256':{**settings['expected_retained_sha256'],**settings['expected_source_sha256']},
                 'code_sha256':{n:digest_file(PROJECT/'src/ds_m01'/n) for n in CODE_MODULES}}
        with tempfile.TemporaryDirectory() as directory:
            report=Path(directory)
            for name in OUTPUT_FILES:
                (report/name).write_text('{}\n')
            receipt['output_sha256']={n:digest_file(report/n) for n in OUTPUT_FILES}
            path=report/'run_receipt.json'; path.write_text(json.dumps(receipt))
            _verify_report(PROJECT,report)
            for changed,message in [(dict(receipt,code_sha256={}), 'every execution'),
                                    (dict(receipt,calibration_used=True),'declared scope'),
                                    (dict(receipt,evaluation_executed='false'),'Boolean')]:
                path.write_text(json.dumps(changed))
                with self.subTest(message=message),self.assertRaisesRegex(ValueError,message):
                    _verify_report(PROJECT,report)
            path.write_text(json.dumps(receipt)); (report/'models.json').write_text('tampered')
            with self.assertRaisesRegex(ValueError,'output changed'):
                _verify_report(PROJECT,report)


if __name__=='__main__':
    unittest.main()
