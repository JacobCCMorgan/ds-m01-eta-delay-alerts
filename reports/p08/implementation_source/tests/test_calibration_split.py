"""Hand-specified metadata boundaries; no July outcomes or models are opened."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from ds_m01.calibration_split import build_departures, build_memberships, validate_settings

ROOT = Path(__file__).resolve().parents[1]
SOURCE = json.loads((ROOT / 'config/m2.json').read_text())
SETTINGS = json.loads((ROOT / 'config/m3d.json').read_text())


def trip(identifier, at, original, **changes):
    tenant = changes.pop('tenant_id', 'T01')
    cutoff = {'train': '2026-07-01T07:00:00Z', 'calibration': '2026-08-01T07:00:00Z',
              'test': '2026-10-15T07:00:00Z', 'excluded': '2026-10-15T07:00:00Z'}[original]
    assignment = dict(tenant_id=tenant, trip_id=identifier, split_role=original,
                      split_group_id=tenant + '/' + identifier, label_cutoff_at=cutoff)
    membership = dict(tenant_id=tenant, trip_id=identifier, split_role=original,
                      vehicle_id=changes.pop('vehicle_id', 'T01-V01'), route_id=changes.pop('route_id', 'ROUTE-01'),
                      unseen_route=changes.pop('unseen_route', False), unseen_vehicle=changes.pop('unseen_vehicle', False),
                      exclusion_reason=changes.pop('exclusion_reason', None))
    departure = dict(tenant_id=tenant, trip_id=identifier, departure_time=at,
                     departure_known_at=changes.pop('departure_known_at', at))
    if changes:
        raise AssertionError(changes)
    return assignment, membership, departure


def memberships(rows, settings=SETTINGS, source=SOURCE):
    return build_memberships([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], source, settings)


class CalibrationSplitTests(unittest.TestCase):
    def test_every_original_group_has_a_role_and_cutoff_or_exclusion_reason(self):
        specs = [trip('train', '2026-06-23T20:00:00-07:00', 'train'),
                 trip('fit', '2026-07-01T00:00:00-07:00', 'calibration'),
                 trip('buffer', '2026-07-11T00:00:00-07:00', 'calibration'),
                 trip('assessment', '2026-07-15T00:00:00-07:00', 'calibration'),
                 trip('test', '2026-08-01T00:00:00-07:00', 'test'),
                 trip('excluded', '2026-07-25T00:00:00-07:00', 'excluded')]
        result = {r['trip_id']: r for r in memberships(specs)}
        self.assertEqual({k:r['role'] for k,r in result.items()}, {
            'train':'train','fit':'calibration_fit','buffer':'calibration_buffer',
            'assessment':'assessment','test':'test','excluded':'excluded'})
        self.assertEqual(result['train']['label_cutoff_at'], '2026-07-01T07:00:00Z')
        self.assertEqual(result['fit']['label_cutoff_at'], '2026-07-15T07:00:00Z')
        self.assertEqual(result['assessment']['label_cutoff_at'], '2026-08-01T07:00:00Z')
        for name in ('buffer','test','excluded'):
            self.assertIsNone(result[name]['label_cutoff_at'])
            self.assertTrue(result[name]['reason'])
        self.assertEqual([k for k,r in result.items() if r['policy_development']], ['assessment'])

    def test_local_calendar_boundaries_and_cross_midnight_trips(self):
        cases = [('2026-07-10T23:59:59.999999-07:00','calibration_fit'),
                 ('2026-07-11T07:00:00Z','calibration_buffer'),
                 ('2026-07-15T06:59:59.999999Z','calibration_buffer'),
                 ('2026-07-15T07:00:00Z','assessment'),
                 ('2026-07-24T23:59:59-07:00','assessment')]
        for i,(at,role) in enumerate(cases):
            with self.subTest(at=at):
                row, = memberships([trip(str(i),at,'calibration')])
                self.assertEqual(row['role'],role)
        source = trip('late', '2026-07-10T23:50:00-07:00', 'calibration')
        # Snapshot times are not inputs to membership. This payload deliberately
        # crosses midnight and cannot change the role of any part of the trip.
        source[2]['last_snapshot_time'] = '2026-07-11T08:10:00Z'
        row, = memberships([source])
        self.assertEqual((row['role'],row['label_cutoff_at']), ('calibration_fit','2026-07-15T07:00:00Z'))

    def test_parent_exclusion_is_never_promoted_even_on_a_calibration_date(self):
        row, = memberships([trip('x','2026-07-02T12:00:00Z','excluded',exclusion_reason='parent_exclusion')])
        self.assertEqual((row['role'],row['reason'],row['label_cutoff_at']),('excluded','parent_exclusion',None))

    def test_heldouts_cannot_enter_active_roles_even_if_flags_are_false(self):
        for original,at in [('train','2026-06-10T12:00:00Z'),('calibration','2026-07-02T12:00:00Z')]:
            for change in ({'route_id':'ROUTE-12'},{'vehicle_id':'T01-V10'},{'unseen_route':True},{'unseen_vehicle':True}):
                with self.subTest(original=original,change=change), self.assertRaisesRegex(ValueError,'heldout'):
                    memberships([trip('x',at,original,**change)])
        excluded,test = memberships([trip('a','2026-07-02T12:00:00Z','excluded',route_id='ROUTE-12'),
                                     trip('b','2026-08-02T12:00:00Z','test',route_id='ROUTE-12')])
        self.assertEqual((excluded['role'],test['role']),('excluded','test'))
        self.assertIsNone(test['label_cutoff_at'])

    def test_wrong_original_roles_dates_and_cutoffs_fail(self):
        for modify in ('role','date','cutoff','group'):
            row=trip('x','2026-07-02T12:00:00Z','calibration')
            if modify=='role':row[1]['split_role']='test'
            elif modify=='date':row[2].update(departure_time='2026-08-02T12:00:00Z',departure_known_at='2026-08-02T12:00:00Z')
            elif modify=='cutoff':row[0]['label_cutoff_at']='2026-10-15T07:00:00Z'
            else:row[0]['split_group_id']='other/x'
            with self.subTest(modify=modify), self.assertRaises(ValueError):memberships([row])

    def test_tenant_keys_and_all_metadata_sets_are_required(self):
        rows=[trip('same','2026-07-02T12:00:00Z','calibration'),
              trip('same','2026-07-02T12:00:00Z','calibration',tenant_id='T02')]
        self.assertEqual(len(memberships(rows)),2)
        with self.assertRaisesRegex(ValueError,'Duplicate'):memberships([rows[0],rows[0]])
        for index in range(3):
            args=[[r[i] for r in rows] for i in range(3)]
            args[index].pop()
            with self.subTest(index=index), self.assertRaisesRegex(ValueError,'identical trip keys'):
                build_memberships(*args,SOURCE,SETTINGS)

    def test_metadata_only_does_not_inspect_target_payloads(self):
        row=trip('x','2026-07-02T12:00:00Z','calibration')
        before=memberships([row])
        for part in row:part.update(actual_arrival_time=object(),delay_label=object(),remaining_duration_seconds=object())
        self.assertEqual(memberships([row]),before)

    def test_departure_clocks_are_aware_ordered_and_known_before_role_cutoff(self):
        for at,known in [('2026-07-02T12:00:00','2026-07-02T12:01:00Z'),
                         ('2026-07-02T12:00:00Z','2026-07-02T11:59:59Z'),
                         ('2026-07-02T12:00:00Z','2026-07-15T07:00:00Z')]:
            with self.subTest(at=at,known=known),self.assertRaises(ValueError):
                memberships([trip('x',at,'calibration',departure_known_at=known)])

    def test_build_departures_uses_earliest_occurrence_and_its_earliest_availability(self):
        first=dict(tenant_id='T01',trip_id='X',trip_event_id='E1',event_type='departure',
                   occurred_at='2026-07-01T10:00:00Z',available_at='2026-07-01T10:02:00Z')
        same=dict(first,trip_event_id='E2',available_at='2026-07-01T10:01:00Z')
        later=dict(first,trip_event_id='E3',occurred_at='2026-07-01T11:00:00Z',available_at='2026-07-01T11:00:00Z')
        row, = build_departures([later,first,same,{'event_type':'arrival','actual_arrival_time':object()}])
        self.assertEqual((row['departure_time'],row['departure_known_at']),('2026-07-01T10:00:00Z','2026-07-01T10:01:00Z'))
        with self.assertRaisesRegex(ValueError,'Duplicate'):build_departures([first,first])
        with self.assertRaisesRegex(ValueError,'before'):build_departures([dict(first,available_at='2026-07-01T09:00:00Z')])

    def test_changed_windows_cutoffs_holds_and_missing_hashes_are_rejected(self):
        for mode in ('window','fitcutoff','assessmentcutoff','heldout','pin','coverage','classsupport'):
            s,source=deepcopy(SETTINGS),deepcopy(SOURCE)
            if mode=='window':s['calendar']['calibration_fit_departure_dates'][1]='2026-07-14'
            elif mode=='fitcutoff':s['calibration_fit_cutoff_at']=s['assessment_label_cutoff_at']
            elif mode=='assessmentcutoff':s['assessment_label_cutoff_at']='2026-10-15T07:00:00Z'
            elif mode=='heldout':source['heldout']['route_ids']=[]
            elif mode=='pin':s['expected_source_sha256'].pop('data/raw/eta_trip_outcome.jsonl')
            elif mode=='coverage':s['eta']['nominal_coverage']=0.9
            else:s['probability']['minimum_class_contributing_trips']=1
            with self.subTest(mode=mode),self.assertRaises(ValueError):validate_settings(s,source)

    def test_inputs_remain_unchanged_and_rows_are_deterministic(self):
        rows=[trip('b','2026-07-03T12:00:00Z','calibration'),trip('a','2026-07-02T12:00:00Z','calibration')]
        before=deepcopy((rows,SETTINGS,SOURCE));result=memberships(rows)
        self.assertEqual([r['trip_id'] for r in result],['a','b'])
        self.assertEqual(result,memberships(list(reversed(rows))))
        self.assertEqual((rows,SETTINGS,SOURCE),before)
        checked=validate_settings(SETTINGS,SOURCE);checked['calendar']['timezone']='changed'
        self.assertEqual(SETTINGS['calendar']['timezone'],'America/Los_Angeles')
