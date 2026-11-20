"""Hand-specified cases for the DS-M01 validation inventory, on synthetic data."""
from datetime import datetime, timedelta
from pathlib import Path
import json
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ds_m01.calibration_metrics import summarize_intervals
from ds_m01.candidate_run import _prediction
from ds_m01.candidate_targets import select_fit_outcomes
from ds_m01.delivery_simulation import simulate_delivery
from ds_m01.features import build_feature_row
from ds_m01.pipeline import label_for_snapshot, snapshot_for_trip
from workflows.p09.metrics import score_workflow


ROOT = Path(__file__).resolve().parents[3]


def timeline():
    return json.loads((ROOT / 'tests/fixtures/point_in_time_case.json').read_text())


def interval(trip, snapshot, target):
    return dict(tenant_id='T', trip_id=trip, snapshot_id=snapshot, eligibility='eligible',
                status='available', target_seconds=target, lower_seconds=5, median_seconds=10,
                upper_seconds=15, prediction_source='fixture')


class ScenarioWitnessTests(unittest.TestCase):
    def test_v05_delayed_position_and_missing_position_fallback(self):
        case = timeline()
        tables = case['tables']
        earlier = dict(tables['telemetry_event'][0], event_time='2026-06-23T09:29:00Z',
                       available_time='2026-06-23T09:30:00Z')
        delayed = dict(earlier, canonical_event_id='LATE', source_event_id='RAW-LATE',
                       event_time='2026-06-23T09:59:00Z', available_time='2026-06-23T10:01:00Z',
                       latitude_deg=35.0)
        route = tables['eta_route_version'][0]
        history = [dict(tenant_id='T01', trip_id=f'H{i}', route_id=route['route_id'],
                        departure_time_bucket='another bucket', route_distance_km=120.0,
                        actual_arrival_time='2026-06-01T12:00:00Z', label_known_at='2026-06-01T13:00:00Z',
                        duration_seconds=duration) for i, duration in enumerate((4200, 4800, 5400, 6000, 6600))]
        for events, state, age in (([earlier, delayed], 'low_confidence', 1860),
                                   ([delayed], 'no_position', None)):
            with self.subTest(state=state):
                tables['telemetry_event'] = events
                snap = snapshot_for_trip('T01', 'TRIP-1', case['snapshot_time'], tables)
                self.assertEqual((snap['position_validity'], snap['position_age_seconds']), (state, age))
                self.assertEqual(snap['selected_position_event_id'], 'GPS-1' if age else None)
                self.assertEqual(snap['latitude_deg'], earlier['latitude_deg'] if age else None)
                lineage = dict(tenant_id='T01', snapshot_id=snap['snapshot_id'],
                               max_source_available_time=earlier['available_time'] if age else '2026-06-23T09:00:00Z')
                feature = build_feature_row(snap, route, lineage)
                self.assertIsNone(feature['numeric']['gnss_speed_kmh'])
                self.assertIsNone(feature['numeric']['remaining_distance_km'])
                # Position loss does not by itself disable a supported candidate.
                # Exercise the actual fallback path when the candidate is unsupported.
                result = _prediction(snap, {'status': 'unsupported', 'reason': 'insufficient_fit_trips'},
                                     feature, history, {'fold_id': 'F', 'training_cutoff_at': '2026-06-22T00:00:00Z'},
                                     'fixture', None)
                self.assertEqual(result['prediction_source'], 'baseline_fallback')
                self.assertEqual(result['fallback_pool'], 'distance_band')
                self.assertEqual([result[f'p{q}_remaining_seconds'] for q in (10, 50, 90)], [840, 1800, 2760])
                self.assertEqual(snapshot_for_trip('T01', 'TRIP-1', delayed['available_time'], tables)
                                 ['selected_position_event_id'], 'LATE')

    def test_v08_literal_undercoverage_and_width(self):
        # At one mature opportunity per trip, [5,15] covers the first three labels.
        labels = [5, 10, 15, 17, 20]
        result = summarize_intervals([interval(f'T{i}', f'S{i}', y) for i, y in enumerate(labels)])
        self.assertEqual(result['nominal_coverage'], .8)
        self.assertEqual(result['counts']['all_eligible_snapshots_covered_trips'], 3)
        self.assertEqual(result['counts']['eligible_trips'], 5)
        self.assertEqual(result['metrics']['trip_all_eligible_snapshots_coverage'], .6)
        self.assertEqual(result['metrics']['mean_width_seconds'], 10)

    def test_v09_trip_and_snapshot_weighting(self):
        # Two strata each contain both trips. Dense contributes 9 snapshots in
        # the first stratum, 1 in the second. Sparse contributes the reverse.
        for dense_count, sparse_count in ((9, 1), (1, 9)):
            dense = [interval('dense', f'D{i}', 10) for i in range(dense_count)]
            sparse = [interval('sparse', f'S{i}', 30) for i in range(sparse_count)]
            scores = summarize_intervals(dense + sparse)
            with self.subTest(dense_count=dense_count):
                self.assertEqual(scores['counts']['scored_trips'], 2)
                self.assertEqual(scores['counts']['scored_snapshots'], 10)
                self.assertEqual(scores['metrics']['trip_weighted_snapshot_coverage'], .5)
                self.assertEqual(scores['metrics']['median_mae_seconds'], 10)
                # Single-observation scores recover the snapshot denominator.
                single = [summarize_intervals([r])['metrics'] for r in dense + sparse]
                self.assertEqual(sum(s['trip_weighted_snapshot_coverage'] for s in single) / 10, dense_count / 10)
                self.assertEqual(sum(s['median_mae_seconds'] for s in single) / 10, 2 * sparse_count)

    def test_v10_same_episode_retry_and_partial_adoption(self):
        def stamp(seconds):
            return (datetime.fromisoformat('2026-07-16T10:00:00+00:00') +
                    timedelta(seconds=seconds)).isoformat().replace('+00:00', 'Z')

        candidates = [dict(tenant_id='T', trip_id='TRIP', episode_id='E', snapshot_id='S' + name,
                           candidate_id=name, snapshot_time=stamp(at), simulated_issue_at=stamp(at),
                           deadline_at=stamp(14400), lead_seconds=14400-at, promise_version_id='P',
                           policy_version='RULE', prediction_version='MODEL', candidate_status='admitted',
                           probability='0.600000', reason='threshold_support_lead_cooldown_capacity_passed')
                      for name, at in (('first', 0), ('reminder', 2200))]
        episode = dict(tenant_id='T', trip_id='TRIP', episode_id='E', policy_version='RULE',
                       episode_started_at=stamp(0), start_snapshot_id='Sfirst', start_promise_version_id='P',
                       episode_last_observed_at=stamp(2200), episode_closed_at=None, closure_reason=None,
                       episode_status='censored', censored_at=stamp(2200), candidate_count=2)
        transport = [dict(outcome=s, latency_seconds=5, receipt_lag_seconds=0)
                     for s in ('transient_failure', 'transient_failure', 'delivered', 'delivered')]
        settings = json.loads((ROOT / 'config/p07.json').read_text())
        with patch('ds_m01.delivery_simulation.transport_outcome', side_effect=transport):
            run = simulate_delivery(candidates, [episode], settings)
        self.assertEqual([r['delivery_status'] for r in run['attempts']], ['failed', 'failed', 'delivered', 'delivered'])
        self.assertEqual(len(run['episodes']), 1)
        self.assertEqual(run['episodes'][0]['delivered_alert_count'], 2)
        self.assertEqual(run['episodes'][0]['duplicate_alert_count'], 1)
        first, reminder = run['attempts'][2:]
        inputs = dict(opportunities=candidates, alerts=run['alerts'], attempts=run['attempts'])
        # Feed independently prescribed recipient records against the observed
        # delivery IDs. Only the first delivered alert is opened and acted on.
        simulation = dict(
            eta_adoption=[
                dict(tenant_id='T', adoption_id='A1', delivery_id=first['delivery_id'],
                     adoption_status='partially_accepted', decided_at=stamp(200)),
                dict(tenant_id='T', adoption_id='A2', delivery_id=reminder['delivery_id'],
                     adoption_status='not_reviewed', decided_at=None)],
            opening_events=[dict(tenant_id='T', alert_id=first['alert_id'], delivery_id=first['delivery_id'],
                                 opened_at=stamp(190), known_at=stamp(191))],
            eta_action=[dict(tenant_id='T', action_id='X', adoption_id='A1', alert_id=first['alert_id'],
                             action_type='contact_driver', action_time=stamp(210), handling_seconds=30)],
            eta_action_followup=[], followup_audit=[dict(tenant_id='T', action_id='X', action_type='contact_driver',
                                                        followup_status='no_known_outcome', canonical_row_emitted=False)])
        score = score_workflow(inputs, simulation, {'observation_cutoff_at': stamp(3600)})
        self.assertEqual(score['population']['transport_attempts'], 4)
        self.assertEqual(score['population']['confirmed_delivered_alerts'], 2)
        self.assertEqual(score['funnel']['opened_alerts'], 1)
        self.assertEqual(score['funnel']['partially_accepted_alerts'], 1)
        self.assertEqual(score['funnel']['accepted_alerts'], 0)
        self.assertEqual(score['actions']['executed_actions'], 1)
        self.assertFalse(score['real_notifications_sent'])

    def test_v12_repeated_hour_and_late_correction(self):
        case = timeline()
        tables = case['tables']
        zone = ZoneInfo('America/Los_Angeles')
        first = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0).isoformat()
        second = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1).isoformat()
        self.assertTrue(first.endswith('-07:00'))
        self.assertTrue(second.endswith('-08:00'))
        departure = '2026-11-01T01:00:00-07:00'
        tables['eta_trip_event'][0].update(occurred_at=departure, available_at=departure)
        tables['eta_trip_plan_version'][0]['planned_departure_at'] = departure
        tables['eta_promise_version'][0].update(window_start_at='2026-11-01T01:35:00-08:00',
                                               window_end_at='2026-11-01T02:00:00-08:00')
        tables['telemetry_event'] = []
        original = dict(case['outcomes'][0], actual_arrival_time='2026-11-01T01:45:00-08:00',
                        label_available_time='2026-11-01T02:05:00-08:00',
                        service_start_time=None, delivery_completed_time=None, followup_through=None)
        cutoff = '2026-11-01T02:30:00-08:00'
        correction = dict(original, outcome_version_id='CORRECTED', supersedes_outcome_version_id='OUTCOME-1',
                          revision_reason='independent gate correction', actual_arrival_time='2026-11-01T02:15:00-08:00',
                          label_available_time=cutoff)
        outcomes = [correction, original]
        snapshots = [snapshot_for_trip('T01', 'TRIP-1', at, tables) for at in (first, second)]
        self.assertNotEqual(snapshots[0]['snapshot_id'], snapshots[1]['snapshot_id'])
        self.assertEqual([s['elapsed_trip_seconds'] for s in snapshots], [1800, 5400])
        for at, expected_version, remaining, delayed in (
            (cutoff, 'OUTCOME-1', [4500, 900], False),
            ('2026-11-01T02:30:01-08:00', 'CORRECTED', [6300, 2700], True),
        ):
            labels = [label_for_snapshot(s, outcomes, tables['eta_promise_version'], at) for s in snapshots]
            self.assertEqual([r['outcome_version_id'] for r in labels], [expected_version] * 2)
            self.assertEqual([r['remaining_duration_seconds'] for r in labels], remaining)
            self.assertEqual([r['delay_outcome'] for r in labels], [delayed] * 2)
            selected = select_fit_outcomes(outcomes, {('T01', 'TRIP-1')}, at)
            self.assertEqual(selected['T01', 'TRIP-1']['outcome_version_id'], expected_version)


if __name__ == '__main__':
    unittest.main()
