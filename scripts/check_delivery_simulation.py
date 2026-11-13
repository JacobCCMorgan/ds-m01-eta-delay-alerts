"""Independently audit the frozen P07 delivery scenario using only the standard library.

The reference uses a sorted event calendar and retained P06 July inputs. It does
not import project simulation/scoring code, fit models, read final-test source
data, or contact a notification service. Existing audit output is never replaced.
"""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo


ORIGIN = 'synthetic_scenario'
OUTCOMES = ('delivered', 'transient_failure', 'permanent_failure', 'unknown')
TRUTH_FIELDS = {'delay_label', 'eligibility', 'actual_arrival_time', 'label_status',
                'label_known_at', 'outcome_version_id', 'target_seconds'}


def clock(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('A timezone-aware clock is required')
    return result.astimezone(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def identity(prefix, *parts):
    return prefix + '-' + hashlib.sha256(canonical(list(parts))).hexdigest()[:24]


def file_sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def read_rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def indexed(rows, fields, *, identical_duplicates=False):
    result = {}
    for row in rows:
        key = tuple(row[name] for name in fields)
        if any(not isinstance(value, str) or not value for value in key):
            raise ValueError('Invalid record identity')
        if key in result and not (identical_duplicates and row == result[key]):
            raise ValueError('Duplicate or conflicting record identity')
        result[key] = row
    return result


def trip(row):
    return row['tenant_id'], row['trip_id']


def snapshot(row):
    return row['tenant_id'], row['snapshot_id']


def transport_draw(candidate, attempt_index, settings):
    """Declared independent SHA256 draws; each dimension has its own hash."""
    cfg = settings['delivery']
    def draw(dimension, modulus):
        message = [cfg['seed'], cfg['version'], candidate['tenant_id'],
                   candidate['candidate_id'], attempt_index, dimension]
        first_bytes = hashlib.sha256(canonical(message)).digest()[:8]
        return int.from_bytes(first_bytes, 'big') % modulus
    ticket, cumulative = draw('outcome', 10000), 0
    for outcome in OUTCOMES:
        cumulative += cfg['outcome_basis_points'][outcome]
        if ticket < cumulative:
            break
    if cumulative <= ticket:
        raise ValueError('Transport probabilities do not cover the draw')
    low, high = cfg['latency_seconds']
    lag_low, lag_high = cfg['receipt_lag_seconds']
    return {'outcome': outcome, 'latency_seconds': low + draw('latency', high-low+1),
            'receipt_lag_seconds': lag_low + draw('receipt_lag', lag_high-lag_low+1)}


def reference_delivery(candidates, episodes, settings, *, counts_as_of=None, draw=None):
    """Reference calendar. No labels or production functions are accepted."""
    cfg = settings['delivery']
    cutoff = clock(counts_as_of or cfg['counts_as_of'])
    draw = draw or transport_draw
    sources = indexed(candidates, ('tenant_id', 'candidate_id'), identical_duplicates=True)
    source_episodes = indexed(episodes, ('tenant_id', 'episode_id'), identical_duplicates=True)
    for row in list(sources.values()) + list(source_episodes.values()):
        if TRUTH_FIELDS.intersection(row):
            raise ValueError('Truth fields must not enter transport simulation')
    counts = Counter((row['tenant_id'], row['episode_id']) for row in sources.values())
    for key, row in source_episodes.items():
        if counts[key] != row['candidate_count']:
            raise ValueError('Incomplete candidate or episode inventory')
    for row in sources.values():
        episode = source_episodes.get((row['tenant_id'], row['episode_id']))
        if episode is None or trip(episode) != trip(row):
            raise ValueError('Candidate crossed episode/tenant/trip identity')
    calendar, alerts, attempts, observed_events = [], {}, {}, []
    successful_service = {}
    priorities = {'closure': 0, 'receipt': 1, 'timeout': 1, 'candidate': 2, 'dispatch': 3, 'service': 4}

    def schedule(at, kind, source, number=0, payload=None):
        calendar.append((at, priorities[kind], source['tenant_id'], source['trip_id'],
                         source.get('candidate_id', source.get('episode_id')), number,
                         len(observed_events)+len(calendar), kind, source, payload))

    def event(at, kind, source, number=None):
        observed_events.append({'event_type': kind, 'event_at': stamp(at),
            'tenant_id': source['tenant_id'], 'trip_id': source['trip_id'],
            'alert_episode_id': source['episode_id'], 'candidate_id': source.get('candidate_id'),
            'attempt_index': number})

    def restriction(source, at):
        episode = source_episodes[source['tenant_id'], source['episode_id']]
        closed = episode['episode_closed_at']
        if closed is not None and clock(closed) <= at:
            return 'suppressed', 'episode_closed'
        if (at-clock(source['simulated_issue_at'])).total_seconds() > cfg['ttl_seconds']:
            return 'failed', 'ttl_expired'
        if (clock(source['deadline_at'])-at).total_seconds() < cfg['minimum_action_lead_seconds']:
            return 'no_action_lead', 'insufficient_action_lead'
        previous = successful_service.get(trip(source))
        if previous is not None and (at-previous).total_seconds() < cfg['repeat_suppression_seconds']:
            return 'suppressed', 'delivery_cooldown'
        return None

    for row in source_episodes.values():
        if row['episode_closed_at'] is not None:
            schedule(clock(row['episode_closed_at']), 'closure', row)
    for key, row in sources.items():
        aid = identity('P07-ALERT', row['candidate_id'])
        alerts[key] = {'tenant_id': row['tenant_id'], 'candidate_id': row['candidate_id'],
            'alert_id': aid, 'trip_id': row['trip_id'], 'snapshot_id': row['snapshot_id'],
            'alert_episode_id': row['episode_id'], 'prediction_version': row['prediction_version'],
            'decision_threshold_version': row['policy_version'], 'promise_version_id': row['promise_version_id'],
            'created_at': stamp(clock(row['simulated_issue_at'])), 'deadline_at': stamp(clock(row['deadline_at'])),
            'probability': row['probability'], 'alert_status': 'queued',
            'status_reason': 'candidate_not_yet_observed', 'first_delivery_id': None,
            'first_delivered_at': None, 'attempt_count': 0, 'result_origin': ORIGIN}
        schedule(clock(row['simulated_issue_at']), 'candidate', row)
    while calendar:
        calendar.sort(key=lambda item: item[:7])
        at, _, _, _, _, number, _, kind, source, payload = calendar.pop(0)
        if at > cutoff:
            break
        if kind == 'closure':
            event(at, kind, source)
            continue
        alert = alerts[source['tenant_id'], source['candidate_id']]
        if kind == 'candidate':
            alert['status_reason'] = 'awaiting_dispatch'
            event(at, kind, source)
            schedule(at, 'dispatch', source, 1)
        elif kind == 'dispatch':
            blocked = restriction(source, at)
            if blocked:
                alert['alert_status'], alert['status_reason'] = blocked
                event(at, kind, source)
                continue
            transport = draw(source, number, settings)
            service = None if transport['outcome'] == 'unknown' else at+timedelta(seconds=transport['latency_seconds'])
            receipt = (at+timedelta(seconds=cfg['unknown_timeout_seconds']) if service is None
                       else service+timedelta(seconds=transport['receipt_lag_seconds']))
            did = identity('P07-DELIVERY', alert['alert_id'], number)
            item = {name: alert[name] for name in ('tenant_id', 'alert_id', 'candidate_id',
                'alert_episode_id', 'trip_id', 'snapshot_id', 'prediction_version',
                'decision_threshold_version', 'promise_version_id', 'deadline_at')}
            item.update(delivery_id=did, attempt_index=number, sent_at=stamp(at),
                scheduled_service_at=stamp(service) if service else None, service_at=None,
                service_outcome=None, scheduled_receipt_at=stamp(receipt), receipt_known_at=None,
                delivered_at=None, delivery_status='queued', attempt_status='dispatched',
                error_reason=None, retryable=False, retry_scheduled_at=None, receipt_observed=False,
                receipt_type='none', recipient_role='dispatcher', received_or_opened_at=None, result_origin=ORIGIN)
            attempts[did] = item
            alert['attempt_count'] += 1
            alert['status_reason'] = 'awaiting_service'
            event(at, kind, source, number)
            schedule(service or receipt, 'service' if service else 'timeout', source, number, (did, transport))
        elif kind == 'service':
            did, transport = payload
            item = attempts[did]
            item['service_at'] = stamp(at)
            blocked = restriction(source, at)
            if blocked:
                alert['alert_status'], alert['status_reason'] = blocked
                item.update(service_outcome='gate_blocked', receipt_known_at=stamp(at),
                    receipt_observed=True, delivery_status='failed', attempt_status='blocked', error_reason=blocked[1])
            else:
                item.update(service_outcome=transport['outcome'], attempt_status='awaiting_receipt')
                alert['status_reason'] = 'awaiting_receipt'
                if transport['outcome'] == 'delivered':
                    successful_service[trip(source)] = at
                schedule(clock(item['scheduled_receipt_at']), 'receipt', source, number, (did, transport))
            event(at, kind, source, number)
        else:
            did, transport = payload
            item = attempts[did]
            item.update(receipt_known_at=stamp(at), receipt_observed=True)
            if kind == 'timeout':
                item.update(delivery_status='unknown', attempt_status='unknown_resolution', error_reason='unknown_timeout')
                alert.update(alert_status='failed', status_reason='unknown_resolution')
            elif transport['outcome'] == 'delivered':
                item.update(delivery_status='delivered', delivered_at=item['service_at'],
                    attempt_status='delivered', receipt_type='service_delivered')
                alert.update(alert_status='delivered', status_reason='confirmed_service_delivery',
                    first_delivery_id=did, first_delivered_at=item['service_at'])
            else:
                outcome = transport['outcome']
                item.update(delivery_status='failed', attempt_status=outcome, error_reason=outcome,
                    retryable=outcome == 'transient_failure')
                alert.update(alert_status='failed', status_reason=outcome)
                if outcome == 'transient_failure':
                    if number >= cfg['max_attempts']:
                        alert['status_reason'] = 'maximum_attempts_exhausted'
                    else:
                        retry = at+timedelta(seconds=cfg['retry_backoff_seconds'][number-1])
                        if (retry-clock(alert['created_at'])).total_seconds() <= cfg['ttl_seconds']:
                            item['retry_scheduled_at'] = stamp(retry)
                            alert.update(alert_status='queued', status_reason='retry_scheduled')
                            schedule(retry, 'dispatch', source, number+1)
                        else:
                            alert['status_reason'] = 'ttl_expired_before_retry'
            event(at, kind, source, number)
    alerts = sorted(alerts.values(), key=lambda row: (row['tenant_id'], row['alert_id']))
    attempts = sorted(attempts.values(), key=lambda row: (row['tenant_id'], row['alert_id'], row['attempt_index']))
    delivered = Counter((r['tenant_id'], r['alert_episode_id']) for r in attempts if r['delivery_status']=='delivered')
    promoted = []
    for key, row in source_episodes.items():
        closed = row['episode_closed_at'] if row['episode_closed_at'] and clock(row['episode_closed_at']) <= cutoff else None
        promoted.append({'tenant_id': row['tenant_id'], 'alert_episode_id': row['episode_id'],
            'trip_id': row['trip_id'], 'episode_started_at': stamp(clock(row['episode_started_at'])),
            'episode_closed_at': stamp(clock(closed)) if closed else None,
            'delivered_alert_count': delivered[key], 'duplicate_alert_count': max(0, delivered[key]-1),
            'counts_as_of': stamp(cutoff), 'value_states': {} if closed else {'episode_closed_at': 'not_mature'},
            'policy_version': row['policy_version'], 'source_episode_status': row['episode_status'],
            'source_closure_reason': row['closure_reason'], 'censored_at': row['censored_at'],
            'original_candidate_count': row['candidate_count'], 'alert_count': row['candidate_count'], 'result_origin': ORIGIN})
    promoted.sort(key=lambda row: (row['tenant_id'], row['alert_episode_id']))
    return {'alerts': alerts, 'attempts': attempts, 'episodes': promoted, 'event_core': observed_events,
        'summary': {'definition_version': 'p07-delivery-v1', 'counts_as_of': stamp(cutoff),
            'candidates': len(sources), 'alerts': len(alerts), 'episodes': len(promoted), 'attempts': len(attempts),
            'delivered_alerts': sum(r['alert_status']=='delivered' for r in alerts),
            'delivered_attempts': sum(r['delivery_status']=='delivered' for r in attempts),
            'duplicate_deliveries': sum(r['duplicate_alert_count'] for r in promoted),
            'alert_status_counts': dict(sorted(Counter(r['alert_status'] for r in alerts).items())),
            'delivery_status_counts': dict(sorted(Counter(r['delivery_status'] for r in attempts).items())),
            'duplicate_candidate_records_ignored': len(candidates)-len(sources),
            'duplicate_episode_records_ignored': len(episodes)-len(source_episodes),
            'event_count': len(observed_events), 'labels_used': False,
            'recipient_openings_generated': False, 'notifications_sent': False, 'synthetic_delivery_simulated': True}}


class Audit:
    def __init__(self):
        self.comparisons = 0
        self.failures = []
        self.maximum_numeric_difference = 0.0

    def compare(self, actual, expected, path):
        if isinstance(expected, dict):
            self.compare(isinstance(actual, dict), True, path+'/mapping')
            if not isinstance(actual, dict):
                return
            self.compare(sorted(actual), sorted(expected), path+'/keys')
            for key in expected.keys() & actual.keys():
                self.compare(actual[key], expected[key], path+'/'+str(key))
            return
        if isinstance(expected, list):
            self.compare(isinstance(actual, list), True, path+'/sequence')
            if not isinstance(actual, list):
                return
            self.compare(len(actual), len(expected), path+'/length')
            for i, (a, e) in enumerate(zip(actual, expected)):
                self.compare(a, e, path+'/'+str(i))
            return
        self.comparisons += 1
        equal = type(actual) is type(expected) and actual == expected
        if type(actual) in (float, int) and type(expected) in (float, int):
            difference = abs(actual-expected)
            self.maximum_numeric_difference = max(self.maximum_numeric_difference, difference)
            equal = math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12)
        if not equal:
            self.failures.append({'path': path, 'actual': actual, 'expected': expected})


def verify_simulation(actual, reference, audit):
    for name in ('alerts', 'attempts', 'episodes'):
        audit.compare(actual[name], reference[name], 'simulation/'+name)
    audit.compare(actual['summary'], reference['summary'], 'simulation/summary')
    core_fields = ('event_type', 'event_at', 'tenant_id', 'trip_id', 'alert_episode_id', 'candidate_id', 'attempt_index')
    audit.compare([{k: row[k] for k in core_fields} for row in actual['events']],
                  reference['event_core'], 'simulation/event_calendar')
    audit.compare(len({r['event_id'] for r in actual['events']}), len(actual['events']), 'events/unique_ids')
    for row in actual['events']:
        payload = {k: v for k, v in row.items() if k != 'event_id'}
        audit.compare(row['event_id'], identity('P07-EVENT', payload), 'events/content_identity')
        audit.compare(row['result_origin'], ORIGIN, 'events/synthetic_origin')


def reference_metrics(opportunities, targets, candidates, simulation, settings, tenant=None):
    """Set-based support and independent first-success classification."""
    included = lambda rows: [r for r in rows if tenant is None or r['tenant_id']==tenant]
    opp = indexed(included(opportunities), ('tenant_id', 'snapshot_id'), identical_duplicates=True)
    labels = indexed(included(targets), ('tenant_id', 'snapshot_id'))
    if opp.keys() != labels.keys():
        raise ValueError('Opportunity/target inventory differs')
    candidates = list(indexed(included(candidates), ('tenant_id', 'candidate_id'), identical_duplicates=True).values())
    alerts, attempts, episodes = (included(simulation[name]) for name in ('alerts', 'attempts', 'episodes'))
    cutoff = clock(settings['delivery']['counts_as_of'])
    minimum = settings['delivery']['minimum_action_lead_seconds']
    eligible = {key for key in opp if labels[key]['eligibility']=='eligible'}
    positive = {trip(opp[key]) for key in eligible if labels[key]['delay_label'] is True}
    negative = {trip(opp[key]) for key in eligible if labels[key]['delay_label'] is False}
    actionable = {trip(opp[key]) for key in eligible if labels[key]['delay_label'] is True
        and (clock(opp[key]['deadline_at'])-clock(opp[key]['snapshot_time'])).total_seconds()>=minimum}
    by_alert = {(r['tenant_id'], r['alert_id']): r for r in alerts}
    successes = [r for r in attempts if r['delivery_status']=='delivered' and r['receipt_known_at']
                 and clock(r['receipt_known_at'])<=cutoff and r['delivered_at']]
    successes.sort(key=lambda r: (clock(r['delivered_at']), r['tenant_id'], r['trip_id'], r['alert_id'], r['delivery_id']))
    first_by_trip, details = {}, []
    for record in successes:
        label = labels[snapshot(record)]
        at = clock(record['delivered_at'])
        lead = (clock(record['deadline_at'])-at).total_seconds()
        trusted = (label['eligibility']=='eligible' or label['reason']=='already_arrived_at_snapshot')
        trusted = trusted and label.get('label_status')=='verified' and label.get('arrival_time_precision')=='second'
        trusted = trusted and label.get('label_known_at') is not None and label.get('actual_arrival_time') is not None
        arrival_lead = (clock(label['actual_arrival_time'])-at).total_seconds() if trusted else None
        if arrival_lead is not None and arrival_lead<=0:
            category = 'post_arrival'
        elif lead<minimum:
            category = 'insufficient_lead'
        elif label['eligibility']=='eligible':
            category = 'useful_true' if label['delay_label'] else 'known_false'
        else:
            category = 'unresolved'
        first_by_trip.setdefault(trip(record), record['delivery_id'])
        row = {name: record[name] for name in ('tenant_id', 'trip_id', 'snapshot_id', 'candidate_id',
            'alert_id', 'alert_episode_id', 'delivery_id', 'promise_version_id', 'deadline_at', 'delivered_at', 'receipt_known_at')}
        row.update(first_successful_delivery_for_trip=first_by_trip[trip(record)]==record['delivery_id'],
            classification=category, target_eligibility=label['eligibility'], target_reason=label['reason'],
            delay_label=label['delay_label'], prospective_deadline_lead_seconds=lead,
            retrospective_arrival_lead_seconds=arrival_lead,
            candidate_to_delivery_seconds=(at-clock(by_alert[record['tenant_id'], record['alert_id']]['created_at'])).total_seconds(),
            successful_attempt_latency_seconds=(at-clock(record['sent_at'])).total_seconds())
        details.append(row)
    first = [r for r in details if r['first_successful_delivery_for_trip']]
    category_counts = Counter(r['classification'] for r in details)
    first_counts = Counter(r['classification'] for r in first)
    observed = [r for r in attempts if clock(r['sent_at'])<=cutoff]
    statuses = Counter(r['delivery_status'] if r['receipt_known_at'] and clock(r['receipt_known_at'])<=cutoff else 'queued' for r in observed)
    delivered_alerts = {(r['tenant_id'], r['alert_id']) for r in details}
    delivered_episodes = {(r['tenant_id'], r['alert_episode_id']) for r in details}
    monitored = {trip(r) for r in opp.values()}
    useful_trips = {trip(r) for r in details if r['classification']=='useful_true'}
    assessable = lambda rows: sum(r['classification']!='unresolved' for r in rows)
    counts = {'monitored_trips': len(monitored), 'opportunities': len(opp), 'candidate_alerts': len(candidates),
        'candidate_trips': len({trip(r) for r in candidates}), 'risk_episodes': len(episodes),
        'scored_trips': len({trip(opp[k]) for k in eligible}), 'eligible_target_snapshots': len(eligible),
        'excluded_target_snapshots': len(opp)-len(eligible), 'observed_positive_trips': len(positive),
        'actionable_positive_trips': len(actionable), 'negative_trips': len(negative),
        'mixed_promise_label_trips': len(positive & negative), 'retained_attempt_records': len(attempts),
        'attempts': len(observed), 'retry_attempts': sum(r['attempt_index']>1 for r in observed),
        'failed_attempts': statuses['failed'], 'unknown_attempts': statuses['unknown'], 'queued_attempts': statuses['queued'],
        'successful_confirmed_deliveries': len(details), 'delivered_alerts': len(delivered_alerts),
        'delivered_trips': len(first), 'episodes_with_delivery': len(delivered_episodes),
        'first_successful_deliveries': len(first), 'repeated_deliveries_within_episode': len(details)-len(delivered_episodes),
        'additional_deliveries_per_trip': len(details)-len(first), 'additional_successes_same_alert': len(details)-len(delivered_alerts),
        'confirmed_useful_delivery_trips': len(useful_trips), 'confirmed_useful_deliveries': category_counts['useful_true'],
        'mature_eligible_deliveries': sum(r['target_eligibility']=='eligible' for r in details),
        'mature_eligible_first_deliveries': sum(r['target_eligibility']=='eligible' for r in first),
        'known_assessable_deliveries': assessable(details), 'known_assessable_first_deliveries': assessable(first)}
    for prefix, category in [('known_false', 'known_false'), ('unresolved', 'unresolved'),
                             ('post_arrival', 'post_arrival'), ('insufficient_lead', 'insufficient_lead')]:
        counts[prefix+'_deliveries'] = category_counts[category]
        counts[prefix+'_first_deliveries'] = first_counts[category]
    counts['useful_first_deliveries'] = first_counts['useful_true']
    denominators = {'delivered_trip_coverage': len(monitored), 'candidate_alert_delivery_rate': len(candidates),
        'delivered_actionable_trip_recall': len(actionable), 'conditional_first_delivery_useful_precision': assessable(first),
        'first_delivery_usefulness_bounds': len(first), 'first_delivery_target_coverage': len(first),
        'first_delivery_assessable_coverage': len(first),
        'successful_deliveries_per_100_monitored_trips': len(monitored)}
    numerators = {'delivered_trip_coverage': len(first), 'candidate_alert_delivery_rate': len(delivered_alerts),
        'delivered_actionable_trip_recall': len(useful_trips), 'conditional_first_delivery_useful_precision': first_counts['useful_true'],
        'first_delivery_target_coverage': sum(r['target_eligibility']=='eligible' for r in first),
        'first_delivery_assessable_coverage': assessable(first),
        'successful_deliveries_per_100_monitored_trips': 100*len(details)}
    ratio = lambda n, d: n/d if d else None
    metrics = {name: ratio(n, denominators[name]) for name, n in numerators.items()}
    metrics['first_delivery_usefulness_bounds'] = {'lower': ratio(first_counts['useful_true'], len(first)),
        'upper': ratio(first_counts['useful_true']+first_counts['unresolved'], len(first))}
    timings = {}
    for name, rows in [('all_successful_deliveries', details), ('first_successful_deliveries', first)]:
        timings[name] = {}
        for field in ('prospective_deadline_lead_seconds', 'retrospective_arrival_lead_seconds',
                      'candidate_to_delivery_seconds', 'successful_attempt_latency_seconds'):
            numbers = [r[field] for r in rows if r[field] is not None]
            timings[name][field] = {'observed': len(numbers), 'missing': len(rows)-len(numbers),
                'minimum': min(numbers) if numbers else None, 'median': median(numbers) if numbers else None,
                'maximum': max(numbers) if numbers else None, 'unit': 'seconds'}
    return {'counts': counts, 'denominators': denominators, 'metrics': metrics, 'lead_seconds': timings,
        'attempt_status_counts': dict(sorted(statuses.items())),
        'alert_status_counts': dict(sorted(Counter(r['alert_status'] for r in alerts).items())),
        'delivery_class_counts': dict(sorted(category_counts.items())),
        'target_exclusion_reason_counts': dict(sorted(Counter(r['reason'] for r in labels.values() if r['eligibility']=='excluded').items())),
        'delivery_rows': details, 'sparse': counts['scored_trips'] < settings['metrics']['sparse_trip_threshold']}


def contained(root, relative):
    path = (root/relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()) or path==root.resolve():
        raise ValueError('Evidence path leaves its declared root')
    return path


def verify_canonical_projection(report, parent, reference, settings, audit):
    directory = report/'store/jsonl'
    candidate_audit = indexed(read_rows(parent/'store/jsonl/p06_candidate_audit.jsonl'), ('tenant_id', 'candidate_id'))
    fields = {'eta_alert_episode': ('tenant_id', 'alert_episode_id', 'trip_id', 'episode_started_at',
                  'episode_closed_at', 'delivered_alert_count', 'duplicate_alert_count', 'counts_as_of', 'value_states'),
              'eta_alert': ('tenant_id', 'alert_id', 'alert_episode_id', 'output_id', 'trip_id', 'snapshot_id',
                  'prediction_version', 'decision_threshold_version', 'created_at', 'alert_status'),
              'eta_notification_delivery': ('tenant_id', 'provenance_id', 'delivery_id', 'alert_id',
                  'recipient_role', 'delivered_at', 'received_or_opened_at', 'receipt_type', 'delivery_status')}
    expected = {'eta_alert_episode': [], 'eta_alert': [], 'eta_notification_delivery': []}
    for row in reference['episodes']:
        expected['eta_alert_episode'].append({name: row[name] for name in fields['eta_alert_episode']})
    for row in reference['alerts']:
        output_id = candidate_audit[row['tenant_id'], row['candidate_id']]['output_id']
        expanded = dict(row, output_id=output_id)
        expected['eta_alert'].append({name: expanded[name] for name in fields['eta_alert']})
    for row in reference['attempts']:
        token = '\x1f'.join(('P07-attempt-provenance', row['tenant_id'], row['delivery_id'], settings['delivery']['version']))
        expanded = dict(row, provenance_id=hashlib.sha256(token.encode()).hexdigest()[:24])
        expected['eta_notification_delivery'].append({name: expanded[name] for name in fields['eta_notification_delivery']})
    for name, records in expected.items():
        audit.compare(sorted(read_rows(directory/(name+'.jsonl')), key=canonical),
                      sorted(records, key=canonical), 'canonical/'+name)
    for name in settings['release']['remaining_reserved_tables']:
        audit.compare(read_rows(directory/(name+'.jsonl')), [], 'canonical/future_empty/'+name)


def audit_report(root, report):
    """Read retained July files, reconstruct transport, then separately score it."""
    audit = Audit()
    settings_path = root/'config/p07.json'
    settings, receipt = read_json(settings_path), read_json(report/'run_receipt.json')
    if settings['source_report'] != 'reports/p06':
        raise ValueError('This auditor is restricted to the retained P06 July report')
    parent = root/'reports/p06'
    frozen = root/'reproducibility/p07/protocol_freeze.json'
    freeze = read_json(frozen)
    bindings = {settings_path: file_sha(settings_path), frozen: file_sha(frozen),
                report/'run_receipt.json': file_sha(report/'run_receipt.json')}
    audit.compare(receipt['protocol_sha256'], bindings[settings_path], 'binding/protocol')
    audit.compare(receipt['freeze_sha256'], bindings[frozen], 'binding/freeze')
    audit.compare(receipt['base_public_commit'], settings['base_public_commit'], 'binding/base_commit')
    audit.compare(receipt['parent_report_sha256'], settings['source_report_sha256'], 'binding/declared_parent_pins')
    audit.compare(receipt['input_sha256'], settings['input_sha256'], 'binding/declared_input_pins')
    audit.compare(receipt['delivery_mode'], ORIGIN, 'binding/synthetic_delivery_mode')
    audit.compare(clock(freeze['frozen_at_utc']) <= clock(receipt['started_at_utc']) <=
        clock(receipt['materialized_at_utc']) <= clock(receipt['completed_at_utc']), True, 'binding/execution_clocks')
    audit.compare(freeze['final_test_evaluated'], False, 'binding/final_test_protected')
    audit.compare(freeze['july_assessment_previously_observed'], True, 'binding/July_knowledge_disclosed')
    for relative, expected in freeze['artifact_sha256'].items():
        path = contained(root, relative)
        audit.compare(file_sha(path), expected, 'binding/frozen_artifact/'+relative)
        bindings[path] = expected
    for relative, expected in settings['source_report_sha256'].items():
        path = contained(parent, relative)
        if file_sha(path) != expected:
            raise ValueError('P06 source pin differs: '+relative)
        audit.compare(file_sha(path), expected, 'binding/P06/'+relative)
        bindings[path] = expected
    for mapping, directory, label in [(receipt['code_sha256'], root/'src/ds_m01', 'code'),
                                      (receipt['output_sha256'], report, 'output')]:
        for relative, expected in mapping.items():
            path = contained(directory, relative)
            audit.compare(file_sha(path), expected, 'binding/'+label+'/'+relative)
            bindings[path] = expected
    inputs = {name: read_rows(parent/(name+'.jsonl')) for name in ('candidates', 'episodes', 'opportunities', 'targets')}
    opportunity_index = indexed(inputs['opportunities'], ('tenant_id', 'snapshot_id'))
    target_index = indexed(inputs['targets'], ('tenant_id', 'snapshot_id'))
    audit.compare(sorted(opportunity_index), sorted(target_index), 'population/retained_identity')
    audit.compare(len(opportunity_index), settings['population']['expected_opportunities'], 'population/opportunities')
    audit.compare(len({trip(r) for r in opportunity_index.values()}), settings['population']['expected_trips'], 'population/trips')
    for key, row in opportunity_index.items():
        date = clock(row['departure_time']).astimezone(ZoneInfo('America/Los_Angeles')).date().isoformat()
        audit.compare(settings['population']['departure_dates'][0] <= date <= settings['population']['departure_dates'][1], True, 'population/July_only')
        label = target_index[key]
        for field in ('trip_id', 'promise_version_id', 'deadline_at'):
            audit.compare(label[field], row[field], 'population/target_original_'+field)
        audit.compare(clock(row['promise_known_at']) <= clock(row['snapshot_time']), True, 'population/promise_asof')
        if label['label_known_at']:
            audit.compare(clock(label['label_known_at']) < clock(settings['population']['label_cutoff_exclusive']), True, 'population/label_cutoff_exclusive')
    reference = reference_delivery(inputs['candidates'], inputs['episodes'], settings)
    actual = {name: read_rows(report/(name+'.jsonl')) for name in ('alerts', 'attempts', 'episodes', 'events')}
    actual['summary'] = read_json(report/'simulation_summary.json')
    verify_simulation(actual, reference, audit)
    verify_canonical_projection(report, parent, reference, settings, audit)
    measured = read_json(report/'metrics.json')
    expected = reference_metrics(inputs['opportunities'], inputs['targets'], inputs['candidates'], reference, settings)
    audit.compare({k: measured[k] for k in expected}, expected, 'metrics/overall')
    slices = [{'tenant_id': tenant, **reference_metrics(inputs['opportunities'], inputs['targets'], inputs['candidates'], reference, settings, tenant)}
              for tenant in sorted({r['tenant_id'] for r in inputs['opportunities']})]
    audit.compare(measured['tenant_slices'], slices, 'metrics/tenant_slices')
    for flag in ('independent_policy_test', 'final_test_evaluated', 'recipient_opening_evaluated',
                 'adoption_evaluated', 'actions_evaluated', 'operational_benefit_established'):
        audit.compare(measured[flag], False, 'scope/'+flag)
    for flag in ('model_or_calibrator_refitted', 'policy_reselected', 'real_notifications_sent',
                 'final_test_evaluated', 'quality_accepted'):
        audit.compare(receipt[flag], False, 'scope/receipt/'+flag)
    for path, expected_hash in bindings.items():
        audit.compare(file_sha(path), expected_hash, 'stability/'+path.name)
    return {'status': 'passed' if not audit.failures else 'failed', 'comparisons': audit.comparisons,
        'failure_count': len(audit.failures), 'failures': audit.failures[:100],
        'absolute_tolerance': 1e-12, 'relative_tolerance': 1e-12,
        'maximum_absolute_numeric_difference': audit.maximum_numeric_difference,
        'production_imports': False, 'implementation': 'Independent standard-library sorted event calendar, transport hash draws, set-based metric counts and first-success classifications.',
        'protocol_sha256': bindings[settings_path], 'freeze_sha256': bindings[frozen],
        'source_report_sha256': settings['source_report_sha256'], 'source_pins_verified': len(settings['source_report_sha256']),
        'auditor_sha256': file_sha(Path(__file__)), 'run_receipt_sha256': bindings[report/'run_receipt.json'],
        'counts': expected['counts'], 'simulation_summary': reference['summary'],
        'outcome_sources_opened': False, 'final_test_evaluated': False, 'real_notifications_sent': False,
        'limits': ['Labels are the pinned, previously audited P06 July targets; this audit does not reopen raw outcome sources.',
                   'Event time/order/identity is independently compared; event status/reason text is hash-bound but not separately reinterpreted.',
                   'Typed DuckDB/Parquet integrity is covered by the separate store replay.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--report', type=Path, default=Path('reports/p07'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    report = args.report.resolve() if args.report.is_absolute() else (root/args.report).resolve()
    output = args.output if args.output is not None else report/'independent_checks.json'
    if output.exists() or output.is_symlink():
        raise SystemExit('Refusing to overwrite audit output: '+str(output))
    try:
        result = audit_report(root, report)
    except Exception as exc:
        result = {'status': 'failed', 'comparisons': 0, 'failure_count': 1,
                  'failures': [{'fatal_error': type(exc).__name__+': '+str(exc)}], 'production_imports': False}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x') as stream:
        json.dump(result, stream, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        stream.write('\n')
    print(json.dumps({k: result[k] for k in ('status', 'comparisons', 'failure_count')}))
    return 0 if result['status']=='passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())

