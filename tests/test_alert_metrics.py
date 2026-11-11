"""Hand-built opportunity populations check policy utility and burden semantics."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
import json
from pathlib import Path
import unittest

from ds_m01.alert_metrics import evaluate_policy, select_policy
from ds_m01.alert_policy import make_rule, simulate_policy


SETTINGS=json.loads((Path(__file__).resolve().parents[1]/'config/p06.json').read_text())
BASE=datetime.fromisoformat('2026-07-15T10:00:00-07:00')
CUTOFF='2026-08-01T07:00:00Z'


def opportunity(i=0, *, trip=None, minutes=0, day=None, probability=.8, lead=7200, tenant='T1', **changes):
    at=BASE+timedelta(days=i if day is None else day,minutes=minutes)
    row=dict(tenant_id=tenant,trip_id=trip or f'T{i:02}',snapshot_id=f'S{i:02}',snapshot_time=at.isoformat(),
             departure_time=(at-timedelta(hours=1)).isoformat(),trip_status_as_of='in_transit',
             promise_version_id=f'P{i:02}',deadline_at=(at+timedelta(seconds=lead)).isoformat(),
             promise_known_at=(at-timedelta(hours=1)).isoformat(),prediction_version='MODEL-'+tenant,
             eta_status='available',probability_status='available',probability_calibration_status='fitted',
             calibrated_probability=probability,
             probability=None if probability is None else format(Decimal(str(probability)).quantize(Decimal('.000001'),rounding=ROUND_HALF_EVEN),'.6f'))
    row.update(changes)
    return row


def target(row, label=True, *, post_arrival=False, arrival=None, **changes):
    at=datetime.fromisoformat(row['snapshot_time'])
    deadline=datetime.fromisoformat(row['deadline_at'])
    if arrival is None and label is not None:
        arrival=deadline+timedelta(minutes=10) if label else at+timedelta(minutes=10)
    if post_arrival:
        arrival=at-timedelta(seconds=1)
    unknown=label is None and not post_arrival
    r={field:row[field] for field in ('tenant_id','trip_id','snapshot_id','snapshot_time','promise_version_id','deadline_at')}
    r.update(eligibility='excluded' if unknown or post_arrival else 'eligible',
             delay_label=None if unknown or post_arrival else label,
             reason='latest_outcome_pending' if unknown else 'already_arrived_at_snapshot' if post_arrival else 'verified_gate_arrival',
             actual_arrival_time=None if unknown else arrival.isoformat(),
             label_known_at=(max(at,arrival or at)+timedelta(hours=2)).isoformat(),label_cutoff_at=CUTOFF,
             label_status='pending' if unknown else 'verified',arrival_time_precision='unknown' if unknown else 'second',
             outcome_version_id='O-'+row['trip_id'],outcome_provenance_id='SOURCE')
    r.update(changes)
    return r


def measure(rows, labels, threshold='0.50', *, disabled=False):
    rule=make_rule(SETTINGS,None if disabled else threshold,enabled=not disabled)
    simulation=simulate_policy(rows,rule)
    return evaluate_policy(rows,labels,simulation,SETTINGS),simulation


class AlertMetricTests(unittest.TestCase):
    def test_unavailable_positive_trip_remains_in_actionable_recall_denominator(self):
        rows=[opportunity(0),opportunity(1,probability=None,probability_status='unavailable',probability_calibration_status='unavailable'),
              opportunity(2,probability=.1)]
        result,_=measure(rows,[target(rows[0]),target(rows[1]),target(rows[2],False)])
        self.assertEqual(result['counts']['scored_trips'],3)
        self.assertEqual(result['counts']['usable_forecast_label_trips'],2)
        self.assertEqual(result['counts']['actionable_positive_trips'],2)
        self.assertEqual(result['metrics']['actionable_trip_recall'],.5)
        self.assertEqual(result['denominators']['all_candidates_per_100_monitored_trips'],3)
        self.assertAlmostEqual(result['metrics']['all_candidates_per_100_monitored_trips'],100/3)

    def test_unknown_and_post_arrival_candidates_stay_in_burden_and_bounds(self):
        rows=[opportunity(i) for i in range(4)]
        labels=[target(rows[0]),target(rows[1],False),target(rows[2],None),target(rows[3],None,post_arrival=True)]
        result,simulation=measure(rows,labels)
        c=result['counts'];m=result['metrics']
        self.assertEqual(len(simulation['candidates']),4)
        self.assertEqual((c['confirmed_true_candidates'],c['known_false_candidates'],c['unresolved_candidates'],c['retrospectively_post_arrival_candidates']),(1,1,1,1))
        self.assertEqual(m['candidate_target_coverage'],.5)
        self.assertEqual(m['conditional_first_candidate_precision'],.5)
        self.assertEqual(result['denominators']['conditional_first_candidate_precision'],2)
        self.assertEqual(m['first_candidate_precision_bounds'],{'lower':.25,'upper':.5})
        self.assertEqual(m['all_candidates_per_100_monitored_trips'],100)
        self.assertEqual(result['candidate_unresolved_reason_counts'],{'latest_outcome_pending':1})

    def test_promise_revisions_use_candidate_specific_labels_and_first_precision(self):
        rows=[opportunity(0,trip='A',lead=14400),opportunity(1,trip='A',day=0,minutes=30,lead=5400)]
        arrival=BASE+timedelta(hours=3)
        labels=[target(rows[0],False,arrival=arrival),target(rows[1],True,arrival=arrival)]
        result,simulation=measure(rows,labels)
        self.assertEqual(len(simulation['episodes']),1)
        self.assertEqual(result['counts']['mixed_promise_label_trips'],1)
        self.assertEqual(result['counts']['confirmed_actionable_true_alert_trips'],1)
        self.assertEqual(result['counts']['repeated_candidates_within_episode'],1)
        self.assertEqual(result['counts']['additional_candidates_per_trip'],1)
        self.assertEqual(result['metrics']['actionable_trip_recall'],1)
        self.assertEqual(result['metrics']['conditional_first_candidate_precision'],0)
        self.assertEqual(result['metrics']['first_candidate_precision_bounds'],{'lower':0,'upper':0})

    def test_actionable_boundary_and_all_positive_recall_have_distinct_denominators(self):
        rows=[opportunity(0,lead=1800),opportunity(1,lead=1799.999999)]
        result,_=measure(rows,[target(r) for r in rows])
        self.assertEqual(result['counts']['observed_positive_trips'],2)
        self.assertEqual(result['counts']['actionable_positive_trips'],1)
        self.assertEqual(result['metrics']['actionable_trip_recall'],1)
        self.assertEqual(result['metrics']['all_observed_positive_trip_recall'],.5)
        self.assertEqual(result['decision_status_counts'],{'admitted':1,'insufficient_lead':1})

    def test_maturity_and_promise_identity_fail_even_for_excluded_targets(self):
        row=opportunity()
        simulation=simulate_policy([row],make_rule(SETTINGS,'.5'))
        for changes in ({'label_known_at':CUTOFF},{'promise_version_id':'WRONG'},
                        {'deadline_at':'2026-07-15T22:00:00-07:00'}, {'label_cutoff_at':'2026-08-02T07:00:00Z'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                evaluate_policy([row],[target(row,None,**changes)],simulation,SETTINGS)
        with self.assertRaises(ValueError):
            evaluate_policy([row],[target(row,True,arrival_time_precision='minute')],simulation,SETTINGS)

    def test_population_and_candidate_ledger_cannot_drop_or_forge_rows(self):
        rows=[opportunity(0),opportunity(1)]
        labels=[target(r) for r in rows]
        _,simulation=measure(rows,labels)
        for altered in ('decision','candidate','identity','episode_count','promise'):
            wrong=deepcopy(simulation)
            if altered=='decision':wrong['decisions'].pop()
            elif altered=='candidate':wrong['candidates'].pop()
            elif altered=='identity':wrong['candidates'][1]['candidate_id']=wrong['candidates'][0]['candidate_id']
            elif altered=='episode_count':wrong['episodes'][0]['candidate_count']+=1
            else:wrong['candidates'][0]['promise_version_id']='P-POISON'
            with self.subTest(altered=altered),self.assertRaises(ValueError):
                evaluate_policy(rows,labels,wrong,SETTINGS)
        with self.assertRaises(ValueError):evaluate_policy(rows,labels[:-1],simulation,SETTINGS)

    def test_exact_duplicate_opportunity_is_idempotent_but_target_duplicates_fail(self):
        row=opportunity()
        original,_=measure([row],[target(row)])
        repeated,_=measure([row,deepcopy(row)],[target(row)])
        self.assertEqual(original,repeated)
        with self.assertRaises(ValueError):measure([row],[target(row),target(row)])

    def test_mixed_policy_versions_or_disabled_summary_cannot_change_claimed_scope(self):
        rows=[opportunity(0),opportunity(1)]
        labels=[target(r) for r in rows]
        _,simulation=measure(rows,labels)
        for change in ('version','enabled','count'):
            wrong=deepcopy(simulation)
            if change=='version':wrong['decisions'][1]['policy_version']='OTHER-POLICY'
            elif change=='enabled':wrong['summary']['enabled']=False
            else:wrong['summary']['opportunities']-=1
            with self.subTest(change=change),self.assertRaises(ValueError):
                evaluate_policy(rows,labels,wrong,SETTINGS)

    def test_disabled_and_empty_populations_use_null_not_perfect_precision(self):
        row=opportunity(probability=1)
        result,simulation=measure([row],[target(row)],disabled=True)
        self.assertEqual(simulation['candidates'],[])
        self.assertEqual(result['metrics']['actionable_trip_recall'],0)
        self.assertIsNone(result['metrics']['conditional_first_candidate_precision'])
        self.assertEqual(result['metrics']['first_candidate_precision_bounds'],{'lower':None,'upper':None})
        empty,_=measure([],[],disabled=True)
        self.assertIsNone(empty['metrics']['actionable_trip_recall'])
        self.assertIsNone(empty['metrics']['all_candidates_per_100_monitored_trips'])

    def test_tenant_slices_keep_separate_support_and_unknown_denominators(self):
        rows=[opportunity(0,tenant='T1'),opportunity(1,tenant='T2')]
        result,_=measure(rows,[target(rows[0]),target(rows[1],None)])
        first,second=result['tenant_slices']
        self.assertEqual(first['metrics']['actionable_trip_recall'],1)
        self.assertIsNone(second['metrics']['actionable_trip_recall'])
        self.assertEqual(second['metrics']['first_candidate_precision_bounds'],{'lower':0,'upper':1})
        self.assertTrue(first['sparse'] and second['sparse'])


class AlertSelectionTests(unittest.TestCase):
    def test_frozen_objective_captures_trips_then_charges_all_candidates_then_ties_high(self):
        probabilities=[.9,.8,.95,.7,.1,.1,.1,.1,.1,.1,.85,.05]
        rows=[opportunity(i,day=i%10,probability=p) for i,p in enumerate(probabilities)]
        labels=[target(r,i<2 if i<10 else None) for i,r in enumerate(rows)]
        result=select_policy(rows,labels,SETTINGS)
        self.assertEqual(result['status'],'selected')
        self.assertEqual(result['rule']['threshold'],'0.800000')
        self.assertEqual(len(result['trials']),22)
        self.assertEqual(sum(t['selected'] for t in result['trials']),1)
        self.assertEqual(result['selected_metrics']['counts']['confirmed_actionable_true_alert_trips'],2)
        self.assertEqual(result['selected_metrics']['counts']['all_admitted_candidates'],4)
        trials={t['trial_id']:t for t in result['trials']}
        self.assertFalse(trials['threshold_0.00']['feasibility']['global_budget'])
        self.assertEqual(trials['threshold_0.70']['metrics']['counts']['all_admitted_candidates'],5)
        self.assertEqual(trials['threshold_0.75']['metrics']['counts']['all_admitted_candidates'],4)
        self.assertEqual(trials['disabled_reference']['metrics']['counts']['all_admitted_candidates'],0)
        self.assertEqual(result,select_policy(list(reversed(rows)),list(reversed(labels)),SETTINGS))

    def test_no_feasible_positive_capture_selects_explicit_disabled_not_threshold_one(self):
        rows=[opportunity(i,probability=.1 if i<2 else .9) for i in range(10)]
        labels=[target(r,i<2) for i,r in enumerate(rows)]
        result=select_policy(rows,labels,SETTINGS)
        self.assertEqual(result['status'],'disabled_no_feasible_capture')
        self.assertFalse(result['rule']['enabled'])
        self.assertIsNone(result['rule']['threshold'])
        self.assertEqual(result['selection_manifest']['selected_trial_id'],'disabled_reference')
        self.assertEqual(len(result['trials']),22)
        self.assertTrue(result['selection_manifest']['support_checks']['minimum_scored_trips'])

    def test_label_only_support_survives_forecast_missingness_but_no_capture_disables(self):
        rows=[opportunity(i,probability=None,probability_status='unavailable',probability_calibration_status='unavailable') for i in range(10)]
        labels=[target(r,i<2) for i,r in enumerate(rows)]
        result=select_policy(rows,labels,SETTINGS)
        self.assertEqual(result['status'],'disabled_no_feasible_capture')
        self.assertTrue(all(result['selection_manifest']['support_checks'].values()))
        self.assertEqual(result['selected_metrics']['counts']['scored_trips'],10)
        self.assertEqual(result['selected_metrics']['counts']['usable_forecast_label_trips'],0)

    def test_insufficient_negative_support_does_not_turn_unknowns_into_negatives(self):
        rows=[opportunity(i) for i in range(12)]
        labels=[target(r,True if i<10 else None) for i,r in enumerate(rows)]
        result=select_policy(rows,labels,SETTINGS)
        self.assertEqual(result['status'],'disabled_insufficient_support')
        self.assertEqual(result['selected_metrics']['counts']['negative_trips'],0)
        self.assertFalse(result['selection_manifest']['support_checks']['minimum_negative_trips'])
        self.assertEqual(len(result['trials']),22)

    def test_each_minimum_support_boundary_can_independently_disable_selection(self):
        for size,positives,failed in ((9,2,'minimum_scored_trips'),
                                      (10,1,'minimum_actionable_positive_trips'),
                                      (10,9,'minimum_negative_trips')):
            rows=[opportunity(i) for i in range(size)]
            labels=[target(r,i<positives) for i,r in enumerate(rows)]
            with self.subTest(failed=failed):
                result=select_policy(rows,labels,SETTINGS)
                self.assertEqual(result['status'],'disabled_insufficient_support')
                self.assertFalse(result['selection_manifest']['support_checks'][failed])
                self.assertEqual(len(result['trials']),22)


if __name__=='__main__':
    unittest.main()
