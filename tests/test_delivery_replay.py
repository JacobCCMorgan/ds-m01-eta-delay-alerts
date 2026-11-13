"""Replay orchestration uses synthetic files; no retained July outcomes are read."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ds_m01.common import digest_file
from ds_m01.delivery_inputs import verify_evaluation_readiness
from ds_m01.delivery_replay import PROTOCOL_SHA256, output_inventory, verify
from ds_m01.delivery_run import summarize


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + '\n')


class DeliveryReplayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.report = self.root / 'fresh-report'
        self.report.mkdir()
        self.settings = dict(base_public_commit='base', source_report_sha256={'source.json':'pinned'},
                             input_sha256={'raw.jsonl':'pinned'}, quality_limits=['synthetic only'],
                             release=dict(canonical_tables=['eta_alert'], remaining_reserved_tables=['eta_action']))
        self.freeze = dict(frozen_at_utc='2026-11-11T19:00:00Z')
        write_json(self.root/'reproducibility/p07/protocol_freeze.json', self.freeze)
        self.simulation = dict(alerts=[], attempts=[], episodes=[], events=[], summary={'counts_as_of':'2026-08-01T07:00:00Z'})
        self.metrics = dict(synthetic_fixture=True, final_test_evaluated=False)
        self.tables = dict(eta_alert=[], eta_action=[])
        self.inputs = dict(opportunities=[], targets=[], candidates=[], episodes=[], parent_tables={'unchanged':[]})
        self.upstream = dict(status='passed', differences=0, source_context_verified=False, july_source_targets_verified=False)
        self.store_receipt = dict(status='passed', counts={'eta_alert':0})
        self.store_replayed = dict(self.store_receipt, logical_rows_compared=0)
        self.codes = {'delivery_run.py':'code-pinned', 'delivery_replay.py':'replay-pinned'}
        for name in output_inventory():
            destination = self.report/name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text('fixture\n')
        for name in ('alerts','attempts','episodes','events'):
            (self.report/(name+'.jsonl')).write_text('')
        for name,value in [('simulation_summary',self.simulation['summary']), ('metrics',self.metrics),
                           ('protocol_freeze',self.freeze), ('parent_replay',self.upstream),
                           ('store_verification',self.store_receipt),
                           ('summary',summarize(self.simulation,self.metrics,self.tables,self.settings))]:
            write_json(self.report/(name+'.json'),value)
        write_json(self.report/'store/verification.json',self.store_receipt)
        self.receipt = dict(status='completed_synthetic_delivery_development', protocol_sha256=PROTOCOL_SHA256,
            freeze_sha256=digest_file(self.root/'reproducibility/p07/protocol_freeze.json'), base_public_commit='base',
            started_at_utc='2026-11-11T20:00:00Z', materialized_at_utc='2026-11-11T20:00:01Z', completed_at_utc='2026-11-11T20:00:02Z',
            code_sha256=self.codes, parent_report_sha256=self.settings['source_report_sha256'], input_sha256=self.settings['input_sha256'],
            delivery_mode='synthetic_scenario', july_source_targets_verified=False, model_or_calibrator_refitted=False,
            policy_reselected=False, real_notifications_sent=False, final_test_evaluated=False, quality_accepted=False)
        self.bind_outputs()
        stack = ExitStack()
        self.addCleanup(stack.close)
        replacements = dict(verify_settings=self.settings, verify_parent=self.root/'parent', verify_freeze=self.freeze,
            code_hashes=self.codes, verify_policy=self.upstream, load_inputs=self.inputs, simulate_delivery=self.simulation,
            score_delivery=self.metrics, build_tables=self.tables, verify_store=self.store_replayed,
            verify_store_receipts=True, verify_evaluation_readiness={})
        self.mocks = {name:stack.enter_context(patch('ds_m01.delivery_replay.'+name,return_value=value))
                      for name,value in replacements.items()}

    def bind_outputs(self):
        self.receipt['output_sha256'] = {name:digest_file(self.report/name) for name in sorted(output_inventory())}
        write_json(self.report/'run_receipt.json',self.receipt)

    def test_valid_replay_rebuilds_each_layer_and_is_read_only(self):
        before = {str(p.relative_to(self.root)):digest_file(p) for p in self.root.rglob('*') if p.is_file()}
        result = verify(self.root,self.report)
        self.assertEqual(result['status'],'passed')
        self.assertFalse(result['july_source_targets_verified'])
        self.mocks['verify_store_receipts'].assert_called_once_with(self.store_receipt,self.store_receipt,self.store_replayed)
        self.mocks['verify_evaluation_readiness'].assert_called_once_with(self.root)
        self.mocks['build_tables'].assert_called_once_with(self.inputs['parent_tables'],self.simulation,
                                                         self.receipt['materialized_at_utc'],self.settings)
        after = {str(p.relative_to(self.root)):digest_file(p) for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before,after)

    def test_scope_flags_require_json_booleans_before_any_policy_replay(self):
        for field in ('model_or_calibrator_refitted','policy_reselected','real_notifications_sent','final_test_evaluated','quality_accepted'):
            for value in (0,None,'false',True):
                receipt = dict(self.receipt,**{field:value})
                write_json(self.report/'run_receipt.json',receipt)
                with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                    verify(self.root,self.report)
        self.mocks['verify_policy'].assert_not_called()

    def test_output_inventory_rejects_missing_extra_and_traversal_entries(self):
        for name in ('metrics.json','../outside.json','/absolute.json'):
            receipt = deepcopy(self.receipt)
            if name=='metrics.json':
                receipt['output_sha256'].pop(name)
            else:
                receipt['output_sha256'][name]='invented-digest'
            write_json(self.report/'run_receipt.json',receipt)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError,'complete output inventory'):
                verify(self.root,self.report)
        self.mocks['verify_policy'].assert_not_called()

    def test_rehashed_metric_corruption_is_rejected_by_recomputation(self):
        write_json(self.report/'metrics.json',dict(self.metrics,final_test_evaluated=0))
        self.bind_outputs()
        with self.assertRaisesRegex(ValueError,'delivery metrics'):
            verify(self.root,self.report)

    def test_code_inventory_and_parent_result_cannot_be_replaced_by_hashes(self):
        receipt = dict(self.receipt,code_sha256={})
        write_json(self.report/'run_receipt.json',receipt)
        with self.assertRaisesRegex(ValueError,'executed source inventory'):
            verify(self.root,self.report)
        write_json(self.report/'parent_replay.json',dict(self.upstream,differences=1))
        self.bind_outputs()
        with self.assertRaisesRegex(ValueError,'historical parent replay scope'):
            verify(self.root,self.report)

    def test_historical_source_mode_does_not_overclaim_current_retained_replay(self):
        self.receipt['july_source_targets_verified']=True
        write_json(self.report/'parent_replay.json',dict(self.upstream,source_context_verified=True,july_source_targets_verified=True))
        self.bind_outputs()
        result=verify(self.root,self.report)
        self.assertFalse(result['july_source_targets_verified'])
        self.mocks['verify_policy'].assert_called_once_with(self.root,self.root/'parent',input_root=None)

    def test_store_and_readiness_failures_propagate_without_success_claim(self):
        for name in ('verify_store_receipts','verify_evaluation_readiness'):
            self.mocks[name].side_effect=ValueError('independent '+name+' failure')
            with self.subTest(name=name), self.assertRaisesRegex(ValueError,'independent '+name):
                verify(self.root,self.report)
            self.mocks[name].side_effect=None


class EvaluationReadinessTests(unittest.TestCase):
    def test_all_six_delivery_sources_and_supporting_run_are_bound_after_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            paths={'config/p07.json','config/p08_evaluation.json','reproducibility/p07/protocol_freeze.json',
                   'reports/p07/run_receipt.json','reports/p07/metrics.json','reports/p07/summary.json'}
            modules=('delivery_inputs.py','delivery_simulation.py','delivery_metrics.py','delivery_store.py','delivery_run.py','delivery_replay.py')
            paths|={'src/ds_m01/'+name for name in modules}
            for name in paths:
                write_json(root/name,{'completed_at_utc':'2026-11-11T20:00:02Z'})
            record=dict(status='ready_for_p08_implementation',july_delivery_results_previously_observed=True,
                        final_test_evaluated=False,quality_accepted=False,p08_evaluator_source_freeze_required=True,
                        recorded_at_utc='2026-11-11T20:00:03Z',artifact_sha256={name:digest_file(root/name) for name in paths})
            destination=root/'reproducibility/p07/evaluation_readiness.json'
            write_json(destination,record)
            self.assertEqual(verify_evaluation_readiness(root),record)
            for name in modules:
                changed=deepcopy(record); changed['artifact_sha256'].pop('src/ds_m01/'+name)
                write_json(destination,changed)
                with self.subTest(name=name), self.assertRaisesRegex(ValueError,'omits a required'):
                    verify_evaluation_readiness(root)
            write_json(destination,dict(record,recorded_at_utc='2026-11-11T20:00:01Z'))
            with self.assertRaisesRegex(ValueError,'Readiness precedes'):
                verify_evaluation_readiness(root)
            write_json(destination,record)
            write_json(root/'reports/p07/metrics.json',{'changed':True})
            with self.assertRaisesRegex(ValueError,'artifact changed'):
                verify_evaluation_readiness(root)


if __name__=='__main__':
    unittest.main()
