"""Trip-level policy development with visible unknowns and notification burden.

Labels never enter the simulator. A threshold is selected only from the frozen
grid using the declared all-trip budget and confirmed useful trip capture.
First-candidate conditional precision and all-first usefulness bounds have
different denominators; neither is an independent policy-test estimate.
"""
from collections import Counter, defaultdict
from collections.abc import Mapping
from decimal import Decimal
import hashlib
import json
from zoneinfo import ZoneInfo

from .common import parse_ts


STATUSES = frozenset(('admitted','disabled','terminal','inactive','unavailable','below_threshold',
                      'insufficient_lead','cooldown','capacity_exhausted'))
TARGET_STATUSES = frozenset(('verified','pending','cancelled','incomplete','lost','conflict'))


def _trip(row):
    return row['tenant_id'], row['trip_id']


def _key(row):
    return row['tenant_id'], row['snapshot_id']


def _id(value, name):
    if not isinstance(value,str) or not value.strip():
        raise ValueError(f'{name} must be nonempty text')
    return value


def _at(value, name):
    try:
        return parse_ts(value)
    except (TypeError,ValueError,AttributeError,OverflowError) as exc:
        raise ValueError(f'{name} must be timezone-aware') from exc


def _ratio(numerator, denominator):
    return numerator/denominator if denominator else None


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _settings(settings):
    try:
        policy, selection, metrics = settings['policy'], settings['selection'], settings['metrics']
        if (settings['protocol_version']!='p06-alert-policy-v1'
                or policy['threshold_grid']!=[f'{i/20:.2f}' for i in range(21)]
                or policy['minimum_action_lead_seconds']!=1800 or policy['repeat_suppression_seconds']!=1800
                or policy['daily_candidate_cap_per_tenant']!=2 or policy['max_candidates_per_100_monitored_trips']!=50
                or selection['minimum_scored_trips']!=10 or selection['minimum_actionable_positive_trips']!=2
                or selection['minimum_negative_trips']!=2 or selection['require_at_least_one_true_alert_trip'] is not True
                or selection['global_threshold'] is not True
                or selection['ordering']!=['maximize_confirmed_actionable_true_alert_trips','minimize_all_admitted_candidates','prefer_higher_threshold']
                or metrics['sparse_trip_threshold']!=20
                or settings['display_timezone']!='America/Los_Angeles'
                or settings['population']['label_cutoff_exclusive']!='2026-08-01T07:00:00Z'):
            raise ValueError('Policy metrics differ from the frozen P06 protocol')
    except (KeyError,TypeError) as exc:
        raise ValueError('Incomplete policy metric settings') from exc
    return policy, selection, metrics


def _index(rows, *, duplicate_identical=False, name):
    indexed={}
    for row in rows:
        if not isinstance(row,Mapping):
            raise ValueError(f'{name} records must be mappings')
        for field in ('tenant_id','trip_id','snapshot_id'):
            _id(row.get(field),field)
        key=_key(row)
        if key in indexed:
            if duplicate_identical and row==indexed[key]:
                continue
            raise ValueError(f'Duplicate or conflicting {name} identity')
        indexed[key]=row
    return indexed


def _usable(row):
    return (row['probability_status']=='available' and row['probability_calibration_status']=='fitted'
            and row['eta_status']=='available')


def _population(opportunities, targets, settings):
    policy,_,_=_settings(settings)
    opp=_index(opportunities,duplicate_identical=True,name='opportunity')
    labels=_index(targets,name='target')
    if opp.keys()!=labels.keys():
        raise ValueError('Targets must retain exactly all monitored opportunities')
    cutoff=_at(settings['population']['label_cutoff_exclusive'],'label cutoff')
    for key,row in opp.items():
        target=labels[key]
        for field in ('trip_id','promise_version_id'):
            _id(row.get(field),field)
            if target.get(field)!=row[field]:
                raise ValueError('Target identity differs from its monitored promise')
        at=_at(row.get('snapshot_time'),'snapshot time')
        deadline=_at(row.get('deadline_at'),'deadline')
        if (_at(target.get('deadline_at'),'target deadline')!=deadline
                or _at(target.get('label_cutoff_at'),'target cutoff')!=cutoff or at>=cutoff):
            raise ValueError('Target deadline or cutoff differs from the frozen opportunity')
        if target.get('snapshot_time') is not None and _at(target['snapshot_time'],'target snapshot time')!=at:
            raise ValueError('Target snapshot clock differs from monitored opportunity')
        if _at(row.get('promise_known_at'),'promise known at')>at:
            raise ValueError('Opportunity uses a promise unavailable at its snapshot')
        if row.get('probability_status') not in ('available','unavailable') or row.get('eta_status') not in ('available','unavailable'):
            raise ValueError('Unknown forecast availability')
        if row.get('probability_calibration_status') not in ('fitted','unsupported','unavailable'):
            raise ValueError('Unknown probability calibration support')
        if row['probability_status']=='available':
            p=row.get('probability')
            if not isinstance(p,str):
                raise ValueError('Policy probability must be canonical decimal text')
            try:
                value=Decimal(p)
                valid=value.is_finite() and 0<=value<=1 and p==f'{value:.6f}'
            except (ValueError,ArithmeticError):
                valid=False
            if not valid:
                raise ValueError('Policy probability must have six decimal places in [0,1]')
        elif row.get('probability') is not None:
            raise ValueError('Unavailable probability must be null')
        if row.get('trip_status_as_of') not in ('planned','in_transit','arrived','cancelled','incomplete','lost'):
            raise ValueError('Unknown snapshot-time trip status')
        eligible=target.get('eligibility')
        if eligible not in ('eligible','excluded'):
            raise ValueError('Unknown target eligibility')
        if (eligible=='eligible' and type(target.get('delay_label')) is not bool) or (eligible=='excluded' and target.get('delay_label') is not None):
            raise ValueError('Only eligible targets may carry Boolean labels')
        known=target.get('label_known_at')
        if known is not None and _at(known,'label known at')>=cutoff:
            raise ValueError('Outcome is not strictly known before the policy label cutoff')
        if target.get('label_status') is not None and target['label_status'] not in TARGET_STATUSES:
            raise ValueError('Unknown outcome label status')
        arrival=target.get('actual_arrival_time')
        if arrival is not None and (known is None or _at(arrival,'arrival')>_at(known,'label known at')):
            raise ValueError('Arrival cannot occur after its label became known')
        if eligible=='eligible':
            if (known is None or target.get('label_status')!='verified' or target.get('arrival_time_precision')!='second'
                    or arrival is None or _at(arrival,'arrival')<=at):
                raise ValueError('Scored targets require a mature verified future gate arrival with second precision')
            if target['delay_label']!=(_at(arrival,'arrival')>deadline):
                raise ValueError('Delay label does not match the snapshot-effective promise')
        if target.get('reason')=='already_arrived_at_snapshot':
            if (eligible!='excluded' or arrival is None or known is None or target.get('label_status')!='verified'
                    or target.get('arrival_time_precision')!='second' or _at(arrival,'arrival')>at):
                raise ValueError('Post-arrival exclusion requires verified past gate-arrival evidence')
    return opp,labels


def _validate_simulation(opp,simulation,settings):
    policy,_,_=_settings(settings)
    if not isinstance(simulation,Mapping):
        raise ValueError('Simulation must be a mapping')
    summary=simulation.get('summary')
    if not isinstance(summary,Mapping) or type(summary.get('enabled')) is not bool:
        raise ValueError('Simulation requires its explicit enabled state')
    version=_id(summary.get('policy_version'),'policy_version')
    decisions=_index(simulation.get('decisions',()),name='decision')
    candidates=_index(simulation.get('candidates',()),name='candidate')
    if decisions.keys()!=opp.keys():
        raise ValueError('Simulation must retain one decision for every opportunity')
    admitted={key for key,row in decisions.items() if row.get('candidate_status')=='admitted'}
    if admitted!=candidates.keys():
        raise ValueError('Candidate ledger differs from admitted decisions')
    if (summary.get('opportunities')!=len(opp) or summary.get('admitted_candidates')!=len(candidates)
            or summary.get('monitored_trips')!=len({_trip(r) for r in opp.values()})):
        raise ValueError('Simulation summary differs from complete opportunity/candidate populations')
    episodes={}
    for episode in simulation.get('episodes',()):
        eid=_id(episode.get('episode_id'),'episode_id')
        if (eid in episodes or _trip(episode) not in {_trip(r) for r in opp.values()}
                or episode.get('policy_version')!=version):
            raise ValueError('Duplicate or foreign risk episode')
        episodes[eid]=episode
    ids=set()
    for key,decision in decisions.items():
        original=opp[key]
        if decision.get('policy_version')!=version:
            raise ValueError('Simulation mixes decision policy versions')
        for field in ('tenant_id','trip_id','snapshot_id','promise_version_id','prediction_version'):
            if decision.get(field)!=original.get(field):
                raise ValueError('Decision identity or prediction version differs from opportunity')
        for field in ('snapshot_time','deadline_at'):
            if _at(decision.get(field),field)!=_at(original.get(field),field):
                raise ValueError('Decision clock differs from opportunity')
        if decision.get('probability')!=original.get('probability') or decision.get('candidate_status') not in STATUSES:
            raise ValueError('Decision probability or candidate status is invalid')
        if (decision['candidate_status']=='disabled')==summary['enabled']:
            raise ValueError('Disabled decision disagrees with explicit policy state')
        if decision.get('lead_seconds')!=(_at(original['deadline_at'],'deadline')-_at(original['snapshot_time'],'snapshot')).total_seconds():
            raise ValueError('Decision lead time differs from frozen promise')
        if key not in admitted:
            if decision.get('candidate_id') is not None:
                raise ValueError('Non-admitted opportunity cannot have a candidate ID')
            continue
        candidate=candidates[key]
        cid=_id(candidate.get('candidate_id'),'candidate_id')
        eid=_id(candidate.get('episode_id'),'episode_id')
        if cid in ids or cid!=decision.get('candidate_id') or eid!=decision.get('episode_id') or eid not in episodes:
            raise ValueError('Candidate/episode identity is duplicated or inconsistent')
        ids.add(cid)
        for field in ('tenant_id','trip_id','snapshot_id','promise_version_id','prediction_version','policy_version','probability'):
            if candidate.get(field)!=decision.get(field):
                raise ValueError('Admitted candidate differs from its decision')
        for field in ('snapshot_time','deadline_at'):
            if _at(candidate.get(field),field)!=_at(original.get(field),field):
                raise ValueError('Candidate clock differs from frozen opportunity')
        if (_trip(episodes[eid])!=_trip(candidate) or episodes[eid].get('policy_version')!=candidate.get('policy_version')
                or _at(candidate.get('simulated_issue_at'),'candidate issue')!=_at(original['snapshot_time'],'snapshot')):
            raise ValueError('Candidate episode or issue clock differs from opportunity')
        if not _usable(original) or original['trip_status_as_of']!='in_transit' or decision['lead_seconds']<policy['minimum_action_lead_seconds']:
            raise ValueError('Candidate violates forecast, active-trip or action-window support')
    by_trip=defaultdict(list)
    by_day=Counter()
    by_episode=Counter(r['episode_id'] for r in candidates.values())
    zone=ZoneInfo(settings['display_timezone'])
    for candidate in candidates.values():
        at=_at(candidate['simulated_issue_at'],'candidate issue')
        by_trip[_trip(candidate)].append(at)
        by_day[candidate['tenant_id'],at.astimezone(zone).date()]+=1
    if any(n>policy['daily_candidate_cap_per_tenant'] for n in by_day.values()):
        raise ValueError('Simulation exceeds the frozen tenant/day capacity')
    for times in by_trip.values():
        times.sort()
        if any((b-a).total_seconds()<policy['repeat_suppression_seconds'] for a,b in zip(times,times[1:])):
            raise ValueError('Simulation violates trip cooldown across episodes')
    if any(episode.get('candidate_count')!=by_episode[eid] for eid,episode in episodes.items()):
        raise ValueError('Episode counts disagree with candidate ledger')
    return decisions,candidates,episodes


def _candidate_class(target):
    if target['eligibility']=='eligible':
        return 'true' if target['delay_label'] else 'false'
    if target['reason']=='already_arrived_at_snapshot':
        return 'post_arrival'
    return 'unresolved'


def _summarize(opp,labels,decisions,candidates,episodes,settings):
    rows=list(opp.values())
    all_trips={_trip(r) for r in rows}
    eligible=[r for r in rows if labels[_key(r)]['eligibility']=='eligible']
    positive={_trip(r) for r in eligible if labels[_key(r)]['delay_label']}
    negative={_trip(r) for r in eligible if not labels[_key(r)]['delay_label']}
    actionable_positive={_trip(r) for r in eligible if labels[_key(r)]['delay_label'] and
                         (_at(r['deadline_at'],'deadline')-_at(r['snapshot_time'],'snapshot')).total_seconds()>=1800}
    classified=Counter(_candidate_class(labels[key]) for key in candidates)
    true_trips={_trip(r) for key,r in candidates.items() if _candidate_class(labels[key])=='true'}
    first={}
    for candidate in sorted(candidates.values(),key=lambda r:(_at(r['simulated_issue_at'],'candidate issue'),r['tenant_id'],r['trip_id'],r['snapshot_id'])):
        first.setdefault(_trip(candidate),candidate)
    first_class=Counter(_candidate_class(labels[_key(r)]) for r in first.values())
    episode_counts=Counter(r['episode_id'] for r in candidates.values())
    admitted_count=len(candidates)
    first_count=len(first)
    mature_first=first_class['true']+first_class['false']
    counts={
        'monitored_trips':len(all_trips),'opportunities':len(rows),
        'eligible_target_snapshots':len(eligible),'excluded_target_snapshots':len(rows)-len(eligible),
        'scored_trips':len({_trip(r) for r in eligible}),
        'usable_forecast_label_trips':len({_trip(r) for r in eligible if _usable(r)}),
        'observed_positive_trips':len(positive),'actionable_positive_trips':len(actionable_positive),
        'negative_trips':len(negative),'mixed_promise_label_trips':len(positive & negative),
        'usable_forecast_opportunities':sum(_usable(r) for r in rows),
        'probability_unavailable_opportunities':sum(r['probability_status']=='unavailable' for r in rows),
        'unsupported_calibration_opportunities':sum(r['probability_calibration_status']=='unsupported' for r in rows),
        'eta_unavailable_opportunities':sum(r['eta_status']=='unavailable' for r in rows),
        'all_admitted_candidates':admitted_count,'alerted_trips':first_count,'risk_episodes':len(episodes),
        'episodes_with_candidates':len(episode_counts),
        'repeated_candidates_within_episode':sum(n-1 for n in episode_counts.values()),
        'additional_candidates_per_trip':admitted_count-first_count,
        'confirmed_true_candidates':classified['true'],'known_false_candidates':classified['false'],
        'unresolved_candidates':classified['unresolved'],'retrospectively_post_arrival_candidates':classified['post_arrival'],
        'confirmed_actionable_true_alert_trips':len(true_trips),
        'first_candidates':first_count,'confirmed_true_first_candidates':first_class['true'],
        'known_false_first_candidates':first_class['false'],'unresolved_first_candidates':first_class['unresolved'],
        'retrospectively_post_arrival_first_candidates':first_class['post_arrival'],
    }
    return {
        'counts':counts,
        'denominators':{'actionable_trip_recall':len(actionable_positive),'all_observed_positive_trip_recall':len(positive),
                        'conditional_first_candidate_precision':mature_first,'candidate_target_coverage':admitted_count,
                        'first_candidate_target_coverage':first_count,'first_candidate_precision_bounds':first_count,
                        'all_candidates_per_100_monitored_trips':len(all_trips)},
        'metrics':{'actionable_trip_recall':_ratio(len(true_trips),len(actionable_positive)),
                   'all_observed_positive_trip_recall':_ratio(len(true_trips),len(positive)),
                   'conditional_first_candidate_precision':_ratio(first_class['true'],mature_first),
                   'candidate_target_coverage':_ratio(classified['true']+classified['false'],admitted_count),
                   'first_candidate_target_coverage':_ratio(mature_first,first_count),
                   'first_candidate_precision_bounds':{'lower':_ratio(first_class['true'],first_count),
                                                       'upper':_ratio(first_class['true']+first_class['unresolved'],first_count)},
                   'all_candidates_per_100_monitored_trips':_ratio(100*admitted_count,len(all_trips)),
                   'alerted_trip_fraction':_ratio(first_count,len(all_trips))},
        'decision_status_counts':dict(sorted(Counter(r['candidate_status'] for r in decisions.values()).items())),
        'decision_reason_counts':dict(sorted(Counter(str(r.get('reason')) for r in decisions.values()).items())),
        'target_exclusion_reason_counts':dict(sorted(Counter(str(r.get('reason')) for r in labels.values() if r['eligibility']=='excluded').items())),
        'candidate_unresolved_reason_counts':dict(sorted(Counter(labels[key]['reason'] for key in candidates if _candidate_class(labels[key])=='unresolved').items())),
        'sparse':counts['scored_trips']<settings['metrics']['sparse_trip_threshold'],
    }


def evaluate_policy(opportunities, targets, simulation, settings):
    """Keep all monitored trips; evaluate only after outcome-free simulation."""
    opp,labels=_population(opportunities,targets,settings)
    decisions,candidates,episodes=_validate_simulation(opp,simulation,settings)
    output=_summarize(opp,labels,decisions,candidates,episodes,settings)
    output.update(scope='July_policy_development_reused_P05_assessment',independent_policy_test=False,
                  final_test_evaluated=False,operational_benefit_established=False,
                  first_candidate_precision_bounds_interpretation='All-first-candidate usefulness identification bounds, not confidence intervals. Unresolved first candidates can be useful or non-useful; known post-arrival first candidates are non-useful. Conditional precision instead conditions on mature eligible first targets.',
                  trip_weighting='Each tenant/trip contributes at most one recall hit; all admitted candidates count toward burden.',
                  scored_trip_definition='At least one mature eligible label, irrespective forecast availability.',
                  tenant_slices=[])
    for tenant in sorted({k[0] for k in opp}):
        output['tenant_slices'].append({'tenant_id':tenant,**_summarize(
            {k:r for k,r in opp.items() if k[0]==tenant},{k:r for k,r in labels.items() if k[0]==tenant},
            {k:r for k,r in decisions.items() if k[0]==tenant},{k:r for k,r in candidates.items() if k[0]==tenant},
            {k:r for k,r in episodes.items() if r['tenant_id']==tenant},settings)})
    return output


def select_policy(opportunities, targets, settings):
    """Evaluate all frozen trials, then choose once under the research budget."""
    from .alert_policy import make_rule, simulate_policy

    policy,selection,_=_settings(settings)
    opp,labels=_population(opportunities,targets,settings)
    rows=[opp[key] for key in sorted(opp)]
    target_rows=[labels[key] for key in sorted(labels)]
    trials=[]
    simulations={}
    for threshold in [*policy['threshold_grid'],None]:
        rule=make_rule(settings,threshold,enabled=threshold is not None)
        simulation=simulate_policy(rows,rule)
        measured=evaluate_policy(rows,target_rows,simulation,settings)
        counts=measured['counts']
        support_checks={'minimum_scored_trips':counts['scored_trips']>=selection['minimum_scored_trips'],
                        'minimum_actionable_positive_trips':counts['actionable_positive_trips']>=selection['minimum_actionable_positive_trips'],
                        'minimum_negative_trips':counts['negative_trips']>=selection['minimum_negative_trips']}
        budget=100*counts['all_admitted_candidates']<=policy['max_candidates_per_100_monitored_trips']*counts['monitored_trips']
        captured=counts['confirmed_actionable_true_alert_trips']>0
        eligible=threshold is not None and all(support_checks.values()) and budget and captured
        trial_id='disabled_reference' if threshold is None else 'threshold_'+threshold
        trials.append({'trial_id':trial_id,'rule':rule,'metrics':measured,
                       'feasibility':{'label_support':all(support_checks.values()),'support_checks':support_checks,
                                      'global_budget':budget,'confirmed_positive_capture':captured,'selectable':eligible},
                       'selected':False})
        simulations[trial_id]=simulation
    feasible=[trial for trial in trials if trial['feasibility']['selectable']]
    if feasible:
        chosen=min(feasible,key=lambda trial:(-trial['metrics']['counts']['confirmed_actionable_true_alert_trips'],
                                              trial['metrics']['counts']['all_admitted_candidates'],
                                              -Decimal(trial['rule']['threshold'])))
        status='selected'
    else:
        chosen=trials[-1]
        status='disabled_insufficient_support' if not chosen['feasibility']['label_support'] else 'disabled_no_feasible_capture'
    chosen['selected']=True
    return {'rule':chosen['rule'],'status':status,'trials':trials,'selected_metrics':chosen['metrics'],
            'simulation':simulations[chosen['trial_id']],
            'selection_manifest':{'protocol_version':settings['protocol_version'],'settings_content_sha256':_digest(settings),
                                  'opportunity_records_sha256':_digest(rows),'target_records_sha256':_digest(target_rows),
                                  'selected_trial_id':chosen['trial_id'],'trials_retained':len(trials),
                                  'threshold_grid':policy['threshold_grid'],'selection_ordering':selection['ordering'],
                                  'support_checks':chosen['feasibility']['support_checks'],
                                  'budget_max_candidates_per_100_monitored_trips':policy['max_candidates_per_100_monitored_trips'],
                                  'daily_candidate_cap_per_tenant':policy['daily_candidate_cap_per_tenant'],
                                  'budget_basis':policy['budget_basis'],'assessment_reused_for_policy_development':True,
                                  'model_or_calibrator_reselected':False,'final_test_evaluated':False,
                                  'operational_approval':False,'no_post_sweep_grid_or_budget_changes':True}}
