"""Parent input binding and strict basic contract validation."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from workflows.p09 import inputs
from workflows.p09.tests.test_engine import fixture


ROOT = Path(__file__).resolve().parents[3]


class WorkflowInputTests(unittest.TestCase):
    def test_published_parent_loads_with_exact_primary_counts(self):
        value = inputs.load_parent(ROOT)
        self.assertEqual(len(value['opportunities']), 3200)
        self.assertEqual(len(value['alerts']), 63)
        self.assertEqual(len(value['attempts']), 59)
        self.assertEqual(len({(r['tenant_id'], r['trip_id']) for r in value['opportunities']}), 400)
        self.assertTrue(all(not value['parent_tables'][name]
                            for name in ('eta_adoption', 'eta_action', 'eta_action_followup')))

    def test_parent_receipt_change_is_rejected(self):
        with patch.object(inputs, 'digest_file', return_value='0' * 64):
            with self.assertRaisesRegex(ValueError, 'receipt changed'):
                inputs.load_parent(ROOT)

    def test_parent_retained_artifact_change_is_rejected(self):
        original = inputs.digest_file
        def changed(path):
            return '0' * 64 if Path(path).name == 'outcome_source.jsonl' else original(path)
        with patch.object(inputs, 'digest_file', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'Retained P08 evidence changed'):
                inputs.load_parent(ROOT)

    def test_settings_boolean_numeric_and_acceptance_checks(self):
        settings = inputs.load_settings(ROOT)
        for field, value in [('seed', True), ('handling_seconds_min', -1),
                             ('decision_delay_seconds_span', 0), ('causal_benefit_estimated', True)]:
            broken = {**settings, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                inputs.validate_settings(broken)

    def test_naive_cutoff_is_rejected(self):
        settings = inputs.load_settings(ROOT)
        settings['observation_cutoff_at'] = '2026-10-15T07:00:00'
        with self.assertRaisesRegex(ValueError, 'Timezone'):
            inputs.validate_settings(settings)

    def test_future_original_promise_is_rejected(self):
        value = fixture(1)
        value['promises'][0]['known_at'] = '2026-09-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError, 'future source'):
            inputs.validate_inputs(value)

    def test_mismatched_original_promise_deadline_is_rejected(self):
        value = fixture(1)
        value['promises'][0]['window_end_at'] = '2026-08-02T14:00:00Z'
        with self.assertRaisesRegex(ValueError, 'deadline'):
            inputs.validate_inputs(value)

    def test_arrival_known_before_event_is_rejected(self):
        value = fixture(1)
        value['outcomes'][1]['label_available_time'] = '2026-08-02T13:00:00Z'
        with self.assertRaisesRegex(ValueError, 'available before'):
            inputs.validate_inputs(value)


if __name__ == '__main__':
    unittest.main()
