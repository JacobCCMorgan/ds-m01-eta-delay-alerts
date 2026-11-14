"""Independent tiny timelines for final denominators and global-first slices."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from ds_m01.final_delivery_metrics import distribution, score_policy_delivery
from ds_m01.final_policy import first_ids, run_policy_delivery
from test_final_policy import SETTINGS, opportunity, stamp


def target(row, arrival=10800):
    from ds_m01.common import parse_ts
    at = stamp(arrival) if arrival is not None else None
    eligible = at is not None and parse_ts(at) > parse_ts(row['snapshot_time'])
    return {**{k: row[k] for k in ('tenant_id', 'trip_id', 'snapshot_id', 'snapshot_time', 'promise_version_id', 'deadline_at')},
        'eligibility': 'eligible' if eligible else 'excluded',
        'delay_label': parse_ts(at) > parse_ts(row['deadline_at']) if eligible else None,
        'reason': 'verified_gate_arrival' if eligible else 'already_arrived_at_snapshot' if at else 'latest_outcome_pending',
        'actual_arrival_time': at, 'label_known_at': '2026-08-20T00:00:00Z',
        'label_cutoff_at': '2026-10-15T07:00:00Z', 'label_status': 'verified' if at else 'pending',
        'arrival_time_precision': 'second' if at else 'unknown', 'outcome_version_id': 'OUTCOME-' + row['trip_id']}


def transport(outcome='delivered', latency=0, lag=0):
    return dict(outcome=outcome, latency_seconds=latency, receipt_lag_seconds=lag)


def bundle_for(rows, draws=None):
    with patch('ds_m01.delivery_simulation.transport_outcome', side_effect=draws or [transport()] * len(rows)):
        return run_policy_delivery(rows, SETTINGS)


def subset(output, dimension, value):
    return next(r for r in output['subgroups'] if r['dimension'] == dimension and r['value'] == value)


class FinalDeliveryMetricsTests(unittest.TestCase):
    def test_unavailable_positive_trip_remains_both_candidate_and_delivery_recall_denominator(self):
        rows = [opportunity(), opportunity('S2', trip='TRIP2')]
        rows[1].update(eta_status='unavailable', eta_reason='unsupported', lower_seconds=None, median_seconds=None,
            upper_seconds=None, lower_seconds_int=None, median_seconds_int=None, upper_seconds_int=None)
        output = score_policy_delivery(rows, [target(r) for r in rows], bundle_for(rows), SETTINGS)
        self.assertEqual(output['candidate']['counts']['actionable_positive_trips'], 2)
        self.assertEqual(output['candidate']['metrics']['actionable_trip_recall'], .5)
        self.assertEqual(output['delivery']['metrics']['delivered_actionable_trip_recall'], .5)
        self.assertEqual(output['delivery']['counts']['monitored_trips'], 2)
        missing = subset(output, 'eta_source', 'unavailable')
        self.assertEqual(missing['delivery']['counts']['actionable_positive_trips'], 1)
        self.assertEqual(missing['delivery']['metrics']['delivered_actionable_trip_recall'], 0)

    def test_first_success_after_failed_candidate_uses_successful_alert_promise(self):
        rows = [opportunity(deadline=7200), opportunity('S2', 1860, deadline=14400, promise='P2')]
        rows[1]['promise_known_at'] = stamp(1800)
        bundle = bundle_for(rows, [transport('permanent_failure'), transport()])
        output = score_policy_delivery(rows, [target(r) for r in rows], bundle, SETTINGS)
        self.assertEqual(output['candidate']['metrics']['conditional_first_candidate_precision'], 1)
        self.assertEqual(output['delivery']['delivery_rows'][0]['promise_version_id'], 'P2')
        self.assertEqual(output['delivery']['delivery_rows'][0]['classification'], 'known_false')
        self.assertEqual(output['delivery']['metrics']['conditional_first_delivery_useful_precision'], 0)
        self.assertEqual(output['delivery']['metrics']['delivered_actionable_trip_recall'], 0)

    def test_snapshot_slice_does_not_replace_global_first_with_later_useful_reminder(self):
        rows = [opportunity(deadline=14400), opportunity('S2', 1860, deadline=7200, promise='P2')]
        rows[1].update(position_validity='low_confidence', promise_known_at=stamp(1800))
        output = score_policy_delivery(rows, [target(r) for r in rows], bundle_for(rows), SETTINGS)
        self.assertEqual(output['delivery']['metrics']['conditional_first_delivery_useful_precision'], 0)
        self.assertEqual(output['delivery']['metrics']['delivered_actionable_trip_recall'], 1)
        later = subset(output, 'original_position_validity', 'low_confidence')
        self.assertEqual(later['candidate']['counts']['first_candidates'], 0)
        self.assertEqual(later['delivery']['counts']['first_successful_deliveries'], 0)
        self.assertIsNone(later['delivery']['metrics']['conditional_first_delivery_useful_precision'])
        self.assertEqual(later['delivery']['counts']['first_effective_deliveries'], 1)
        self.assertEqual(later['candidate']['counts']['repeated_candidates_within_episode'], 1)
        self.assertEqual(later['delivery']['counts']['repeated_deliveries_within_episode'], 1)
        self.assertEqual(later['delivery']['counts']['within_slice_repeated_deliveries'], 0)
        self.assertEqual(output['delivery']['lead_seconds']['first_effective_deliveries']['prospective_deadline_lead_seconds']['N'], 1)

    def test_known_postarrival_is_nonuseful_burden_and_unknown_stays_unresolved(self):
        rows = [opportunity(), opportunity('S2', trip='TRIP2')]
        labels = [target(rows[0], arrival=0), target(rows[1], arrival=None)]
        output = score_policy_delivery(rows, labels, bundle_for(rows), SETTINGS)
        counts = output['delivery']['counts']
        self.assertEqual(counts['successful_confirmed_deliveries'], 2)
        self.assertEqual(counts['post_arrival_first_deliveries'], 1)
        self.assertEqual(counts['unresolved_first_deliveries'], 1)
        self.assertEqual(output['delivery']['metrics']['conditional_first_delivery_useful_precision'], 0)
        self.assertEqual(output['delivery']['metrics']['first_delivery_usefulness_bounds'], {'lower': 0, 'upper': .5})
        self.assertEqual(output['delivery']['metrics']['successful_deliveries_per_100_monitored_trips'], 100)
        self.assertFalse(output['quality_descriptors']['candidate_budget_50_per_100_met'])

    def test_target_changes_cannot_change_frozen_first_delivery_identity(self):
        rows = [opportunity(), opportunity('S2', 1860)]
        bundle = bundle_for(rows)
        positive = score_policy_delivery(rows, [target(r) for r in rows], bundle, SETTINGS)
        unknown = score_policy_delivery(rows, [target(r, arrival=None) for r in rows], bundle, SETTINGS)
        self.assertEqual(positive['first_successful_delivery_ids'], unknown['first_successful_delivery_ids'])
        self.assertEqual(positive['first_candidate_ids'], unknown['first_candidate_ids'])
        self.assertNotEqual(positive['first_effective_delivery_ids'], unknown['first_effective_delivery_ids'])

    def test_receipt_boundary_inclusive_and_label_boundary_exclusive(self):
        rows = [opportunity()]
        bundle = bundle_for(rows)
        bundle['delivery']['attempts'][0]['receipt_known_at'] = SETTINGS['delivery']['counts_as_of']
        output = score_policy_delivery(rows, [target(rows[0])], bundle, SETTINGS)
        self.assertEqual(output['delivery']['counts']['successful_confirmed_deliveries'], 1)
        bad = target(rows[0]); bad['label_known_at'] = SETTINGS['population']['label_cutoff_exclusive']
        with self.assertRaisesRegex(ValueError, 'strictly before'):
            score_policy_delivery(rows, [bad], bundle, SETTINGS)

    def test_fixed_distribution_interpolation_and_all_empty_novelty_groups(self):
        self.assertEqual(distribution([0, 10])['p10'], 1)
        self.assertEqual(distribution([0, 10])['p25'], 2.5)
        self.assertEqual(distribution([])['null_reason'], 'no_observed_values')
        output = score_policy_delivery([], [], bundle_for([]), SETTINGS)
        novelty = [r for r in output['subgroups'] if r['dimension'] == 'novelty_group']
        self.assertEqual(len(novelty), 4)
        self.assertTrue(all(r['empty'] for r in novelty))
        self.assertTrue(all(r['delivery']['metrics']['delivered_actionable_trip_recall'] is None for r in novelty))
        self.assertFalse(output['policy_delivery_bootstrap'])
        self.assertIsNone(output['quality_descriptors']['candidate_budget_50_per_100_met'])

    def test_retry_burden_and_descriptive_transport_slices_have_distinct_denominators(self):
        rows = [opportunity()]
        bundle = bundle_for(rows, [transport('transient_failure'), transport()])
        output = score_policy_delivery(rows, [target(rows[0])], bundle, SETTINGS)
        self.assertEqual(output['delivery']['metrics']['attempts_per_100_monitored_trips'], 200)
        self.assertEqual(output['delivery']['metrics']['successful_deliveries_per_100_monitored_trips'], 100)
        self.assertEqual(output['delivery']['counts']['repeated_deliveries_within_episode'], 0)
        retry = next(r for r in output['delivery_slices'] if r['dimension'] == 'alert_transport_path' and r['value'] == 'retry_success')
        self.assertEqual(retry['alerts'], 1)
        self.assertEqual(retry['candidate_to_delivery_seconds']['median'], 30)

    def test_corrupted_first_identity_or_policy_decision_is_rejected(self):
        rows = [opportunity()]
        bundle = bundle_for(rows)
        broken = deepcopy(bundle); broken['first_successful_delivery_ids'] = []
        with self.assertRaisesRegex(ValueError, 'First-event'):
            score_policy_delivery(rows, [target(rows[0])], broken, SETTINGS)
        broken = deepcopy(bundle); broken['policy']['decisions'][0]['probability'] = '0.800000'
        with self.assertRaisesRegex(ValueError, 'Candidate ledger'):
            score_policy_delivery(rows, [target(rows[0])], broken, SETTINGS)


if __name__ == '__main__':
    unittest.main()
