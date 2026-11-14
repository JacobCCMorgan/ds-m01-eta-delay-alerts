"""Label-free adapter for the frozen final policy and transport scenarios."""
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import math
from zoneinfo import ZoneInfo

from .alert_policy import OPPORTUNITY_FIELDS, simulate_policy
from .common import parse_ts
from .delivery_simulation import simulate_delivery
from .release_store import _check_opportunity


PROTOCOL_FILE_SHA256 = 'b9d07fee882785b2de3325ec78048c481e6dc6ee0553bc1830786e597187d194'
EXTRA_FIELDS = frozenset(('novelty_group', 'departure_month', 'deadline_remaining_seconds',
                          'departure_time_bucket', 'scenario_id'))
SCENARIOS = ('S00_unmodified', 'S01_missing_position', 'S02_stale_position',
    'S03_unseen_route_slot', 'S04_natural_entity_holdouts', 'S05_stationary_window',
    'S06_distance_scale', 'S07_transport_degraded', 'S08_missing_and_degraded')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def validate_settings(settings):
    """Require the pre-outcome scientific specification, without reading data."""
    # This canonical-content pin is independent of whitespace in the retained file.
    if not isinstance(settings, Mapping) or digest(settings) != SETTINGS_DIGEST:
        raise ValueError('Frozen final-evaluation settings changed')
    return settings


def novelty_group(row):
    return ('unseen_vehicle' if row['unseen_vehicle'] else 'seen_vehicle') + '_' + (
        'unseen_route' if row['unseen_route'] else 'seen_route')


def policy_opportunities(rows):
    """Project only approved metadata, rejecting every undeclared target field."""
    result, unique = [], {}
    for original in rows:
        if not isinstance(original, Mapping) or not OPPORTUNITY_FIELDS <= set(original) or set(original) - OPPORTUNITY_FIELDS - EXTRA_FIELDS:
            raise ValueError('Final opportunity schema is incomplete or contains target/undeclared fields')
        json.dumps(original, allow_nan=False)
        row = {name: original[name] for name in OPPORTUNITY_FIELDS}
        _check_opportunity(row)
        if 'novelty_group' in original and original['novelty_group'] != novelty_group(row):
            raise ValueError('Novelty metadata differs from frozen entity flags')
        if 'departure_month' in original and original['departure_month'] != parse_ts(row['departure_time']).astimezone(ZoneInfo('America/Los_Angeles')).strftime('%Y-%m'):
            raise ValueError('Departure month differs from original local departure')
        if 'deadline_remaining_seconds' in original:
            value = original['deadline_remaining_seconds']
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                    or value != (parse_ts(row['deadline_at']) - parse_ts(row['snapshot_time'])).total_seconds()):
                raise ValueError('Prospective deadline advance differs from its original clocks')
        if original.get('departure_time_bucket') is not None and not isinstance(original['departure_time_bucket'], str):
            raise ValueError('Departure bucket must be text or null')
        if 'scenario_id' in original and original['scenario_id'] not in SCENARIOS:
            raise ValueError('Unknown final scenario identity')
        key = row['tenant_id'], row['snapshot_id']
        if key in unique:
            if original != unique[key]:
                raise ValueError('Conflicting duplicate final opportunity')
            continue
        unique[key] = deepcopy(dict(original))
        result.append(row)
    return sorted(result, key=lambda r: (parse_ts(r['snapshot_time']), r['tenant_id'], r['trip_id'], r['snapshot_id']))


def delivery_settings(settings, scenario_id):
    validate_settings(settings)
    if scenario_id not in SCENARIOS:
        raise ValueError('Scenario is outside the nine frozen cases')
    delivery = deepcopy(settings['delivery'])
    if scenario_id in ('S07_transport_degraded', 'S08_missing_and_degraded'):
        stress = next(s for s in settings['stress']['scenarios'] if s['id'] == 'S07_transport_degraded')
        delivery.update(deepcopy(stress['delivery_overrides']))
    # Never put a scenario identifier in the seed or candidate/model identity.
    return {'delivery': delivery}


def first_ids(policy, delivery):
    """Select global first events solely from the frozen event ledger."""
    first_candidates, first_successes = {}, {}
    for row in sorted(policy['candidates'], key=lambda r: (parse_ts(r['simulated_issue_at']), r['tenant_id'], r['trip_id'], r['snapshot_id'])):
        first_candidates.setdefault((row['tenant_id'], row['trip_id']), row['candidate_id'])
    cutoff = parse_ts(delivery['summary']['counts_as_of'])
    successes = [r for r in delivery['attempts'] if r['delivery_status'] == 'delivered'
        and r['delivered_at'] is not None and r['receipt_known_at'] is not None and parse_ts(r['receipt_known_at']) <= cutoff]
    for row in sorted(successes, key=lambda r: (parse_ts(r['delivered_at']), r['tenant_id'], r['trip_id'], r['alert_id'], r['delivery_id'])):
        first_successes.setdefault((row['tenant_id'], row['trip_id']), row['delivery_id'])
    return ([{'tenant_id': k[0], 'trip_id': k[1], 'candidate_id': value} for k, value in sorted(first_candidates.items())],
            [{'tenant_id': k[0], 'trip_id': k[1], 'delivery_id': value} for k, value in sorted(first_successes.items())])


def run_policy_delivery(opportunities, settings, scenario_id='S00_unmodified'):
    validate_settings(settings)
    source = list(opportunities)
    rows = policy_opportunities(source)
    zone = ZoneInfo(settings['population']['timezone'])
    begin, end = settings['population']['departure_dates_inclusive']
    cutoff = parse_ts(settings['population']['label_cutoff_exclusive'])
    for row in rows:
        if not begin <= parse_ts(row['departure_time']).astimezone(zone).date().isoformat() <= end:
            raise ValueError('Final opportunity lies outside original final departure dates')
        if not parse_ts(row['departure_time']) <= parse_ts(row['snapshot_time']) < cutoff:
            raise ValueError('Final snapshot crosses departure or exclusive final cutoff')
        if (row['unseen_route'] != (row['route_id'] in settings['population']['heldout_routes'])
                or row['unseen_vehicle'] != (row['vehicle_id'] in settings['population']['heldout_vehicles'])):
            raise ValueError('Original novelty identity differs from frozen heldout entities')
    if any(r.get('scenario_id', scenario_id) != scenario_id for r in source):
        raise ValueError('Opportunity scenario metadata differs from requested scenario')
    rule = deepcopy(settings['pipeline']['selected_rule'])
    policy = simulate_policy(rows, rule)
    transport = simulate_delivery(policy['candidates'], policy['episodes'], delivery_settings(settings, scenario_id))
    candidates, successes = first_ids(policy, transport)
    return dict(scenario_id=scenario_id, rule=rule, policy=policy, delivery=transport,
        first_candidate_ids=candidates, first_successful_delivery_ids=successes,
        labels_used=False, threshold_reselected=False, transport_redrawn=False)


# Canonical digest of the P07-frozen P08 specification (not a final-data digest).
SETTINGS_DIGEST = 'e98a45ab3d543595acb2e43fd42b8f7207ce0b2807c22ef668f12a61b36b9fdc'
