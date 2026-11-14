import copy
import unittest

from ds_m01.alert_inputs import bind_opportunities, probability_projection, eta_projection


def fixture():
    snapshot = {'tenant_id':'T1','trip_id':'trip','snapshot_id':'s1','snapshot_time':'2026-07-16T20:00:00Z',
                'vehicle_id':'v1','trip_status_as_of':'in_transit','position_validity':'valid',
                'promise_version_id':'p1','provenance_id':'source-s1'}
    promise = {'tenant_id':'T1','trip_id':'trip','promise_version_id':'p1','known_at':'2026-07-15T00:00:00Z',
               'window_end_at':'2026-07-16T21:00:00Z','provenance_id':'source-p1'}
    member = {'tenant_id':'T1','trip_id':'trip','role':'assessment','unseen_route':False,'unseen_vehicle':False,
              'route_id':'r1','departure_time':'2026-07-16T19:00:00Z'}
    shared = {'tenant_id':'T1','trip_id':'trip','snapshot_id':'s1','snapshot_time':snapshot['snapshot_time'],
              'variant':'calibrated','route_id':'r1','calibration_role':'assessment',
              'departure_time':member['departure_time'],'position_validity':'valid','elapsed_trip_seconds':3600,
              'status':'available','prediction_source':'native','calibration_status':'fitted','calibration_reason':None,
              'reason':'verified_gate_arrival'}
    eta = {**shared,'lower_seconds':1000.1,'median_seconds':2000.5,'upper_seconds':4000.1}
    probability = {**shared,'promise_version_id':'p1','deadline_at':promise['window_end_at'],
                   'promise_known_at':promise['known_at'],'promise_provenance_id':'source-p1','probability':0.5000005,
                   'raw_probability':0.6,'eligibility':'eligible','delay_label':True,
                   'actual_arrival_time':'2026-07-16T21:30:00Z','label_known_at':'2026-07-18T00:00:00Z',
                   'label_cutoff_at':'2026-08-01T07:00:00Z','label_status':'verified','arrival_time_precision':'second'}
    settings = {'population':{'expected_trips':1,'expected_opportunities':1,'label_cutoff_exclusive':'2026-08-01T07:00:00Z'}}
    return [eta],[probability],[snapshot],[promise],[member],[{'tenant_id':'T1','prediction_version':'v1'}],settings


class AlertInputTests(unittest.TestCase):
    def test_half_even_probability_is_the_declared_policy_number(self):
        self.assertEqual(probability_projection(0.4999995),'0.500000')
        self.assertEqual(probability_projection(0.5000005),'0.500000')
        self.assertEqual(probability_projection(0.5000015),'0.500002')
        self.assertEqual(probability_projection(0),'0.000000')
        self.assertEqual(probability_projection(1),'1.000000')

    def test_probability_rejects_nonfinite_boolean_and_out_of_range(self):
        for value in (float('nan'),float('inf'),-0.0000001,1.0000001,True,'0.5'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                probability_projection(value)

    def test_eta_projection_is_outward_and_contains_rounded_median(self):
        self.assertEqual(eta_projection(0.59,0.6,0.61),(0,1,1))
        self.assertEqual(eta_projection(1.49,1.5,1.51),(1,2,2))
        self.assertEqual(eta_projection(2.49,2.5,2.51),(2,2,3))
        with self.assertRaises(ValueError): eta_projection(3,2,4)

    def test_outcome_values_and_target_reason_never_enter_policy_inputs(self):
        args=fixture();before,targets=bind_opportunities(*args)
        args[0][0]['reason']='different_target_reason'
        args[1][0].update(delay_label=False,reason='different_target_reason',actual_arrival_time='2026-07-16T20:30:00Z')
        after,new_targets=bind_opportunities(*args)
        self.assertEqual(before,after)
        self.assertNotEqual(targets,new_targets)
        self.assertNotIn('actual_arrival_time',before[0])
        self.assertNotIn('eligibility',before[0])

    def test_label_unknown_does_not_remove_opportunity_or_change_forecast(self):
        args=fixture();before,_=bind_opportunities(*args)
        args[1][0].update(eligibility='not_mature',delay_label=None,actual_arrival_time=None,label_known_at=None)
        after,targets=bind_opportunities(*args)
        self.assertEqual(before,after)
        self.assertEqual(targets[0]['eligibility'],'not_mature')

    def test_unavailable_is_audited_without_zero_or_fake_eta(self):
        args=fixture();args[0][0]['status']='unavailable';args[1][0]['status']='unavailable'
        rows,_=bind_opportunities(*args)
        self.assertEqual(len(rows),1)
        for key in ('median_seconds','median_seconds_int','lower_seconds','upper_seconds','probability','calibrated_probability'):
            self.assertIsNone(rows[0][key])

    def test_missing_and_consistently_dropped_opportunities_fail(self):
        args=list(fixture());args[0]=[]
        with self.assertRaisesRegex(ValueError,'inventory'): bind_opportunities(*args)
        args=list(fixture());args[0]=[];args[1]=[];args[2]=[]
        with self.assertRaisesRegex(ValueError,'inventory'): bind_opportunities(*args)

    def test_cross_trip_and_duplicate_snapshot_bindings_fail(self):
        args=fixture();args[1][0]['trip_id']='wrong'
        with self.assertRaisesRegex(ValueError,'identity'):bind_opportunities(*args)
        args=fixture();args[2].append(copy.deepcopy(args[2][0]))
        with self.assertRaisesRegex(ValueError,'Duplicate'):bind_opportunities(*args)

    def test_promise_known_after_snapshot_and_stale_version_fail(self):
        args=fixture();args[3][0]['known_at']='2026-07-17T00:00:00Z'
        with self.assertRaisesRegex(ValueError,'Promise unavailable'):bind_opportunities(*args)
        args=fixture();later=copy.deepcopy(args[3][0]);later.update(promise_version_id='p2',known_at='2026-07-16T19:59:59Z')
        args[3].append(later)
        with self.assertRaisesRegex(ValueError,'superseded'):bind_opportunities(*args)

    def test_future_promise_version_does_not_change_decisions(self):
        args=fixture();before,_=bind_opportunities(*args)
        later=copy.deepcopy(args[3][0]);later.update(promise_version_id='p2',known_at='2026-07-17T00:00:00Z',window_end_at='2026-08-01T00:00:00Z')
        args[3].append(later);after,_=bind_opportunities(*args)
        self.assertEqual(before,after)

    def test_label_cutoff_exclusive_and_unknown_is_not_on_time(self):
        args=fixture();args[1][0]['label_known_at']='2026-08-01T07:00:00Z'
        with self.assertRaisesRegex(ValueError,'cutoff'):bind_opportunities(*args)
        args=fixture();args[1][0].update(eligibility='not_mature',delay_label=False)
        with self.assertRaisesRegex(ValueError,'Unscored'):bind_opportunities(*args)

    def test_heldout_group_and_changed_promise_identity_fail(self):
        args=fixture();args[4][0]['unseen_vehicle']=True
        with self.assertRaisesRegex(ValueError,'Heldout'):bind_opportunities(*args)
        args=fixture();args[1][0]['promise_version_id']='other'
        with self.assertRaisesRegex(ValueError,'promise binding'):bind_opportunities(*args)

    def test_simultaneously_known_conflicting_promises_are_rejected(self):
        args=fixture();other=copy.deepcopy(args[3][0]);other.update(promise_version_id='p2',window_end_at='2026-07-16T23:00:00Z')
        args[3].append(other)
        with self.assertRaisesRegex(ValueError,'Ambiguous'):bind_opportunities(*args)

    def test_snapshot_at_cutoff_and_outside_departure_calendar_are_rejected(self):
        args=fixture();args[2][0]['snapshot_time']='2026-08-01T07:00:00Z'
        with self.assertRaisesRegex(ValueError,'cutoff window'):bind_opportunities(*args)
        args=fixture();args[4][0]['departure_time']='2026-07-14T19:00:00Z'
        with self.assertRaisesRegex(ValueError,'departure calendar'):bind_opportunities(*args)

    def test_membership_trip_without_opportunities_is_rejected(self):
        args=fixture();other=copy.deepcopy(args[4][0]);other['trip_id']='missing'
        args[4].append(other);args[6]['population']['expected_trips']=2
        with self.assertRaisesRegex(ValueError,'trip inventory'):bind_opportunities(*args)
