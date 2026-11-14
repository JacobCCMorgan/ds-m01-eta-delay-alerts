"""Synthetic boundary checks for durable pre-target evidence and exact replay."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ds_m01.common import digest_file, write_jsonl
from ds_m01.final_gate import OUTCOME_PATH, write_json
from ds_m01.final_replay import compare
from ds_m01.final_run import LABEL_FREE_SCENARIO_FILES, inventory, open_final_outcomes, read_payload, save_payload


class FinalRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.attempt = self.root / 'attempt'
        self.attempt.mkdir()
        self.source = self.root / 'input'
        (self.source / OUTCOME_PATH).parent.mkdir(parents=True)
        write_jsonl(self.source / OUTCOME_PATH, [
            {'tenant_id': 'T1', 'trip_id': 'A', 'synthetic_fixture': True},
            {'tenant_id': 'T2', 'trip_id': 'B', 'synthetic_fixture': True}])
        self.settings = {'input_sha256': {OUTCOME_PATH: digest_file(self.source / OUTCOME_PATH)},
                         'stress': {'scenarios': [{'id': 'S' + str(i)} for i in range(9)]}}
        self.inputs = {'memberships': [{'tenant_id': 'T1', 'trip_id': 'A'}], 'inventory': {'synthetic_fixture': True}}

    def seal(self):
        write_json(self.attempt / 'forecasts.json', {'label_free': True})
        paths = ['implementation_freeze.json', 'inventory.json', 'inputs/memberships.jsonl',
                 'inputs/snapshots.jsonl', 'inputs/features.jsonl', 'inputs/promise_features.jsonl',
                 'inputs/promises.jsonl', 'inputs/inventory.json', 'inputs/source_sha256.json']
        paths += ['scenarios/' + s['id'] + '/' + name for s in self.settings['stress']['scenarios']
                  for name in LABEL_FREE_SCENARIO_FILES]
        for path in paths:
            write_json(self.attempt / path, {'synthetic_fixture': True})
        receipt = {'status': 'all_label_free_scenarios_retained_before_target_access',
                   'scenario_ids': [s['id'] for s in self.settings['stress']['scenarios']],
                   'metadata_inventory': self.inputs['inventory'], 'final_outcomes_opened': False,
                   'quality_accepted': False, 'model_or_calibrator_refitted': False, 'policy_reselected': False,
                   'output_sha256': inventory(self.attempt)}
        write_json(self.attempt / 'pretarget_receipt.json', receipt)
        return receipt

    def test_no_receipt_means_no_target_reader_or_exposure(self):
        with patch('ds_m01.final_run.read_jsonl') as reader:
            with self.assertRaisesRegex(ValueError, 'pre-target receipt'):
                open_final_outcomes(self.source, self.settings, self.inputs, self.attempt, {})
        reader.assert_not_called()
        self.assertFalse((self.attempt / 'exposure_record.json').exists())

    def test_tampered_label_free_artifact_blocks_target_access(self):
        receipt = self.seal()
        write_json(self.attempt / 'forecasts.json', {'label_free': False})
        with patch('ds_m01.final_run.read_jsonl') as reader:
            with self.assertRaisesRegex(ValueError, 'Label-free output changed'):
                open_final_outcomes(self.source, self.settings, self.inputs, self.attempt, receipt)
        reader.assert_not_called()
        self.assertFalse((self.attempt / 'exposure_record.json').exists())

    def test_empty_hash_map_or_missing_scenario_cannot_authorize_targets(self):
        for change in ({'output_sha256': {}}, {'scenario_ids': ['S0']}, {'status': 'failed'},
                       {'final_outcomes_opened': True}, {'metadata_inventory': {}}):
            receipt = {**self.seal(), **change}
            write_json(self.attempt / 'pretarget_receipt.json', receipt)
            with self.assertRaises(ValueError):
                open_final_outcomes(self.source, self.settings, self.inputs, self.attempt, receipt)
            self.assertFalse((self.attempt / 'exposure_record.json').exists())

    def test_access_record_precedes_reader_and_selects_only_fixed_trips(self):
        receipt = self.seal()
        from ds_m01.common import read_jsonl
        def guarded_reader(path):
            self.assertTrue((self.attempt / 'exposure_record.json').is_file())
            return read_jsonl(path)
        with patch('ds_m01.final_run.read_jsonl', side_effect=guarded_reader):
            rows = open_final_outcomes(self.source, self.settings, self.inputs, self.attempt, receipt)
        self.assertEqual([('T1', 'A')], [(r['tenant_id'], r['trip_id']) for r in rows])
        with self.assertRaises(FileExistsError):
            open_final_outcomes(self.source, self.settings, self.inputs, self.attempt, receipt)

    def test_source_hash_failure_keeps_exposure_record(self):
        receipt = self.seal()
        self.settings['input_sha256'][OUTCOME_PATH] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'outcome source changed'):
            open_final_outcomes(self.source, self.settings, self.inputs, self.attempt, receipt)
        self.assertTrue((self.attempt / 'exposure_record.json').is_file())

    def test_serialized_forecast_payload_has_one_canonical_filename(self):
        payload = {'eta_rows': [{'median_seconds': 3.5}], 'probability_rows': [], 'inventory': {'n': 1}}
        save_payload(self.attempt / 'case', payload)
        self.assertEqual(payload, read_payload(self.attempt / 'case', forecasts=True))
        self.assertTrue((self.attempt / 'case/forecast_eta.jsonl').exists())
        self.assertFalse((self.attempt / 'case/eta_rows.jsonl').exists())

    def test_replay_allows_only_declared_numeric_tolerance(self):
        self.assertEqual(1, compare(1.0, 1.0 + 1e-11))
        with self.assertRaises(ValueError):
            compare(1.0, 1.001)
        for old, new in [('0.550000', '0.549999'), (1, 1.0), (True, 1), ('candidate-A', 'candidate-B')]:
            with self.assertRaises(ValueError):
                compare(old, new)


if __name__ == '__main__':
    unittest.main()
