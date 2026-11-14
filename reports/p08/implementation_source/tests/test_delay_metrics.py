"""Hand calculations and external metric comparisons for probability assessment."""
from copy import deepcopy
import math
import random
import unittest

from ds_m01.delay_metrics import evaluate_methods, summarize


def row(snapshot, trip, y, p, *, method='logistic_delay', fold='F01', source=None, **extra):
    value = dict(tenant_id='T01', trip_id=trip, snapshot_id=snapshot, fold_id=fold,
                 method_id=method, eligibility='eligible' if y is not None else 'excluded',
                 delay_label=y, label_status='verified' if y is not None else 'pending',
                 reason=None if y is not None else 'not_mature',
                 probability=p, status='available' if p is not None else 'unavailable',
                 prediction_source=source or ('model' if p is not None else 'unavailable'),
                 snapshot_time='2026-03-01T12:00:00Z', elapsed_trip_seconds=60,
                 deadline_remaining_seconds=3600, position_validity='valid',
                 route_id='R01', departure_time_bucket='1-08@America/Los_Angeles')
    value.update(extra)
    return value


def pair(snapshot, trip, y, baseline, candidate, **extra):
    return [row(snapshot, trip, y, baseline, method='fold_prevalence', **extra),
            row(snapshot, trip, y, candidate, **extra)]


class DelayMetricsTests(unittest.TestCase):
    def test_trip_weighted_losses_prevalence_and_class_counts(self):
        rows = [row('s1', 'short', True, .2), row('s2', 'long', False, .1),
                row('s3', 'long', True, .8), row('s4', 'unknown', None, .9)]
        r = summarize(rows)
        self.assertEqual(r['counts']['opportunities'], 4)
        self.assertEqual((r['counts']['scored_snapshots'], r['counts']['scored_trips']), (3, 2))
        # Trip A has error .64; trip B averages .01 and .04. Snapshot counts
        # must not let trip B receive twice the total mass of trip A.
        self.assertAlmostEqual(r['metrics']['brier_score'], (.64 + (.01+.04)/2)/2)
        self.assertAlmostEqual(r['metrics']['log_loss'], (-math.log(.2)+(-math.log(.9)-math.log(.8))/2)/2)
        self.assertAlmostEqual(r['metrics']['weighted_event_rate'], .75)
        self.assertAlmostEqual(r['metrics']['mean_probability'], (.2+(.1+.8)/2)/2)
        self.assertEqual(r['scored_class_support'], dict(positive_snapshots=2, negative_snapshots=1,
                                                       positive_trips=2, negative_trips=1, mixed_label_trips=1))
        self.assertEqual(r['exclusion_reason_counts'], {'not_mature': 1})
        self.assertNotIn('accuracy', r['metrics'])

    def test_weighted_rank_metrics_agree_with_sklearn_grouped_ties(self):
        from sklearn.metrics import average_precision_score, roc_auc_score, brier_score_loss, log_loss
        import numpy as np
        rng = random.Random(2718)
        labels, scores = [], []
        rows = []
        for t, n in enumerate((1, 3, 2, 7, 4, 1)):
            for j in range(n):
                y = bool(rng.randrange(2)); p = rng.choice((0, .1, .4, .5, .9, 1))
                rows.append(row(f's{t}_{j}', f't{t}', y, p))
                labels.append(int(y)); scores.append(p)
        counts = {r['trip_id']: sum(z['trip_id'] == r['trip_id'] for z in rows) for r in rows}
        weights = [1/counts[r['trip_id']] for r in rows]
        actual = summarize(rows)['metrics']
        self.assertAlmostEqual(actual['roc_auc'], roc_auc_score(labels, scores, sample_weight=weights), places=13)
        self.assertAlmostEqual(actual['average_precision'], average_precision_score(labels, scores, sample_weight=weights), places=13)
        self.assertAlmostEqual(actual['brier_score'], brier_score_loss(labels, scores, sample_weight=weights), places=13)
        self.assertAlmostEqual(actual['log_loss'], log_loss(labels, np.clip(scores, 1e-15, 1-1e-15), sample_weight=weights), places=13)

    def test_ties_have_half_roc_and_noninterpolated_pr_area(self):
        tied = summarize([row('a','a',False,.5),row('b','b',True,.5)], include_curves=True)
        self.assertEqual(tied['metrics']['roc_auc'], .5)
        self.assertEqual(tied['metrics']['average_precision'], .5)
        self.assertEqual(len(tied['ranking_curve']), 2)
        self.assertEqual(tied['ranking_curve'][1]['recall'], 1)
        self.assertEqual(tied['ranking_curve'][1]['precision'], .5)
        reversed_rank = summarize([row('a','a',False,.9),row('b','b',True,.1)])
        self.assertEqual(reversed_rank['metrics']['roc_auc'], 0)
        self.assertEqual(reversed_rank['metrics']['average_precision'], .5)
        perfect = summarize([row('a','a',False,.1),row('b','b',True,.9)])
        self.assertEqual(perfect['metrics']['roc_auc'], 1)
        self.assertEqual(perfect['metrics']['average_precision'], 1)

    def test_one_class_ranking_is_explicitly_null_but_losses_are_available(self):
        for y in (True, False):
            with self.subTest(y=y):
                r = summarize([row('a','a',y,.4),row('b','b',y,.8)], include_curves=True)
                self.assertIsNone(r['metrics']['roc_auc'])
                self.assertIsNone(r['metrics']['average_precision'])
                self.assertIsNone(r['ranking_curve'])
                self.assertEqual(r['ranking_undefined_reason'], 'single_class_scored_labels')
                self.assertIsNotNone(r['metrics']['brier_score'])
                self.assertIsNotNone(r['metrics']['log_loss'])

    def test_empty_and_all_unknown_are_not_zero_event_metrics(self):
        for rows in ([], [row('a','a',None,.1)], [row('a','a',False,None)]):
            r = summarize(rows)
            self.assertEqual(r['counts']['scored_snapshots'], 0)
            self.assertTrue(all(x is None for x in r['metrics'].values()))
            self.assertEqual(r['ranking_undefined_reason'], 'no_scored_snapshots')
            self.assertEqual(len(r['reliability']), 10)
            self.assertTrue(all(b['mean_probability'] is None for b in r['reliability']))
        methods, bins, slices = evaluate_methods([])
        self.assertEqual(methods['opportunities_per_method'], 0)
        self.assertEqual(len(bins), 80)
        self.assertFalse(slices)

    def test_reliability_boundaries_include_zero_and_one(self):
        r = summarize([row(str(i),str(i),bool(i%2),p) for i,p in enumerate((0.,.1,.2,1.))])
        bins = r['reliability']
        self.assertEqual([b['snapshots'] for b in bins], [1,1,1,0,0,0,0,0,0,1])
        self.assertFalse(bins[0]['upper_inclusive'])
        self.assertTrue(bins[-1]['upper_inclusive'])
        self.assertAlmostEqual(sum(b['cohort_weight_fraction'] for b in bins), 1)
        self.assertEqual(sum(b['positive_snapshots']+b['negative_snapshots'] for b in bins),4)

    def test_reliability_preserves_cohort_trip_weights_inside_bins(self):
        r = summarize([row('a','shared',False,.2),row('b','shared',True,.8),row('c','short',True,.2)])
        lower, upper = r['reliability'][2], r['reliability'][8]
        self.assertAlmostEqual(lower['observed_event_rate'], 2/3)
        self.assertAlmostEqual(lower['cohort_weight_fraction'], .75)
        self.assertAlmostEqual(upper['cohort_weight_fraction'], .25)
        self.assertAlmostEqual(r['metrics']['fixed_bin_ece'], .75*abs(.2-2/3)+.25*abs(.8-1))
        self.assertEqual((lower['trips'],upper['trips']), (2,1))
        self.assertEqual(r['counts']['scored_trips'], 2)  # A trip can be in both bins.

    def test_extreme_probabilities_clip_only_log_loss(self):
        r = summarize([row('a','a',True,0),row('b','b',False,1)])
        self.assertEqual(r['counts']['log_loss_clipped_snapshots'], 2)
        self.assertEqual(r['metrics']['brier_score'], 1)
        self.assertTrue(math.isfinite(r['metrics']['log_loss']))
        self.assertEqual(r['reliability'][0]['mean_probability'], 0)
        self.assertEqual(r['reliability'][-1]['mean_probability'], 1)
        self.assertEqual(r['metrics']['roc_auc'], 0)

    def test_reordering_never_changes_results(self):
        rows = [row('c','a',True,.7),row('a','a',False,.2),row('b','b',True,.2),row('z','z',None,None)]
        self.assertEqual(summarize(rows, include_curves=True), summarize(list(reversed(rows)), include_curves=True))
        paired = pair('c','c',True,.2,.8)+pair('a','a',False,.2,.3)
        self.assertEqual(evaluate_methods(paired), evaluate_methods(list(reversed(paired))))

    def test_unknown_cancelled_and_nonmature_labels_cannot_be_zero(self):
        for change in (dict(eligibility='excluded',delay_label=False), dict(eligibility='excluded',delay_label=0),
                       dict(delay_label=1),dict(delay_label=0),dict(label_status='pending'),dict(label_status='cancelled')):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):summarize([dict(row('a','a',True,.5),**change)])
        excluded = row('a','a',None,.7,label_status='cancelled',reason='cancelled')
        r = summarize([excluded])
        self.assertEqual(r['counts']['excluded_snapshots'],1)
        self.assertEqual(r['label_status_counts'],{'cancelled':1})

    def test_bad_probability_status_numeric_fields_and_duplicates_fail(self):
        base = row('a','a',True,.5)
        invalid = [dict(base, probability=x) for x in (-.1,1.1,float('nan'),float('inf'),True,None)]
        invalid += [dict(base,status='unavailable'),dict(base,prediction_source='other'),
                    dict(base,elapsed_trip_seconds=-1),dict(base,deadline_remaining_seconds=float('nan')),
                    dict(base,tenant_id=''),dict(base,eligibility='pending')]
        for r in invalid:
            with self.subTest(r=r):
                with self.assertRaises(ValueError):summarize([r])
        with self.assertRaisesRegex(ValueError,'Duplicate'):summarize([base,deepcopy(base)])
        with self.assertRaisesRegex(ValueError,'multiple folds'):
            summarize([base,dict(base,snapshot_id='b',fold_id='F02')])

    def test_invalid_or_changed_metric_protocol_is_rejected(self):
        invalid = [dict(reliability_bin_edges=[0,.3,.2,1]),dict(reliability_bin_edges=[.1,1]),
                   dict(reliability_bin_edges=[0,1,1]),dict(reliability_bin_edges=[0,True,1]),
                   dict(log_loss_epsilon=0),dict(log_loss_epsilon=.5),dict(sparse_below_scored_trips=True),
                   dict(sparse_below_scored_trips=0),dict(early_trip_cutoff_seconds=0),
                   dict(advance_bin_edges_seconds=[0,900,7200]),dict(strata=['actual_arrival_time']),
                   dict(cohorts=['delivered']),dict(method_ids=['logistic_delay'])]
        for setting in invalid:
            with self.subTest(setting=setting):
                with self.assertRaises(ValueError):summarize([],setting)

    def test_native_fallback_and_complete_common_populations_stay_distinct(self):
        rows=pair('a','a',True,.3,.8)+pair('b','b',False,.3,.3)+pair('c','c',None,.3,None)
        rows[3]['prediction_source']='prevalence_fallback'
        report,bins,slices=evaluate_methods(rows)
        candidate=report['methods']['logistic_delay']
        self.assertEqual(candidate['availability']['opportunities'],3)
        self.assertEqual(candidate['availability']['prevalence_fallback_opportunities'],1)
        self.assertEqual(candidate['availability']['unavailable_opportunities'],1)
        self.assertEqual(candidate['cohorts']['native']['counts']['scored_snapshots'],1)
        self.assertEqual(candidate['cohorts']['delivered']['counts']['scored_snapshots'],2)
        self.assertEqual(candidate['cohorts']['common_supported']['counts']['scored_snapshots'],2)
        self.assertEqual(len(bins),80)
        subset=[s for s in slices if s['method_id']=='logistic_delay' and s['stratum']=='fold_id' and s['cohort']=='native'][0]
        self.assertEqual(subset['opportunities'],3)  # availability denominator includes unknowns
        self.assertEqual(subset['eligible_opportunities'],2)
        self.assertEqual(subset['scored_snapshots'],1)

    def test_missing_hard_case_nulls_fixed_anchor_and_remains_counted(self):
        report,_,_=evaluate_methods(pair('a','a',True,.3,.99)+pair('b','b',False,.3,None))
        candidate=report['methods']['logistic_delay']
        anchor=candidate['cohorts']['baseline_anchor']
        self.assertFalse(anchor['cohort_complete'])
        self.assertEqual((anchor['cohort_snapshots'],anchor['counts']['scored_snapshots']),(2,1))
        self.assertTrue(all(v is None for v in anchor['metrics'].values()))
        self.assertEqual(anchor['ranking_undefined_reason'],'incomplete_fixed_cohort')
        self.assertIsNotNone(candidate['cohorts']['delivered']['metrics']['brier_score'])
        self.assertEqual(candidate['cohorts']['common_supported']['cohort_snapshots'],1)

    def test_pairing_prevents_missing_rows_changed_labels_and_false_fallback(self):
        good=pair('a','a',True,.3,.8)+pair('b','b',False,.3,.3)
        invalid=[good[:-1], good+[dict(good[0])], [dict(r,method_id='unexpected') for r in good]]
        for fields in (dict(delay_label=False),dict(trip_id='different'),dict(elapsed_trip_seconds=5),
                       dict(deadline_remaining_seconds=0),dict(position_validity='no_position'),
                       dict(prediction_source='prevalence_fallback')):
            r=deepcopy(good);r[1].update(fields);invalid.append(r)
        for rows in invalid:
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):evaluate_methods(rows)

    def test_snapshot_early_and_deadline_bands_are_fixed_at_boundaries(self):
        rows=[]
        for i,seconds in enumerate((-1,0,1800,1800.1,7200,7200.1,None)):
            rows.extend(pair(str(i),str(i),bool(i%2),.3,.7,deadline_remaining_seconds=seconds,
                             elapsed_trip_seconds=3599 if i<3 else 3600,position_validity='no_position'))
        _,_,slices=evaluate_methods(rows,dict(sparse_below_scored_trips=2))
        selected=[s for s in slices if s['cohort']=='delivered' and s['method_id']=='logistic_delay']
        bands={s['value']:s for s in selected if s['stratum']=='deadline_advance'}
        self.assertEqual({k:v['opportunities'] for k,v in bands.items()},
                         {'overdue':1,'0-30m':2,'30-120m':2,'over-2h':1,'unknown':1})
        self.assertFalse(bands['0-30m']['sparse'])
        self.assertTrue(bands['overdue']['sparse'])
        early={s['value']:s['opportunities'] for s in selected if s['stratum']=='early_trip'}
        self.assertEqual(early,{'early':3,'later':4})
        gps=[s for s in selected if s['stratum']=='position_validity']
        self.assertEqual(gps[0]['value'],'no_position')
        self.assertEqual(gps[0]['opportunities'],7)

    def test_snapshot_promise_changes_allow_mixed_trip_labels(self):
        rows=pair('a','one',True,.3,.7)+pair('b','one',False,.3,.2)
        report,_,_=evaluate_methods(rows)
        support=report['methods']['logistic_delay']['cohorts']['delivered']['scored_class_support']
        self.assertEqual(support['mixed_label_trips'],1)
        self.assertEqual((support['positive_trips'],support['negative_trips']),(1,1))


if __name__ == '__main__':
    unittest.main()
