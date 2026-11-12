"""Hand-specified transport timelines, independent of trip outcome labels."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ds_m01.delivery_simulation import simulate_delivery, transport_outcome


START = datetime(2026, 7, 16, 10, tzinfo=timezone.utc)
SETTINGS = json.loads((Path(__file__).resolve().parents[1] / 'config/p07.json').read_text())


def stamp(seconds):
    return (START + timedelta(seconds=seconds)).isoformat().replace('+00:00', 'Z')


def candidate(identifier='C1', seconds=0, *, tenant='T1', trip='TRIP', episode='E1', deadline=14400, promise='P1'):
    return dict(tenant_id=tenant, trip_id=trip, snapshot_id='S-' + identifier,
        snapshot_time=stamp(seconds), promise_version_id=promise, deadline_at=stamp(deadline),
        prediction_version='MODEL-' + tenant, policy_version='RULE', candidate_status='admitted',
        candidate_id=identifier, episode_id=episode, lead_seconds=deadline-seconds,
        probability='0.600000', reason='threshold_support_lead_cooldown_capacity_passed',
        simulated_issue_at=stamp(seconds))


def episodes_for(candidates, *, closed_at=None, last_observed=None):
    grouped = {}
    for row in candidates:
        grouped.setdefault((row['tenant_id'], row['episode_id']), []).append(row)
    result = []
    for (tenant, identifier), rows in grouped.items():
        first = min(rows, key=lambda r: r['snapshot_time'])
        last = (stamp(closed_at) if closed_at is not None else stamp(last_observed)
                if last_observed is not None else max(r['snapshot_time'] for r in rows))
        result.append(dict(tenant_id=tenant, trip_id=first['trip_id'], policy_version='RULE', episode_id=identifier,
            episode_started_at=first['snapshot_time'], start_snapshot_id=first['snapshot_id'],
            start_promise_version_id=first['promise_version_id'], episode_last_observed_at=last,
            episode_closed_at=stamp(closed_at) if closed_at is not None else None,
            closure_reason='supported_probability_below_threshold' if closed_at is not None else None,
            episode_status='closed' if closed_at is not None else 'censored',
            censored_at=None if closed_at is not None else last, candidate_count=len(rows)))
    return result


def draw(outcome='delivered', latency=5, lag=0):
    return dict(outcome=outcome, latency_seconds=latency, receipt_lag_seconds=lag)


class DeliverySimulationTests(unittest.TestCase):
    def run_case(self, candidates=None, episodes=None, draws=None, cutoff=None):
        candidates = candidates if candidates is not None else [candidate()]
        episodes = episodes if episodes is not None else episodes_for(candidates)
        with patch('ds_m01.delivery_simulation.transport_outcome', side_effect=draws or [draw()]):
            return simulate_delivery(candidates, episodes, SETTINGS, counts_as_of=stamp(cutoff) if cutoff is not None else None)

    def test_failed_attempt_then_success_counts_one_delivery_and_waits_for_receipt(self):
        result = self.run_case(draws=[draw('transient_failure', 5, 10), draw()])
        attempts = result['attempts']
        self.assertEqual([a['sent_at'] for a in attempts], [stamp(0), stamp(45)])
        self.assertEqual([a['delivery_status'] for a in attempts], ['failed', 'delivered'])
        self.assertEqual(attempts[0]['receipt_known_at'], stamp(15))
        self.assertEqual(attempts[1]['delivered_at'], stamp(50))
        self.assertNotEqual(attempts[0]['delivery_id'], attempts[1]['delivery_id'])
        self.assertEqual(result['alerts'][0]['first_delivery_id'], attempts[1]['delivery_id'])
        self.assertEqual(result['episodes'][0]['delivered_alert_count'], 1)
        self.assertEqual(result['episodes'][0]['duplicate_alert_count'], 0)

    def test_three_confirmed_transient_failures_exhaust_bounded_retries(self):
        result = self.run_case(draws=[draw('transient_failure')] * 3)
        self.assertEqual([a['sent_at'] for a in result['attempts']], [stamp(0), stamp(35), stamp(160)])
        self.assertEqual(result['alerts'][0]['status_reason'], 'maximum_attempts_exhausted')
        self.assertEqual(result['summary']['delivered_attempts'], 0)
        self.assertEqual(result['attempts'][-1]['retry_scheduled_at'], None)

    def test_permanent_failure_does_not_retry(self):
        result = self.run_case(draws=[draw('permanent_failure')])
        self.assertEqual(len(result['attempts']), 1)
        self.assertFalse(result['attempts'][0]['retryable'])
        self.assertEqual(result['alerts'][0]['alert_status'], 'failed')

    def test_unknown_times_out_unresolved_without_retry_or_negative_delivery_claim(self):
        before = self.run_case(draws=[draw('unknown')], cutoff=119.999999)
        self.assertEqual(before['attempts'][0]['delivery_status'], 'queued')
        self.assertIsNone(before['attempts'][0]['receipt_known_at'])
        result = self.run_case(draws=[draw('unknown')], cutoff=120)
        self.assertEqual(len(result['attempts']), 1)
        self.assertEqual(result['attempts'][0]['delivery_status'], 'unknown')
        self.assertIsNone(result['attempts'][0]['delivered_at'])
        self.assertEqual(result['alerts'][0]['status_reason'], 'unknown_resolution')
        self.assertEqual(result['summary']['delivered_attempts'], 0)

    def test_latency_compression_cannot_break_delivery_cooldown_across_revisions(self):
        rows = [candidate(), candidate('C2', 1800, episode='E2', promise='P2')]
        result = self.run_case(rows, draws=[draw(latency=60)])
        alerts = {r['candidate_id']: r for r in result['alerts']}
        self.assertEqual(alerts['C1']['alert_status'], 'delivered')
        self.assertEqual(alerts['C2']['status_reason'], 'delivery_cooldown')
        self.assertEqual(alerts['C2']['attempt_count'], 0)
        self.assertEqual(result['summary']['delivered_attempts'], 1)

    def test_exact_delivery_cooldown_allows_distinct_reminder_and_counts_repeat(self):
        rows = [candidate(), candidate('C2', 1860)]
        result = self.run_case(rows, draws=[draw(latency=60), draw(latency=0)])
        self.assertEqual(result['summary']['delivered_attempts'], 2)
        self.assertEqual(result['episodes'][0]['delivered_alert_count'], 2)
        self.assertEqual(result['episodes'][0]['duplicate_alert_count'], 1)

    def test_service_gate_protects_two_previously_dispatched_attempts(self):
        rows = [candidate(), candidate('C2', 1)]
        result = self.run_case(rows, draws=[draw(latency=60), draw(latency=5)])
        attempts = {r['candidate_id']: r for r in result['attempts']}
        self.assertEqual(attempts['C2']['delivered_at'], stamp(6))
        self.assertEqual(attempts['C1']['service_outcome'], 'gate_blocked')
        self.assertEqual(attempts['C1']['error_reason'], 'delivery_cooldown')
        self.assertEqual(result['summary']['delivered_attempts'], 1)

    def test_lead_boundary_uses_exact_service_clock_and_keeps_blocked_attempt(self):
        exact = self.run_case([candidate(deadline=1805)])
        self.assertEqual(exact['alerts'][0]['alert_status'], 'delivered')
        fractional = self.run_case([candidate(deadline=1804.999999)])
        self.assertEqual(fractional['alerts'][0]['alert_status'], 'no_action_lead')
        self.assertEqual(fractional['attempts'][0]['error_reason'], 'insufficient_action_lead')
        self.assertIsNone(fractional['attempts'][0]['delivered_at'])

    def test_closure_at_dispatch_precedes_candidate_and_no_attempt_is_sent(self):
        rows = [candidate()]
        result = self.run_case(rows, episodes_for(rows, closed_at=0))
        self.assertEqual(result['alerts'][0]['status_reason'], 'episode_closed')
        self.assertEqual(result['attempts'], [])
        self.assertEqual([e['event_type'] for e in result['events']], ['closure', 'candidate', 'dispatch'])

    def test_closure_precedes_equal_time_service_but_does_not_erase_prior_success(self):
        rows = [candidate()]
        blocked = self.run_case(rows, episodes_for(rows, closed_at=5))
        self.assertEqual(blocked['alerts'][0]['status_reason'], 'episode_closed')
        delivered = self.run_case(rows, episodes_for(rows, closed_at=5), draws=[draw(latency=0, lag=5)])
        self.assertEqual(delivered['alerts'][0]['alert_status'], 'delivered')
        at_five = [e['event_type'] for e in delivered['events'] if e['event_at'] == stamp(5)]
        self.assertEqual(at_five, ['closure', 'receipt'])

    def test_censored_feed_does_not_close_risk_or_prevent_pending_delivery(self):
        rows = [candidate()]
        result = self.run_case(rows, episodes_for(rows, last_observed=0))
        self.assertEqual(result['alerts'][0]['alert_status'], 'delivered')
        self.assertIsNone(result['episodes'][0]['episode_closed_at'])
        self.assertEqual(result['episodes'][0]['value_states'], {'episode_closed_at': 'not_mature'})

    def test_cutoff_retains_unconfirmed_service_without_counting_future_receipt(self):
        before_service = self.run_case(draws=[draw(lag=15)], cutoff=4)
        self.assertIsNone(before_service['attempts'][0]['service_outcome'])
        self.assertIsNone(before_service['attempts'][0]['service_at'])
        waiting = self.run_case(draws=[draw(lag=15)], cutoff=10)
        attempt = waiting['attempts'][0]
        self.assertEqual(attempt['service_outcome'], 'delivered')
        self.assertEqual(attempt['service_at'], stamp(5))
        self.assertEqual(attempt['scheduled_receipt_at'], stamp(20))
        self.assertIsNone(attempt['receipt_known_at'])
        self.assertIsNone(attempt['delivered_at'])
        self.assertFalse(attempt['receipt_observed'])
        self.assertEqual(waiting['summary']['delivered_alerts'], 0)
        self.assertEqual(waiting['alerts'][0]['alert_status'], 'queued')
        exact = self.run_case(draws=[draw(lag=15)], cutoff=20)
        self.assertEqual(exact['summary']['delivered_attempts'], 1)
        self.assertEqual(exact['attempts'][0]['receipt_known_at'], stamp(20))

    def test_ttl_is_inclusive_at_service_and_blocks_expired_delivery(self):
        exact = self.run_case(draws=[draw(latency=600)])
        self.assertEqual(exact['alerts'][0]['alert_status'], 'delivered')
        expired = self.run_case(draws=[draw(latency=601)])
        self.assertEqual(expired['alerts'][0]['status_reason'], 'ttl_expired')
        self.assertEqual(expired['summary']['delivered_attempts'], 0)

    def test_future_candidates_and_zero_candidate_episodes_remain_visible(self):
        rows = [candidate(seconds=60)]
        episodes = episodes_for(rows)
        empty = deepcopy(episodes[0])
        empty.update(episode_id='EMPTY', trip_id='OTHER', candidate_count=0)
        episodes.append(empty)
        result = self.run_case(rows, episodes, cutoff=0)
        self.assertEqual(len(result['alerts']), 1)
        self.assertEqual(len(result['episodes']), 2)
        self.assertEqual(result['alerts'][0]['status_reason'], 'candidate_not_yet_observed')
        self.assertEqual(result['attempts'], [])
        self.assertEqual(result['events'], [])

    def test_exact_duplicate_inputs_are_idempotent_and_conflicts_fail(self):
        rows = [candidate()]
        episodes = episodes_for(rows)
        original = self.run_case(rows, episodes)
        repeated = self.run_case(rows * 2, episodes * 2)
        for key in ('alerts', 'attempts', 'episodes', 'events'):
            self.assertEqual(original[key], repeated[key])
        self.assertEqual(repeated['summary']['duplicate_candidate_records_ignored'], 1)
        conflict = deepcopy(rows[0]); conflict['probability'] = '0.700000'
        with self.assertRaisesRegex(ValueError, 'Conflicting duplicate'):
            simulate_delivery(rows + [conflict], episodes, SETTINGS)
        conflicting_episode = deepcopy(episodes[0]); conflicting_episode['candidate_count'] = 2
        with self.assertRaisesRegex(ValueError, 'Conflicting duplicate'):
            simulate_delivery(rows, episodes + [conflicting_episode], SETTINGS)

    def test_tenant_isolation_and_order_invariance_preserve_stable_ids(self):
        rows = [candidate(), candidate('C2', tenant='T2')]
        episodes = episodes_for(rows)
        before = deepcopy((rows, episodes))
        original = self.run_case(rows, episodes, draws=[draw(), draw()])
        reversed_result = self.run_case(rows[::-1], episodes[::-1], draws=[draw(), draw()])
        self.assertEqual(original, reversed_result)
        self.assertEqual(original['summary']['delivered_attempts'], 2)
        self.assertEqual((rows, episodes), before)
        self.assertEqual(len({r['alert_id'] for r in original['alerts']}), 2)

    def test_truth_fields_are_rejected_before_values_are_serialized(self):
        rows = [candidate()]
        episodes = episodes_for(rows)
        rows[0]['actual_arrival_time'] = object()
        with self.assertRaisesRegex(ValueError, 'cannot accept target outcomes'):
            simulate_delivery(rows, episodes, SETTINGS)

    def test_missing_candidates_cross_tenant_binding_and_changed_protocol_fail(self):
        rows = [candidate()]
        episodes = episodes_for(rows)
        with self.assertRaisesRegex(ValueError, 'Complete candidates'):
            simulate_delivery([], episodes, SETTINGS)
        bad = deepcopy(episodes); bad[0]['tenant_id'] = 'OTHER'
        with self.assertRaisesRegex(ValueError, 'does not belong'):
            simulate_delivery(rows, bad, SETTINGS)
        changed = deepcopy(SETTINGS); changed['delivery']['ttl_seconds'] = -1
        with self.assertRaisesRegex(ValueError, 'Invalid bounded delivery setting'):
            simulate_delivery(rows, episodes, changed)

    def test_independently_frozen_stress_parameters_and_later_cutoff_are_supported(self):
        settings = deepcopy(SETTINGS)
        settings['delivery'].update(counts_as_of='2026-10-15T07:00:00Z', latency_seconds=[120, 120],
            receipt_lag_seconds=[30, 30], outcome_basis_points={
                'delivered': 0, 'transient_failure': 0, 'permanent_failure': 10000, 'unknown': 0})
        rows = [candidate()]
        result = simulate_delivery(rows, episodes_for(rows), settings)
        self.assertEqual(result['summary']['counts_as_of'], '2026-10-15T07:00:00Z')
        self.assertEqual(result['attempts'][0]['service_at'], stamp(120))
        self.assertEqual(result['attempts'][0]['receipt_known_at'], stamp(150))
        self.assertEqual(result['attempts'][0]['delivery_status'], 'failed')

    def test_invalid_stress_shapes_and_probability_mass_are_rejected(self):
        mutations = [('outcome_basis_points', {'delivered': 9000}), ('latency_seconds', [60, 5]),
                     ('retry_backoff_seconds', [30]), ('max_attempts', True)]
        rows = [candidate()]
        for name, value in mutations:
            with self.subTest(name=name):
                settings = deepcopy(SETTINGS); settings['delivery'][name] = value
                with self.assertRaises(ValueError):
                    simulate_delivery(rows, episodes_for(rows), settings)

    def test_transport_draw_is_stable_and_uses_attempt_identity(self):
        first = transport_outcome(candidate(), 1, SETTINGS)
        # Fixed independently calculated SHA256 vector: outcome bucket2142,
        # latency remainder11 and receipt-lag remainder13.
        self.assertEqual(first, {'outcome': 'delivered', 'latency_seconds': 16, 'receipt_lag_seconds': 13})
        self.assertEqual(first, transport_outcome(candidate(), 1, SETTINGS))
        self.assertEqual(set(first), {'outcome', 'latency_seconds', 'receipt_lag_seconds'})
        self.assertTrue(5 <= first['latency_seconds'] <= 60)
        self.assertTrue(0 <= first['receipt_lag_seconds'] <= 15)
        self.assertEqual(transport_outcome(candidate(), 2, SETTINGS),
                         {'outcome': 'transient_failure', 'latency_seconds': 54, 'receipt_lag_seconds': 12})
        with self.assertRaisesRegex(ValueError, 'retry bound'):
            transport_outcome(candidate(), 4, SETTINGS)


if __name__ == '__main__':
    unittest.main()
