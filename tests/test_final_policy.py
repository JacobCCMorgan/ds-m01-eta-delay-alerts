"""Synthetic final-period contracts; no retained final outcome payloads."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest

from ds_m01.alert_policy import simulate_policy
from ds_m01.delivery_simulation import simulate_delivery, transport_outcome
from ds_m01.final_policy import SCENARIOS, delivery_settings, policy_opportunities, run_policy_delivery


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = json.loads((ROOT / 'config/p08_evaluation.json').read_text())
START = datetime(2026, 8, 16, 16, tzinfo=timezone.utc)


def stamp(seconds=0):
    return (START + timedelta(seconds=seconds)).isoformat().replace('+00:00', 'Z')


def opportunity(sid='S1', seconds=0, *, trip='TRIP1', tenant='T01', deadline=7200, promise='P1', probability=.7):
    return dict(tenant_id=tenant, trip_id=trip, snapshot_id=sid, snapshot_time=stamp(seconds),
        route_id='ROUTE-01', vehicle_id=tenant + '-V01', departure_time=stamp(-3600),
        trip_status_as_of='in_transit', promise_version_id=promise, deadline_at=stamp(deadline), promise_known_at=stamp(-7200),
        snapshot_provenance_id='SP-' + tenant + '-' + sid, promise_provenance_id='PP-' + tenant + '-' + trip + '-' + promise,
        prediction_version='MODEL-' + tenant, position_validity='valid', elapsed_trip_seconds=3600 + seconds,
        unseen_route=False, unseen_vehicle=False, eta_status='available', eta_reason='available', eta_source='candidate',
        eta_calibration_status='fitted', lower_seconds=3000.25, median_seconds=4000.5, upper_seconds=5000.75,
        lower_seconds_int=3000, median_seconds_int=4000, upper_seconds_int=5001,
        probability_status='available', probability_reason='available', probability_source='candidate',
        probability_calibration_status='fitted', raw_probability=.65, calibrated_probability=probability,
        probability=f'{probability:.6f}', fallback_reason='none', novelty_group='seen_vehicle_seen_route',
        departure_month='2026-08', deadline_remaining_seconds=deadline - seconds,
        departure_time_bucket='6-06@America/Los_Angeles')


def registry(tenants=('T01',)):
    return [dict(tenant_id=tenant, prediction_version='MODEL-' + tenant, training_cutoff_at='2026-07-01T07:00:00Z',
        calibration_cutoff_at='2026-07-15T07:00:00Z', feature_definition_version='fixture-features',
        nominal_interval_coverage='0.800000', split_version='original-test-split', interval_calibration_version='INTERVAL-' + tenant,
        probability_calibration_version='PROBABILITY-' + tenant, baseline_definition='Frozen original training fixture',
        source_bindings={'tenant_id': tenant, 'model_sha256': 'a' * 64}) for tenant in tenants]


class FinalPolicyTests(unittest.TestCase):
    def test_adapter_matches_unchanged_policy_and_transport_engines(self):
        rows = [opportunity(), opportunity('S2', 1800)]
        result = run_policy_delivery(rows, SETTINGS)
        projected = policy_opportunities(rows)
        policy = simulate_policy(projected, SETTINGS['pipeline']['selected_rule'])
        self.assertEqual(result['policy'], policy)
        self.assertEqual(result['delivery'], simulate_delivery(policy['candidates'], policy['episodes'], {'delivery': SETTINGS['delivery']}))
        self.assertFalse(result['labels_used'])
        self.assertFalse(result['threshold_reselected'])

    def test_all_nine_scenarios_preserve_model_and_candidate_identity(self):
        outputs = {}
        for scenario in SCENARIOS:
            row = opportunity(); row['scenario_id'] = scenario
            outputs[scenario] = run_policy_delivery([row], SETTINGS, scenario)
        identity = outputs['S00_unmodified']['policy']['candidates'][0]['candidate_id']
        for result in outputs.values():
            self.assertEqual(result['policy']['candidates'][0]['candidate_id'], identity)
            self.assertEqual(result['policy']['candidates'][0]['prediction_version'], 'MODEL-T01')
        self.assertEqual(outputs['S00_unmodified']['delivery'], outputs['S04_natural_entity_holdouts']['delivery'])
        self.assertEqual(outputs['S07_transport_degraded']['delivery'], outputs['S08_missing_and_degraded']['delivery'])

    def test_degraded_transport_keeps_seed_dimension_and_attempt_identities(self):
        row = opportunity()
        candidate = run_policy_delivery([row], SETTINGS)['policy']['candidates'][0]
        normal, degraded = delivery_settings(SETTINGS, 'S00_unmodified'), delivery_settings(SETTINGS, 'S07_transport_degraded')
        for name in ('seed', 'version', 'max_attempts', 'retry_backoff_seconds', 'ttl_seconds', 'unknown_timeout_seconds'):
            self.assertEqual(normal['delivery'][name], degraded['delivery'][name])
        self.assertEqual(degraded['delivery']['latency_seconds'], [60, 180])
        self.assertEqual(transport_outcome(candidate, 1, degraded), transport_outcome(deepcopy(candidate), 1, degraded))

    def test_unknown_or_target_fields_fail_before_projecting_values(self):
        row = opportunity(); row['actual_arrival_time'] = object()
        with self.assertRaisesRegex(ValueError, 'target/undeclared'):
            run_policy_delivery([row], SETTINGS)
        row = opportunity(); row['unapproved_metadata'] = 'value'
        with self.assertRaisesRegex(ValueError, 'target/undeclared'):
            policy_opportunities([row])

    def test_natural_holdouts_are_permitted_and_original_novelty_is_checked(self):
        row = opportunity()
        row.update(route_id='ROUTE-12', vehicle_id='T01-V10', unseen_route=True, unseen_vehicle=True,
                   novelty_group='unseen_vehicle_unseen_route')
        self.assertEqual(run_policy_delivery([row], SETTINGS)['policy']['summary']['opportunities'], 1)
        row['unseen_route'] = False
        row['novelty_group'] = 'unseen_vehicle_seen_route'
        with self.assertRaisesRegex(ValueError, 'heldout entities'):
            run_policy_delivery([row], SETTINGS)

    def test_calendar_cutoff_and_fixed_settings_reject_drift(self):
        row = opportunity(); row.update(departure_time='2026-07-16T15:00:00Z', departure_month='2026-07')
        with self.assertRaisesRegex(ValueError, 'departure dates'):
            run_policy_delivery([row], SETTINGS)
        changed = deepcopy(SETTINGS); changed['pipeline']['selected_rule']['threshold'] = '0.500000'
        with self.assertRaisesRegex(ValueError, 'settings changed'):
            run_policy_delivery([opportunity()], changed)

    def test_missing_forecast_remains_in_opportunity_decisions_without_candidate(self):
        row = opportunity()
        row.update(eta_status='unavailable', eta_reason='unsupported', lower_seconds=None, median_seconds=None,
                   upper_seconds=None, lower_seconds_int=None, median_seconds_int=None, upper_seconds_int=None)
        output = run_policy_delivery([row], SETTINGS)
        self.assertEqual(output['policy']['summary']['opportunities'], 1)
        self.assertEqual(output['policy']['decisions'][0]['candidate_status'], 'unavailable')
        self.assertEqual(output['policy']['candidates'], [])
        self.assertEqual(output['delivery']['alerts'], [])

    def test_metadata_projection_duplicates_and_clock_values_are_checked(self):
        row = opportunity()
        self.assertEqual(len(policy_opportunities([row, deepcopy(row)])), 1)
        broken = deepcopy(row); broken['deadline_remaining_seconds'] = 0
        with self.assertRaisesRegex(ValueError, 'deadline advance'):
            policy_opportunities([broken])
        broken = deepcopy(row); broken['probability'] = '0.699999'
        with self.assertRaisesRegex(ValueError, 'projection'):
            policy_opportunities([broken])


if __name__ == '__main__':
    unittest.main()
