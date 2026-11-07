"""Metadata-only whole-trip roles for the frozen July calibration protocol."""

from copy import deepcopy
from collections.abc import Mapping
from zoneinfo import ZoneInfo

from .common import parse_ts, utc


PROTOCOL_ID = 'm3d-july-calibration-v1'
BASE_COMMIT = '0a1255222dd82a14596d2a033b779d55b633542c'
CALENDAR = {
    'timezone': 'America/Los_Angeles',
    'train_departure_dates': ['2026-01-01', '2026-06-23'],
    'calibration_fit_departure_dates': ['2026-07-01', '2026-07-10'],
    'calibration_buffer_departure_dates': ['2026-07-11', '2026-07-14'],
    'assessment_departure_dates': ['2026-07-15', '2026-07-24'],
    'test_departure_dates': ['2026-08-01', '2026-09-30'],
}
CUTOFFS = {
    'training_cutoff_at': '2026-07-01T07:00:00Z',
    'calibration_fit_cutoff_at': '2026-07-15T07:00:00Z',
    'assessment_label_cutoff_at': '2026-08-01T07:00:00Z',
}
SOURCE_PATHS = {
    'data/raw/eta_trip_event.jsonl', 'data/raw/eta_route_version.jsonl',
    'data/raw/eta_promise_version.jsonl', 'data/raw/eta_trip_outcome.jsonl',
    'data/processed/eta_snapshot.jsonl', 'data/processed/eta_split_assignment.jsonl',
    'data/processed/trip_membership.jsonl', 'data/processed/feature_lineage.jsonl',
}
PARENT_PATHS = {'config/m2.json', 'config/m3b2.json', 'config/m3b3.json', 'config/m3c.json'}


def _stamp(value, name):
    try:
        return parse_ts(value)
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f'{name} requires a timezone-aware timestamp') from exc


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} requires a nonempty string')
    return value


def _key(row):
    if not isinstance(row, Mapping):
        raise ValueError('Trip records must be mappings')
    return tuple(_identifier(row.get(name), name) for name in ('tenant_id', 'trip_id'))


def _index(rows):
    result = {}
    for row in rows:
        key = _key(row)
        if key in result:
            raise ValueError(f'Duplicate tenant/trip metadata: {key}')
        result[key] = row
    return result


def _pins(values, paths, field):
    if not isinstance(values, Mapping) or set(values) != paths:
        raise ValueError(f'{field} must cover the exact frozen path set')
    for digest in values.values():
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError(f'{field} contains an invalid SHA256')


def validate_settings(settings, source_config):
    """Return a copy after checking the frozen design, without opening any data.

    Content digests are checked by the input loader. This function checks the
    declared split and method contract before labels or source values are used.
    """
    if not isinstance(settings, Mapping) or not isinstance(source_config, Mapping):
        raise ValueError('Complete protocol and source configuration are required')
    try:
        if settings['protocol_id'] != PROTOCOL_ID or settings['base_commit_sha'] != BASE_COMMIT:
            raise ValueError('Unexpected calibration protocol or public base')
        if settings['final_training_id'] != 'R01' or settings['calendar'] != CALENDAR:
            raise ValueError('Frozen July windows and final-training ID cannot change')
        for key, value in CUTOFFS.items():
            if _stamp(settings[key], key) != parse_ts(value):
                raise ValueError('Frozen calibration clock boundary cannot change')
        split = source_config['split']
        if (source_config['timezone'] != CALENDAR['timezone'] or split['group_by'] != ['tenant_id', 'trip_id']
                or split['train_departure_dates'] != CALENDAR['train_departure_dates']
                or split['calibration_departure_dates'] != ['2026-07-01', '2026-07-24']
                or split['test_departure_dates'] != CALENDAR['test_departure_dates']
                or parse_ts(split['train_label_cutoff_exclusive']) != parse_ts(CUTOFFS['training_cutoff_at'])
                or parse_ts(split['calibration_label_cutoff_exclusive']) != parse_ts(CUTOFFS['assessment_label_cutoff_at'])):
            raise ValueError('Parent M2 split or cutoff differs from the calibration protocol')
        held = source_config['heldout']
        if held != {'route_ids': ['ROUTE-12'], 'vehicle_ids': ['T01-V10', 'T02-V10', 'T03-V10']}:
            raise ValueError('Frozen heldout entity definitions cannot change')
        _pins(settings['expected_source_sha256'], SOURCE_PATHS, 'Source pins')
        _pins(settings['expected_parent_config_sha256'], PARENT_PATHS, 'Parent config pins')
        eta, probability = settings['eta'], settings['probability']
        if (eta['base_candidate_id'] != 'gradient_boosting_quantile' or eta['fallback_method_id'] != 'historical_baseline'
                or eta['calibration_method_id'] != 'trip_max_cqr' or eta['nominal_coverage'] != 0.8
                or type(eta['minimum_fit_trips']) is not int or eta['minimum_fit_trips'] != 10
                or eta['lower_bound_floor_seconds'] != 0.0):
            raise ValueError('Frozen ETA calibration design differs')
        if (probability['base_method_id'] != 'logistic_delay' or probability['fallback_method_id'] != 'fold_prevalence'
                or probability['calibration_method_id'] != 'nonnegative_sigmoid_logit'
                or probability['logit_clip_epsilon'] != 1e-6 or probability['penalty_multiplier'] != 0.5
                or probability['penalty_center'] != [1.0, 0.0]
                or type(probability['minimum_fit_trips']) is not int or probability['minimum_fit_trips'] != 10
                or type(probability['minimum_class_contributing_trips']) is not int or probability['minimum_class_contributing_trips'] != 2):
            raise ValueError('Frozen probability calibration design differs')
        _stamp(settings['recorded_at_utc'], 'Protocol record time')
    except (KeyError, TypeError) as exc:
        raise ValueError('Complete calibration protocol fields are required') from exc
    return deepcopy(dict(settings))


def build_departures(events):
    """Extract original departure metadata, ignoring non-departure payloads.

    This matches the source definition: the earliest actual departure and the
    earliest availability for that occurrence. Outcome tables are not inputs.
    """
    result, seen_ids = {}, set()
    for event in events:
        if not isinstance(event, Mapping):
            raise ValueError('Event records must be mappings')
        if event.get('event_type') not in ('departure', 'departed'):
            continue
        key = _key(event)
        identifier = key[0], _identifier(event.get('trip_event_id'), 'trip_event_id')
        if identifier in seen_ids:
            raise ValueError('Duplicate departure event identity')
        seen_ids.add(identifier)
        occurred = _stamp(event.get('occurred_at'), 'Departure occurrence')
        available = _stamp(event.get('available_at'), 'Departure availability')
        if available < occurred:
            raise ValueError('Departure cannot be known before it occurred')
        clocks = occurred, available
        if key not in result or clocks < result[key]:
            result[key] = clocks
    return [dict(tenant_id=key[0], trip_id=key[1], departure_time=utc(clocks[0]), departure_known_at=utc(clocks[1]))
            for key, clocks in sorted(result.items())]


def build_memberships(assignments, trip_memberships, departure_rows, source_config, settings):
    """Retain all original groups with one role and an explicit reason per trip.

    Departure rows are normalized records from build_departures(). Every
    original trip, including excluded and final-test trips, requires metadata.
    Metadata inventory is permitted; target/feature payloads of protected groups
    must be gated by these roles in the caller before use.
    """
    settings = validate_settings(settings, source_config)
    assignments, trips, departures = (_index(rows) for rows in (assignments, trip_memberships, departure_rows))
    if assignments.keys() != trips.keys() or assignments.keys() != departures.keys():
        raise ValueError('Assignments, membership and departure metadata must have identical trip keys')
    zone = ZoneInfo(settings['calendar']['timezone'])
    held = source_config['heldout']
    output = []
    for key in sorted(assignments):
        assignment, trip, departure = assignments[key], trips[key], departures[key]
        original = assignment.get('split_role')
        if original not in ('train', 'calibration', 'test', 'excluded') or trip.get('split_role') != original:
            raise ValueError('Original split roles disagree or are invalid')
        if assignment.get('split_group_id') != '/'.join(key):
            raise ValueError('Frozen split group ID disagrees with tenant/trip')
        at = _stamp(departure.get('departure_time'), 'Departure time')
        known = _stamp(departure.get('departure_known_at'), 'Departure known time')
        if known < at:
            raise ValueError('Departure cannot be known before it occurred')
        day = at.astimezone(zone).date().isoformat()
        vehicle = _identifier(trip.get('vehicle_id'), 'vehicle_id')
        route = _identifier(trip.get('route_id'), 'route_id')
        if type(trip.get('unseen_route')) is not bool or type(trip.get('unseen_vehicle')) is not bool:
            raise ValueError('Heldout metadata flags must be explicit booleans')
        unseen_route = trip['unseen_route'] or route in held['route_ids']
        unseen_vehicle = trip['unseen_vehicle'] or vehicle in held['vehicle_ids']
        if original in ('train', 'calibration') and (unseen_route or unseen_vehicle):
            raise ValueError('Frozen heldout entities cannot enter training or July')
        original_cutoff = _stamp(assignment.get('label_cutoff_at'), 'Original label cutoff')
        if original != 'excluded':
            begin, end = source_config['split'][{'train':'train_departure_dates', 'calibration':'calibration_departure_dates', 'test':'test_departure_dates'}[original]]
            if not begin <= day <= end:
                raise ValueError('Departure date lies outside its frozen original split')
            expected_cutoff = (source_config['split']['train_label_cutoff_exclusive'] if original == 'train'
                               else source_config['split']['calibration_label_cutoff_exclusive'] if original == 'calibration'
                               else source_config['extract_as_of'])
            if original_cutoff != parse_ts(expected_cutoff):
                raise ValueError('Original label cutoff differs from frozen M2 role')
        if original == 'train':
            role, cutoff, reason = 'train', settings['training_cutoff_at'], 'original_training'
        elif original == 'test':
            role, cutoff, reason = 'test', None, 'protected_final_test'
        elif original == 'excluded':
            role, cutoff = 'excluded', None
            reason = ('heldout_route_and_vehicle' if unseen_route and unseen_vehicle else 'heldout_route' if unseen_route
                      else 'heldout_vehicle' if unseen_vehicle else trip.get('exclusion_reason') or 'original_exclusion')
        elif CALENDAR['calibration_fit_departure_dates'][0] <= day <= CALENDAR['calibration_fit_departure_dates'][1]:
            role, cutoff, reason = 'calibration_fit', settings['calibration_fit_cutoff_at'], 'july_calibration_fit'
        elif CALENDAR['calibration_buffer_departure_dates'][0] <= day <= CALENDAR['calibration_buffer_departure_dates'][1]:
            role, cutoff, reason = 'calibration_buffer', None, 'july_maturity_buffer'
        elif CALENDAR['assessment_departure_dates'][0] <= day <= CALENDAR['assessment_departure_dates'][1]:
            role, cutoff, reason = 'assessment', settings['assessment_label_cutoff_at'], 'july_assessment_and_policy_development'
        else:
            raise ValueError('Unassigned original calibration date')
        if cutoff is not None and known >= parse_ts(cutoff):
            raise ValueError('Departure metadata was not known before the role cutoff')
        output.append(dict(
            tenant_id=key[0], trip_id=key[1], split_group_id=assignment['split_group_id'],
            original_split_role=original, original_label_cutoff_at=utc(original_cutoff),
            role=role, reason=reason, departure_time=utc(at), departure_known_at=utc(known), departure_date=day,
            vehicle_id=vehicle, route_id=route, unseen_route=unseen_route, unseen_vehicle=unseen_vehicle,
            training_cutoff_at=utc(settings['training_cutoff_at']), calibration_fit_cutoff_at=utc(settings['calibration_fit_cutoff_at']),
            label_cutoff_at=utc(cutoff) if cutoff is not None else None, policy_development=role == 'assessment',
        ))
    return output
