"""Final metadata selection never opens or hashes an outcome source."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ds_m01.common import digest_file,parse_ts,utc
from ds_m01.final_inputs import build_population,prepare_records,verify_label_free_files,SOURCE_FILES
from test_calibration_split import trip,SOURCE,SETTINGS as CALIBRATION

ROOT=Path(__file__).resolve().parents[1]
SETTINGS=json.loads((ROOT/'config/p08_evaluation.json').read_text())


def population_fixture():
    specs=[trip('TRAIN','2026-06-01T16:00:00Z','train')]
    for i,(route,vehicle) in enumerate(((False,False),(False,True),(True,False),(True,True))):
        specs.append(trip('FINAL'+str(i),'2026-09-30T23:30:00-07:00','test',
                          route_id='ROUTE-12' if route else 'ROUTE-01',vehicle_id='T01-V10' if vehicle else 'T01-V01',
                          unseen_route=route,unseen_vehicle=vehicle))
    assignments=[dict(s[0],split_version='m1-split-v1') for s in specs]
    memberships=[s[1] for s in specs]
    events=[dict(tenant_id=s[2]['tenant_id'],trip_id=s[2]['trip_id'],event_type='departure',
                 trip_event_id='D'+str(i),occurred_at=s[2]['departure_time'],available_at=s[2]['departure_known_at'])
            for i,s in enumerate(specs)]
    return assignments,memberships,events


def records_fixture():
    assignments,original,events=population_fixture()
    members=build_population(assignments,original,events,SOURCE,CALIBRATION,SETTINGS)
    snapshots,routes,lineage,promises=[],[],[],[]
    for member in members:
        sid='S'+member['trip_id'];at=utc(parse_ts(member['departure_time'])+timedelta(hours=1))
        route_version=member['route_id']+'-V1'
        snapshots.append(dict(tenant_id='T01',trip_id=member['trip_id'],snapshot_id=sid,snapshot_time=at,
            vehicle_id=member['vehicle_id'],trip_status_as_of='in_transit',position_validity='no_position',
            promise_version_id='P'+sid,route_version_id=route_version,provenance_id='PR'+sid,
            feature_definition_version='m1-features-v1',elapsed_trip_seconds=3600,
            remaining_distance_km=None,gnss_speed_kmh=None,position_age_seconds=None,selected_position_event_id=None,
            last_position_time=None,position_available_at=None,departure_time_bucket='night'))
        lineage.append(dict(tenant_id='T01',snapshot_id=sid,max_source_available_time=member['departure_time']))
        promises.append(dict(tenant_id='T01',trip_id=member['trip_id'],promise_version_id='P'+sid,
                             known_at=member['departure_time'],window_start_at=at,
                             window_end_at=utc(parse_ts(at)+timedelta(hours=2)),provenance_id='PP'+sid))
    for route in ('ROUTE-01','ROUTE-12'):
        routes.append(dict(tenant_id='T01',route_id=route,route_version_id=route+'-V1',planned_distance_km=100,
                           known_at='2026-01-01T00:00:00Z',valid_from='2026-01-01T00:00:00Z',valid_to=None))
    return members,snapshots,routes,lineage,events,promises


class FinalInputTests(unittest.TestCase):
    def test_every_original_holdout_group_and_cross_midnight_snapshot_remain(self):
        result=prepare_records(*records_fixture(),SOURCE,SETTINGS)
        self.assertEqual(result['inventory']['trips'],4)
        self.assertEqual(result['inventory']['opportunities'],4)
        self.assertEqual(set(result['inventory']['novelty_trips'].values()),{1})
        self.assertTrue(all(r['snapshot_time'].startswith('2026-10-01') for r in result['snapshots']))
        self.assertTrue(all(r['departure_month']=='2026-09' for r in result['snapshots']))
        self.assertFalse(result['inventory']['outcomes_opened'])

    def test_metadata_flags_and_training_presence_define_novelty(self):
        args=list(population_fixture())
        args[1][-1]['unseen_route']=False
        with self.assertRaisesRegex(ValueError,'heldout flags'):
            build_population(*args,SOURCE,CALIBRATION,SETTINGS)
        args=list(population_fixture());args[1][1]['route_id']='NO-TRAINING-ROUTE'
        with self.assertRaisesRegex(ValueError,'Route novelty'):
            build_population(*args,SOURCE,CALIBRATION,SETTINGS)

    def test_missing_snapshot_lineage_wrong_vehicle_and_future_clock_fail(self):
        for mutation in ('snapshot','lineage','vehicle','clock','promise'):
            args=list(records_fixture())
            if mutation=='snapshot':args[1].pop()
            elif mutation=='lineage':args[3].pop()
            elif mutation=='vehicle':args[1][0]['vehicle_id']='FOREIGN'
            elif mutation=='clock':args[1][0]['snapshot_time']='2026-10-15T07:00:00Z'
            else:args[5][0]['known_at']='2026-10-15T07:00:00Z'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                prepare_records(*args,SOURCE,SETTINGS)

    def test_future_promise_cannot_rewrite_snapshot_deadline(self):
        args=list(records_fixture())
        expected=prepare_records(*args,SOURCE,SETTINGS)
        args[5].append(dict(args[5][0],promise_version_id='FUTURE',known_at='2026-10-02T00:00:00Z',
                            window_end_at='2026-10-03T00:00:00Z'))
        actual=prepare_records(*args,SOURCE,SETTINGS)
        self.assertEqual(actual['snapshots'],expected['snapshots'])
        self.assertEqual(actual['promise_features'],expected['promise_features'])

    def test_source_extra_truth_never_enters_features_context_or_inventory(self):
        args=list(records_fixture());before=deepcopy(args)
        expected=prepare_records(*args,SOURCE,SETTINGS)
        self.assertEqual(args,before)
        for row in args[1]:row.update(actual_arrival_time=object(),delay_label=object())
        for row in args[4]:row['hidden_truth']=object()
        actual=prepare_records(*args,SOURCE,SETTINGS)
        self.assertEqual(actual,expected)

    def test_only_seven_exact_label_free_paths_are_hashed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);pins={}
            for name in SOURCE_FILES:
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('{}\n');pins[name]=digest_file(p)
            pins['data/raw/eta_trip_outcome.jsonl']='do-not-open'
            with patch('ds_m01.final_inputs.digest_file',wraps=digest_file) as spy:
                result=verify_label_free_files(root,{'input_sha256':pins})
            self.assertEqual(set(result),set(SOURCE_FILES))
            self.assertEqual({str(c.args[0].relative_to(root)) for c in spy.call_args_list},set(SOURCE_FILES))
            (root/SOURCE_FILES[0]).write_text('changed')
            with self.assertRaisesRegex(ValueError,'source changed'):
                verify_label_free_files(root,{'input_sha256':pins})


if __name__=='__main__':unittest.main()
