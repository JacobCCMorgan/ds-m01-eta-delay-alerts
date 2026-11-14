import copy
import unittest

from ds_m01.calibration_metrics import summarize_intervals, build_metrics


def row(trip, sid, target=5, lo=2, hi=8, status='available'):
    return dict(tenant_id='T1', trip_id=trip, snapshot_id=sid, snapshot_time='2026-07-16T08:00:00Z',
                fold_id='R01', calibration_role='assessment', variant='raw',
                eligibility='eligible', reason='verified_gate_arrival', label_status='verified',
                target_seconds=target, delay_label=False, status=status, prediction_source='native',
                lower_seconds=lo, median_seconds=5, upper_seconds=hi, elapsed_trip_seconds=100,
                deadline_remaining_seconds=1000, position_validity='fresh', route_id='R1')


class CalibrationMetricsTests(unittest.TestCase):
    def test_trip_weighting_and_simultaneous_coverage_differ(self):
        rows = [row('A','1'), row('A','2', target=10), row('B','3')]
        r = summarize_intervals(rows)
        self.assertEqual(r['metrics']['trip_weighted_snapshot_coverage'], .75)
        self.assertEqual(r['metrics']['trip_all_eligible_snapshots_coverage'], .5)
        self.assertEqual(r['metrics']['median_mae_seconds'], 1.25)
        self.assertEqual(r['metrics']['mean_interval_score_seconds'], 11)

    def test_unavailable_eligible_trip_stays_in_trip_denominator(self):
        a, b = row('A','1'), row('B','2')
        b.update(status='unavailable', prediction_source='unavailable', lower_seconds=None, median_seconds=None, upper_seconds=None)
        r = summarize_intervals([a,b])
        self.assertEqual(r['counts']['unavailable_eligible_snapshots'], 1)
        self.assertEqual(r['metrics']['trip_weighted_snapshot_coverage'], 1)
        self.assertEqual(r['metrics']['trip_all_eligible_snapshots_coverage'], .5)
        self.assertEqual(r['metrics']['trip_all_snapshots_coverage_given_full_availability'], 1)

    def test_excluded_labels_visible_and_no_score(self):
        a = row('A','1')
        a.update(eligibility='excluded', reason='pending', target_seconds=None)
        r = summarize_intervals([a])
        self.assertEqual(r['counts']['excluded_snapshots'], 1)
        self.assertIsNone(r['metrics']['mean_width_seconds'])

    def test_zero_lower_endpoint_is_valid(self):
        self.assertEqual(summarize_intervals([row('A','1',lo=0)])['metrics']['mean_width_seconds'], 8)

    def test_duplicate_and_bad_values_rejected(self):
        for rows in ([row('A','1'),row('A','1')], [row('A','1',lo=-1)], [row('A','1',hi=float('inf'))], [row('A','1',target=0)]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                summarize_intervals(rows)

    def test_empty_is_defined(self):
        r = summarize_intervals([])
        self.assertEqual(r['counts']['opportunities'], 0)
        self.assertIsNone(r['metrics']['trip_all_eligible_snapshots_coverage'])

    def test_pairing_rejects_dropped_forecast_and_changed_label(self):
        a = row('A','1')
        b = copy.deepcopy(a)
        b['variant'] = 'calibrated'
        for changed in ({'snapshot_id':'missing'}, {'target_seconds':6}, {'median_seconds':6}, {'status':'unavailable'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                build_metrics([a,{**b,**changed}], [])

    def test_probability_summary_keeps_one_class_reason(self):
        a=row('A','1')
        eta=[a,{**a,'variant':'calibrated','lower_seconds':0,'upper_seconds':10,'calibration_applied':True}]
        p={**a,'prediction_source':'model','probability':.2}
        prob=[p,{**p,'variant':'calibrated','probability':.1,'calibration_applied':True}]
        result=build_metrics(eta,prob)
        m=result['probability']['assessment']['overall']['calibrated']
        self.assertAlmostEqual(m['metrics']['brier_score'],.01)
        self.assertIsNone(m['metrics']['roc_auc'])
        self.assertEqual(m['ranking_undefined_reason'],'single_class_scored_labels')
        self.assertFalse(result['final_test_evaluated'])
