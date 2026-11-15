"""Workflow conformance against clock, lineage, missingness and independence cases."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from workflows.p09.engine import simulate_workflow, validate_simulation, _number
from workflows.p09.inputs import load_settings


ROOT = Path(__file__).resolve().parents[3]


def fixture(count=80):
    inputs = dict(parent_tables={}, attempts=[], alerts=[], opportunities=[], promises=[],
                  outcomes=[], source_sha256={}, parent_report='fixture', scenario_id='S00_unmodified',
                  parent_delivery_summary={})
    for number in range(count):
        tenant, trip, snapshot = 'T1', 'TRIP-' + str(number), 'SNAP-' + str(number)
        alert, delivery, promise = 'ALERT-' + str(number), 'DELIVERY-' + str(number), 'PROMISE-' + str(number)
        inputs['promises'].append(dict(tenant_id=tenant, trip_id=trip, promise_version_id=promise,
            provenance_id='PROV-' + promise, known_at='2026-08-01T00:00:00Z',
            window_start_at='2026-08-02T12:00:00Z', window_end_at='2026-08-02T13:00:00Z'))
        inputs['opportunities'].append(dict(tenant_id=tenant, trip_id=trip, snapshot_id=snapshot,
            prediction_version='MODEL', promise_version_id=promise, snapshot_time='2026-08-02T10:00:00Z'))
        inputs['alerts'].append(dict(tenant_id=tenant, trip_id=trip, alert_id=alert,
            snapshot_id=snapshot, prediction_version='MODEL', promise_version_id=promise,
            created_at='2026-08-02T10:00:00Z', deadline_at='2026-08-02T13:00:00Z'))
        inputs['attempts'].append(dict(tenant_id=tenant, trip_id=trip, alert_id=alert, delivery_id=delivery,
            sent_at='2026-08-02T10:00:00Z', delivered_at='2026-08-02T10:00:30Z',
            receipt_known_at='2026-08-02T10:00:31Z', delivery_status='delivered', receipt_observed=True,
            result_origin='synthetic_scenario'))
        for version, available, status, arrival in (
                ('V1', '2026-08-02T10:10:00Z', 'pending', None),
                ('V2', '2026-08-02T15:00:00Z', 'verified', '2026-08-02T14:00:00Z')):
            inputs['outcomes'].append(dict(tenant_id=tenant, trip_id=trip,
                outcome_version_id='OUTCOME-' + str(number) + '-' + version,
                provenance_id='PROV-' + str(number) + version,
                label_status=status, label_available_time=available, actual_arrival_time=arrival,
                arrival_time_precision='second' if arrival else 'unknown',
                arrival_evidence_ref='fixture-independent-gate#' + trip if arrival else None,
                cancellation_time=None, followup_through=available))
    return inputs


class WorkflowEngineTests(unittest.TestCase):
    def setUp(self):
        self.inputs = fixture()
        self.settings = load_settings(ROOT)

    def run_workflow(self):
        return simulate_workflow(self.inputs, self.settings)

    def test_deterministic_independent_of_input_order_and_no_mutation(self):
        before = deepcopy(self.inputs)
        result = self.run_workflow()
        self.assertEqual(self.inputs, before)
        for name in ('attempts', 'alerts', 'promises', 'opportunities', 'outcomes'):
            self.inputs[name].reverse()
        self.assertEqual(result, self.run_workflow())

    def test_review_and_action_choices_do_not_depend_on_outcomes(self):
        original = self.run_workflow()
        self.inputs['outcomes'] = []
        changed = self.run_workflow()
        for name in ('opening_events', 'eta_adoption', 'eta_action', 'decision_audit', 'promise_versions'):
            self.assertEqual(original[name], changed[name])
        self.assertFalse(changed['eta_action_followup'])
        self.assertTrue(any(row['followup_status'] == 'no_known_outcome' for row in changed['followup_audit']))

    def test_undelivered_and_unobserved_are_not_nonadoption(self):
        self.inputs['attempts'][0].update(delivery_status='failed', delivered_at=None, receipt_known_at=None)
        self.inputs['attempts'][1]['receipt_observed'] = False
        result = self.run_workflow()
        self.assertEqual(len(result['eta_adoption']), 78)
        self.assertFalse(any(r['delivery_id'] in {'DELIVERY-0', 'DELIVERY-1'} for r in result['eta_adoption']))

    def test_retries_do_not_create_duplicate_adoption(self):
        duplicate = deepcopy(self.inputs['attempts'][0])
        duplicate.update(delivery_id='LATER-RETRY', delivered_at='2026-08-02T10:01:00Z',
                         receipt_known_at='2026-08-02T10:01:01Z')
        self.inputs['attempts'].append(duplicate)
        result = self.run_workflow()
        self.assertEqual(len(result['eta_adoption']), 80)
        self.assertFalse(any(r['delivery_id'] == 'LATER-RETRY' for r in result['eta_adoption']))

    def test_opening_does_not_establish_review_or_adoption(self):
        result = self.run_workflow()
        opened_not_reviewed = [r for r in result['decision_audit']
                              if r['opening_id'] is not None and r['adoption_status'] == 'not_reviewed']
        self.assertTrue(opened_not_reviewed)
        action_parents = {r['adoption_id'] for r in result['eta_action']}
        self.assertTrue(all(r['decided_at'] is None and r['adoption_id'] not in action_parents
                            for r in opened_not_reviewed))

    def test_all_controlled_review_states_are_retained(self):
        result = self.run_workflow()
        self.assertEqual(set(result['summary']['adoption_status_counts']),
                         {'accepted', 'partially_accepted', 'overridden', 'rejected', 'not_reviewed'})

    def test_rejected_no_action_does_not_become_intervention_followup(self):
        result = self.run_workflow()
        no_actions = {r['action_id'] for r in result['eta_action'] if r['action_type'] == 'no_action'}
        self.assertTrue(no_actions)
        self.assertTrue(all(r['action_id'] not in no_actions for r in result['eta_action_followup']))

    def test_reschedule_preserves_original_deadline_and_arrival(self):
        result = self.run_workflow()
        rows = [r for r in result['followup_audit'] if r['action_type'] == 'reschedule']
        self.assertTrue(rows)
        self.assertTrue(all(r['original_deadline_at'] == '2026-08-02T13:00:00Z' for r in rows))
        self.assertTrue(all(r['followup_deadline_at'] == '2026-08-02T13:30:00Z' for r in rows))
        confirmed = [r for r in rows if r['review_outcome'] == 'confirmed']
        self.assertTrue(confirmed)
        self.assertTrue(all(r['actual_arrival_time'] == '2026-08-02T14:00:00Z'
                            and r['original_promise_delay_seconds'] == 3600
                            and r['observed_delay_seconds'] == 1800 for r in confirmed))

    def test_immature_followup_does_not_become_zero_delay(self):
        self.settings['observation_cutoff_at'] = '2026-08-03T16:00:00Z'
        result = self.run_workflow()
        immature = [r for r in result['followup_audit'] if r['followup_status'] == 'immature']
        self.assertTrue(immature)
        self.assertTrue(all(r['observed_delay_seconds'] is None
                            and r['original_promise_delay_seconds'] is None for r in immature))

    def test_lost_unreported_and_disputed_remain_unknown(self):
        result = self.run_workflow()
        selected = [r for r in result['followup_audit'] if r['followup_status'] in ('lost', 'not_reported', 'disputed')]
        self.assertEqual({r['followup_status'] for r in selected}, {'lost', 'not_reported', 'disputed'})
        self.assertTrue(all(r['observed_delay_seconds'] is None for r in selected))

    def test_future_outcome_revision_is_not_selected(self):
        future = deepcopy(self.inputs['outcomes'][1])
        future.update(outcome_version_id='FUTURE', label_available_time='2027-01-01T00:00:00Z',
                      actual_arrival_time='2026-08-02T16:00:00Z')
        self.inputs['outcomes'].append(future)
        result = self.run_workflow()
        self.assertFalse(any(r['outcome_version_id'] == 'FUTURE' for r in result['eta_action_followup']))

    def test_imprecise_verified_arrivals_do_not_create_point_delays(self):
        baseline = self.run_workflow()
        confirmed_ids = {r['action_id'] for r in baseline['followup_audit'] if r['review_outcome'] == 'confirmed'}
        self.assertTrue(confirmed_ids)
        for precision in ('date_only', 'interval', 'unknown', None):
            for outcome in self.inputs['outcomes']:
                if outcome['label_status'] == 'verified':
                    outcome['arrival_time_precision'] = precision
            changed = self.run_workflow()
            selected = [r for r in changed['followup_audit'] if r['action_id'] in confirmed_ids]
            self.assertTrue(all(r['review_outcome'] == 'not_reported'
                                and r['observed_delay_seconds'] is None
                                and r['original_promise_delay_seconds'] is None for r in selected))

    def test_missing_gate_evidence_and_conflicting_cancellation_remain_unknown(self):
        baseline = self.run_workflow()
        confirmed_ids = {r['action_id'] for r in baseline['followup_audit'] if r['review_outcome'] == 'confirmed'}
        for field, value in (('arrival_evidence_ref', None), ('arrival_evidence_ref', '  '),
                             ('cancellation_time', '2026-08-02T11:00:00Z')):
            changed_inputs = deepcopy(self.inputs)
            for outcome in changed_inputs['outcomes']:
                if outcome['label_status'] == 'verified':
                    outcome[field] = value
            changed = simulate_workflow(changed_inputs, self.settings)
            selected = [r for r in changed['followup_audit'] if r['action_id'] in confirmed_ids]
            self.assertTrue(all(r['review_outcome'] == 'not_reported'
                                and r['observed_delay_seconds'] is None for r in selected))

    def test_minute_precision_retains_coarse_source_precision(self):
        for outcome in self.inputs['outcomes']:
            if outcome['label_status'] == 'verified':
                outcome['arrival_time_precision'] = 'minute'
        changed = self.run_workflow()
        confirmed = [r for r in changed['followup_audit'] if r['review_outcome'] == 'confirmed']
        self.assertTrue(confirmed)
        self.assertTrue(all(r['source_arrival_time_precision'] == 'minute' for r in confirmed))

    def test_future_receipt_is_excluded(self):
        self.inputs['attempts'][0]['receipt_known_at'] = '2027-01-01T00:00:00Z'
        result = self.run_workflow()
        self.assertEqual(len(result['eta_adoption']), 79)

    def test_mutated_canonical_row_fails_reconstruction(self):
        result = self.run_workflow()
        result['eta_adoption'][0]['adoption_status'] = 'accepted'
        result['eta_adoption'][0]['adoption_reason'] = 'tampered'
        with self.assertRaisesRegex(ValueError, 'reconstruction'):
            validate_simulation(self.inputs, result, self.settings)

    def test_invalid_cross_tenant_delivery_is_rejected(self):
        self.inputs['attempts'][0]['tenant_id'] = 'OTHER'
        with self.assertRaisesRegex(ValueError, 'lineage'):
            self.run_workflow()

    def test_receipt_before_delivery_is_rejected(self):
        self.inputs['attempts'][0]['receipt_known_at'] = '2026-08-02T10:00:00Z'
        with self.assertRaisesRegex(ValueError, 'Receipt'):
            self.run_workflow()

    def test_duplicate_identity_is_rejected(self):
        self.inputs['attempts'].append(deepcopy(self.inputs['attempts'][0]))
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.run_workflow()

    def test_canonical_rows_match_contract_exactly(self):
        result = self.run_workflow()
        fields = json.loads((ROOT / 'contracts/fields.json').read_text())
        for table in ('eta_adoption', 'eta_action', 'eta_action_followup'):
            expected = {f['name'] for f in fields if f['table'] == table}
            self.assertTrue(result[table])
            self.assertTrue(all(set(row) == expected for row in result[table]))

    def test_acceptance_claims_cannot_be_enabled(self):
        self.settings['quality_accepted'] = True
        with self.assertRaisesRegex(ValueError, 'acceptance'):
            self.run_workflow()


if __name__ == '__main__':
    unittest.main()
