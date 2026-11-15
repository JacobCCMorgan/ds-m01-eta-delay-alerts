"""Independent checks reject coherent-looking workflow and accounting tampering."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from workflows.p09.audit import audit_workflow
from workflows.p09.engine import simulate_workflow
from workflows.p09.inputs import load_parent, load_settings
from workflows.p09.metrics import score_workflow
from workflows.p09.tests.test_engine import fixture


ROOT = Path(__file__).resolve().parents[3]


class IndependentAuditTests(unittest.TestCase):
    def setUp(self):
        self.inputs = fixture()
        self.settings = load_settings(ROOT)
        self.build()

    def build(self):
        self.simulation = simulate_workflow(self.inputs, self.settings)
        self.metrics = score_workflow(self.inputs, self.simulation, self.settings)

    def audit(self):
        return audit_workflow(self.inputs, self.simulation, self.metrics, self.settings)

    def assert_failure(self, code):
        result = self.audit()
        self.assertEqual(result['status'], 'failed')
        self.assertGreater(result['failure_count'], 0)
        self.assertTrue(any(row['check'] == code for row in result['failures']),
                        (code, result['failures'][:20]))

    def canonical_followup(self, audit):
        return next(r for r in self.simulation['eta_action_followup']
                    if (r['tenant_id'], r['action_id']) == (audit['tenant_id'], audit['action_id']))

    def test_hand_specified_reschedule_uses_same_arrival_and_two_deadlines(self):
        before = deepcopy((self.inputs, self.simulation, self.metrics, self.settings))
        row = next(r for r in self.simulation['followup_audit']
                   if r['action_type'] == 'reschedule' and r['followup_status'] == 'mature')
        self.assertEqual(row['actual_arrival_time'], '2026-08-02T14:00:00Z')
        self.assertEqual(row['original_deadline_at'], '2026-08-02T13:00:00Z')
        self.assertEqual(row['followup_deadline_at'], '2026-08-02T13:30:00Z')
        self.assertEqual(row['original_promise_delay_seconds'], 3600)
        self.assertEqual(row['observed_delay_seconds'], 1800)
        result = self.audit()
        self.assertEqual(result['status'], 'passed', result['failures'])
        self.assertFalse(result['causal_benefit_estimated'])
        self.assertEqual((self.inputs, self.simulation, self.metrics, self.settings), before)

    def test_published_parent_passes_without_calling_simulator_or_scorer(self):
        self.inputs = load_parent(ROOT)
        self.build()
        with (patch('workflows.p09.engine.simulate_workflow', side_effect=AssertionError('not independent')),
              patch('workflows.p09.metrics.score_workflow', side_effect=AssertionError('not independent'))):
            result = self.audit()
        self.assertEqual(result['status'], 'passed', result['failures'])
        self.assertGreater(result['comparison_count'], 1000)

    def test_wrong_delay_is_rejected_even_when_metrics_are_recomputed(self):
        row = next(r for r in self.simulation['followup_audit'] if r['followup_status'] == 'mature')
        row['original_promise_delay_seconds'] += 1
        self.canonical_followup(row)['original_promise_delay_seconds'] += 1
        self.metrics = score_workflow(self.inputs, self.simulation, self.settings)
        self.assert_failure('hand_computed_original_delay')

    def test_unknown_delay_cannot_be_relabelled_zero(self):
        row = next(r for r in self.simulation['followup_audit'] if r['followup_status'] == 'lost')
        row['original_promise_delay_seconds'] = 0
        row['observed_delay_seconds'] = 0
        self.canonical_followup(row).update(original_promise_delay_seconds=0, observed_delay_seconds=0)
        self.assert_failure('unobserved_followup_delay_is_null')

    def test_shifted_arrival_is_rejected_even_if_delay_matches_shift(self):
        row = next(r for r in self.simulation['followup_audit'] if r['followup_status'] == 'mature')
        row['actual_arrival_time'] = '2026-08-02T13:30:00Z'
        row['original_promise_delay_seconds'] = 1800
        row['observed_delay_seconds'] = 1800
        self.assert_failure('arrival_not_shifted')

    def test_future_outcome_revision_is_not_available_to_earlier_followup(self):
        row = next(r for r in self.simulation['followup_audit'] if r['followup_status'] == 'mature')
        selected = next(r for r in self.inputs['outcomes'] if r['outcome_version_id'] == row['outcome_version_id'])
        future = deepcopy(selected)
        future.update(outcome_version_id='FUTURE-REVISION', label_available_time='2027-01-01T00:00:00Z')
        self.inputs['outcomes'].append(future)
        row.update(outcome_version_id='FUTURE-REVISION', outcome_available_at=future['label_available_time'])
        self.canonical_followup(row)['outcome_version_id'] = 'FUTURE-REVISION'
        self.simulation['outcome_references'].append(future)
        self.assert_failure('outcome_known_by_observation_end')

    def test_latest_known_revision_withdraws_older_verified_arrival(self):
        row = next(r for r in self.simulation['followup_audit'] if r['followup_status'] == 'mature')
        old = next(r for r in self.inputs['outcomes'] if r['outcome_version_id'] == row['outcome_version_id'])
        replacement = deepcopy(old)
        replacement.update(outcome_version_id='WITHDRAWN-REVISION', label_available_time='2026-08-02T16:00:00Z',
                           label_status='pending', actual_arrival_time=None)
        self.inputs['outcomes'].append(replacement)
        self.assert_failure('latest_cutoff_known_outcome_selected')

    def test_missing_outcome_keeps_auxiliary_state_and_no_fabricated_version(self):
        self.inputs['outcomes'] = []
        self.build()
        self.assertEqual(self.audit()['status'], 'passed')
        row = next(r for r in self.simulation['followup_audit'] if r['followup_status'] == 'no_known_outcome')
        row.update(outcome_version_id='INVENTED', canonical_row_emitted=True)
        projected = {name: row[name] for name in (
            'tenant_id', 'action_id', 'outcome_version_id', 'followup_promise_version_id',
            'original_promise_version_id', 'outcome_observation_end', 'observed_delay_seconds',
            'original_promise_delay_seconds', 'review_outcome', 'result_origin')}
        projected['provenance_id'] = 'FORGED-PROVENANCE'
        self.simulation['eta_action_followup'].append(projected)
        self.assert_failure('retained_outcome_exists')

    def test_cross_tenant_action_is_rejected(self):
        self.simulation['eta_action'][0]['tenant_id'] = 'ANOTHER-TENANT'
        self.assert_failure('action_decision_fk')

    def test_duplicate_retry_cannot_add_second_adoption(self):
        copied = deepcopy(self.simulation['decision_audit'][0])
        copied['adoption_id'] = 'SECOND-ADOPTION'
        self.simulation['decision_audit'].append(copied)
        self.assert_failure('retry_does_not_duplicate_decision')

    def test_opening_before_receipt_knowledge_is_rejected(self):
        row = self.simulation['opening_events'][0]
        row['opened_at'] = '2026-08-02T09:59:00Z'
        self.assert_failure('opening_clock_order')

    def test_unknown_service_receipt_cannot_support_prior_adoption(self):
        self.inputs['attempts'][0]['receipt_observed'] = False
        self.assert_failure('all_confirmed_alerts_accounted')

    def test_target_influenced_review_profile_is_rejected(self):
        row = self.simulation['decision_audit'][0]
        row['decision_profile'] = (row['decision_profile'] + 1) % 10
        self.assert_failure('review_profile_identity_only')

    def test_forged_accepted_rate_or_denominator_is_rejected(self):
        self.metrics['rates']['accepted_or_partial_per_delivery']['denominator'] += 1
        self.assert_failure('metric_recount:rates')

    def test_boolean_count_cannot_impersonate_one(self):
        self.inputs = fixture(1)
        self.build()
        self.metrics['population']['alerts'] = True
        self.assert_failure('metric_recount:population')

    def test_zero_denominator_is_null_not_zero_or_certainty(self):
        self.inputs = fixture(0)
        self.build()
        self.assertEqual(self.audit()['status'], 'passed')
        self.assertIsNone(self.metrics['rates']['opened_per_delivery']['value'])
        self.metrics['rates']['opened_per_delivery']['value'] = 0.0
        self.assert_failure('metric_recount:rates')

    def test_immature_window_retains_unknown_even_with_verified_arrival(self):
        self.settings['observation_cutoff_at'] = '2026-08-03T16:00:00Z'
        self.build()
        self.assertEqual(self.audit()['status'], 'passed')
        row = next(r for r in self.simulation['followup_audit'] if r['followup_status'] == 'immature')
        row['observed_delay_seconds'] = 0
        self.assert_failure('unobserved_followup_delay_is_null')

    def test_source_followup_end_cannot_precede_its_arrival(self):
        self.inputs['outcomes'][1]['followup_through'] = '2026-08-02T13:00:00Z'
        self.assert_failure('arrival_within_source_observation')

    def test_source_followup_clock_is_not_workflow_cutoff(self):
        row = next(r for r in self.simulation['followup_audit'] if r['outcome_version_id'] is not None)
        row['source_followup_through'] = row['outcome_observation_end']
        self.assert_failure('source_observation_clock_binding')

    def test_false_approval_and_benefit_claims_are_rejected(self):
        self.simulation['summary']['quality_accepted'] = True
        self.metrics['causal_benefit_estimated'] = True
        self.assert_failure('summary_false_claim:quality_accepted')

    def test_summary_omission_cannot_hide_unreviewed_records(self):
        self.simulation['summary']['adoption_status_counts'].pop('not_reviewed')
        self.assert_failure('summary_adoption_status_counts')

    def test_tenant_metrics_are_recounted_separately(self):
        self.metrics['by_tenant']['T1']['actions']['handling_seconds_sum'] += 1
        self.assert_failure('metric_recount:by_tenant')

    def test_equal_knowledge_clock_uses_documented_version_tie_break(self):
        row = next(r for r in self.simulation['followup_audit'] if r['followup_status'] == 'mature')
        selected = next(r for r in self.inputs['outcomes'] if r['outcome_version_id'] == row['outcome_version_id'])
        revision = deepcopy(selected)
        revision.update(outcome_version_id='ZZ-PENDING-REVISION', label_status='pending', actual_arrival_time=None)
        self.inputs['outcomes'].append(revision)
        self.build()
        self.assertEqual(self.audit()['status'], 'passed')
        replacement = next(r for r in self.simulation['followup_audit'] if r['action_id'] == row['action_id'])
        self.assertEqual(replacement['outcome_version_id'], 'ZZ-PENDING-REVISION')
        self.assertIsNone(replacement['observed_delay_seconds'])

    def test_missing_event_is_rejected_even_when_summary_count_is_adjusted(self):
        self.simulation['events'].pop()
        self.simulation['summary']['counts']['events'] -= 1
        self.assert_failure('event_inventory_complete')

    def test_date_only_arrival_cannot_support_an_exact_point_delay(self):
        row = next(r for r in self.simulation['followup_audit'] if r['followup_status'] == 'mature')
        selected = next(r for r in self.inputs['outcomes'] if r['outcome_version_id'] == row['outcome_version_id'])
        selected['arrival_time_precision'] = 'date_only'
        self.assert_failure('mature_arrival_has_point_precision')

    def test_missing_independent_arrival_evidence_cannot_be_confirmed(self):
        row = next(r for r in self.simulation['followup_audit'] if r['followup_status'] == 'mature')
        selected = next(r for r in self.inputs['outcomes'] if r['outcome_version_id'] == row['outcome_version_id'])
        selected['arrival_evidence_ref'] = None
        self.assert_failure('mature_arrival_has_evidence_reference')


if __name__ == '__main__':
    unittest.main()
