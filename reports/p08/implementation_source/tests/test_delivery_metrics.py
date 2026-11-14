"""Distinct transport and usefulness populations with independently timed cases."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import unittest

from ds_m01.common import parse_ts,utc
from ds_m01.delivery_metrics import score_delivery

PROJECT=Path(__file__).resolve().parents[1]
CUTOFF='2026-08-01T07:00:00Z'


def opportunity(sid='S1',trip='TRIP',at='2026-07-16T16:00:00Z',deadline='2026-07-16T18:00:00Z',promise='P1',tenant='T01'):
    return dict(tenant_id=tenant,trip_id=trip,snapshot_id=sid,snapshot_time=at,
                promise_version_id=promise,promise_known_at='2026-07-15T12:00:00Z',deadline_at=deadline,
                prediction_version='MODEL-'+tenant,probability='0.700000')


def target(row,arrival='2026-07-16T18:30:00Z',reason=None):
    eligible=arrival is not None and parse_ts(arrival)>parse_ts(row['snapshot_time'])
    return {**{k:row[k] for k in ('tenant_id','trip_id','snapshot_id','promise_version_id','deadline_at')},
            'eligibility':'eligible' if eligible else 'excluded',
            'delay_label':parse_ts(arrival)>parse_ts(row['deadline_at']) if eligible else None,
            'reason':reason or ('verified_gate_arrival' if eligible else 'already_arrived_at_snapshot' if arrival else 'latest_outcome_pending'),
            'actual_arrival_time':arrival,'label_known_at':'2026-07-17T00:00:00Z',
            'label_cutoff_at':CUTOFF,'label_status':'verified' if arrival else 'pending',
            'arrival_time_precision':'second' if arrival else 'unknown','outcome_version_id':'O-'+row['trip_id']}


def candidate(row,episode=None):
    return {**row,'candidate_id':'C-'+row['snapshot_id'],'episode_id':episode or 'E-'+row['trip_id'],
            'policy_version':'FROZEN-POLICY','simulated_issue_at':row['snapshot_time']}


def attempt(c,index=1,status='delivered',sent=None,at=None,known=None):
    sent=sent or c['snapshot_time']
    at=at or utc(parse_ts(sent)+timedelta(seconds=5))
    known=known or at
    return {**{k:c[k] for k in ('tenant_id','trip_id','snapshot_id','candidate_id','promise_version_id','deadline_at','prediction_version')},
            'alert_episode_id':c['episode_id'],'alert_id':'A-'+c['candidate_id'],
            'decision_threshold_version':c['policy_version'],
            'delivery_id':'D-'+c['candidate_id']+'-'+str(index),'attempt_index':index,
            'sent_at':sent,'service_at':None if status=='queued' else at,
            'delivered_at':at if status=='delivered' else None,
            'receipt_known_at':None if status=='queued' else known,
            'delivery_status':status,'receipt_type':'service_delivered' if status=='delivered' else 'none',
            'recipient_role':'dispatcher','received_or_opened_at':None,
            'attempt_status':'completed' if status!='queued' else 'queued','error_reason':None,'retryable':status=='failed'}


def simulation(candidates,attempts,cutoff=CUTOFF):
    alerts=[];episodes={}
    for c in candidates:
        unique={r['delivery_id']:r for r in attempts if r['candidate_id']==c['candidate_id'] and r['tenant_id']==c['tenant_id']}
        successes=[r for r in unique.values() if r['delivery_status']=='delivered' and r['receipt_known_at'] is not None
                   and parse_ts(r['receipt_known_at'])<=parse_ts(cutoff)]
        successes.sort(key=lambda r:(parse_ts(r['delivered_at']),r['delivery_id']))
        alerts.append({**{k:c[k] for k in ('tenant_id','trip_id','snapshot_id','candidate_id','promise_version_id','deadline_at','prediction_version','probability')},
            'alert_id':'A-'+c['candidate_id'],'alert_episode_id':c['episode_id'],'decision_threshold_version':c['policy_version'],
            'created_at':c['simulated_issue_at'],'alert_status':'delivered' if successes else 'failed' if unique else 'queued',
            'status_reason':'fixture','attempt_count':len(unique),
            'first_delivery_id':successes[0]['delivery_id'] if successes else None,
            'first_delivered_at':successes[0]['delivered_at'] if successes else None})
        key=c['tenant_id'],c['episode_id']
        if key not in episodes:
            episodes[key]=dict(tenant_id=c['tenant_id'],trip_id=c['trip_id'],alert_episode_id=c['episode_id'],
                               counts_as_of=cutoff,delivered_alert_count=0,duplicate_alert_count=0)
        episodes[key]['delivered_alert_count']+=len(successes)
    for e in episodes.values():e['duplicate_alert_count']=max(e['delivered_alert_count']-1,0)
    return dict(alerts=alerts,attempts=attempts,episodes=list(episodes.values()),summary={'counts_as_of':cutoff})


class DeliveryMetricsTests(unittest.TestCase):
    def setUp(self):
        self.settings=json.loads((PROJECT/'config/p07.json').read_text())

    def score(self,rows,targets,candidates,attempts):
        return score_delivery(rows,targets,candidates,simulation(candidates,attempts),self.settings)

    def test_attempt_cannot_substitute_foreign_model_or_policy_version(self):
        row=opportunity(); c=candidate(row)
        for field in ('prediction_version','decision_threshold_version'):
            ledger=simulation([c],[attempt(c)])
            ledger['attempts'][0][field]='FOREIGN-VERSION'
            with self.subTest(field=field), self.assertRaisesRegex(ValueError,'Attempt identity'):
                score_delivery([row],[target(row)],[c],ledger,self.settings)

    def test_alert_status_first_receipt_and_attempt_count_must_match_observed_ledger(self):
        row=opportunity(); c=candidate(row)
        for changes in ({'alert_status':'failed'}, {'first_delivery_id':'FOREIGN-RECEIPT'},
                        {'first_delivered_at':row['snapshot_time']}, {'attempt_count':2}):
            ledger=simulation([c],[attempt(c)])
            ledger['alerts'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError,'Alert '):
                score_delivery([row],[target(row)],[c],ledger,self.settings)
        ledger=simulation([c],[attempt(c,status='unknown')])
        ledger['alerts'][0]['alert_status']='delivered'
        with self.assertRaisesRegex(ValueError,'Alert delivery status'):
            score_delivery([row],[target(row)],[c],ledger,self.settings)

    def test_failed_attempt_then_success_counts_two_attempts_one_delivery_no_repeat(self):
        row=opportunity();c=candidate(row)
        attempts=[attempt(c,status='failed'),attempt(c,2,sent='2026-07-16T16:01:00Z',at='2026-07-16T16:01:05Z')]
        output=self.score([row],[target(row)],[c],attempts)
        count=output['counts']
        self.assertEqual((count['attempts'],count['failed_attempts'],count['retry_attempts']),(2,1,1))
        self.assertEqual((count['successful_confirmed_deliveries'],count['repeated_deliveries_within_episode']),(1,0))
        self.assertEqual(output['delivery_rows'][0]['candidate_to_delivery_seconds'],65)
        self.assertEqual(output['metrics']['delivered_actionable_trip_recall'],1)
        self.assertFalse(output['recipient_opening_evaluated'])

    def test_first_success_uses_later_alert_promise_after_first_candidate_fails(self):
        early=opportunity('S1',deadline='2026-07-16T18:00:00Z',promise='P1')
        later=opportunity('S2',at='2026-07-16T16:30:00Z',deadline='2026-07-16T19:00:00Z',promise='P2')
        first,second=candidate(early),candidate(later)
        output=self.score([early,later],[target(early),target(later)],[first,second],
                          [attempt(first,status='failed'),attempt(second,at='2026-07-16T16:31:00Z')])
        self.assertEqual(output['counts']['mixed_promise_label_trips'],1)
        self.assertEqual(output['counts']['actionable_positive_trips'],1)
        self.assertEqual(output['delivery_rows'][0]['promise_version_id'],'P2')
        self.assertEqual(output['delivery_rows'][0]['classification'],'known_false')
        self.assertEqual(output['metrics']['conditional_first_delivery_useful_precision'],0)
        self.assertEqual(output['metrics']['delivered_actionable_trip_recall'],0)

    def test_unknown_transport_and_unknown_target_are_distinct_and_neither_negative(self):
        rows=[opportunity('S1','A'),opportunity('S2','B'),opportunity('S3','C')]
        candidates=[candidate(r) for r in rows]
        targets=[target(rows[0]),target(rows[1],None),target(rows[2])]
        output=self.score(rows,targets,candidates,[attempt(candidates[0],status='unknown'),attempt(candidates[1]),attempt(candidates[2],status='queued')])
        self.assertEqual(output['counts']['unknown_attempts'],1)
        self.assertEqual(output['counts']['queued_attempts'],1)
        self.assertEqual(output['counts']['successful_confirmed_deliveries'],1)
        self.assertEqual(output['counts']['unresolved_first_deliveries'],1)
        self.assertEqual(output['metrics']['first_delivery_usefulness_bounds'],{'lower':0,'upper':1})
        self.assertIsNone(output['metrics']['conditional_first_delivery_useful_precision'])
        self.assertEqual(output['metrics']['delivered_trip_coverage'],1/3)
        self.assertEqual(output['counts']['actionable_positive_trips'],2)

    def test_delivery_time_action_boundary_retains_fractional_seconds(self):
        rows=[opportunity('S1','A'),opportunity('S2','B')]
        candidates=[candidate(r) for r in rows]
        attempts=[attempt(candidates[0],at='2026-07-16T17:30:00Z'),
                  attempt(candidates[1],at='2026-07-16T17:30:00.000001Z')]
        output=self.score(rows,[target(r) for r in rows],candidates,attempts)
        self.assertEqual(output['counts']['confirmed_useful_deliveries'],1)
        self.assertEqual(output['counts']['insufficient_lead_deliveries'],1)
        self.assertEqual(output['denominators']['conditional_first_delivery_useful_precision'],2)
        self.assertEqual(output['metrics']['conditional_first_delivery_useful_precision'],.5)
        self.assertAlmostEqual(output['delivery_rows'][1]['prospective_deadline_lead_seconds'],1799.999999)

    def test_eligible_target_that_arrives_before_delivery_is_nonuseful_burden(self):
        rows=[opportunity('S1','A'),opportunity('S2','B')]
        candidates=[candidate(r) for r in rows]
        labels=[target(rows[0]),target(rows[1],arrival='2026-07-16T16:15:00Z')]
        output=self.score(rows,labels,candidates,[attempt(candidates[0]),attempt(candidates[1],at='2026-07-16T16:20:00Z')])
        self.assertEqual(output['counts']['post_arrival_deliveries'],1)
        self.assertEqual(output['counts']['successful_confirmed_deliveries'],2)
        self.assertEqual(output['denominators']['conditional_first_delivery_useful_precision'],2)
        self.assertEqual(output['metrics']['conditional_first_delivery_useful_precision'],.5)
        self.assertEqual(output['metrics']['first_delivery_usefulness_bounds'],{'lower':.5,'upper':.5})
        self.assertEqual(output['delivery_rows'][1]['retrospective_arrival_lead_seconds'],-300)

    def test_excluded_known_postarrival_is_in_precision_denominator_and_not_unknown(self):
        rows=[opportunity('S1','A'),opportunity('S2','B')]
        candidates=[candidate(r) for r in rows]
        labels=[target(rows[0]),target(rows[1],arrival='2026-07-16T15:59:00Z')]
        output=self.score(rows,labels,candidates,[attempt(c) for c in candidates])
        self.assertEqual(output['counts']['mature_eligible_first_deliveries'],1)
        self.assertEqual(output['counts']['known_assessable_first_deliveries'],2)
        self.assertEqual(output['counts']['post_arrival_first_deliveries'],1)
        self.assertEqual(output['metrics']['conditional_first_delivery_useful_precision'],.5)
        self.assertEqual(output['metrics']['first_delivery_usefulness_bounds'],{'lower':.5,'upper':.5})

    def test_known_receipt_cutoff_is_inclusive_but_future_receipt_does_not_count(self):
        rows=[opportunity('S1','A'),opportunity('S2','B')]
        candidates=[candidate(r) for r in rows]
        attempts=[attempt(candidates[0],known=CUTOFF),attempt(candidates[1],known='2026-08-01T07:00:00.000001Z')]
        output=self.score(rows,[target(r) for r in rows],candidates,attempts)
        self.assertEqual(output['counts']['successful_confirmed_deliveries'],1)
        self.assertEqual(output['counts']['queued_attempts'],1)
        self.assertEqual(output['metrics']['delivered_actionable_trip_recall'],.5)

    def test_identical_receipt_is_idempotent_but_conflicting_replay_fails(self):
        row=opportunity();c=candidate(row);a=attempt(c)
        output=self.score([row],[target(row)],[c],[a,deepcopy(a)])
        self.assertEqual(output['counts']['attempts'],1)
        self.assertEqual(output['counts']['successful_confirmed_deliveries'],1)
        with self.assertRaisesRegex(ValueError,'conflicting'):
            self.score([row],[target(row)],[c],[a,dict(a,receipt_known_at='2026-07-16T16:00:06Z')])

    def test_two_distinct_delivered_reminders_are_one_real_repeat(self):
        rows=[opportunity('S1'),opportunity('S2',at='2026-07-16T16:30:00Z')]
        candidates=[candidate(r) for r in rows]
        output=self.score(rows,[target(r) for r in rows],candidates,[attempt(c) for c in candidates])
        self.assertEqual(output['counts']['successful_confirmed_deliveries'],2)
        self.assertEqual(output['counts']['delivered_trips'],1)
        self.assertEqual(output['counts']['repeated_deliveries_within_episode'],1)
        self.assertEqual(output['counts']['additional_deliveries_per_trip'],1)
        self.assertEqual(output['counts']['retry_attempts'],0)

    def test_monitor_denominator_keeps_trip_without_candidate_or_success(self):
        rows=[opportunity('S1','A'),opportunity('S2','B')]
        c=candidate(rows[0])
        output=self.score(rows,[target(r) for r in rows],[c],[attempt(c)])
        self.assertEqual(output['counts']['monitored_trips'],2)
        self.assertEqual(output['counts']['candidate_alerts'],1)
        self.assertEqual(output['metrics']['delivered_actionable_trip_recall'],.5)
        self.assertEqual(output['metrics']['successful_deliveries_per_100_monitored_trips'],50)

    def test_empty_population_has_null_rates_without_false_zero_performance(self):
        output=self.score([],[],[],[])
        self.assertEqual(output['counts']['monitored_trips'],0)
        self.assertIsNone(output['metrics']['delivered_actionable_trip_recall'])
        self.assertIsNone(output['metrics']['conditional_first_delivery_useful_precision'])
        self.assertEqual(output['metrics']['first_delivery_usefulness_bounds'],{'lower':None,'upper':None})

    def test_cross_tenant_promise_clock_and_episode_count_corruption_fail(self):
        row=opportunity();c=candidate(row);a=attempt(c);labels=[target(row)]
        mutations=[]
        sim=simulation([c],[deepcopy(a)]);sim['attempts'][0]['tenant_id']='FOREIGN';mutations.append(sim)
        sim=simulation([c],[deepcopy(a)]);sim['alerts'][0]['promise_version_id']='LATER-PROMISE';mutations.append(sim)
        sim=simulation([c],[deepcopy(a)]);sim['episodes'][0]['delivered_alert_count']=2;mutations.append(sim)
        for sim in mutations:
            with self.assertRaises(ValueError):score_delivery([row],labels,[c],sim,self.settings)
        bad=target(row);bad['label_known_at']=CUTOFF
        with self.assertRaisesRegex(ValueError,'strictly before'):
            self.score([row],[bad],[c],[a])

    def test_unresolved_too_late_success_cannot_inflate_usefulness_upper_bound(self):
        row=opportunity();c=candidate(row)
        output=self.score([row],[target(row,arrival=None)],[c],[attempt(c,at='2026-07-16T17:30:00.000001Z')])
        self.assertEqual(output['counts']['insufficient_lead_first_deliveries'],1)
        self.assertEqual(output['metrics']['first_delivery_usefulness_bounds'],{'lower':0,'upper':0})
        self.assertEqual(output['metrics']['conditional_first_delivery_useful_precision'],0)
        self.assertEqual(output['denominators']['conditional_first_delivery_useful_precision'],1)
        self.assertEqual(output['metrics']['first_delivery_assessable_coverage'],1)
        self.assertEqual(output['metrics']['first_delivery_target_coverage'],0)

    def test_delivery_at_exact_arrival_is_postarrival_and_same_time_order_is_stable(self):
        rows=[opportunity('S1'),opportunity('S2',at='2026-07-16T16:00:01Z',promise='P2',deadline='2026-07-16T18:01:00Z')]
        candidates=[candidate(r) for r in rows]
        labels=[target(r,arrival='2026-07-16T16:15:00Z') for r in rows]
        attempts=[attempt(c,at='2026-07-16T16:15:00Z') for c in candidates]
        first=self.score(rows,labels,candidates,attempts)
        second=self.score(list(reversed(rows)),list(reversed(labels)),list(reversed(candidates)),list(reversed(attempts)))
        self.assertEqual(first,second)
        self.assertEqual(first['counts']['post_arrival_deliveries'],2)
        self.assertEqual(first['delivery_rows'][0]['promise_version_id'],'P1')


if __name__=='__main__':unittest.main()
