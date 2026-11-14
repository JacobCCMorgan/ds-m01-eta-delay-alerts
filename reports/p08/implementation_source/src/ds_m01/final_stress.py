"""Nine predeclared feature/transport sensitivities, selected without outcomes."""
from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal
import math

from .common import parse_ts
from .feature_transform import _rows
from .final_inputs import key, trip, require

SCENARIOS = ('S00_unmodified','S01_missing_position','S02_stale_position','S03_unseen_route_slot',
             'S04_natural_entity_holdouts','S05_stationary_window','S06_distance_scale',
             'S07_transport_degraded','S08_missing_and_degraded')
UNKNOWN_ROUTE = '__P08_UNSEEN_ROUTE__'
FEATURE_FIELDS = {'tenant_id','trip_id','snapshot_id','snapshot_time','route_version_id',
                  'feature_definition_version','numeric','categorical'}


def apply_stress(features,scenario_id,settings,transformers):
    """Retain all identities/clocks; derived overrides never rewrite raw telemetry.

    S05 consecutiveness refers to the complete chronological opportunity stream,
    including invalid-position rows. It does not bridge rows removed by filtering.
    """
    require(tuple(r['id'] for r in settings['stress']['scenarios'])==SCENARIOS, 'Stress scenario inventory changed')
    require(scenario_id in SCENARIOS, 'Unknown final stress scenario')
    features = list(features)
    require(all(set(r)==FEATURE_FIELDS for r in features), 'Stress accepts only label-free feature fields')
    require(all(isinstance(r['route_version_id'],str) and r['route_version_id'].strip() for r in features),
            'Stress route version identity must be explicit')
    versions={key(r):r['route_version_id'] for r in features}
    original = [dict(r,route_version_id=versions[key(r)]) for r in _rows(features)]
    result = deepcopy(original)
    if scenario_id=='S03_unseen_route_slot':
        require(all(UNKNOWN_ROUTE not in r['categorical']['route_id']['vocabulary'] for r in transformers),
                'Frozen unseen-route token already occurs in saved vocabulary')
    windows = {}
    if scenario_id=='S05_stationary_window':
        groups = defaultdict(list)
        for row in original:
            groups[trip(row)].append(row)
        for rows in groups.values():
            rows.sort(key=lambda r:(parse_ts(r['snapshot_time']),r['snapshot_id']))
            for start in range(len(rows)-2):
                window = rows[start:start+3]
                if (all(r['numeric']['elapsed_trip_seconds']>=3600 and r['categorical']['position_validity']=='valid'
                        and r['numeric']['remaining_distance_km'] is not None for r in window)
                        and (parse_ts(window[-1]['snapshot_time'])-parse_ts(window[0]['snapshot_time'])).total_seconds()>=3600):
                    remaining = window[0]['numeric']['remaining_distance_km']
                    require(all(remaining<=r['numeric']['route_distance_km'] for r in window),
                            'Stationary-window retained distance exceeds route bound')
                    windows.update({key(r):remaining for r in window})
                    break
    audit = []
    for before,row in zip(original,result):
        numeric,category = row['numeric'],row['categorical']
        applicable,reason = True,'fixed_transform'
        if scenario_id in ('S01_missing_position','S08_missing_and_degraded'):
            category['position_validity']='no_position'
            numeric.update(remaining_distance_km=None,gnss_speed_kmh=None,position_age_seconds=None)
        elif scenario_id=='S02_stale_position':
            applicable = numeric['elapsed_trip_seconds']>=1800 and category['position_validity']!='no_position'
            if applicable:
                category['position_validity']='low_confidence'
                numeric.update(remaining_distance_km=None,gnss_speed_kmh=None,position_age_seconds=1800.0)
            else:
                reason='no_original_position_or_elapsed_below_1800'
        elif scenario_id=='S03_unseen_route_slot':
            category['route_id']=UNKNOWN_ROUTE
        elif scenario_id=='S05_stationary_window':
            applicable = key(row) in windows
            if applicable:
                numeric.update(remaining_distance_km=windows[key(row)],gnss_speed_kmh=0.0,position_age_seconds=0.0)
                category['position_validity']='valid'
            else:
                reason='outside_first_qualifying_consecutive_window'
        elif scenario_id=='S06_distance_scale':
            for field in ('route_distance_km','remaining_distance_km'):
                if numeric[field] is not None:
                    numeric[field] = float(Decimal(str(numeric[field]))*Decimal('1.25'))
                    require(math.isfinite(numeric[field]), 'Scaled stress distance is nonfinite')
            require(numeric['route_distance_km']>0 and (numeric['remaining_distance_km'] is None
                    or 0<=numeric['remaining_distance_km']<=numeric['route_distance_km']), 'Scaled distance violates route bound')
        else:
            reason='unmodified_features'
        changed = before['numeric']!=numeric or before['categorical']!=category
        audit.append(dict(tenant_id=row['tenant_id'],trip_id=row['trip_id'],snapshot_id=row['snapshot_id'],
                          snapshot_time=row['snapshot_time'],scenario_id=scenario_id,applicable=applicable,
                          affected=changed,reason=reason,before={'numeric':deepcopy(before['numeric']),
                          'categorical':deepcopy(before['categorical'])},
                          after={'numeric':deepcopy(numeric),'categorical':deepcopy(category)}))
    result = [dict(r,route_version_id=versions[key(r)]) for r in _rows(result)]
    require([key(r) for r in result]==[key(r) for r in original], 'Stress changed opportunity inventory')
    delivery = deepcopy(settings['delivery'])
    degraded = scenario_id in ('S07_transport_degraded','S08_missing_and_degraded')
    if degraded:
        declaration = next(r for r in settings['stress']['scenarios'] if r['id']=='S07_transport_degraded')
        require(set(declaration['delivery_overrides'])=={'outcome_basis_points','latency_seconds','receipt_lag_seconds'},
                'Transport stress changes more than the frozen scenario ranges')
        delivery.update(deepcopy(declaration['delivery_overrides']))
    return {'features':result,'audit':audit,'delivery':delivery,
            'summary':{'scenario_id':scenario_id,'opportunities':len(result),
                       'trips':len({trip(r) for r in result}),'affected':sum(r['affected'] for r in audit),
                       'applicable':sum(r['applicable'] for r in audit),'not_applicable':sum(not r['applicable'] for r in audit),
                       'reason_counts':dict(sorted(Counter(r['reason'] for r in audit).items())),
                       'transport_changed':degraded,'source_observations_rewritten':False,'outcomes_used':False}}
