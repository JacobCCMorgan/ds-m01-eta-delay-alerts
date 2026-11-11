"""Outcome-free, chronological research notification-candidate policy.

Candidates are not deliveries. State uses only each opportunity's known status,
canonical probability, promise and forecast support. Capacity and cooldown are
consumed only by admitted candidates, never by hidden labels or failed gates.
"""
from collections import Counter
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import json
from zoneinfo import ZoneInfo

from .common import parse_ts, utc

PROTOCOL_VERSION = 'p06-alert-policy-v1'
PROTOCOL_SHA256 = '7b7dbe70ec1d44199bd971607eeec9a029fdee5567a3c4fa1f42f7fac9ee0470'
DEFINITION_VERSION = 'p06-chronological-candidate-policy-v1'
TIMEZONE = 'America/Los_Angeles'
SIX_PLACES = Decimal('0.000001')
THRESHOLDS = tuple(Decimal(i) / 20 for i in range(21))
TRIP_STATES = frozenset(('planned', 'in_transit', 'arrived', 'cancelled', 'incomplete', 'lost'))
TERMINAL_STATES = TRIP_STATES - {'planned', 'in_transit'}
RULE_VALUES = {
    'minimum_action_lead_seconds': 1800,
    'repeat_suppression_seconds': 1800,
    'daily_candidate_cap_per_tenant': 2,
    'timezone': TIMEZONE,
}
OPPORTUNITY_FIELDS = frozenset('''tenant_id trip_id snapshot_id snapshot_time route_id vehicle_id departure_time
trip_status_as_of promise_version_id deadline_at promise_known_at snapshot_provenance_id promise_provenance_id
prediction_version position_validity elapsed_trip_seconds unseen_route unseen_vehicle
eta_status eta_reason eta_source eta_calibration_status lower_seconds median_seconds upper_seconds
lower_seconds_int median_seconds_int upper_seconds_int probability_status probability_reason probability_source
probability_calibration_status raw_probability calibrated_probability probability fallback_reason'''.split())
IDENTITY_FIELDS = ('tenant_id', 'trip_id', 'snapshot_id', 'snapshot_time',
                   'promise_version_id', 'deadline_at', 'prediction_version')


def _digest(value):
    try:
        data = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError('Policy records must have finite JSON values') from exc
    return hashlib.sha256(data.encode()).hexdigest()


def _identifier(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field} must be a nonempty string')
    return value


def _time(value, field):
    try:
        return parse_ts(value)
    except (TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise ValueError(f'{field} must be a timezone-aware timestamp') from exc


def _decimal(value, field):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f'{field} must be a finite decimal probability')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f'{field} must be a finite decimal probability') from exc
    if not result.is_finite() or not 0 <= result <= 1:
        raise ValueError(f'{field} must be between zero and one')
    return result


def _rule_version(values):
    return 'P06-RULE-' + _digest({'definition_version': DEFINITION_VERSION,
                                'protocol_sha256': PROTOCOL_SHA256, **values})[:24]


def _validate_rule(rule):
    required = set(RULE_VALUES) | {'policy_version', 'enabled', 'threshold'}
    if not isinstance(rule, Mapping) or set(rule) != required:
        raise ValueError('Rule must contain the complete frozen policy fields')
    values = {name: rule[name] for name in rule if name != 'policy_version'}
    if type(values['enabled']) is not bool:
        raise ValueError('Rule enabled must be Boolean')
    for key, value in RULE_VALUES.items():
        if type(values[key]) is not type(value) or values[key] != value:
            raise ValueError(f'Frozen policy parameter changed: {key}')
    if values['enabled']:
        threshold = _decimal(values['threshold'], 'threshold')
        if (threshold not in THRESHOLDS or not isinstance(values['threshold'], str)
                or values['threshold'] != format(threshold, '.6f')):
            raise ValueError('Enabled rule requires a canonical frozen-grid threshold')
    elif values['threshold'] is not None:
        raise ValueError('Disabled rule must have a null threshold')
    if rule['policy_version'] != _rule_version(values):
        raise ValueError('Policy version does not bind the complete rule')
    return dict(rule)


def make_rule(settings, threshold, enabled=True):
    """Create one frozen-grid rule, or explicit disabled policy, without labels.

    The version binds the frozen protocol, implementation definition and all
    effective rule parameters. The enclosing runner verifies config-file bytes.
    """
    if not isinstance(settings, Mapping) or type(enabled) is not bool:
        raise ValueError('Complete settings and a Boolean enabled state are required')
    try:
        policy, prediction = settings['policy'], settings['prediction']
        if (settings['protocol_version'] != PROTOCOL_VERSION or settings['display_timezone'] != TIMEZONE
                or [_decimal(v, 'threshold grid') for v in policy['threshold_grid']] != list(THRESHOLDS)
                or policy['comparison'] != 'greater_than_or_equal'
                or policy['ordering'] != ['snapshot_time', 'tenant_id', 'trip_id', 'snapshot_id']
                or policy['active_trip_states'] != ['in_transit']
                or set(policy['terminal_trip_states']) != TERMINAL_STATES
                or prediction['variant'] != 'calibrated'
                or prediction['required_probability_calibration_status'] != 'fitted'
                or prediction['require_eta_available'] is not True
                or prediction['probability_decimal_places'] != 6
                or prediction['probability_rounding'] != 'ROUND_HALF_EVEN'):
            raise ValueError('Settings differ from the frozen policy definition')
        for key in RULE_VALUES:
            if key != 'timezone' and (type(policy[key]) is not int or policy[key] != RULE_VALUES[key]):
                raise ValueError(f'Frozen policy parameter changed: {key}')
    except (KeyError, TypeError) as exc:
        raise ValueError('Complete frozen policy configuration is required') from exc
    if enabled:
        value = _decimal(threshold, 'threshold')
        if value not in THRESHOLDS:
            raise ValueError('Threshold is outside the predeclared grid')
        text = format(value, '.6f')
    else:
        if threshold is not None:
            raise ValueError('Disabled rule requires threshold=None')
        text = None
    values = dict(enabled=enabled, threshold=text, **RULE_VALUES)
    return _validate_rule(dict(policy_version=_rule_version(values), **values))


def _opportunities(rows):
    retained, times, fingerprints = {}, {}, {}
    count = 0
    for source in rows:
        count += 1
        if not isinstance(source, Mapping):
            raise ValueError('Opportunities must be mappings')
        if set(source) - OPPORTUNITY_FIELDS:
            raise ValueError('Opportunity contains undeclared fields; targets cannot enter policy')
        # Check the field allowlist before reading any potential target payload.
        row = dict(source)
        for name in ('tenant_id', 'trip_id', 'snapshot_id', 'promise_version_id', 'prediction_version'):
            _identifier(row.get(name), name)
        key = row['tenant_id'], row['snapshot_id']
        fingerprint = _digest(row)
        if key in fingerprints:
            if fingerprints[key] != fingerprint:
                raise ValueError('Conflicting duplicate tenant/snapshot opportunity')
            continue
        fingerprints[key] = fingerprint
        at = _time(row.get('snapshot_time'), 'snapshot_time')
        departure = _time(row.get('departure_time'), 'departure_time')
        known = _time(row.get('promise_known_at'), 'promise_known_at')
        deadline = _time(row.get('deadline_at'), 'deadline_at')
        if departure > at or known > at:
            raise ValueError('Departure and promise must be known by the snapshot clock')
        if row.get('trip_status_as_of') not in TRIP_STATES:
            raise ValueError('Unknown point-in-time trip state')
        if row.get('eta_status') not in ('available', 'unavailable'):
            raise ValueError('Unknown ETA availability')
        if row.get('probability_status') not in ('available', 'unavailable'):
            raise ValueError('Unknown probability availability')
        calibration = row.get('probability_calibration_status')
        if calibration not in ('fitted', 'unsupported', 'unavailable', 'not_applied'):
            raise ValueError('Unknown probability calibration status')
        probability = row.get('probability')
        precise = row.get('calibrated_probability')
        if row['probability_status'] == 'unavailable':
            if probability is not None:
                raise ValueError('Unavailable probability must be null')
        else:
            p = _decimal(probability, 'probability')
            original = _decimal(precise, 'calibrated_probability')
            projected = original.quantize(SIX_PLACES, rounding=ROUND_HALF_EVEN)
            if not isinstance(probability, str) or probability != format(p, '.6f') or p != projected:
                raise ValueError('Policy probability must be the six-decimal half-even source projection')
        row.update(snapshot_time=utc(at), departure_time=utc(departure),
                   promise_known_at=utc(known), deadline_at=utc(deadline))
        retained[key], times[key] = row, at
    ordered = sorted(retained, key=lambda key: (times[key], retained[key]['tenant_id'],
                                               retained[key]['trip_id'], retained[key]['snapshot_id']))
    return [retained[key] for key in ordered], count


def simulate_policy(opportunities, rule):
    """Return decisions, admitted candidates and censored/closed risk episodes.

    Each unique input has a decision, including missing support, inactive trips,
    insufficient lead and suppression. End of stream censors open episodes;
    no future arrival or retrospective eligibility can close or filter them.
    """
    rule = _validate_rule(rule)
    rows, input_count = _opportunities(opportunities)
    threshold = Decimal(rule['threshold']) if rule['enabled'] else None
    zone = ZoneInfo(rule['timezone'])
    decisions, candidates, episodes = [], [], []
    active, last_candidate, daily, last_seen = {}, {}, Counter(), {}

    def close(trip, at, reason):
        episode = active.pop(trip, None)
        if episode is not None:
            episode.update(episode_closed_at=utc(at), episode_last_observed_at=utc(at),
                           closure_reason=reason, episode_status='closed')
        return episode

    for row in rows:
        trip = row['tenant_id'], row['trip_id']
        at, deadline = parse_ts(row['snapshot_time']), parse_ts(row['deadline_at'])
        last_seen[trip] = at
        lead = (deadline - at).total_seconds()
        identity = {name: row[name] for name in IDENTITY_FIELDS}
        decision = dict(**identity, policy_version=rule['policy_version'], candidate_status=None,
                        candidate_id=None, episode_id=active[trip]['episode_id'] if trip in active else None,
                        lead_seconds=lead, probability=row.get('probability'), reason=None)
        if trip in active:
            active[trip]['episode_last_observed_at'] = utc(at)
        state = row['trip_status_as_of']
        if not rule['enabled']:
            status, reason = 'disabled', 'policy_disabled'
        elif state in TERMINAL_STATES:
            closed = close(trip, at, 'known_' + state)
            if closed:
                decision['episode_id'] = closed['episode_id']
            status, reason = 'terminal', 'known_' + state
        elif state != 'in_transit':
            status, reason = 'inactive', 'trip_not_in_transit'
        elif row['probability_status'] != 'available':
            status, reason = 'unavailable', 'probability_unavailable'
        elif row['probability_calibration_status'] != 'fitted':
            status, reason = 'unavailable', 'probability_calibration_unsupported'
        elif Decimal(row['probability']) < threshold:
            closed = close(trip, at, 'supported_probability_below_threshold')
            if closed:
                decision['episode_id'] = closed['episode_id']
            status, reason = 'below_threshold', 'supported_probability_below_threshold'
        else:
            if trip not in active:
                episode = dict(
                    tenant_id=trip[0], trip_id=trip[1], policy_version=rule['policy_version'],
                    episode_id='P06-EP-' + _digest([rule['policy_version'], *trip, row['snapshot_id']])[:24],
                    episode_started_at=utc(at), start_snapshot_id=row['snapshot_id'],
                    start_promise_version_id=row['promise_version_id'], episode_last_observed_at=utc(at),
                    episode_closed_at=None, closure_reason=None, episode_status='open', censored_at=None,
                    candidate_count=0,
                )
                active[trip] = episode
                episodes.append(episode)
            episode = active[trip]
            decision['episode_id'] = episode['episode_id']
            day = trip[0], at.astimezone(zone).date().isoformat()
            if row['eta_status'] != 'available':
                status, reason = 'unavailable', 'eta_unavailable'
            elif lead < rule['minimum_action_lead_seconds']:
                status, reason = 'insufficient_lead', 'promise_lead_below_minimum'
            elif trip in last_candidate and (at - last_candidate[trip]).total_seconds() < rule['repeat_suppression_seconds']:
                status, reason = 'cooldown', 'trip_candidate_cooldown'
            elif daily[day] >= rule['daily_candidate_cap_per_tenant']:
                status, reason = 'capacity_exhausted', 'tenant_local_day_candidate_cap'
            else:
                status, reason = 'admitted', 'threshold_support_lead_cooldown_capacity_passed'
                candidate_id = 'P06-CAND-' + _digest([rule['policy_version'], *trip, row['snapshot_id'],
                                                    row['prediction_version'], row['promise_version_id']])[:24]
                decision['candidate_id'] = candidate_id
                last_candidate[trip] = at
                daily[day] += 1
                episode['candidate_count'] += 1
        decision.update(candidate_status=status, reason=reason)
        decisions.append(decision)
        if status == 'admitted':
            candidates.append(dict(decision, simulated_issue_at=utc(at)))
    for trip, episode in active.items():
        episode.update(episode_status='censored', censored_at=utc(last_seen[trip]))
    return {'decisions': decisions, 'candidates': candidates, 'episodes': episodes,
            'summary': {'definition_version': DEFINITION_VERSION, 'policy_version': rule['policy_version'],
                        'enabled': rule['enabled'], 'input_records': input_count,
                        'opportunities': len(rows), 'duplicate_records_ignored': input_count - len(rows),
                        'monitored_trips': len(last_seen), 'admitted_candidates': len(candidates),
                        'episodes': len(episodes),
                        'closed_episodes': sum(r['episode_status'] == 'closed' for r in episodes),
                        'censored_episodes': sum(r['episode_status'] == 'censored' for r in episodes),
                        'decision_status_counts': dict(sorted(Counter(r['candidate_status'] for r in decisions).items())),
                        'daily_candidate_counts': [dict(tenant_id=tenant, local_date=day, candidates=count)
                                                   for (tenant, day), count in sorted(daily.items())],
                        'scope': 'research_candidates_not_deliveries', 'labels_used': False,
                        'delivery_simulated': False}}
