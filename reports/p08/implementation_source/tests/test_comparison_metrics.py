"""Hand-calculated M3b.3 metric, abstention and carry-forward cases."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from ds_m01.comparison_metrics import BASELINE, compare

PROTOCOL = json.loads((Path(__file__).resolve().parents[1] / 'config/m3b3.json').read_text())
LINEAR, BOOSTED = PROTOCOL['methods'][1:]


def fixture(snapshot, trip, target, baseline, linear, boosted, *, fold='F01'):
    def prediction(value):
        if value is None:
            return {'source': 'unavailable', 'quantiles': None}
        if value == 'fallback':
            return {'source': 'baseline_fallback', 'quantiles': list(baseline)}
        return {'source': 'native', 'quantiles': list(value)}
    return {'snapshot_id': snapshot, 'trip_id': trip, 'tenant_id': 'T1', 'fold_id': fold,
            'target_seconds': target, 'eligible': target is not None,
            'label_status': 'eligible' if target is not None else 'not_mature',
            'predictions': {BASELINE: prediction(baseline), LINEAR: prediction(linear), BOOSTED: prediction(boosted)},
            'route_id': 'R', 'route_support': 'tenant_global', 'position_validity': 'valid',
            'departure_time_bucket': '2-06@America/Los_Angeles', 'elapsed_slice': 'early'}


class ComparisonMetricsTests(unittest.TestCase):
    def test_trip_weighting_fractional_truth_intervals_and_native_denominators(self):
        rows = [fixture('s1', 'short', 10.5, [8, 10, 12], [8, 10, 12], [8, 10, 12]),
                fixture('s2', 'long', 4.5, [3, 7, 9], 'fallback', [3, 7, 9]),
                fixture('s3', 'long', 5.5, [4, 7, 9], [4, 7, 9], [4, 7, 9]),
                fixture('s4', 'unknown', None, None, None, None)]
        summary, slices, decision = compare(rows, PROTOCOL)
        methods = summary['methods']
        # Two trip means: (0.5 + (2.5+1.5)/2)/2 = 1.25, not 1.5.
        score = methods[BASELINE]['cohorts']['delivered']
        self.assertEqual((score['scored_snapshots'], score['scored_trips']), (3, 2))
        self.assertAlmostEqual(score['median_mae_seconds'], 1.25)
        self.assertAlmostEqual(score['median_rmse_seconds'], ((.25 + (6.25+2.25)/2)/2)**.5)
        self.assertAlmostEqual(score['pinball_p50_seconds'], .625)
        self.assertAlmostEqual(score['pinball_p10_seconds'], (.25+.15)/2)
        self.assertAlmostEqual(score['pinball_p90_seconds'], (.15+(.45+.35)/2)/2)
        self.assertEqual(score['p10_p90_coverage'], 1.0)
        self.assertAlmostEqual(score['p10_p90_mean_width_seconds'], (4+(6+5)/2)/2)
        self.assertEqual(methods[LINEAR]['availability']['opportunities'], 4)
        self.assertEqual(methods[LINEAR]['availability']['baseline_fallback_opportunities'], 1)
        self.assertEqual(methods[BASELINE]['cohorts']['common_native']['scored_snapshots'], 2)
        self.assertEqual(methods[LINEAR]['cohorts']['native']['scored_snapshots'], 2)
        self.assertEqual(methods[LINEAR]['cohorts']['baseline_anchor']['scored_snapshots'], 3)
        self.assertEqual(decision['carry_forward_method'], BASELINE)
        self.assertTrue(all(r['sparse'] for r in slices))
        self.assertTrue(any(r['value'] == '06:00–11:59' for r in slices))

    def test_missing_hard_case_and_fallback_only_candidate_cannot_win(self):
        rows = [fixture('s1', 'a', 11, [1, 2, 3], [10, 11, 12], 'fallback'),
                fixture('s2', 'b', 90, [1, 2, 3], None, 'fallback')]
        summary, _, decision = compare(rows, PROTOCOL)
        self.assertEqual(summary['methods'][LINEAR]['cohorts']['native']['median_mae_seconds'], 0)
        self.assertIsNone(summary['methods'][LINEAR]['cohorts']['baseline_anchor']['median_mae_seconds'])
        self.assertFalse(decision['candidates'][LINEAR]['gates']['preserves_baseline_availability'])
        self.assertFalse(decision['candidates'][BOOSTED]['gates']['sufficient_native_coverage'])
        self.assertEqual(decision['carry_forward_method'], BASELINE)

    def test_fallback_coverage_gate_counts_unknown_targets(self):
        rows = [fixture('s1', 'a', 20, [8,10,12], [19,20,21], [19,20,21]),
                fixture('s2', 'b', 20, [8,10,12], [19,20,21], [19,20,21], fold='F02'),
                fixture('s3', 'c', None, [8,10,12], None, [19,20,21])]
        _, _, decision = compare(rows, PROTOCOL)
        self.assertEqual(decision['candidates'][LINEAR]['baseline_supported_opportunities_lost'], 1)
        self.assertEqual(decision['carry_forward_method'], BOOSTED)

    def test_near_tie_chooses_simpler_eligible_method_and_order_is_deterministic(self):
        rows = [fixture(f's{i}', f't{i}', 20, [5, 10, 15], [17, 18, 21], [17, 18.01, 21],
                        fold='F01' if i < 3 else 'F02') for i in range(6)]
        a = compare(rows, PROTOCOL)
        self.assertEqual(a, compare(list(reversed(rows)), PROTOCOL))
        decision = a[2]
        self.assertTrue(all(v['eligible_for_promotion'] for v in decision['candidates'].values()))
        self.assertEqual(decision['carry_forward_method'], LINEAR)
        ci = decision['candidates'][LINEAR]['candidate_minus_baseline_mae_interval']
        self.assertEqual((ci['lower_seconds'], ci['upper_seconds']), (-8.0, -8.0))
        self.assertEqual(ci['fold_trip_counts'], {'F01':3,'F02':3})

    def test_poor_quantile_quality_blocks_good_median_and_null_cohorts_are_honest(self):
        rows = [fixture(f's{i}', f't{i}', 10, [6, 8, 12], [0.1, 10, 400], None) for i in range(3)]
        metrics, _, decision = compare(rows, PROTOCOL)
        self.assertTrue(decision['candidates'][LINEAR]['gates']['mae_reduction'])
        self.assertFalse(decision['candidates'][LINEAR]['gates']['quantile_loss'])
        self.assertEqual(decision['carry_forward_method'], BASELINE)
        self.assertIsNone(metrics['methods'][BOOSTED]['cohorts']['common_native']['median_mae_seconds'])

    def test_inconsistent_fallback_identity_and_unknown_zero_rejected(self):
        row = fixture('s', 't', 10.0, [6, 8, 12], 'fallback', None)
        wrong = deepcopy(row)
        wrong['predictions'][LINEAR]['quantiles'][1] = 9
        invalid = [([wrong], 'Fallback'), ([row, deepcopy(row)], 'Duplicate'),
                   ([dict(row, eligible=False, label_status='not_mature', target_seconds=0)], 'positive'),
                   ([row, dict(row, snapshot_id='new', fold_id='F02')], 'multiple folds')]
        for data, message in invalid:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                compare(data, PROTOCOL)


if __name__ == '__main__':
    unittest.main()
