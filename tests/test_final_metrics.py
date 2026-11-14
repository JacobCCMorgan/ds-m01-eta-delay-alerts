"""Hand-derived final scores, fixed support and clustered-resampling guards."""
from copy import deepcopy
import json
import math
from pathlib import Path
import random
import unittest
from unittest.mock import patch

import numpy as np

from ds_m01.final_metrics import (attach_targets,summarize_eta,summarize_probability,paired_comparison,
    bootstrap_plan,bootstrap_summary,build_metrics,GROUPS)

SETTINGS=json.loads((Path(__file__).resolve().parents[1]/'config/p08_evaluation.json').read_text())


def row(trip='A',snapshot='1',*,family='eta',y=10,p=.5,median=10,lower=0,upper=20,available=True,eligible=True):
    result=dict(tenant_id='T1',trip_id=trip,snapshot_id=trip+'-'+snapshot,
        snapshot_time='2026-08-10T10:00:00Z',departure_time='2026-08-10T09:00:00Z',route_id='R1',vehicle_id='V1',
        novelty_group=GROUPS[0],position_validity='valid',elapsed_trip_seconds=3600,deadline_remaining_seconds=1800,
        promise_version_id='P1',deadline_at='2026-08-10T10:30:00Z',promise_known_at='2026-08-10T08:00:00Z',
        method_id='pipeline',variant='raw',status='available' if available else 'unavailable',
        prediction_source='native' if available else 'unavailable',calibration_status='not_applied',
        calibration_applied=False,calibration_reason='raw_forecast',eligibility='eligible' if eligible else 'excluded',
        reason='verified' if eligible else 'latest_outcome_pending',label_status='verified' if eligible else 'pending',
        label_cutoff_at='2026-10-15T07:00:00Z',label_known_at='2026-08-11T07:00:00Z',outcome_version_id='O-'+trip)
    if family=='eta':result.update(target_seconds=y if eligible else None,
        lower_seconds=lower if available else None,median_seconds=median if available else None,upper_seconds=upper if available else None)
    else:result.update(delay_label=bool(y) if eligible else None,probability=p if available else None)
    return result


def variants(base,family):
    raw=deepcopy(base);cal=deepcopy(base);canonical=deepcopy(base);baseline=deepcopy(base)
    for item in cal:item.update(variant='calibrated',calibration_status='fitted',calibration_applied=True)
    for item in canonical:
        item.update(variant='canonical',calibration_status='fitted',calibration_applied=True)
        if family=='probability' and item['status']=='available':item['canonical_probability']=f"{item['probability']:.6f}"
    for item in baseline:item['method_id']='historical_baseline' if family=='eta' else 'fold_prevalence'
    return [*raw,*cal,*canonical,*baseline]


class FinalMetricsTests(unittest.TestCase):
    def test_snapshot_counts_do_not_overweight_dense_trips_and_rmse_uses_squared_error(self):
        rows=[row('A','1',median=8),row('A','2',median=12),row('B',median=20,upper=30)]
        result=summarize_eta(rows,raw_quantiles=True)
        self.assertEqual(result['metrics']['median_mae_seconds'],6)
        self.assertAlmostEqual(result['metrics']['median_rmse_seconds'],math.sqrt(52))
        self.assertEqual(result['metrics']['mean_width_seconds'],25)
        self.assertEqual(result['metrics']['pinball_0.5_seconds'],3)

    def test_missing_interval_is_whole_trip_miss_and_has_separate_conditional_denominator(self):
        result=summarize_eta([row('A','1'),row('A','2',available=False),row('B')])
        self.assertEqual(result['metrics']['trip_all_eligible_snapshots_coverage'],.5)
        self.assertEqual(result['metrics']['trip_all_snapshots_coverage_given_full_availability'],1)
        self.assertEqual(result['counts']['eligible_trips'],2)
        self.assertEqual(result['counts']['fully_available_eligible_trips'],1)
        self.assertEqual(result['counts']['unavailable_eligible_snapshots'],1)
        self.assertEqual(result['metrics']['trip_weighted_snapshot_coverage'],1)

    def test_all_missing_intervals_still_have_zero_whole_trip_coverage(self):
        result=summarize_eta([row(available=False)])
        self.assertEqual(result['metrics']['trip_all_eligible_snapshots_coverage'],0)
        self.assertIsNone(result['metrics']['median_mae_seconds'])
        self.assertEqual(result['undefined_reasons']['median_mae_seconds'],'no_scored_snapshots')

    def test_interval_score_includes_both_tail_penalties(self):
        result=summarize_eta([row('A',y=5,lower=10,median=12,upper=15),row('B',y=20,lower=10,median=12,upper=15)])
        self.assertEqual(result['metrics']['mean_interval_score_seconds'],55)
        self.assertNotIn('pinball_0.1_seconds',result['metrics'])

    def test_probability_losses_keep_trip_weights_in_reliability_bins(self):
        result=summarize_probability([row('A','1',family='probability',y=0,p=.1),
            row('A','2',family='probability',y=1,p=.9),row('B',family='probability',y=1,p=.1)])
        self.assertAlmostEqual(result['metrics']['brier_score'],.41)
        bins=result['reliability']
        self.assertEqual(len(bins),10)
        self.assertEqual(bins[1]['cohort_weight'],1.5)
        self.assertEqual(bins[1]['cohort_weight_fraction'],.75)
        self.assertAlmostEqual(bins[1]['observed_event_rate'],2/3)
        self.assertEqual(bins[9]['cohort_weight'],.5)
        self.assertAlmostEqual(result['metrics']['fixed_bin_ece'],.45)
        self.assertEqual(result['eligible_class_support']['mixed_label_trips'],1)

    def test_probability_endpoints_are_clipped_only_for_logloss(self):
        result=summarize_probability([row('A',family='probability',y=1,p=0),row('B',family='probability',y=0,p=1)])
        self.assertEqual(result['counts']['log_loss_clipped_snapshots'],2)
        self.assertEqual(result['metrics']['brier_score'],1)
        self.assertEqual(result['reliability'][0]['mean_probability'],0)
        self.assertEqual(result['reliability'][9]['mean_probability'],1)
        self.assertAlmostEqual(result['metrics']['log_loss'],(-math.log(1e-15)-math.log1p(-(1-1e-15)))/2)

    def test_rankings_handle_ties_and_single_class_as_declared(self):
        tied=summarize_probability([row('A',family='probability',y=1),row('B',family='probability',y=0)])
        self.assertEqual(tied['metrics']['roc_auc'],.5)
        self.assertEqual(tied['metrics']['average_precision'],.5)
        single=summarize_probability([row(family='probability',y=1)])
        self.assertIsNone(single['metrics']['roc_auc']);self.assertIsNone(single['metrics']['average_precision'])
        self.assertEqual(single['undefined_reasons']['roc_auc'],'single_class_scored_labels')
        self.assertEqual(single['metrics']['brier_score'],.25)

    def test_empty_groups_keep_null_metrics_reasons_and_ten_empty_bins(self):
        result=summarize_probability([])
        self.assertTrue(all(value is None for value in result['metrics'].values()))
        self.assertEqual(len(result['reliability']),10)
        self.assertTrue(all(bin['cohort_weight_fraction'] is None for bin in result['reliability']))
        self.assertEqual(summarize_eta([])['undefined_reasons']['trip_all_eligible_snapshots_coverage'],'no_eligible_trips')

    def test_fixed_paired_support_reports_lost_rows_and_preserves_whole_trip_population(self):
        left=[row('A',median=9),row('B',median=20,upper=30)]
        right=[row('A',median=8),row('B',available=False)]
        result=paired_comparison(left,right,'eta',SETTINGS)
        self.assertEqual(result['counts']['left_only_scored_snapshots'],1)
        self.assertEqual(result['counts']['paired_scored_trips'],1)
        self.assertEqual(result['metrics']['median_mae_seconds']['right_minus_left'],1)
        self.assertEqual(result['metrics']['trip_all_eligible_snapshots_coverage']['right_minus_left'],-.5)
        altered=deepcopy(right);altered[0]['target_seconds']=11
        with self.assertRaisesRegex(ValueError,'target or original metadata'):paired_comparison(left,altered,'eta',SETTINGS)

    def test_bootstrap_is_stratified_deterministic_and_shared_not_method_redrawn(self):
        rows=[row(f'T{i:02d}') for i in range(20)]
        for i,item in enumerate(rows):
            item['tenant_id']='T1' if i<10 else 'T2'
            item['novelty_group']=GROUPS[i%2]
        first=bootstrap_plan(rows,SETTINGS);second=bootstrap_plan(list(reversed(rows)),SETTINGS)
        self.assertEqual(first['metadata'],second['metadata'])
        self.assertTrue(np.array_equal(first['draws'],second['draws']))
        self.assertEqual(first['draws'].shape,(2000,20))
        for tenant in ('T1','T2'):
            for group in GROUPS[:2]:
                indices=[first['index'][(r['tenant_id'],r['trip_id'])] for r in rows if r['tenant_id']==tenant and r['novelty_group']==group]
                self.assertTrue(np.all(first['draws'][:,indices].sum(axis=1)==5))
        after=deepcopy(rows)
        for r in after:r['median_seconds']=11
        result=paired_comparison(rows,after,'eta',SETTINGS,plan=first)
        self.assertEqual(result['metrics']['median_mae_seconds']['interval']['lower'],1)
        self.assertEqual(result['metrics']['median_mae_seconds']['interval']['upper'],1)

    def test_small_support_and_any_undefined_bootstrap_denominator_suppress_intervals(self):
        small=bootstrap_summary([row()],'eta',SETTINGS)
        self.assertEqual(small['median_mae_seconds']['undefined_reason'],'fewer_than_20_original_scored_trips')
        self.assertEqual(small['median_mae_seconds']['defined_replicates'],2000)
        rows=[row(f'T{i:02d}') for i in range(21)]
        with patch.object(random.Random,'choice',side_effect=lambda group:group[0]):
            plan=bootstrap_plan(rows,SETTINGS)
        result=bootstrap_summary(rows[1:],'eta',SETTINGS,plan=plan)
        self.assertEqual(result['median_mae_seconds']['undefined_replicates'],2000)
        self.assertEqual(result['median_mae_seconds']['undefined_reason'],'undefined_replicate_denominator')
        self.assertIsNone(result['median_mae_seconds']['lower'])

    def test_whole_trip_coverage_retains_missing_misses_but_ci_requires_scored_support(self):
        rows=[row(f'T{i:02d}',available=False) for i in range(25)]
        self.assertEqual(summarize_eta(rows)['metrics']['trip_all_eligible_snapshots_coverage'],0)
        interval=bootstrap_summary(rows,'eta',SETTINGS)['trip_all_eligible_snapshots_coverage']
        self.assertEqual(interval['original_contributing_trips'],25)
        self.assertEqual(interval['original_scored_trips'],0)
        self.assertEqual(interval['defined_replicates'],2000)
        self.assertEqual(interval['undefined_reason'],'fewer_than_20_original_scored_trips')
        self.assertIsNone(interval['lower'])

    def test_bootstrap_linear_percentiles_match_separate_trip_resampling_reference(self):
        rows=[row(f'T{i:02d}',y=1,median=i+1,upper=30) for i in range(21)]
        result=bootstrap_summary(rows,'eta',SETTINGS)
        rng=random.Random(20261111);means=[];roots=[]
        keys=list(range(21))
        for _ in range(2000):
            sample=[rng.choice(keys) for _ in keys]
            means.append(math.fsum(sample)/21)
            roots.append(math.sqrt(math.fsum(value*value for value in sample)/21))
        def percentile(values,p):
            values=sorted(values);position=(len(values)-1)*p;index=int(position)
            return values[index]+(values[index+1]-values[index])*(position-index)
        for name,reference in [('median_mae_seconds',means),('median_rmse_seconds',roots)]:
            self.assertAlmostEqual(result[name]['lower'],percentile(reference,.025))
            self.assertAlmostEqual(result[name]['upper'],percentile(reference,.975))

    def test_canonical_half_even_probability_and_outward_eta_change_only_projection(self):
        eta=variants([row(y=.7,lower=.1,median=.5,upper=.8)],'eta')
        probability=variants([row(family='probability',y=1,p=.4999995)],'probability')
        canonical=next(r for r in eta if r['variant']=='canonical')
        canonical.update(lower_seconds=0,median_seconds=0,upper_seconds=1)
        canonical=next(r for r in probability if r['variant']=='canonical')
        canonical.update(probability=.5,canonical_probability='0.500000')
        result=build_metrics(eta,probability,SETTINGS)
        self.assertAlmostEqual(result['eta']['methods']['pipeline/calibrated']['metrics']['median_mae_seconds'],.2)
        self.assertAlmostEqual(result['eta']['methods']['pipeline/canonical']['metrics']['median_mae_seconds'],.7)
        self.assertNotEqual(result['probability']['methods']['pipeline/calibrated']['metrics']['brier_score'],
                            result['probability']['methods']['pipeline/canonical']['metrics']['brier_score'])

    def test_build_keeps_four_novelty_groups_and_canonical_projection_separate(self):
        eta=variants([row()], 'eta');probability=variants([row(family='probability',y=1)],'probability')
        result=build_metrics(eta,probability,SETTINGS)
        groups=[r for r in result['eta']['slices'] if r['method_id']=='pipeline' and r['variant']=='calibrated' and r['field']=='novelty_group']
        self.assertEqual({r['value'] for r in groups},set(GROUPS))
        self.assertEqual(sum(r['counts']['opportunities'] for r in groups),1)
        self.assertTrue(all(r['metrics']['median_mae_seconds'] is None for r in groups if r['value']!=GROUPS[0]))
        self.assertIn('pinball_0.1_seconds',result['eta']['methods']['pipeline/raw']['metrics'])
        self.assertNotIn('pinball_0.1_seconds',result['eta']['methods']['pipeline/calibrated']['metrics'])
        self.assertEqual(len([r for r in result['eta']['paired_comparisons'] if r['field']=='overall']),4)
        self.assertFalse(result['quality_accepted'])

    def test_bad_projection_or_calibration_support_change_fails(self):
        eta=variants([row()], 'eta');probability=variants([row(family='probability',y=1)],'probability')
        bad=deepcopy(eta);next(r for r in bad if r['variant']=='calibrated')['median_seconds']=11
        with self.assertRaisesRegex(ValueError,'changed median'):build_metrics(bad,probability,SETTINGS)
        bad=deepcopy(probability);next(r for r in bad if r['variant']=='canonical')['canonical_probability']='0.500001'
        with self.assertRaisesRegex(ValueError,'six-decimal'):build_metrics(eta,bad,SETTINGS)

    def test_join_rejects_target_leakage_clock_mismatch_and_omitted_denominators(self):
        original=row();target={k:original[k] for k in ('tenant_id','trip_id','snapshot_id','snapshot_time',
            'eligibility','label_status','label_known_at','outcome_version_id','target_seconds','reason')}
        forecast={k:v for k,v in original.items() if k not in target or k in ('tenant_id','trip_id','snapshot_id','snapshot_time')}
        joined=attach_targets([forecast],[target],'eta')
        self.assertEqual(joined[0]['target_seconds'],10)
        with self.assertRaisesRegex(ValueError,'already contains target'):attach_targets([original],[target],'eta')
        with self.assertRaisesRegex(ValueError,'inventories'):attach_targets([forecast],[],'eta')
        bad=deepcopy(target);bad['trip_id']='FOREIGN'
        with self.assertRaisesRegex(ValueError,'identity/clock'):attach_targets([forecast],[bad],'eta')
        bad=deepcopy(target);bad['deadline_at']='2026-08-10T11:00:00Z'
        with self.assertRaisesRegex(ValueError,'changes the forecast promise'):attach_targets([forecast],[bad],'eta')

    def test_invalid_status_values_and_protocol_tuning_fail(self):
        for change in ({'probability':True},{'probability':float('nan')},{'probability':1.1},{'delay_label':1}):
            bad=row(family='probability');bad.update(change)
            with self.assertRaises(ValueError):summarize_probability([bad])
        changed=deepcopy(SETTINGS);changed['uncertainty']['forecast_bootstrap']['replicates']=100
        with self.assertRaisesRegex(ValueError,'frozen protocol'):bootstrap_plan([row()],changed)

    def test_direct_metric_inputs_reject_outcomes_first_known_at_cutoff(self):
        bad=row();bad['label_known_at']='2026-10-15T07:00:00Z'
        with self.assertRaisesRegex(ValueError,'strictly precede'):summarize_eta([bad])
        bad=row(family='probability');bad['actual_arrival_time']='2026-08-10T10:00:00Z'
        with self.assertRaisesRegex(ValueError,'already arrived'):summarize_probability([bad])

    def test_unsupported_calibration_cannot_change_forecast_values(self):
        eta=variants([row()], 'eta');probability=variants([row(family='probability')],'probability')
        for item in probability:
            if item['variant'] in ('calibrated','canonical'):item.update(calibration_status='unsupported',probability=.6)
            if item['variant']=='canonical':item['canonical_probability']='0.600000'
        with self.assertRaisesRegex(ValueError,'identity raw'):build_metrics(eta,probability,SETTINGS)


if __name__=='__main__':unittest.main()
