"""Predeclared sensitivity windows and missingness, using only toy features."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import unittest

from ds_m01.final_stress import apply_stress,SCENARIOS,UNKNOWN_ROUTE

ROOT=Path(__file__).resolve().parents[1]
SETTINGS=json.loads((ROOT/'config/p08_evaluation.json').read_text())
TRANSFORMERS=[{'categorical':{'route_id':{'vocabulary':['R1']}}}]


def feature(i=0,elapsed=3600,quality='valid',remaining=60,trip='TRIP'):
    at=datetime(2026,8,2,12,tzinfo=timezone.utc)+timedelta(seconds=elapsed)
    return dict(tenant_id='T01',trip_id=trip,snapshot_id='S'+str(i),snapshot_time=at.isoformat(),
                route_version_id='RV1',feature_definition_version='m3b1-features-v1',
                numeric=dict(elapsed_trip_seconds=float(elapsed),route_distance_km=100.0,
                    remaining_distance_km=remaining if quality=='valid' else None,
                    gnss_speed_kmh=40.0 if quality=='valid' else None,
                    position_age_seconds=0.0 if quality=='valid' else 1200.0 if quality=='low_confidence' else None),
                categorical=dict(route_id='R1',departure_time_bucket='day',position_validity=quality))


class FinalStressTests(unittest.TestCase):
    def apply(self,rows,scenario):
        return apply_stress(rows,scenario,SETTINGS,TRANSFORMERS)

    def test_all_nine_cases_keep_complete_inventory_and_input_bytes(self):
        rows=[feature(0),feature(1,elapsed=5400,quality='no_position')]
        before=deepcopy(rows)
        for case in SCENARIOS:
            output=self.apply(rows,case)
            self.assertEqual([r['snapshot_id'] for r in output['features']],['S0','S1'])
            self.assertEqual(output['summary']['opportunities'],2)
            self.assertEqual(len(output['audit']),2)
            self.assertEqual([r['route_version_id'] for r in output['features']],['RV1','RV1'])
            self.assertFalse(output['summary']['outcomes_used'])
        self.assertEqual(rows,before)

    def test_missing_position_masks_all_optional_position_inputs(self):
        result=self.apply([feature()],'S01_missing_position')
        row=result['features'][0]
        self.assertEqual(row['categorical']['position_validity'],'no_position')
        self.assertTrue(all(row['numeric'][f] is None for f in ('remaining_distance_km','gnss_speed_kmh','position_age_seconds')))
        self.assertEqual(row['numeric']['route_distance_km'],100)
        self.assertEqual(result['audit'][0]['before']['numeric']['gnss_speed_kmh'],40)

    def test_stale_position_exact_1800_boundary_and_original_missingness(self):
        rows=[feature(0,1799.999999),feature(1,1800),feature(2,3600,'no_position')]
        result=self.apply(rows,'S02_stale_position')
        self.assertEqual(result['summary']['not_applicable'],2)
        self.assertEqual(result['features'][0]['categorical']['position_validity'],'valid')
        self.assertEqual(result['features'][1]['numeric']['position_age_seconds'],1800)
        self.assertIsNone(result['features'][1]['numeric']['remaining_distance_km'])

    def test_unknown_route_requires_token_absent_from_every_saved_vocabulary(self):
        result=self.apply([feature()],'S03_unseen_route_slot')
        self.assertEqual(result['features'][0]['categorical']['route_id'],UNKNOWN_ROUTE)
        contaminated=deepcopy(TRANSFORMERS);contaminated[0]['categorical']['route_id']['vocabulary'].append(UNKNOWN_ROUTE)
        with self.assertRaisesRegex(ValueError,'already occurs'):
            apply_stress([feature()],'S03_unseen_route_slot',SETTINGS,contaminated)

    def test_stationary_window_is_earliest_three_consecutive_original_opportunities(self):
        rows=[feature(i,elapsed=1800+i*1800,remaining=70-i*5) for i in range(6)]
        result=self.apply(list(reversed(rows)),'S05_stationary_window')
        affected=[r for r in result['audit'] if r['affected']]
        self.assertEqual([r['snapshot_id'] for r in affected],['S1','S2','S3'])
        self.assertEqual({r['after']['numeric']['remaining_distance_km'] for r in affected},{65})
        self.assertTrue(all(r['after']['numeric']['gnss_speed_kmh']==0 for r in affected))
        self.assertEqual(result['summary']['not_applicable'],3)

    def test_stationary_window_cannot_bridge_invalid_middle_or_short_span(self):
        rows=[feature(0,3600),feature(1,5400,'no_position'),feature(2,7200),feature(3,9000)]
        self.assertEqual(self.apply(rows,'S05_stationary_window')['summary']['affected'],0)
        rows=[feature(0,3600),feature(1,4000),feature(2,5000)]
        self.assertEqual(self.apply(rows,'S05_stationary_window')['summary']['affected'],0)
        rows=[feature(0,3600),feature(1,5400),feature(2,7200)]
        rows[1]['numeric']['route_distance_km']=50
        with self.assertRaisesRegex(ValueError,'exceeds route bound'):
            self.apply(rows,'S05_stationary_window')

    def test_decimal_distance_scaling_preserves_nulls_and_bounds(self):
        rows=[feature(0,remaining=.1),feature(1,quality='no_position')]
        rows[0]['numeric']['route_distance_km']=.3
        result=self.apply(rows,'S06_distance_scale')['features']
        self.assertEqual(result[0]['numeric']['route_distance_km'],.375)
        self.assertEqual(result[0]['numeric']['remaining_distance_km'],.125)
        self.assertIsNone(result[1]['numeric']['remaining_distance_km'])

    def test_transport_degradation_preserves_exogenous_draw_keys_and_fixed_rules(self):
        standard=self.apply([feature()],'S00_unmodified')
        degraded=self.apply([feature()],'S07_transport_degraded')
        changed={k for k in standard['delivery'] if standard['delivery'][k]!=degraded['delivery'][k]}
        self.assertEqual(changed,{'outcome_basis_points','latency_seconds','receipt_lag_seconds'})
        self.assertEqual(degraded['delivery']['outcome_basis_points']['delivered'],6000)
        self.assertEqual(standard['features'],degraded['features'])
        combined=self.apply([feature()],'S08_missing_and_degraded')
        self.assertEqual(combined['features'],self.apply([feature()],'S01_missing_position')['features'])
        self.assertEqual(combined['delivery'],degraded['delivery'])

    def test_extra_target_fields_and_duplicate_opportunities_fail(self):
        row=feature();row['delay_label']=True
        with self.assertRaisesRegex(ValueError,'label-free'):
            self.apply([row],'S00_unmodified')
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            self.apply([feature(),feature()],'S00_unmodified')


if __name__=='__main__':unittest.main()
