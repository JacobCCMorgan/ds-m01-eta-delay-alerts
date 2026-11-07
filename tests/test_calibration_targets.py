import copy
import unittest
from ds_m01.calibration_targets import build_eta_targets

OP={'tenant_id':'T','trip_id':'A','snapshot_id':'S','snapshot_time':'2026-07-10T23:59:00-07:00'}
OUT={'tenant_id':'T','trip_id':'A','outcome_version_id':'V1','label_available_time':'2026-07-12T00:00:00-07:00',
     'label_status':'verified','actual_arrival_time':'2026-07-11T00:01:00.250000-07:00',
     'arrival_time_precision':'second','arrival_evidence_ref':'gate','cancellation_time':None}
CUTOFF='2026-07-15T00:00:00-07:00'

class CalibrationTargetTests(unittest.TestCase):
    def build(self, outcomes, opp=None):
        return build_eta_targets([opp or OP],{('T','A')},outcomes,CUTOFF)[0]
    def test_crossmidnight_fractional_remaining(self):
        self.assertEqual(self.build([OUT])['target_seconds'],120.25)
    def test_minute_precision_duration_retained(self):
        self.assertEqual(self.build([{**OUT,'arrival_time_precision':'minute'}])['eligibility'],'eligible')
    def test_exact_cutoff_revision_is_not_used(self):
        later={**OUT,'outcome_version_id':'V2','label_available_time':CUTOFF,'label_status':'cancelled'}
        self.assertEqual(self.build([OUT,later])['outcome_version_id'],'V1')
    def test_known_withdrawal_does_not_revert(self):
        later={**OUT,'outcome_version_id':'V2','label_available_time':'2026-07-14T00:00:00-07:00','label_status':'pending'}
        r=self.build([OUT,later]); self.assertEqual(r['reason'],'latest_outcome_pending'); self.assertIsNone(r['target_seconds'])
    def test_gate_evidence_and_cancellation_required(self):
        for changes,reason in [({'arrival_evidence_ref':None},'missing_arrival_evidence'),({'cancellation_time':'2026-07-11T00:02:00-07:00'},'conflicting_cancellation_evidence'),({'arrival_time_precision':'unknown'},'insufficient_arrival_precision')]:
            with self.subTest(changes=changes): self.assertEqual(self.build([{**OUT,**changes}])['reason'],reason)
    def test_already_arrived_remains_explicit(self):
        self.assertEqual(self.build([OUT],{**OP,'snapshot_time':OUT['actual_arrival_time']})['reason'],'already_arrived_at_snapshot')
    def test_foreign_and_future_payloads_ignored(self):
        class Guard(dict):
            def get(self,k,*args):
                if k not in ('tenant_id','trip_id','label_available_time'): raise AssertionError('protected target opened')
                return super().get(k,*args)
        other=Guard(tenant_id='T',trip_id='FINAL')
        future=Guard(tenant_id='T',trip_id='A',label_available_time=CUTOFF)
        self.assertEqual(self.build([other,future,OUT])['target_seconds'],120.25)
    def test_equal_time_ambiguity_rejected(self):
        with self.assertRaises(ValueError):self.build([OUT,{**OUT,'outcome_version_id':'V2'}])
    def test_verification_clock_cannot_precede_arrival(self):
        with self.assertRaises(ValueError):self.build([{**OUT,'label_available_time':'2026-07-10T23:59:30-07:00'}])
    def test_duplicates_and_foreign_opportunities_rejected(self):
        with self.assertRaises(ValueError):build_eta_targets([OP,OP],{('T','A')},[OUT],CUTOFF)
        with self.assertRaises(ValueError):build_eta_targets([OP],set(),[OUT],CUTOFF)
