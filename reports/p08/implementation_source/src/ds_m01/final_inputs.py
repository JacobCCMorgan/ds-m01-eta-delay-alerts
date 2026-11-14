"""Metadata-only final population and point-in-time features; never open outcomes."""
from collections import Counter, defaultdict
from pathlib import Path
import json

from .calibration_split import build_departures, build_memberships
from .candidate_model import canonical_digest
from .common import digest_file, parse_ts, read_jsonl, utc
from .delay_targets import prepare_promise_features
from .delivery_inputs import verify_evaluation_readiness
from .feature_run import FEATURE_INPUT_FILES, build_snapshot_feature_row

PROTOCOL_SHA256 = 'b9d07fee882785b2de3325ec78048c481e6dc6ee0553bc1830786e597187d194'
SOURCE_FILES = (*FEATURE_INPUT_FILES, 'data/raw/eta_promise_version.jsonl')
SNAPSHOT_FIELDS = ('tenant_id','trip_id','snapshot_id','snapshot_time','vehicle_id',
    'trip_status_as_of','position_validity','promise_version_id','route_version_id','provenance_id')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def trip(row):
    return row['tenant_id'], row['trip_id']


def key(row):
    return row['tenant_id'], row['snapshot_id']


def index(rows, fields):
    result = {}
    for row in rows:
        identity = tuple(row.get(field) for field in fields)
        require(all(isinstance(v,str) and v.strip() for v in identity), 'Invalid final identity')
        require(identity not in result, 'Duplicate final identity: '+str(identity))
        result[identity] = row
    return result


def verify_settings(root):
    root = Path(root)
    path = root/'config/p08_evaluation.json'
    require(digest_file(path)==PROTOCOL_SHA256, 'Frozen final protocol changed')
    settings = json.loads(path.read_text())
    for name, expected in settings['artifact_sha256'].items():
        require(digest_file(root/name)==expected, 'Frozen final artifact changed: '+name)
    for name, expected in settings['existing_core_sha256'].items():
        require(digest_file(root/'src/ds_m01'/name)==expected, 'Frozen final core changed: '+name)
        require(digest_file(Path(__file__).parent/name)==expected, 'Executed final core differs: '+name)
    verify_evaluation_readiness(root)
    return settings


def verify_label_free_files(input_root, settings):
    """The explicit seven-file allowlist intentionally excludes even outcome hashing."""
    expected = {name:settings['input_sha256'][name] for name in SOURCE_FILES}
    actual = {name:digest_file(Path(input_root)/name) for name in SOURCE_FILES}
    require(actual==expected, 'Frozen label-free final source changed')
    return actual


def build_population(assignments, original_memberships, events, source_config, calibration_settings, settings):
    """Validate the original complete metadata before selecting whole test trips."""
    full = build_memberships(assignments,original_memberships,build_departures(events),source_config,calibration_settings)
    assignment_index = index(assignments,('tenant_id','trip_id'))
    original_index = index(original_memberships,('tenant_id','trip_id'))
    held = source_config['heldout']
    population = settings['population']
    require(population['heldout_routes']==held['route_ids'] and population['heldout_vehicles']==held['vehicle_ids'],
            'Final holdouts differ from original source')
    require(population['departure_dates_inclusive']==source_config['split']['test_departure_dates']
            and population['timezone']==source_config['timezone'], 'Final test calendar differs from original split')
    require(parse_ts(population['label_cutoff_exclusive'])==parse_ts(source_config['extract_as_of']), 'Final cutoff differs')
    training_routes = {(r['tenant_id'],r['route_id']) for r in full if r['role']=='train'}
    training_vehicles = {(r['tenant_id'],r['vehicle_id']) for r in full if r['role']=='train'}
    result = []
    for row in full:
        original = original_index[trip(row)]
        unseen_route = row['route_id'] in held['route_ids']
        unseen_vehicle = row['vehicle_id'] in held['vehicle_ids']
        require(original['unseen_route'] is unseen_route and original['unseen_vehicle'] is unseen_vehicle,
                'Original heldout flags differ from frozen entity IDs')
        if row['original_split_role']!='test':
            continue
        require(((row['tenant_id'],row['route_id']) not in training_routes)==unseen_route,
                'Route novelty differs from training metadata')
        require(((row['tenant_id'],row['vehicle_id']) not in training_vehicles)==unseen_vehicle,
                'Vehicle novelty differs from training metadata')
        group = ('unseen_vehicle' if unseen_vehicle else 'seen_vehicle')+'_'+('unseen_route' if unseen_route else 'seen_route')
        require(group in population['primary_novelty_groups'], 'Unknown final novelty group')
        result.append(dict(row, role='test', reason='original_final_test', policy_development=False,
                           label_cutoff_at=utc(population['label_cutoff_exclusive']), novelty_group=group,
                           departure_month=row['departure_date'][:7],
                           split_version=assignment_index[trip(row)]['split_version']))
    require(len({row['split_version'] for row in result})<=1, 'Final split versions disagree')
    return result


def prepare_records(memberships, snapshots, routes, lineage, events, promises, source_config, settings):
    """Pure label-free source binding, also used by hand fixtures."""
    members = index(memberships,('tenant_id','trip_id'))
    promises = [{field:r.get(field) for field in ('tenant_id','trip_id','promise_version_id','known_at',
                 'window_start_at','window_end_at','provenance_id')} for r in promises if trip(r) in members]
    selected = sorted((r for r in snapshots if trip(r) in members),key=lambda r:(*trip(r),parse_ts(r['snapshot_time']),r['snapshot_id']))
    snapshot_index = index(selected,('tenant_id','snapshot_id'))
    require({trip(r) for r in selected}==set(members), 'Every final trip must retain all frozen opportunities')
    routes = index(routes,('tenant_id','route_version_id'))
    lineage = index((r for r in lineage if key(r) in snapshot_index),('tenant_id','snapshot_id'))
    require(lineage.keys()==snapshot_index.keys(), 'Final lineage inventory is incomplete')
    departures = defaultdict(list)
    for event in events:
        if event.get('event_type') in ('departure','departed') and trip(event) in members:
            departures[trip(event)].append(event)
    bound = prepare_promise_features(selected,set(members),promises)
    promise_index = index(bound,('tenant_id','snapshot_id'))
    features, context = [], []
    cutoff = parse_ts(settings['population']['label_cutoff_exclusive'])
    for snapshot in selected:
        member = members[trip(snapshot)]
        route = routes.get((snapshot['tenant_id'],snapshot['route_version_id']))
        require(route is not None and route['route_id']==member['route_id'], 'Final route version differs from trip membership')
        require(snapshot['vehicle_id']==member['vehicle_id'], 'Final vehicle differs from trip membership')
        at,departure = parse_ts(snapshot['snapshot_time']),parse_ts(member['departure_time'])
        require(departure<=at<cutoff, 'Final snapshot lies outside departure/cutoff')
        event = next((r for r in departures[trip(snapshot)] if parse_ts(r['occurred_at'])==departure
                      and parse_ts(r['available_at'])<=at),None)
        require(event is not None, 'Final snapshot lacks known departure evidence')
        feature = build_snapshot_feature_row(snapshot,route,lineage[key(snapshot)],departure_event=event,
                                              position_stale_seconds=source_config['position_stale_seconds'])
        features.append(feature)
        promise = promise_index[key(snapshot)]
        record = {field:snapshot[field] for field in SNAPSHOT_FIELDS}
        record.update(snapshot_time=utc(at),route_id=member['route_id'],departure_time=member['departure_time'],
                      departure_month=member['departure_month'],novelty_group=member['novelty_group'],
                      unseen_route=member['unseen_route'],unseen_vehicle=member['unseen_vehicle'],
                      elapsed_trip_seconds=feature['numeric']['elapsed_trip_seconds'],
                      deadline_remaining_seconds=promise['promise_seconds_remaining'],
                      departure_time_bucket=feature['categorical']['departure_time_bucket'],
                      promise_known_at=promise['promise_known_at'],deadline_at=promise['deadline_at'],
                      promise_provenance_id=promise['promise_provenance_id'])
        context.append(record)
    groups = settings['population']['primary_novelty_groups']
    inventory = {'trips':len(members),'opportunities':len(context),
                 'trip_keys':[list(k) for k in sorted(members)],
                 'snapshot_keys':[list(k) for k in sorted(snapshot_index)],
                 'novelty_trips':{group:sum(r['novelty_group']==group for r in memberships) for group in groups},
                 'novelty_opportunities':{group:sum(r['novelty_group']==group for r in context) for group in groups},
                 'tenant_trips':dict(sorted(Counter(r['tenant_id'] for r in memberships).items())),
                 'membership_sha256':canonical_digest(memberships),'snapshot_context_sha256':canonical_digest(context),
                 'feature_sha256':canonical_digest(features),'promise_features_sha256':canonical_digest(bound),
                 'outcomes_opened':False,'target_based_pruning':False}
    return dict(memberships=memberships,snapshots=context,features=features,promise_features=bound,
                promises=promises,inventory=inventory)


def prepare_inputs(root,input_root,settings=None):
    root,input_root = Path(root),Path(input_root)
    frozen = verify_settings(root)
    require(settings is None or settings==frozen, 'Caller final settings differ from frozen protocol')
    settings = frozen
    hashes = verify_label_free_files(input_root,settings)
    source = json.loads((root/'config/m2.json').read_text())
    calibration = json.loads((root/'config/m3d.json').read_text())
    rows = {name:read_jsonl(input_root/name) for name in SOURCE_FILES}
    memberships = build_population(rows['data/processed/eta_split_assignment.jsonl'],rows['data/processed/trip_membership.jsonl'],
                                   rows['data/raw/eta_trip_event.jsonl'],source,calibration,settings)
    result = prepare_records(memberships,rows['data/processed/eta_snapshot.jsonl'],rows['data/raw/eta_route_version.jsonl'],
                             rows['data/processed/feature_lineage.jsonl'],rows['data/raw/eta_trip_event.jsonl'],
                             rows['data/raw/eta_promise_version.jsonl'],source,settings)
    require(verify_label_free_files(input_root,settings)==hashes,'Final inputs changed during preparation')
    result['source_sha256'] = hashes
    return result
