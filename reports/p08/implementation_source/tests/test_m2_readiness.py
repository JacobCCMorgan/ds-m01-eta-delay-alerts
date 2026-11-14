"""Regression tests for misleading full-scale and model-readiness claims."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('m2_readiness', Path(__file__).parents[1] / 'scripts/check_m2_readiness.py')
readiness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readiness)


def write_rows(root, name, rows):
    path = root / 'data/raw' / (name + '.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))


class ContinuityTest(unittest.TestCase):
    def test_equal_row_counts_do_not_hide_changed_business_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            m1, m2 = Path(tmp) / 'm1', Path(tmp) / 'm2'
            write_rows(m1, 'eta_trip', [{'trip_id': 'A', 'registered_at': '2026-01-01'}])
            write_rows(m2, 'eta_trip', [{'trip_id': 'A', 'registered_at': '2026-02-01'}])
            report = readiness.check_continuity(m1, m2)
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['files'][0]['missing_or_changed_m1_rows'], 1)

    def test_expansion_preserves_business_rows_despite_new_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            m1, m2 = Path(tmp) / 'm1', Path(tmp) / 'm2'
            write_rows(m1, 'eta_trip', [{'trip_id': 'A'}])
            write_rows(m2, 'eta_trip', [{'trip_id': 'B'}, {'trip_id': 'A'}])
            write_rows(m1, 'provenance_record', [{'retrieved_at': 'old receipt'}])
            write_rows(m2, 'provenance_record', [{'retrieved_at': 'new receipt'}])
            report = readiness.check_continuity(m1, m2)
            self.assertEqual(report['status'], 'passed')
            self.assertEqual(len(report['files']), 1)


class SupportTest(unittest.TestCase):
    @staticmethod
    def supported_roles():
        return [dict(split_role=role, eligible_trips=100,
                     delayed_eligible_snapshots=60, on_time_eligible_snapshots=80)
                for role in ('train', 'calibration', 'test')]

    def test_no_delay_class_blocks_even_with_many_snapshots(self):
        roles = self.supported_roles()
        roles[1]['delayed_eligible_snapshots'] = 0
        result = readiness.support_assessment(roles, [])
        self.assertFalse(result['ready_to_attempt_m3'])
        self.assertTrue(any('calibration' in message for message in result['blockers']))

    def test_sparse_heldout_cohort_is_visible_without_claiming_power(self):
        cohort = dict(cohort='unseen_vehicle_and_route', eligible_trips=2,
                      delayed_eligible_snapshots=3, on_time_eligible_snapshots=4)
        result = readiness.support_assessment(self.supported_roles(), [cohort])
        self.assertTrue(result['ready_to_attempt_m3'])
        self.assertTrue(any('unseen_vehicle_and_route' in message for message in result['cautions']))
        self.assertIn('not a statistical power', result['threshold_meaning'])


if __name__ == '__main__':
    unittest.main()
