"""Deterministic synthetic transport; target outcomes are never an input.

Receiver service state and receipt knowledge are separate clocks. A successful
service immediately prevents another delivery, while only a receipt known by
the observation cutoff establishes a published confirmed delivery.
"""
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import heapq
import json


VERSION = 'p07-delivery-v1'
ORIGIN = 'synthetic_scenario'
CANDIDATE_FIELDS = frozenset(('tenant_id', 'trip_id', 'snapshot_id', 'snapshot_time',
    'promise_version_id', 'deadline_at', 'prediction_version', 'policy_version',
    'candidate_status', 'candidate_id', 'episode_id', 'lead_seconds', 'probability',
    'reason', 'simulated_issue_at'))
EPISODE_FIELDS = frozenset(('tenant_id', 'trip_id', 'policy_version', 'episode_id',
    'episode_started_at', 'start_snapshot_id', 'start_promise_version_id',
    'episode_last_observed_at', 'episode_closed_at', 'closure_reason',
    'episode_status', 'censored_at', 'candidate_count'))
DELIVERY = dict(version=VERSION, seed='DS-M01-P07-20261111', max_attempts=3,
    retry_backoff_seconds=[30, 120], ttl_seconds=600, minimum_action_lead_seconds=1800,
    repeat_suppression_seconds=1800, counts_as_of='2026-08-01T07:00:00Z',
    outcome_basis_points={'delivered': 8000, 'transient_failure': 1500,
                         'permanent_failure': 300, 'unknown': 200},
    latency_seconds=[5, 60], receipt_lag_seconds=[0, 15], unknown_timeout_seconds=120)
OUTCOMES = ('delivered', 'transient_failure', 'permanent_failure', 'unknown')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _identifier(prefix, *parts):
    return prefix + '-' + hashlib.sha256(_json(list(parts)).encode('utf-8')).hexdigest()[:24]


def _at(value):
    if not isinstance(value, str):
        raise ValueError('Scenario timestamps must be timezone-aware strings')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('Invalid scenario timestamp') from exc
    if result.tzinfo is None:
        raise ValueError('Scenario timestamps require an explicit timezone')
    return result.astimezone(timezone.utc)


def _utc(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _settings(settings):
    if not isinstance(settings, Mapping) or not isinstance(settings.get('delivery'), Mapping):
        raise ValueError('Complete delivery protocol settings are required')
    config = settings['delivery']
    if set(config) != set(DELIVERY) or config['version'] != VERSION:
        raise ValueError('Delivery protocol version or setting fields differ')
    if not isinstance(config['seed'], str) or not config['seed'].strip() or len(config['seed']) > 256:
        raise ValueError('Delivery seed requires a nonempty bounded namespace')
    _at(config['counts_as_of'])
    # The caller pins an exact phase/scenario configuration. These bounds protect
    # the reusable algorithm while admitting independently frozen stress laws.
    for name, lower, upper in (('max_attempts', 1, 10), ('ttl_seconds', 1, 86400),
            ('unknown_timeout_seconds', 1, 86400), ('minimum_action_lead_seconds', 0, 604800),
            ('repeat_suppression_seconds', 0, 604800)):
        if type(config[name]) is not int or not lower <= config[name] <= upper:
            raise ValueError('Invalid bounded delivery setting: ' + name)
    backoff = config['retry_backoff_seconds']
    if (not isinstance(backoff, list) or len(backoff) != config['max_attempts'] - 1
            or any(type(v) is not int or not 0 <= v <= 86400 for v in backoff)):
        raise ValueError('Retry backoff must cover every bounded retry')
    for name in ('latency_seconds', 'receipt_lag_seconds'):
        value = config[name]
        if (not isinstance(value, list) or len(value) != 2
                or any(type(v) is not int or not 0 <= v <= 86400 for v in value)
                or value[0] > value[1]):
            raise ValueError('Invalid inclusive transport duration range: ' + name)
    weights = config['outcome_basis_points']
    if (not isinstance(weights, Mapping) or set(weights) != set(OUTCOMES)
            or any(type(v) is not int or v < 0 for v in weights.values()) or sum(weights.values()) != 10000):
        raise ValueError('Outcome basis points must be nonnegative integers summing to10000')
    return config


def _draw(candidate, attempt_index, settings, dimension, size):
    payload = [settings['seed'], settings['version'], candidate['tenant_id'],
               candidate['candidate_id'], attempt_index, dimension]
    return int.from_bytes(hashlib.sha256(_json(payload).encode('utf-8')).digest()[:8], 'big') % size


def transport_outcome(candidate, attempt_index, settings):
    """Pure independent transport draw, replaceable by mocks in boundary tests."""
    config = _settings(settings)
    if type(attempt_index) is not int or not 1 <= attempt_index <= config['max_attempts']:
        raise ValueError('Attempt index is outside the frozen retry bound')
    value, cumulative = _draw(candidate, attempt_index, config, 'outcome', 10000), 0
    for outcome in OUTCOMES:
        cumulative += config['outcome_basis_points'][outcome]
        if value < cumulative:
            break
    values = {'outcome': outcome}
    for dimension, key in (('latency', 'latency_seconds'), ('receipt_lag', 'receipt_lag_seconds')):
        lo, hi = config[key]
        values[key] = lo + _draw(candidate, attempt_index, config, dimension, hi - lo + 1)
    return values


def _unique(rows, fields, identity):
    result, fingerprints, ignored = {}, {}, 0
    for original in rows:
        # Reject an undeclared target field before copying or reading its value.
        if not isinstance(original, Mapping) or set(original) != fields:
            raise ValueError('Unexpected input fields; delivery cannot accept target outcomes')
        row = dict(original)
        for field in identity:
            if not isinstance(row[field], str) or not row[field].strip():
                raise ValueError('Nonempty tenant-qualified identities are required')
        key = tuple(row[field] for field in identity)
        fingerprint = _json(row)
        if key in result:
            if fingerprints[key] != fingerprint:
                raise ValueError('Conflicting duplicate delivery input identity')
            ignored += 1
        else:
            result[key], fingerprints[key] = row, fingerprint
    return result, ignored


def _inputs(candidates, episodes):
    candidates, candidate_dupes = _unique(candidates, CANDIDATE_FIELDS, ('tenant_id', 'candidate_id'))
    episodes, episode_dupes = _unique(episodes, EPISODE_FIELDS, ('tenant_id', 'episode_id'))
    global_ids = set()
    counts = Counter()
    for key, row in episodes.items():
        start, last = _at(row['episode_started_at']), _at(row['episode_last_observed_at'])
        if start > last or type(row['candidate_count']) is not int or row['candidate_count'] < 0:
            raise ValueError('Invalid source episode clock or candidate count')
        for name in ('trip_id', 'policy_version', 'start_snapshot_id', 'start_promise_version_id'):
            if not isinstance(row[name], str) or not row[name].strip():
                raise ValueError('Source episode context requires stable identifiers')
        if row['episode_status'] == 'closed':
            if row['episode_closed_at'] is None or not row['closure_reason'] or row['censored_at'] is not None:
                raise ValueError('Closed episode requires a known closure and reason')
            if not start <= _at(row['episode_closed_at']) <= last:
                raise ValueError('Episode closure clock disagrees with source observation')
        elif row['episode_status'] == 'censored':
            if row['episode_closed_at'] is not None or row['closure_reason'] is not None or row['censored_at'] is None:
                raise ValueError('Censored episode must not invent a closure')
            if _at(row['censored_at']) != last:
                raise ValueError('Episode censoring must match its last observed clock')
        else:
            raise ValueError('Retained episodes must be closed or censored')
    for row in candidates.values():
        if row['candidate_id'] in global_ids:
            raise ValueError('Candidate identity cannot cross tenants')
        global_ids.add(row['candidate_id'])
        key = row['tenant_id'], row['episode_id']
        episode = episodes.get(key)
        if episode is None or any(row[f] != episode[f] for f in ('trip_id', 'policy_version')):
            raise ValueError('Candidate does not belong to its tenant/trip/episode/rule')
        issued, snapshot, deadline = (_at(row[f]) for f in ('simulated_issue_at', 'snapshot_time', 'deadline_at'))
        if (issued != snapshot or not _at(episode['episode_started_at']) <= issued <= _at(episode['episode_last_observed_at'])
                or row['candidate_status'] != 'admitted'
                or isinstance(row['lead_seconds'], bool)
                or row['lead_seconds'] != (deadline - issued).total_seconds()):
            raise ValueError('Candidate clock, admission or frozen promise lead differs')
        for name in ('snapshot_id', 'prediction_version', 'promise_version_id'):
            if not isinstance(row[name], str) or not row[name].strip():
                raise ValueError('Candidate source versions require stable identifiers')
        try:
            probability = Decimal(row['probability'])
            if not probability.is_finite() or not 0 <= probability <= 1 or format(probability, '.6f') != row['probability']:
                raise ValueError('Candidate probability is not canonical six-decimal data')
        except (TypeError, ArithmeticError) as exc:
            raise ValueError('Candidate probability is invalid') from exc
        counts[key] += 1
    if any(counts[key] != row['candidate_count'] for key, row in episodes.items()):
        raise ValueError('Complete candidates must match retained episode counts')
    return candidates, episodes, candidate_dupes, episode_dupes


def simulate_delivery(candidates, episodes, settings, *, counts_as_of=None):
    """Replay all candidate/episode inputs without truth or recipient behavior."""
    config = _settings(settings)
    cutoff = _at(counts_as_of if counts_as_of is not None else config['counts_as_of'])
    sources, source_episodes, candidate_dupes, episode_dupes = _inputs(candidates, episodes)
    alerts, attempts, events, queue = {}, {}, [], []
    closed, serviced, last_success = set(), set(), {}
    serial = 0

    def push(at, kind, candidate=None, attempt_index=0, episode=None, payload=None):
        nonlocal serial
        serial += 1
        context = candidate if candidate is not None else episode
        rank = {'closure': 0, 'receipt': 1, 'timeout': 1, 'candidate': 2, 'dispatch': 3, 'service': 4}[kind]
        heapq.heappush(queue, (at, rank, context['tenant_id'], context['trip_id'],
            candidate['candidate_id'] if candidate is not None else episode['episode_id'], attempt_index,
            serial, kind, candidate, episode, payload))

    def event(at, kind, status, reason, candidate=None, attempt=None, episode=None):
        context = candidate if candidate is not None else episode
        alert_id = _identifier('P07-ALERT', candidate['candidate_id']) if candidate else None
        row = dict(event_type=kind, event_at=_utc(at), tenant_id=context['tenant_id'], trip_id=context['trip_id'],
            alert_episode_id=context['episode_id'], alert_id=alert_id,
            delivery_id=attempt['delivery_id'] if attempt else None,
            candidate_id=candidate['candidate_id'] if candidate else None,
            attempt_index=attempt['attempt_index'] if attempt else None,
            event_status=status, event_reason=reason, result_origin=ORIGIN)
        row['event_id'] = _identifier('P07-EVENT', row)
        events.append(row)

    def gate(candidate, at):
        if (candidate['tenant_id'], candidate['episode_id']) in closed:
            return 'suppressed', 'episode_closed'
        if at > _at(candidate['simulated_issue_at']) + timedelta(seconds=config['ttl_seconds']):
            return 'failed', 'ttl_expired'
        if (_at(candidate['deadline_at']) - at).total_seconds() < config['minimum_action_lead_seconds']:
            return 'no_action_lead', 'insufficient_action_lead'
        trip = candidate['tenant_id'], candidate['trip_id']
        if trip in last_success and (at - last_success[trip]).total_seconds() < config['repeat_suppression_seconds']:
            return 'suppressed', 'delivery_cooldown'
        return None

    for row in source_episodes.values():
        if row['episode_closed_at'] is not None:
            push(_at(row['episode_closed_at']), 'closure', episode=row)
    for row in sources.values():
        alert_id = _identifier('P07-ALERT', row['candidate_id'])
        alerts[alert_id] = dict(tenant_id=row['tenant_id'], alert_id=alert_id, candidate_id=row['candidate_id'],
            alert_episode_id=row['episode_id'], trip_id=row['trip_id'], snapshot_id=row['snapshot_id'],
            prediction_version=row['prediction_version'], decision_threshold_version=row['policy_version'],
            created_at=_utc(_at(row['simulated_issue_at'])), promise_version_id=row['promise_version_id'],
            deadline_at=_utc(_at(row['deadline_at'])), probability=row['probability'], alert_status='queued',
            status_reason='candidate_not_yet_observed', first_delivery_id=None, first_delivered_at=None,
            attempt_count=0, result_origin=ORIGIN)
        push(_at(row['simulated_issue_at']), 'candidate', candidate=row)

    while queue and queue[0][0] <= cutoff:
        at, _, _, _, _, index, _, kind, candidate, episode, payload = heapq.heappop(queue)
        if kind == 'closure':
            closed.add((episode['tenant_id'], episode['episode_id']))
            event(at, kind, 'closed', episode['closure_reason'], episode=episode)
            continue
        alert = alerts[_identifier('P07-ALERT', candidate['candidate_id'])]
        if kind == 'candidate':
            alert['status_reason'] = 'awaiting_dispatch'
            event(at, kind, 'queued', 'candidate_observed', candidate=candidate)
            push(at, 'dispatch', candidate, 1)
            continue
        if kind == 'dispatch':
            if alert['alert_id'] in serviced:
                continue
            blocked = gate(candidate, at)
            if blocked:
                alert['alert_status'], alert['status_reason'] = blocked
                event(at, kind, blocked[0], blocked[1], candidate=candidate)
                continue
            draw = transport_outcome(candidate, index, settings)
            if (set(draw) != {'outcome', 'latency_seconds', 'receipt_lag_seconds'} or draw['outcome'] not in OUTCOMES
                    or any(type(draw[k]) is not int or draw[k] < 0 for k in ('latency_seconds', 'receipt_lag_seconds'))):
                raise ValueError('Transport primitive returned an invalid outcome or duration')
            service_at = at + timedelta(seconds=draw['latency_seconds']) if draw['outcome'] != 'unknown' else None
            receipt_at = (service_at + timedelta(seconds=draw['receipt_lag_seconds']) if service_at is not None
                          else at + timedelta(seconds=config['unknown_timeout_seconds']))
            delivery_id = _identifier('P07-DELIVERY', alert['alert_id'], index)
            attempt = dict(tenant_id=candidate['tenant_id'], delivery_id=delivery_id, alert_id=alert['alert_id'],
                candidate_id=candidate['candidate_id'], alert_episode_id=candidate['episode_id'],
                trip_id=candidate['trip_id'], snapshot_id=candidate['snapshot_id'],
                prediction_version=candidate['prediction_version'], decision_threshold_version=candidate['policy_version'],
                promise_version_id=candidate['promise_version_id'], deadline_at=alert['deadline_at'],
                attempt_index=index, sent_at=_utc(at), scheduled_service_at=_utc(service_at) if service_at else None,
                service_at=None, service_outcome=None, scheduled_receipt_at=_utc(receipt_at), receipt_known_at=None,
                delivered_at=None, delivery_status='queued', attempt_status='dispatched', error_reason=None,
                retryable=False, retry_scheduled_at=None, receipt_observed=False, receipt_type='none',
                recipient_role='dispatcher', received_or_opened_at=None, result_origin=ORIGIN)
            attempts[delivery_id] = attempt
            alert['attempt_count'] += 1
            alert['status_reason'] = 'awaiting_service'
            event(at, kind, 'queued', 'attempt_dispatched', candidate, attempt)
            if service_at is None:
                push(receipt_at, 'timeout', candidate, index, payload=delivery_id)
            else:
                push(service_at, 'service', candidate, index, payload=(delivery_id, draw))
            continue
        if kind == 'timeout':
            attempt = attempts[payload]
            attempt.update(receipt_known_at=_utc(at), receipt_observed=True, delivery_status='unknown',
                           attempt_status='unknown_resolution', error_reason='unknown_timeout')
            alert.update(alert_status='failed', status_reason='unknown_resolution')
            event(at, kind, 'unknown', 'unknown_timeout_no_retry', candidate, attempt)
            continue
        if kind == 'service':
            delivery_id, draw = payload
            attempt = attempts[delivery_id]
            attempt['service_at'] = _utc(at)
            blocked = gate(candidate, at)
            if blocked:
                alert['alert_status'], alert['status_reason'] = blocked
                attempt.update(service_outcome='gate_blocked', receipt_known_at=_utc(at), receipt_observed=True,
                    delivery_status='failed', attempt_status='blocked', error_reason=blocked[1])
                event(at, kind, blocked[0], blocked[1], candidate, attempt)
                continue
            attempt.update(service_outcome=draw['outcome'], attempt_status='awaiting_receipt')
            alert['status_reason'] = 'awaiting_receipt'
            if draw['outcome'] == 'delivered':
                serviced.add(alert['alert_id'])
                last_success[candidate['tenant_id'], candidate['trip_id']] = at
            event(at, kind, draw['outcome'], 'synthetic_service_result', candidate, attempt)
            push(_at(attempt['scheduled_receipt_at']), 'receipt', candidate, index, payload=delivery_id)
            continue
        attempt = attempts[payload]
        attempt.update(receipt_known_at=_utc(at), receipt_observed=True)
        outcome = attempt['service_outcome']
        if outcome == 'delivered':
            attempt.update(delivery_status='delivered', delivered_at=attempt['service_at'],
                           attempt_status='delivered', receipt_type='service_delivered')
            alert.update(alert_status='delivered', status_reason='confirmed_service_delivery',
                         first_delivery_id=attempt['delivery_id'], first_delivered_at=attempt['delivered_at'])
        else:
            attempt.update(delivery_status='failed', attempt_status=outcome, error_reason=outcome,
                           retryable=outcome == 'transient_failure')
            alert.update(alert_status='failed', status_reason=outcome)
            if outcome == 'transient_failure' and index < config['max_attempts']:
                retry_at = at + timedelta(seconds=config['retry_backoff_seconds'][index - 1])
                if retry_at <= _at(alert['created_at']) + timedelta(seconds=config['ttl_seconds']):
                    attempt['retry_scheduled_at'] = _utc(retry_at)
                    alert.update(alert_status='queued', status_reason='retry_scheduled')
                    push(retry_at, 'dispatch', candidate, index + 1)
                else:
                    alert['status_reason'] = 'ttl_expired_before_retry'
            elif outcome == 'transient_failure':
                alert['status_reason'] = 'maximum_attempts_exhausted'
        event(at, kind, attempt['delivery_status'], alert['status_reason'], candidate, attempt)

    delivered = Counter((r['tenant_id'], r['alert_episode_id']) for r in attempts.values() if r['delivery_status'] == 'delivered')
    promoted = []
    for key, source in source_episodes.items():
        closed_at = source['episode_closed_at'] if key in closed else None
        count = delivered[key]
        promoted.append(dict(tenant_id=source['tenant_id'], alert_episode_id=source['episode_id'],
            trip_id=source['trip_id'], episode_started_at=_utc(_at(source['episode_started_at'])),
            episode_closed_at=_utc(_at(closed_at)) if closed_at else None,
            delivered_alert_count=count, duplicate_alert_count=max(count - 1, 0), counts_as_of=_utc(cutoff),
            value_states={} if closed_at else {'episode_closed_at': 'not_mature'},
            policy_version=source['policy_version'], source_episode_status=source['episode_status'],
            source_closure_reason=source['closure_reason'], censored_at=source['censored_at'],
            original_candidate_count=source['candidate_count'], alert_count=source['candidate_count'], result_origin=ORIGIN))
    alerts = sorted(alerts.values(), key=lambda r: (r['tenant_id'], r['alert_id']))
    attempts = sorted(attempts.values(), key=lambda r: (r['tenant_id'], r['alert_id'], r['attempt_index']))
    promoted.sort(key=lambda r: (r['tenant_id'], r['alert_episode_id']))
    return dict(alerts=alerts, attempts=attempts, episodes=promoted, events=events,
        summary=dict(definition_version=VERSION, counts_as_of=_utc(cutoff), candidates=len(sources),
            alerts=len(alerts), episodes=len(promoted), attempts=len(attempts),
            delivered_alerts=sum(r['alert_status'] == 'delivered' for r in alerts),
            delivered_attempts=sum(r['delivery_status'] == 'delivered' for r in attempts),
            duplicate_deliveries=sum(r['duplicate_alert_count'] for r in promoted),
            alert_status_counts=dict(sorted(Counter(r['alert_status'] for r in alerts).items())),
            delivery_status_counts=dict(sorted(Counter(r['delivery_status'] for r in attempts).items())),
            duplicate_candidate_records_ignored=candidate_dupes, duplicate_episode_records_ignored=episode_dupes,
            event_count=len(events), labels_used=False, recipient_openings_generated=False,
            notifications_sent=False, synthetic_delivery_simulated=True))
