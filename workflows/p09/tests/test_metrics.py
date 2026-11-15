"""Hand-computed workflow denominators, unknown states and promise comparisons."""
from copy import deepcopy
import unittest

from workflows.p09.metrics import score_workflow


STAMP = '2026-09-01T12:00:00Z'
LATER = '2026-09-01T12:05:00Z'
CUTOFF = '2026-10-01T00:00:00Z'


def fixture():
    settings = {'observation_cutoff_at': CUTOFF}
    alerts = [{'tenant_id': 'T', 'alert_id': n} for n in 'ABC']
    def delivery(d, a, status):
        return dict(tenant_id='T', delivery_id=d, alert_id=a, delivery_status=status,
                    receipt_observed=status == 'delivered', delivered_at=STAMP if status == 'delivered' else None,
                    receipt_known_at=STAMP if status == 'delivered' else None)
    inputs = {'opportunities': [dict(tenant_id='T', trip_id=n, snapshot_id=n) for n in 'ABC'], 'alerts': alerts,
              'attempts': [delivery('retry-failure', 'A', 'failed'), delivery('da', 'A', 'delivered'),
                           delivery('db', 'B', 'delivered'), delivery('dc', 'C', 'failed')]}
    simulation = {'eta_adoption': [dict(tenant_id='T', adoption_id='aa', delivery_id='da', adoption_status='accepted', decided_at=LATER),
                                   dict(tenant_id='T', adoption_id='ab', delivery_id='db', adoption_status='not_reviewed', decided_at=None)],
        'eta_action': [dict(tenant_id='T', action_id='xa', adoption_id='aa', alert_id='A', action_type='reschedule',
                            action_time=LATER, handling_seconds=120)],
        'eta_action_followup': [dict(tenant_id='T', action_id='xa', outcome_version_id='oa',
            outcome_observation_end=CUTOFF, original_promise_delay_seconds=1800, observed_delay_seconds=0,
            review_outcome='confirmed')],
        'opening_events': [dict(tenant_id='T', alert_id='A', delivery_id='da', opened_at=LATER, known_at=LATER)],
        'followup_audit': [dict(tenant_id='T', action_id='xa', action_type='reschedule', followup_status='mature',
                               outcome_version_id='oa', canonical_row_emitted=True)]}
    return inputs, simulation, settings


class WorkflowMetricTests(unittest.TestCase):
    def test_failed_retry_and_absent_delivery_do_not_inflate_adoption_denominator(self):
        inputs, simulation, settings = fixture()
        result = score_workflow(inputs, simulation, settings)
        self.assertEqual(result['population']['transport_attempts'], 4)
        self.assertEqual(result['population']['confirmed_delivered_alerts'], 2)
        self.assertEqual(result['population']['non_delivered_alerts'], 1)
        self.assertEqual(result['rates']['opened_per_delivery'], {'numerator': 1, 'denominator': 2, 'value': .5})
        self.assertEqual(result['funnel']['not_reviewed_alerts'], 1)
        self.assertEqual(result['funnel']['rejected_alerts'], 0)
        self.assertEqual(result['by_tenant']['T']['population'], result['population'])

    def test_empty_population_has_unknown_rates_not_zero_success_claim(self):
        i, s, settings = fixture()
        i = {k: [] for k in i}; s = {k: [] for k in s}
        result = score_workflow(i, s, settings)
        self.assertEqual(result['by_tenant'], {})
        self.assertTrue(all(r['value'] is None for r in result['rates'].values()))

    def test_promise_extension_is_not_an_arrival_or_causal_improvement(self):
        result = score_workflow(*fixture())
        self.assertEqual(result['followup']['deadline_definition_difference_seconds'], 1800)
        self.assertEqual(result['followup']['original_delay_seconds_sum'], 1800)
        self.assertEqual(result['followup']['comparison_delay_seconds_sum'], 0)
        self.assertFalse(result['followup']['arrival_change_claimed'])
        self.assertFalse(result['causal_benefit_estimated'])
        self.assertFalse(result['quality_accepted'])

    def test_unresolved_followup_retains_denominator_and_is_not_zero_delay(self):
        i, s, settings = fixture()
        s['eta_action_followup'] = []
        s['followup_audit'][0].update(followup_status='no_known_outcome', outcome_version_id=None, canonical_row_emitted=False)
        result = score_workflow(i, s, settings)
        self.assertEqual(result['followup']['known_arrival_comparisons'], 0)
        self.assertEqual(result['followup']['actions_without_comparable_arrival'], 1)
        self.assertEqual(result['actions']['executed_actions'], 1)

    def test_override_is_separate_from_acceptance(self):
        i, s, settings = fixture(); s['eta_adoption'][0]['adoption_status'] = 'overridden'
        result = score_workflow(i, s, settings)
        self.assertEqual(result['funnel']['overridden_alerts'], 1)
        self.assertEqual(result['rates']['accepted_or_partial_per_reviewed']['value'], 0)

    def test_rejected_no_action_is_not_executed_work(self):
        i, s, settings = fixture()
        s['eta_adoption'][1].update(adoption_status='rejected', decided_at=LATER)
        s['opening_events'].append(dict(tenant_id='T', alert_id='B', delivery_id='db', opened_at=LATER, known_at=LATER))
        s['eta_action'].append(dict(tenant_id='T', action_id='xb', adoption_id='ab', alert_id='B',
            action_type='no_action', action_time=None, handling_seconds=None))
        s['followup_audit'].append(dict(tenant_id='T', action_id='xb', action_type='no_action',
            followup_status='no_action', outcome_version_id=None, canonical_row_emitted=False))
        result = score_workflow(i, s, settings)
        self.assertEqual(result['actions']['records'], 2)
        self.assertEqual(result['actions']['executed_actions'], 1)
        self.assertEqual(result['actions']['handling_unknown_records'], 1)
        self.assertEqual(result['followup']['actions_without_comparable_arrival'], 0)

    def test_read_only(self):
        args = fixture(); before = deepcopy(args)
        score_workflow(*args)
        self.assertEqual(args, before)

    def test_missing_adoption_state_is_not_silently_dropped(self):
        i, s, settings = fixture(); s['eta_adoption'].pop()
        with self.assertRaisesRegex(ValueError, 'Every observed confirmed'): score_workflow(i, s, settings)

    def test_duplicate_successful_attempt_uses_service_clock_before_receipt_clock(self):
        i, s, settings = fixture()
        i['attempts'][1]['delivered_at'] = '2026-09-01T11:59:30Z'
        i['attempts'].append({**i['attempts'][1], 'delivery_id': 'earlier-service',
                             'delivered_at': '2026-09-01T11:59:00Z', 'receipt_known_at': '2026-09-01T12:00:30Z'})
        with self.assertRaisesRegex(ValueError, 'duplicate retry'): score_workflow(i, s, settings)
        s['eta_adoption'][0]['delivery_id'] = 'earlier-service'; s['opening_events'][0]['delivery_id'] = 'earlier-service'
        self.assertEqual(score_workflow(i, s, settings)['population']['confirmed_delivered_alerts'], 2)

    def test_other_tenant_delivery_reference_fails(self):
        i, s, settings = fixture(); s['eta_adoption'][0]['tenant_id'] = 'OTHER'
        with self.assertRaises(ValueError): score_workflow(i, s, settings)

    def test_failed_delivery_cannot_be_called_nonadoption(self):
        i, s, settings = fixture(); s['eta_adoption'][1]['delivery_id'] = 'dc'
        with self.assertRaises(ValueError): score_workflow(i, s, settings)

    def test_boolean_handling_seconds_is_rejected(self):
        i, s, settings = fixture(); s['eta_action'][0]['handling_seconds'] = True
        with self.assertRaisesRegex(ValueError, 'Handling seconds'): score_workflow(i, s, settings)

    def test_boolean_or_negative_or_fractional_delays_are_rejected(self):
        for value in (True, -1, .5, float('inf')):
            with self.subTest(value=value):
                i, s, settings = fixture(); s['eta_action_followup'][0]['observed_delay_seconds'] = value
                with self.assertRaises(ValueError): score_workflow(i, s, settings)

    def test_future_opening_cannot_enter_observed_funnel(self):
        i, s, settings = fixture(); s['opening_events'][0]['known_at'] = '2027-01-01T00:00:00Z'
        with self.assertRaises(ValueError): score_workflow(i, s, settings)

    def test_review_cannot_precede_or_omit_observed_opening(self):
        for mode in ('missing', 'future'):
            with self.subTest(mode=mode):
                i, s, settings = fixture()
                if mode == 'missing': s['opening_events'] = []
                else: s['opening_events'][0]['known_at'] = '2026-09-01T12:06:00Z'
                with self.assertRaisesRegex(ValueError, 'previously observed opening'):
                    score_workflow(i, s, settings)

    def test_unknown_delay_cannot_be_relabelled_known_without_maturity(self):
        i, s, settings = fixture(); s['followup_audit'][0]['followup_status'] = 'immature'
        with self.assertRaisesRegex(ValueError, 'mature confirmed'): score_workflow(i, s, settings)


if __name__ == '__main__': unittest.main()
