"""Score confirmed deliveries against each alert's original promise target.

Transport simulation is outcome-free. This module alone joins retained July
labels after simulation; failed, queued and unknown attempts never become
negative delivery targets. Candidate, attempt and successful-delivery counts
have separate denominators.
"""
from collections import Counter, defaultdict
from collections.abc import Mapping
from statistics import median

from .common import parse_ts, utc

TARGET_STATUSES = {'verified','pending','cancelled','incomplete','lost','conflict'}
ALERT_STATUSES = {'queued','delivered','failed','suppressed','no_action_lead'}
ATTEMPT_STATUSES = {'queued','delivered','failed','unknown'}


def _trip(row):
    return row['tenant_id'],row['trip_id']


def _snapshot(row):
    return row['tenant_id'],row['snapshot_id']


def _stamp(value, field):
    try:
        return parse_ts(value)
    except (TypeError,ValueError,AttributeError,OverflowError) as exc:
        raise ValueError(field+' requires a timezone-aware clock') from exc


def _index(rows, fields, *, idempotent=False):
    result = {}
    for row in rows:
        if not isinstance(row,Mapping):
            raise ValueError('Delivery records must be mappings')
        key = tuple(row.get(field) for field in fields)
        if any(not isinstance(v,str) or not v.strip() for v in key):
            raise ValueError('Missing or invalid delivery identity')
        if key in result:
            if idempotent and result[key]==row:
                continue
            raise ValueError('Duplicate or conflicting delivery identity')
        result[key] = row
    return result


def _ratio(numerator, denominator):
    return numerator/denominator if denominator else None


def _settings(settings):
    try:
        label_cutoff = _stamp(settings['population']['label_cutoff_exclusive'],'label cutoff')
        delivery = settings['delivery']
        counts_as_of = _stamp(delivery['counts_as_of'],'delivery observation cutoff')
        minimum = delivery['minimum_action_lead_seconds']
        sparse = settings['metrics']['sparse_trip_threshold']
        if type(minimum) is not int or minimum<0 or type(sparse) is not int or sparse<1:
            raise ValueError('Invalid delivery metric support or lead setting')
    except (KeyError,TypeError) as exc:
        raise ValueError('Complete delivery metric settings are required') from exc
    return label_cutoff,counts_as_of,minimum,sparse


def _population(opportunities,targets,label_cutoff):
    opportunities = _index(opportunities,('tenant_id','snapshot_id'),idempotent=True)
    targets = _index(targets,('tenant_id','snapshot_id'))
    if opportunities.keys()!=targets.keys():
        raise ValueError('Delivery scoring must preserve all monitored opportunities and targets')
    for key,row in opportunities.items():
        target = targets[key]
        for field in ('trip_id','promise_version_id'):
            if not isinstance(row.get(field),str) or not row[field] or target.get(field)!=row[field]:
                raise ValueError('Target differs from original snapshot promise identity')
        snapshot = _stamp(row['snapshot_time'],'snapshot')
        deadline = _stamp(row['deadline_at'],'deadline')
        if (_stamp(target['deadline_at'],'target deadline')!=deadline or snapshot>=label_cutoff
                or _stamp(target['label_cutoff_at'],'target cutoff')!=label_cutoff
                or _stamp(row['promise_known_at'],'promise known')>snapshot):
            raise ValueError('Target or promise clock differs from its original opportunity')
        if target.get('snapshot_time') is not None and _stamp(target['snapshot_time'],'target snapshot')!=snapshot:
            raise ValueError('Target snapshot clock differs')
        eligibility = target.get('eligibility')
        if eligibility not in ('eligible','excluded') or (eligibility=='eligible' and type(target.get('delay_label')) is not bool) or (eligibility=='excluded' and target.get('delay_label') is not None):
            raise ValueError('Only eligible targets may have a Boolean delay label')
        known = target.get('label_known_at')
        arrival = target.get('actual_arrival_time')
        if known is not None and _stamp(known,'label known')>=label_cutoff:
            raise ValueError('Target knowledge must be strictly before label cutoff')
        if target.get('label_status') is not None and target['label_status'] not in TARGET_STATUSES:
            raise ValueError('Unknown target status')
        if arrival is not None and (known is None or _stamp(arrival,'arrival')>_stamp(known,'label known')):
            raise ValueError('Arrival cannot occur after it was known')
        if eligibility=='eligible':
            if (known is None or target.get('label_status')!='verified' or target.get('arrival_time_precision')!='second'
                    or arrival is None or _stamp(arrival,'arrival')<=snapshot):
                raise ValueError('Eligible delivery target requires mature verified future gate arrival')
            if target['delay_label']!=(_stamp(arrival,'arrival')>deadline):
                raise ValueError('Delay label differs from its original snapshot deadline')
        if target.get('reason')=='already_arrived_at_snapshot':
            if (eligibility!='excluded' or target.get('label_status')!='verified' or target.get('arrival_time_precision')!='second'
                    or arrival is None or known is None or _stamp(arrival,'arrival')>snapshot):
                raise ValueError('Post-arrival exclusion requires verified past gate arrival')
    return opportunities,targets


def _known_success(attempt,cutoff):
    return (attempt['delivery_status']=='delivered' and attempt.get('delivered_at') is not None
            and attempt.get('receipt_known_at') is not None
            and _stamp(attempt['receipt_known_at'],'receipt known')<=cutoff)


def _ledger(opportunities,candidates,simulation,cutoff):
    candidates = _index(candidates,('tenant_id','candidate_id'),idempotent=True)
    _index(candidates.values(),('tenant_id','snapshot_id'))
    alerts = _index(simulation['alerts'],('tenant_id','alert_id'),idempotent=True)
    attempts = _index(simulation['attempts'],('tenant_id','delivery_id'),idempotent=True)
    episodes = _index(simulation['episodes'],('tenant_id','alert_episode_id'),idempotent=True)
    if _stamp(simulation['summary']['counts_as_of'],'summary cutoff')!=cutoff:
        raise ValueError('Simulation and evaluation observation cutoffs differ')
    by_candidate = _index(alerts.values(),('tenant_id','candidate_id'))
    if by_candidate.keys()!=candidates.keys():
        raise ValueError('Every admitted candidate must have exactly one alert')
    trips = {_trip(row) for row in opportunities.values()}
    for episode in episodes.values():
        if _trip(episode) not in trips or _stamp(episode['counts_as_of'],'episode cutoff')!=cutoff:
            raise ValueError('Episode is outside monitored trips or observation cutoff')
    for key,candidate in candidates.items():
        opportunity = opportunities.get(_snapshot(candidate))
        alert = by_candidate[key]
        if opportunity is None:
            raise ValueError('Candidate is outside monitored opportunities')
        for field in ('trip_id','promise_version_id','prediction_version'):
            if candidate.get(field)!=opportunity.get(field) or alert.get(field)!=candidate.get(field):
                raise ValueError('Alert differs from frozen candidate promise or model')
        if candidate.get('probability')!=opportunity.get('probability') or alert.get('probability')!=candidate.get('probability'):
            raise ValueError('Alert probability differs from frozen candidate')
        if alert.get('snapshot_id')!=candidate['snapshot_id'] or alert.get('decision_threshold_version')!=candidate.get('policy_version'):
            raise ValueError('Alert snapshot or policy differs from candidate')
        for field in ('snapshot_time','deadline_at'):
            if _stamp(candidate[field],field)!=_stamp(opportunity[field],field):
                raise ValueError('Candidate clock differs from original opportunity')
        if (_stamp(alert['deadline_at'],'alert deadline')!=_stamp(candidate['deadline_at'],'candidate deadline')
                or _stamp(alert['created_at'],'alert creation')!=_stamp(candidate['simulated_issue_at'],'candidate issue')
                or _stamp(candidate['simulated_issue_at'],'candidate issue')!=_stamp(opportunity['snapshot_time'],'snapshot')):
            raise ValueError('Alert issue or deadline clock differs from candidate')
        episode_key = alert['tenant_id'],alert['alert_episode_id']
        if (episode_key not in episodes or _trip(episodes[episode_key])!=_trip(alert)
                or alert['alert_episode_id']!=candidate['episode_id']):
            raise ValueError('Alert episode is outside its tenant/trip')
        if alert.get('alert_status') not in ALERT_STATUSES:
            raise ValueError('Unknown canonical alert status')
    indices = defaultdict(set)
    for attempt in attempts.values():
        akey = attempt['tenant_id'],attempt['alert_id']
        alert = alerts.get(akey)
        if alert is None:
            raise ValueError('Delivery attempt has no same-tenant alert')
        for field in ('candidate_id','alert_episode_id','trip_id','snapshot_id','promise_version_id',
                      'prediction_version','decision_threshold_version'):
            if attempt.get(field)!=alert.get(field):
                raise ValueError('Attempt identity differs from its frozen alert')
        if _stamp(attempt['deadline_at'],'attempt deadline')!=_stamp(alert['deadline_at'],'alert deadline'):
            raise ValueError('Attempt deadline differs from its original promise')
        index = attempt.get('attempt_index')
        if type(index) is not int or index<1 or index in indices[akey]:
            raise ValueError('Attempt indices must be distinct positive integers per alert')
        indices[akey].add(index)
        sent = _stamp(attempt['sent_at'],'attempt sent')
        if sent<_stamp(alert['created_at'],'alert creation'):
            raise ValueError('Attempt cannot precede candidate creation')
        status = attempt.get('delivery_status')
        if status not in ATTEMPT_STATUSES or attempt.get('receipt_type') not in ('service_delivered','none'):
            raise ValueError('Unknown delivery status or receipt type')
        service = attempt.get('service_at')
        known = attempt.get('receipt_known_at')
        delivered = attempt.get('delivered_at')
        if service is not None and _stamp(service,'service')<sent:
            raise ValueError('Service event cannot precede attempt')
        if known is not None and _stamp(known,'receipt known')<sent:
            raise ValueError('Receipt cannot precede attempt')
        if status=='delivered':
            if (delivered is None or known is None or attempt['receipt_type']!='service_delivered'
                    or not sent<=_stamp(delivered,'delivery')<=_stamp(known,'receipt known')
                    or (service is not None and _stamp(service,'service')!=_stamp(delivered,'delivery'))):
                raise ValueError('Confirmed delivery needs coherent service and receipt clocks')
        elif delivered is not None or attempt['receipt_type']!='none':
            raise ValueError('Non-delivered attempts cannot claim successful receipt')
        if attempt.get('received_or_opened_at') is not None:
            raise ValueError('P07 service delivery does not establish recipient opening')
    successes = [row for row in attempts.values() if _known_success(row,cutoff)]
    successes_by_alert = defaultdict(list)
    for row in successes:
        successes_by_alert[row['tenant_id'],row['alert_id']].append(row)
    for key,alert in alerts.items():
        confirmed = sorted(successes_by_alert[key], key=lambda row:
                           (_stamp(row['delivered_at'],'delivery'),row['delivery_id']))
        first = confirmed[0] if confirmed else None
        if (alert['alert_status']=='delivered')!=bool(first):
            raise ValueError('Alert delivery status differs from confirmed receipts known by cutoff')
        if (alert.get('first_delivery_id')!=(first['delivery_id'] if first else None)
                or alert.get('first_delivered_at')!=(first['delivered_at'] if first else None)):
            raise ValueError('Alert first-success binding differs from confirmed receipts')
        if type(alert.get('attempt_count')) is not int or alert['attempt_count']!=len(indices[key]):
            raise ValueError('Alert attempt count differs from retained attempts')
    episode_counts = Counter((row['tenant_id'],row['alert_episode_id']) for row in successes)
    for key,episode in episodes.items():
        count = episode_counts[key]
        if (type(episode.get('delivered_alert_count')) is not int or type(episode.get('duplicate_alert_count')) is not int
                or episode['delivered_alert_count']!=count or episode['duplicate_alert_count']!=max(count-1,0)):
            raise ValueError('Episode counts differ from confirmed receipts known by cutoff')
    return list(candidates.values()),list(alerts.values()),list(attempts.values()),list(episodes.values()),successes


def _timing(values,total):
    return {'observed':len(values),'missing':total-len(values),
            'minimum':min(values) if values else None,'median':median(values) if values else None,
            'maximum':max(values) if values else None,'unit':'seconds'}


def _summarize(opportunities,targets,candidates,alerts,attempts,episodes,successes,cutoff,minimum,sparse):
    monitored = {_trip(row) for row in opportunities.values()}
    eligible = [row for key,row in opportunities.items() if targets[key]['eligibility']=='eligible']
    positive = {_trip(row) for row in eligible if targets[_snapshot(row)]['delay_label']}
    negative = {_trip(row) for row in eligible if not targets[_snapshot(row)]['delay_label']}
    actionable = {_trip(row) for row in eligible if targets[_snapshot(row)]['delay_label']
                  and (_stamp(row['deadline_at'],'deadline')-_stamp(row['snapshot_time'],'snapshot')).total_seconds()>=minimum}
    by_alert = {(row['tenant_id'],row['alert_id']):row for row in alerts}
    details,first = [],set()
    for attempt in sorted(successes,key=lambda row:(_stamp(row['delivered_at'],'delivered'),row['tenant_id'],row['trip_id'],row['alert_id'],row['delivery_id'])):
        target = targets[_snapshot(attempt)]
        alert = by_alert[attempt['tenant_id'],attempt['alert_id']]
        at = _stamp(attempt['delivered_at'],'delivered')
        lead = (_stamp(attempt['deadline_at'],'deadline')-at).total_seconds()
        verified_arrival = ((target['eligibility']=='eligible' or target['reason']=='already_arrived_at_snapshot')
                            and target.get('actual_arrival_time') is not None and target.get('label_status')=='verified'
                            and target.get('arrival_time_precision')=='second' and target.get('label_known_at') is not None)
        retrospective = (_stamp(target['actual_arrival_time'],'arrival')-at).total_seconds() if verified_arrival else None
        post_arrival = retrospective is not None and retrospective<=0
        if post_arrival:
            classification = 'post_arrival'
        elif lead<minimum:
            classification = 'insufficient_lead'
        elif target['eligibility']=='eligible':
            classification = 'useful_true' if target['delay_label'] else 'known_false'
        else:
            classification = 'unresolved'
        trip = _trip(attempt)
        details.append({**{field:attempt[field] for field in ('tenant_id','trip_id','snapshot_id','candidate_id','alert_id','alert_episode_id','delivery_id','promise_version_id','deadline_at','delivered_at','receipt_known_at')},
                        'first_successful_delivery_for_trip':trip not in first,'classification':classification,
                        'target_eligibility':target['eligibility'],'target_reason':target['reason'],
                        'delay_label':target['delay_label'],'prospective_deadline_lead_seconds':lead,
                        'retrospective_arrival_lead_seconds':retrospective,
                        'candidate_to_delivery_seconds':(at-_stamp(alert['created_at'],'candidate creation')).total_seconds(),
                        'successful_attempt_latency_seconds':(at-_stamp(attempt['sent_at'],'sent')).total_seconds()})
        first.add(trip)
    first_rows = [row for row in details if row['first_successful_delivery_for_trip']]
    classes = Counter(row['classification'] for row in details)
    first_classes = Counter(row['classification'] for row in first_rows)
    useful = {_trip(row) for row in details if row['classification']=='useful_true'}
    observed_attempts = [row for row in attempts if _stamp(row['sent_at'],'sent')<=cutoff]
    status_counts = Counter(row['delivery_status'] if row.get('receipt_known_at') is not None
                            and _stamp(row['receipt_known_at'],'known')<=cutoff else 'queued' for row in observed_attempts)
    episode_success = Counter((row['tenant_id'],row['alert_episode_id']) for row in details)
    alert_success = Counter((row['tenant_id'],row['alert_id']) for row in details)
    eligible_first = sum(row['target_eligibility']=='eligible' for row in first_rows)
    eligible_successes = sum(row['target_eligibility']=='eligible' for row in details)
    assessable_first = sum(row['classification']!='unresolved' for row in first_rows)
    assessable_successes = sum(row['classification']!='unresolved' for row in details)
    counts = dict(monitored_trips=len(monitored),opportunities=len(opportunities),candidate_alerts=len(candidates),
        candidate_trips=len({_trip(row) for row in candidates}),risk_episodes=len(episodes),
        scored_trips=len({_trip(row) for row in eligible}),eligible_target_snapshots=len(eligible),
        excluded_target_snapshots=len(opportunities)-len(eligible),observed_positive_trips=len(positive),
        actionable_positive_trips=len(actionable),negative_trips=len(negative),mixed_promise_label_trips=len(positive&negative),
        retained_attempt_records=len(attempts),attempts=len(observed_attempts),
        retry_attempts=sum(row['attempt_index']>1 for row in observed_attempts),
        failed_attempts=status_counts['failed'],unknown_attempts=status_counts['unknown'],queued_attempts=status_counts['queued'],
        successful_confirmed_deliveries=len(details),delivered_alerts=len(alert_success),delivered_trips=len(first),
        episodes_with_delivery=len(episode_success),first_successful_deliveries=len(first_rows),
        repeated_deliveries_within_episode=sum(n-1 for n in episode_success.values()),
        additional_deliveries_per_trip=len(details)-len(first),additional_successes_same_alert=sum(n-1 for n in alert_success.values()),
        confirmed_useful_delivery_trips=len(useful),confirmed_useful_deliveries=classes['useful_true'],
        mature_eligible_deliveries=eligible_successes,mature_eligible_first_deliveries=eligible_first,
        known_assessable_deliveries=assessable_successes,known_assessable_first_deliveries=assessable_first,
        known_false_deliveries=classes['known_false'],unresolved_deliveries=classes['unresolved'],
        post_arrival_deliveries=classes['post_arrival'],insufficient_lead_deliveries=classes['insufficient_lead'],
        useful_first_deliveries=first_classes['useful_true'],known_false_first_deliveries=first_classes['known_false'],
        unresolved_first_deliveries=first_classes['unresolved'],post_arrival_first_deliveries=first_classes['post_arrival'],
        insufficient_lead_first_deliveries=first_classes['insufficient_lead'])
    return {'counts':counts,'denominators':{
        'delivered_trip_coverage':len(monitored),'candidate_alert_delivery_rate':len(candidates),
        'delivered_actionable_trip_recall':len(actionable),'conditional_first_delivery_useful_precision':assessable_first,
        'first_delivery_usefulness_bounds':len(first_rows),'first_delivery_target_coverage':len(first_rows),
        'first_delivery_assessable_coverage':len(first_rows),
        'successful_deliveries_per_100_monitored_trips':len(monitored)},
        'metrics':{
            'delivered_trip_coverage':_ratio(len(first),len(monitored)),
            'candidate_alert_delivery_rate':_ratio(len(alert_success),len(candidates)),
            'delivered_actionable_trip_recall':_ratio(len(useful),len(actionable)),
            'conditional_first_delivery_useful_precision':_ratio(first_classes['useful_true'],assessable_first),
            'first_delivery_usefulness_bounds':{'lower':_ratio(first_classes['useful_true'],len(first_rows)),
                                               'upper':_ratio(first_classes['useful_true']+first_classes['unresolved'],len(first_rows))},
            'first_delivery_target_coverage':_ratio(eligible_first,len(first_rows)),
            'first_delivery_assessable_coverage':_ratio(assessable_first,len(first_rows)),
            'successful_deliveries_per_100_monitored_trips':_ratio(100*len(details),len(monitored))},
        'lead_seconds':{population:{field:_timing([row[field] for row in rows if row[field] is not None],len(rows))
                                  for field in ('prospective_deadline_lead_seconds','retrospective_arrival_lead_seconds',
                                                'candidate_to_delivery_seconds','successful_attempt_latency_seconds')}
                       for population,rows in (('all_successful_deliveries',details),('first_successful_deliveries',first_rows))},
        'attempt_status_counts':dict(sorted(status_counts.items())),
        'alert_status_counts':dict(sorted(Counter(row['alert_status'] for row in alerts).items())),
        'delivery_class_counts':dict(sorted(classes.items())),
        'target_exclusion_reason_counts':dict(sorted(Counter(row['reason'] for row in targets.values() if row['eligibility']=='excluded').items())),
        'delivery_rows':details,'sparse':counts['scored_trips']<sparse}


def score_delivery(opportunities,targets,candidates,simulation,settings):
    """Count successful receipts as of cutoff, then join immutable alert targets."""
    label_cutoff,cutoff,minimum,sparse = _settings(settings)
    opportunities,targets = _population(opportunities,targets,label_cutoff)
    candidates,alerts,attempts,episodes,successes = _ledger(opportunities,candidates,simulation,cutoff)
    output = _summarize(opportunities,targets,candidates,alerts,attempts,episodes,successes,cutoff,minimum,sparse)
    output.update(scope='July_delivery_scenario_on_retained_policy_development',counts_as_of=utc(cutoff),
        label_cutoff_exclusive=utc(label_cutoff),independent_policy_test=False,final_test_evaluated=False,
        recipient_opening_evaluated=False,adoption_evaluated=False,actions_evaluated=False,operational_benefit_established=False,
        conditional_precision_definition='Useful first successes divided by all known-assessable first successes (classification other than unresolved). Known post-arrival or insufficient-lead deliveries are non-useful even when their original target is excluded.',
        bounds_definition='All-first-success usefulness identification bounds, not confidence intervals; only unresolved deliveries that could still be useful increase the upper bound.',
        target_coverage_definition='Target coverage uses original eligible labels; assessable coverage additionally includes known non-useful timing outcomes.',
        first_success_ordering=['delivered_at','tenant_id','trip_id','alert_id','delivery_id'],
        target_binding='Each successful attempt retains the original candidate snapshot promise; later promises do not replace that target.',
        tenant_slices=[])
    for tenant in sorted({key[0] for key in opportunities}):
        output['tenant_slices'].append({'tenant_id':tenant,**_summarize(
            {k:row for k,row in opportunities.items() if k[0]==tenant},{k:row for k,row in targets.items() if k[0]==tenant},
            [row for row in candidates if row['tenant_id']==tenant],[row for row in alerts if row['tenant_id']==tenant],
            [row for row in attempts if row['tenant_id']==tenant],[row for row in episodes if row['tenant_id']==tenant],
            [row for row in successes if row['tenant_id']==tenant],cutoff,minimum,sparse)})
    return output
