import copy
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
import json
from pathlib import Path
import unittest

from ds_m01.alert_policy import make_rule, simulate_policy

SETTINGS = json.loads((Path(__file__).resolve().parents[1] / 'config/p06.json').read_text())
START = datetime.fromisoformat('2026-07-16T10:00:00-07:00')


def opportunity(sid='S1', trip='A', tenant='T1', *, seconds=0, probability=.8,
                lead=7200, at=None, promise='P1', status='in_transit', **changes):
    at = at or START + timedelta(seconds=seconds)
    row = dict(tenant_id=tenant, trip_id=trip, snapshot_id=sid, snapshot_time=at.isoformat(),
               departure_time=(at-timedelta(hours=2)).isoformat(), trip_status_as_of=status,
               promise_version_id=promise, deadline_at=(at+timedelta(seconds=lead)).isoformat(),
               promise_known_at=(at-timedelta(hours=1)).isoformat(), prediction_version='MODEL-'+tenant,
               eta_status='available', probability_status='available', probability_calibration_status='fitted',
               calibrated_probability=probability,
               probability=None if probability is None else format(Decimal(str(probability)).quantize(Decimal('.000001'),rounding=ROUND_HALF_EVEN), '.6f'))
    row.update(changes)
    return row


class AlertPolicyTests(unittest.TestCase):
    def run_policy(self, rows, threshold='0.50', enabled=True):
        return simulate_policy(rows,make_rule(SETTINGS,threshold,enabled=enabled))

    def test_frozen_grid_and_explicit_disabled_versions(self):
        self.assertEqual(make_rule(SETTINGS,'.5'),make_rule(SETTINGS,'0.50'))
        self.assertNotEqual(make_rule(SETTINGS,'.5')['policy_version'],make_rule(SETTINGS,'.55')['policy_version'])
        disabled=make_rule(SETTINGS,None,enabled=False)
        self.assertIsNone(disabled['threshold'])
        for threshold in ['.51',-1,2,float('nan'),True,None]:
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):make_rule(SETTINGS,threshold)
        with self.assertRaises(ValueError):make_rule(SETTINGS,'1',enabled=False)
        for field,value in [('daily_candidate_cap_per_tenant',3),('minimum_action_lead_seconds',1799),('ordering',['probability'])]:
            changed=copy.deepcopy(SETTINGS);changed['policy'][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):make_rule(changed,'.5')

    def test_rule_version_rejects_mutation(self):
        rule=make_rule(SETTINGS,'.5');rule['threshold']='0.550000'
        with self.assertRaisesRegex(ValueError,'version'):simulate_policy([],rule)

    def test_decimal_half_even_before_threshold_and_equality(self):
        rows=[opportunity('S1','A',probability=.4999995),opportunity('S2','B',probability=.4999985),
              opportunity('S3','C',probability=.5)]
        result=self.run_policy(rows)
        self.assertEqual([r['candidate_status'] for r in result['decisions']],['admitted','below_threshold','admitted'])
        self.assertEqual(result['decisions'][0]['probability'],'0.500000')
        wrong=opportunity(probability=.4999995);wrong['probability']='0.499999'
        with self.assertRaisesRegex(ValueError,'projection'):self.run_policy([wrong])
        with self.assertRaises(ValueError):self.run_policy([opportunity(probability=.5,probability_status='unavailable')])

    def test_lead_boundary_is_prospective_deadline_not_eta(self):
        rows=[opportunity('A','A',lead=1800),opportunity('B','B',lead=1799.999999),opportunity('C','C',lead=-1)]
        result=self.run_policy(rows)
        self.assertEqual([r['candidate_status'] for r in result['decisions']],['admitted','insufficient_lead','insufficient_lead'])
        self.assertEqual(result['decisions'][0]['lead_seconds'],1800)

    def test_cooldown_survives_risk_resolution_new_episode_and_revision(self):
        rows=[opportunity('S1',seconds=0),opportunity('S2',seconds=600,probability=.1),
              opportunity('S3',seconds=1200,promise='P2'),opportunity('S4',seconds=1800,promise='P2')]
        result=self.run_policy(rows)
        self.assertEqual([r['candidate_status'] for r in result['decisions']],['admitted','below_threshold','cooldown','admitted'])
        self.assertEqual(len(result['episodes']),2)
        self.assertEqual(result['episodes'][0]['episode_status'],'closed')
        self.assertEqual(result['episodes'][1]['episode_status'],'censored')
        self.assertNotEqual(result['episodes'][0]['episode_id'],result['episodes'][1]['episode_id'])
        self.assertEqual([r['promise_version_id'] for r in result['candidates']],['P1','P2'])

    def test_promise_revision_alone_does_not_reset_episode_or_cooldown(self):
        result=self.run_policy([opportunity('S1'),opportunity('S2',seconds=600,promise='P2'),
                                opportunity('S3',seconds=1800,promise='P2')])
        self.assertEqual([r['candidate_status'] for r in result['decisions']],['admitted','cooldown','admitted'])
        self.assertEqual(len(result['episodes']),1)
        self.assertEqual(result['episodes'][0]['candidate_count'],2)
        self.assertEqual(result['episodes'][0]['start_promise_version_id'],'P1')
        self.assertEqual(result['candidates'][0]['promise_version_id'],'P1')

    def test_missing_probability_preserves_open_risk_without_consuming_cooldown(self):
        missing=opportunity('S2',seconds=600,probability_status='unavailable',
                            probability_calibration_status='unavailable',probability=None,calibrated_probability=None)
        result=self.run_policy([opportunity('S1'),missing,opportunity('S3',seconds=1200),opportunity('S4',seconds=1800)])
        self.assertEqual([r['candidate_status'] for r in result['decisions']],['admitted','unavailable','cooldown','admitted'])
        self.assertEqual(len(result['episodes']),1)
        self.assertEqual(len(result['candidates']),2)

    def test_eta_support_separates_risk_state_from_candidate_support(self):
        result=self.run_policy([opportunity('S1',eta_status='unavailable'),opportunity('S2',seconds=60),
                                opportunity('S3',seconds=120,probability=.1,eta_status='unavailable')])
        self.assertEqual([r['candidate_status'] for r in result['decisions']],['unavailable','admitted','below_threshold'])
        self.assertEqual(result['episodes'][0]['start_snapshot_id'],'S1')
        self.assertEqual(result['episodes'][0]['episode_status'],'closed')
        self.assertEqual(result['episodes'][0]['candidate_count'],1)

    def test_unsupported_probability_is_not_treated_as_calibrated(self):
        result=self.run_policy([opportunity('S1',probability_calibration_status='unsupported'),opportunity('S2',seconds=60)])
        self.assertEqual([r['candidate_status'] for r in result['decisions']],['unavailable','admitted'])
        self.assertEqual(result['episodes'][0]['start_snapshot_id'],'S2')
        self.assertEqual(result['decisions'][0]['reason'],'probability_calibration_unsupported')

    def test_chronological_capacity_does_not_rank_future_high_probability(self):
        rows=[opportunity('S3','C',seconds=120,probability=.99),opportunity('S1','A',probability=.5),
              opportunity('S2','B',seconds=60,probability=.6)]
        result=self.run_policy(rows)
        self.assertEqual([r['trip_id'] for r in result['candidates']],['A','B'])
        self.assertEqual(result['decisions'][-1]['candidate_status'],'capacity_exhausted')
        self.assertEqual(result['summary']['daily_candidate_counts'],[{'tenant_id':'T1','local_date':'2026-07-16','candidates':2}])

    def test_same_clock_order_is_declared_identity_order_and_tenant_caps_are_separate(self):
        rows=[opportunity('C','C',probability=.99),opportunity('A','A',probability=.5),opportunity('B','B',probability=.6),
              opportunity('C','C','T2'),opportunity('A','A','T2'),opportunity('B','B','T2')]
        first=self.run_policy(rows)
        self.assertEqual([(r['tenant_id'],r['trip_id']) for r in first['candidates']],[('T1','A'),('T1','B'),('T2','A'),('T2','B')])
        self.assertEqual(first,self.run_policy(list(reversed(rows))))

    def test_local_midnight_resets_capacity_not_trip_cooldown(self):
        at=datetime.fromisoformat('2026-07-16T23:40:00-07:00')
        rows=[opportunity('A1','A',at=at),opportunity('B1','B',at=at+timedelta(minutes=10)),
              opportunity('B2','B',at=at+timedelta(minutes=20)),opportunity('C1','C',at=at+timedelta(minutes=20)),
              opportunity('B3','B',at=at+timedelta(minutes=40))]
        r=self.run_policy(rows)
        self.assertEqual([d['candidate_status'] for d in r['decisions']],['admitted','admitted','cooldown','admitted','admitted'])
        self.assertEqual([d['local_date'] for d in r['summary']['daily_candidate_counts']],['2026-07-16','2026-07-17'])
        self.assertEqual([d['candidates'] for d in r['summary']['daily_candidate_counts']],[2,2])

    def test_dst_repeated_hour_uses_absolute_elapsed_time(self):
        one=datetime.fromisoformat('2026-11-01T01:45:00-07:00')
        two=datetime.fromisoformat('2026-11-01T01:15:00-08:00')
        r=self.run_policy([opportunity('S2',at=two),opportunity('S1',at=one)])
        self.assertEqual(len(r['candidates']),2)
        self.assertEqual(r['summary']['daily_candidate_counts'][0]['local_date'],'2026-11-01')

    def test_known_terminal_states_close_without_future_labels(self):
        for status in ['arrived','cancelled','incomplete','lost']:
            with self.subTest(status=status):
                r=self.run_policy([opportunity('S1'),opportunity('S2',seconds=60,status=status)])
                self.assertEqual(r['decisions'][-1]['candidate_status'],'terminal')
                self.assertEqual(r['episodes'][0]['closure_reason'],'known_'+status)
                self.assertEqual(r['episodes'][0]['episode_status'],'closed')
                self.assertIsNone(r['episodes'][0]['censored_at'])
        r=self.run_policy([opportunity(status='planned')])
        self.assertEqual(r['decisions'][0]['candidate_status'],'inactive')
        self.assertEqual(r['episodes'],[])

    def test_end_of_stream_censors_risk_even_when_last_forecast_unavailable(self):
        last=opportunity('S2',seconds=60,probability=None,calibrated_probability=None,
                         probability_status='unavailable',probability_calibration_status='unavailable')
        r=self.run_policy([opportunity('S1'),last]);episode=r['episodes'][0]
        self.assertEqual(episode['episode_status'],'censored')
        self.assertIsNone(episode['episode_closed_at'])
        self.assertIsNone(episode['closure_reason'])
        self.assertEqual(episode['censored_at'],r['decisions'][-1]['snapshot_time'])

    def test_disabled_is_not_threshold_one_and_preserves_every_opportunity(self):
        rows=[opportunity(probability=1)]
        self.assertEqual(len(self.run_policy(rows,'1')['candidates']),1)
        r=self.run_policy(rows,None,enabled=False)
        self.assertEqual(r['decisions'][0]['candidate_status'],'disabled')
        self.assertEqual(r['candidates'],[]);self.assertEqual(r['episodes'],[])

    def test_exact_duplicates_are_idempotent_and_conflicts_fail(self):
        a=opportunity();before=copy.deepcopy(a)
        once=self.run_policy([a]);twice=self.run_policy([a,copy.deepcopy(a)])
        for key in ['decisions','candidates','episodes']:self.assertEqual(once[key],twice[key])
        self.assertEqual(twice['summary']['duplicate_records_ignored'],1)
        self.assertEqual(a,before)
        for changed in [{'trip_id':'other'},{'promise_version_id':'other'},{'position_validity':'stale'}]:
            with self.subTest(changed=changed),self.assertRaisesRegex(ValueError,'Conflicting duplicate'):
                self.run_policy([a,{**a,**changed}])

    def test_forbidden_label_fields_rejected_without_inspecting_value(self):
        class Forbidden:
            def __str__(self):raise AssertionError('target payload accessed')
        for field in ['eligibility','delay_label','actual_arrival_time','label_known_at','outcome_version_id']:
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'targets cannot enter'):
                self.run_policy([{**opportunity(),field:Forbidden()}])

    def test_clock_status_and_probability_validation(self):
        changes=[{'snapshot_time':'2026-07-16T10:00:00'},
                 {'promise_known_at':'2026-07-20T00:00:00Z'},
                 {'departure_time':'2026-07-20T00:00:00Z'},
                 {'trip_status_as_of':'active'},{'eta_status':'made_up'},
                 {'probability_status':'made_up'},{'probability_calibration_status':'guaranteed'},
                 {'calibrated_probability':float('nan')},{'probability':'0.8'},
                 {'probability':'.800000'},{'probability':True}]
        for change in changes:
            with self.subTest(change=change),self.assertRaises(ValueError):self.run_policy([{**opportunity(),**change}])

    def test_empty_input_is_defined_and_has_no_delivery_claim(self):
        r=self.run_policy([])
        self.assertEqual(r['summary']['opportunities'],0)
        self.assertFalse(r['summary']['labels_used'])
        self.assertFalse(r['summary']['delivery_simulated'])
        self.assertEqual(r['summary']['scope'],'research_candidates_not_deliveries')
        self.assertEqual(r['decisions'],[])
