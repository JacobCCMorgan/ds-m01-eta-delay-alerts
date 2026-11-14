"""Deterministic, explicitly synthetic M1 source data; no predictions or features.

The 120-trip trial is a stable subset of the 1,800-trip design.  Outcome
truth and scenario labels are written only below validation/, never into
telemetry or a model-input table.  See docs/generator_assumptions.md.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .common import parse_ts, stable_seed, utc


VERSION = "ds-m01-synthetic-generator-1.0.1"
ZONE = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc
SCENARIOS = (
    "normal", "late_arrival", "cancelled", "gps_dropout", "late_gps",
    "promise_revision", "device_swap", "arrival_report_late",
    "outcome_revision", "unfinished", "invalid_position",
)
ACTIVE_TABLES = (
    "core_tenant", "core_vehicle", "core_device",
    "core_vehicle_device_assignment", "eta_trip", "eta_facility_version",
    "eta_route_version", "eta_trip_plan_version", "eta_promise_version",
    "eta_trip_event", "eta_trip_outcome", "telemetry_event",
    "provenance_record",
)
PRIMARY_KEYS = {
    "core_tenant": "tenant_id", "core_vehicle": "vehicle_id",
    "core_device": "device_id", "core_vehicle_device_assignment": "assignment_id",
    "eta_trip": "trip_id", "eta_facility_version": "facility_version_id",
    "eta_route_version": "route_version_id", "eta_trip_plan_version": "trip_version_id",
    "eta_promise_version": "promise_version_id", "eta_trip_event": "trip_event_id",
    "eta_trip_outcome": "outcome_version_id", "telemetry_event": "canonical_event_id",
}
NO_PROVENANCE_COLUMN = {"core_tenant", "core_vehicle", "core_device"}

# Fictional site positions in California, grouped four per tenant.  These are
# synthetic scenario coordinates, not claimed customer sites or road routes.
SITE_COORDINATES = (
    (34.060, -118.180), (34.085, -117.750), (33.940, -117.370), (34.130, -117.280),
    (35.390, -119.050), (36.300, -119.350), (36.760, -119.820), (36.940, -120.080),
    (38.600, -121.460), (37.980, -121.300), (37.660, -121.010), (37.370, -121.880),
)


def _distance_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371.0088 * 2 * math.asin(math.sqrt(h))


def _stamp(dt):
    return utc(dt)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _plan(config):
    """Build all trips first, so sampling cannot alter entities or histories."""
    seed = int(config.get("seed", 20261021))
    namespace = config.get("namespace", "ds-m01-2026-v1")

    def rng(*tokens):
        return random.Random(stable_seed(seed, namespace, *tokens))

    plans = []
    for month in range(1, 10):
        vehicle_uses = Counter()
        permutations = {}
        for local_ordinal in range(200):
            block = local_ordinal // 30
            if block not in permutations:
                permutation = list(range(30))
                rng("vehicles", month, block).shuffle(permutation)
                permutations[block] = permutation
            vehicle_index = permutations[block][local_ordinal % 30]
            tenant_index, within_tenant = divmod(vehicle_index, 10)
            occurrence = vehicle_uses[vehicle_index]
            vehicle_uses[vehicle_index] += 1
            ordinal = (month-1)*200 + local_ordinal
            tenant = f"T{tenant_index+1:02d}"
            trip = f"{tenant}-TRIP-{ordinal+1:06d}"
            route_index = tenant_index*4 + (ordinal//15) % 4
            origin_index = route_index
            destination_index = tenant_index*4 + (route_index % 4 + 1) % 4
            trip_rng = rng("trip", ordinal)
            # At most seven trips per vehicle/month; every trip occupies a
            # distinct day. This is deliberately a sparse engineering trial.
            departure = datetime(2026, month, 2+3*occurrence,
                                 6+within_tenant % 5, trip_rng.randrange(20), tzinfo=ZONE)
            planned_distance = round(_distance_km(SITE_COORDINATES[origin_index],
                                                  SITE_COORDINATES[destination_index]), 3)
            plans.append({
                "ordinal": ordinal, "month": month, "tenant_id": tenant,
                "vehicle_id": f"{tenant}-V{within_tenant+1:02d}",
                "default_device": f"{tenant}-D{within_tenant+1:02d}",
                "trip_id": trip, "scenario": SCENARIOS[ordinal % len(SCENARIOS)],
                "route_id": f"ROUTE-{route_index+1:02d}",
                "route_version_id": f"ROUTE-{route_index+1:02d}-V1",
                "origin_version_id": f"SITE-{origin_index+1:02d}-V1",
                "destination_version_id": f"SITE-{destination_index+1:02d}-V1",
                "origin": SITE_COORDINATES[origin_index],
                "destination": SITE_COORDINATES[destination_index],
                "distance_km": planned_distance, "planned_departure": departure,
            })

    # Device-service fixtures use staggered appointments. One spare device per
    # tenant is sufficient and never installed on two vehicles simultaneously.
    swap_groups = defaultdict(list)
    for p in plans:
        if p["scenario"] == "device_swap":
            swap_groups[(p["tenant_id"], p["planned_departure"].date())].append(p)
    for group in swap_groups.values():
        for index, p in enumerate(sorted(group, key=lambda x: x["ordinal"])):
            p["planned_departure"] = p["planned_departure"].replace(hour=4+6*index, minute=0)

    for p in plans:
        r = rng("movement", p["ordinal"])
        gate_rng = rng("independent-gate", p["ordinal"])
        departure = p["planned_departure"] + timedelta(minutes=r.randint(-3, 12))
        base_minutes = p["distance_km"] / 58 * 60 + 14
        motion_minutes = max(25, base_minutes * r.uniform(.86, 1.25) + r.uniform(0, 12))
        if p["scenario"] == "late_arrival":
            motion_minutes += r.uniform(60, 120)
        if p["scenario"] == "device_swap":
            motion_minutes += 10
        motion_minutes = round(motion_minutes, 3)
        # Gate confirmation is a distinct simulated source with an independent
        # gate-queue draw. It is never inferred by applying a GPS geofence.
        gate_queue_minutes = gate_rng.uniform(3, 24)
        arrival = departure + timedelta(minutes=motion_minutes+gate_queue_minutes)
        cancellation = departure + timedelta(minutes=25) if p["scenario"] == "cancelled" else None
        p.update({
            "departure": departure, "motion_minutes": motion_minutes,
            "arrival": None if cancellation else arrival, "cancellation": cancellation,
            "physical_arrival": None if cancellation else arrival,
            "service_start": None if cancellation else arrival+timedelta(minutes=gate_rng.randint(6, 35)),
            "promise_start": p["planned_departure"]+timedelta(minutes=base_minutes-5),
            "promise_end": p["planned_departure"]+timedelta(minutes=base_minutes+35),
        })
        p["delivery_completed"] = None if cancellation else p["service_start"]+timedelta(minutes=gate_rng.randint(20, 65))
        if p["scenario"] == "device_swap":
            p["swap_at"] = departure+timedelta(minutes=motion_minutes/2)
            p["swap_back_at"] = arrival+timedelta(minutes=15)
            p["swap_device"] = f"{p['tenant_id']}-D11"

    # Independent schedule checks do not depend on the chosen trial subset.
    by_vehicle = defaultdict(list)
    spare_intervals = defaultdict(list)
    for p in plans:
        by_vehicle[p["vehicle_id"]].append((p["departure"], p["physical_arrival"] or p["cancellation"]))
        if "swap_at" in p:
            spare_intervals[p["swap_device"]].append((p["swap_at"], p["swap_back_at"]))
    for key, intervals in list(by_vehicle.items())+list(spare_intervals.items()):
        intervals.sort()
        for previous, following in zip(intervals, intervals[1:]):
            if previous[1] >= following[0]:
                raise ValueError(f"Overlapping physical schedule for {key}")
    return plans, rng


def _sync_close(handles):
    """Flush Python buffers and request durable writes before closing files."""
    errors = []
    for handle in handles:
        try:
            handle.flush()
            os.fsync(handle.fileno())
        except OSError as exc:
            errors.append(exc)
        finally:
            handle.close()
    if errors:
        raise errors[0]


def _inspect_jsonl(path, expected_count):
    """Read persisted bytes, rejecting truncated final buffers or blank rows."""
    digest = hashlib.sha256()
    count = 0
    with Path(path).open("rb") as handle:
        for line in handle:
            if not line.endswith(b"\n") or not line.strip():
                raise IOError(f"Incomplete or blank JSONL record in {path}")
            count += 1
            digest.update(line)
    if count != expected_count:
        raise IOError(f"Persisted row count differs for {path}: expected {expected_count}, observed {count}")
    return {"rows": count, "sha256": digest.hexdigest(), "bytes": Path(path).stat().st_size}


def _sync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _generate_staged(out_dir: Path, config: dict) -> dict:
    """Write canonical raw tables and isolated validation fixtures.

    Required config: generated_at (actual UTC run timestamp). Supported trial
    sizes: 120 and 1800. Also accepts sample_size=0 for the complete 1800 design.
    Returns only table row counts; it does not create snapshots, labels, splits,
    predictions, or a fitted model.
    """
    out_dir = Path(out_dir)
    generated_at = _stamp(parse_ts(config["generated_at"]))
    cutoff = parse_ts(config.get("extract_as_of", "2026-10-15T07:00:00Z"))
    sample_size = int(config.get("sample_size", 120))
    if sample_size not in (0, 120, 1800):
        raise ValueError("sample_size must be 120 (fixed subset) or 1800/0 (full design)")
    plans, rng = _plan(config)
    selected = [p for p in plans if sample_size != 120 or p["ordinal"] % 15 == 0]
    raw_dir = out_dir / "data" / "raw"
    validation_dir = out_dir / "validation"
    raw_dir.mkdir(parents=True, exist_ok=True)
    validation_dir.mkdir(parents=True, exist_ok=True)
    handles = {table: (raw_dir / f"{table}.jsonl").open("w", encoding="utf-8") for table in ACTIVE_TABLES}
    truth_handle = (validation_dir / "latent_truth.jsonl").open("w", encoding="utf-8")
    counts = Counter({table: 0 for table in ACTIVE_TABLES})
    namespace = config.get("namespace", "ds-m01-2026-v1")
    dataset_id = f"{namespace}:{VERSION}"
    scenario_counts = Counter()
    start = datetime(2025, 12, 1, tzinfo=ZONE)

    def append(table, row):
        counts[table] += 1
        handles[table].write(_json(row)+"\n")

    def emit(table, row, *, occurred=None, available=None, scenario="shared_world", parents=(),
             null_reasons=None, record_state="active"):
        """Every raw record gets a separately addressable provenance row."""
        row = dict(row)
        key = {"tenant_id": row["tenant_id"], PRIMARY_KEYS[table]: row[PRIMARY_KEYS[table]]}
        pid = "PRV-"+hashlib.sha256((table+"\x1f"+_json(key)).encode()).hexdigest()[:24]
        if table not in NO_PROVENANCE_COLUMN:
            row["provenance_id"] = pid
        line_number = counts[table]+1
        reference = f"data/raw/{table}.jsonl#line={line_number}"
        local_time = (occurred or available or start).astimezone(ZONE)
        value_states = {}
        for name, value in row.items():
            if value is None:
                state, reason = (null_reasons or {}).get(name, ("not_applicable", "Not applicable to this explicitly synthetic source record."))
                value_states[f"{table}.{name}"] = {"state": state, "reason": reason}
        # The raw record is uniformly synthetic. Per-field overrides are only
        # needed for mixed/derived records; none are emitted by this module.
        prov = {
            "provenance_id": pid, "source_system_id": f"synthetic:{table}",
            "source_record_id": str(row[PRIMARY_KEYS[table]]), "record_version": "1",
            "record_state": record_state, "event_time": _stamp(occurred) if occurred else None,
            "available_time": _stamp(available or occurred or start),
            "retrieved_at": generated_at, "extract_as_of": _stamp(cutoff),
            "raw_record_reference": reference, "origin_type": "synthetic",
            "source_dataset_id": dataset_id, "synthetic_scenario_id": scenario,
            "value_states": value_states, "source_timezone": "America/Los_Angeles",
            "source_utc_offset_minutes": int(local_time.utcoffset().total_seconds()/60),
            "tenant_id": row["tenant_id"], "field_origins": {},
            "availability_basis": "simulated", "availability_evidence": f"{VERSION}:{scenario}",
            "canonical_table": table, "canonical_record_key": key,
            "parent_provenance_ids": list(parents),
        }
        append(table, row)
        append("provenance_record", prov)
        return pid

    try:
        for ti in range(3):
            tenant = f"T{ti+1:02d}"
            emit("core_tenant", {"tenant_id": tenant, "tenant_name": f"Fictional Portfolio Fleet {ti+1}",
                                "report_timezone": "America/Los_Angeles", "base_currency": "USD"}, available=start)
            for vi in range(10):
                emit("core_vehicle", {"tenant_id": tenant, "vehicle_id": f"{tenant}-V{vi+1:02d}",
                                      "vin_raw": None, "vin": None, "vehicle_class": "heavy_truck",
                                      "fuel_type": "diesel"}, available=start,
                     null_reasons={"vin": ("not_applicable", "Fictional vehicle; no real VIN asserted."),
                                   "vin_raw": ("not_applicable", "Fictional vehicle; no real VIN asserted.")})
            for di in range(12):
                emit("core_device", {"tenant_id": tenant, "device_id": f"{tenant}-D{di+1:02d}",
                                     "device_model": "PortfolioTelemetry-SIM-V1"}, available=start)
            for ri in range(4):
                index = ti*4+ri
                dest_index = ti*4+(ri+1) % 4
                lat, lon = SITE_COORDINATES[index]
                emit("eta_facility_version", {
                    "tenant_id": tenant, "facility_version_id": f"SITE-{index+1:02d}-V1",
                    "facility_id": f"SITE-{index+1:02d}", "timezone": "America/Los_Angeles",
                    "arrival_definition": "gate_crossing", "latitude_deg": lat, "longitude_deg": lon,
                    "valid_from": _stamp(start), "valid_to": None, "known_from": _stamp(start), "known_to": None,
                }, available=start)
                emit("eta_route_version", {
                    "tenant_id": tenant, "route_version_id": f"ROUTE-{index+1:02d}-V1",
                    "route_id": f"ROUTE-{index+1:02d}", "origin_facility_version_id": f"SITE-{index+1:02d}-V1",
                    "destination_facility_version_id": f"SITE-{dest_index+1:02d}-V1",
                    "planned_distance_km": round(_distance_km(SITE_COORDINATES[index], SITE_COORDINATES[dest_index]), 3),
                    "valid_from": _stamp(start), "valid_to": None, "known_at": _stamp(start),
                }, available=start)

        # Emit complete shared assignment history, independent of sample size.
        # An interval initially has an open end. At the recorded switch it is
        # superseded by a closed historical version; old knowledge is retained.
        swaps = defaultdict(list)
        for p in plans:
            if "swap_at" in p:
                swaps[p["vehicle_id"]].append(p)
        for ti in range(3):
            tenant = f"T{ti+1:02d}"
            for vi in range(10):
                vehicle = f"{tenant}-V{vi+1:02d}"
                default_device = f"{tenant}-D{vi+1:02d}"
                switches = []
                for p in sorted(swaps[vehicle], key=lambda x: x["swap_at"]):
                    switches.extend([(p["swap_at"], p["swap_device"], p["trip_id"]),
                                     (p["swap_back_at"], default_device, p["trip_id"])])
                boundaries = [(start, default_device, "shared_world"), *switches]
                for index, (begin, device, cause) in enumerate(boundaries):
                    end = boundaries[index+1][0] if index+1 < len(boundaries) else None
                    known_begin = begin if index == 0 else begin+timedelta(minutes=2)
                    known_end = end+timedelta(minutes=2) if end else None
                    base = {"tenant_id": tenant, "vehicle_id": vehicle, "device_id": device,
                            "valid_from": _stamp(begin), "resolution_status": "resolved"}
                    emit("core_vehicle_device_assignment", {
                        **base, "assignment_id": f"{vehicle}-ASSIGN-{index:04d}-OPEN", "valid_to": None,
                        "known_from": _stamp(known_begin), "known_to": _stamp(known_end) if known_end else None,
                        "valid_to_state": "open", "known_to_state": "known" if known_end else "open",
                    }, occurred=begin, available=known_begin, scenario=f"assignment:{cause}")
                    if end:
                        emit("core_vehicle_device_assignment", {
                            **base, "assignment_id": f"{vehicle}-ASSIGN-{index:04d}-CLOSED", "valid_to": _stamp(end),
                            "known_from": _stamp(known_end), "known_to": None,
                            "valid_to_state": "known", "known_to_state": "open",
                        }, occurred=begin, available=known_end, scenario=f"assignment:{cause}", record_state="corrected")

        for p in selected:
            scenario = p["scenario"]
            scenario_counts[scenario] += 1
            tid = p["trip_id"]
            tenant = p["tenant_id"]
            dep = p["departure"]
            plan_known = p["planned_departure"]-timedelta(days=2)
            scenario_id = f"{namespace}:{tid}:{scenario}"
            r = rng("observed-position", p["ordinal"])
            emit("eta_trip", {"tenant_id": tenant, "trip_id": tid,
                              "registered_at": _stamp(plan_known-timedelta(hours=1))},
                 occurred=plan_known-timedelta(hours=1), available=plan_known-timedelta(hours=1), scenario=scenario_id)
            emit("eta_trip_plan_version", {
                "tenant_id": tenant, "trip_version_id": f"{tid}-PLAN-V1", "trip_id": tid,
                "vehicle_id": p["vehicle_id"], "route_version_id": p["route_version_id"],
                "planned_departure_at": _stamp(p["planned_departure"]),
                "origin_facility_version_id": p["origin_version_id"],
                "destination_facility_version_id": p["destination_version_id"],
                "plan_known_at": _stamp(plan_known), "supersedes_trip_version_id": None,
            }, occurred=plan_known, available=plan_known, scenario=scenario_id)
            promise_known = plan_known+timedelta(minutes=15)
            emit("eta_promise_version", {
                "tenant_id": tenant, "promise_version_id": f"{tid}-PROMISE-V1", "trip_id": tid,
                "window_start_at": _stamp(p["promise_start"]), "window_end_at": _stamp(p["promise_end"]),
                "known_at": _stamp(promise_known), "supersedes_promise_version_id": None, "change_reason": "initial",
            }, occurred=promise_known, available=promise_known, scenario=scenario_id)
            if scenario == "promise_revision":
                revision_known = dep+timedelta(minutes=min(35, p["motion_minutes"]/2))
                emit("eta_promise_version", {
                    "tenant_id": tenant, "promise_version_id": f"{tid}-PROMISE-V2", "trip_id": tid,
                    "window_start_at": _stamp(p["promise_start"]+timedelta(minutes=45)),
                    "window_end_at": _stamp(p["promise_end"]+timedelta(minutes=45)),
                    "known_at": _stamp(revision_known), "supersedes_promise_version_id": f"{tid}-PROMISE-V1",
                    "change_reason": "customer",
                }, occurred=revision_known, available=revision_known, scenario=scenario_id)

            def trip_event(suffix, event_type, occurred, available, status):
                emit("eta_trip_event", {"tenant_id": tenant, "trip_event_id": f"{tid}-{suffix}",
                                       "trip_id": tid, "event_type": event_type, "occurred_at": _stamp(occurred),
                                       "available_at": _stamp(available), "trip_status": status},
                     occurred=occurred, available=available, scenario=scenario_id)

            trip_event("DEPARTURE", "departure", dep, dep+timedelta(seconds=20), "in_transit")
            gate_available = None
            if p["cancellation"]:
                trip_event("CANCELLATION", "cancellation", p["cancellation"],
                           p["cancellation"]+timedelta(seconds=20), "cancelled")
            elif scenario == "unfinished":
                # This is unresolved source follow-up, not a claim that the
                # physical vehicle keeps driving for nine months.
                trip_event("FOLLOWUP-INCOMPLETE", "status_update", dep+timedelta(hours=12),
                           dep+timedelta(hours=12, seconds=20), "incomplete")
            else:
                delay = timedelta(days=20) if scenario == "arrival_report_late" else timedelta(seconds=30)
                gate_available = p["arrival"]+delay
                trip_event("GATE-ARRIVAL", "status_update", p["arrival"], gate_available, "arrived")

            # Pending observations exist before final labels and remain visible
            # at earlier fit/calibration cutoffs. They carry no arrival target.
            def outcome(version, status, available, arrival=None, supersedes=None, reason=None):
                is_verified = status == "verified"
                incomplete = status in ("pending", "incomplete")
                cancellation = p["cancellation"] if status == "cancelled" else None
                states = {}
                for field in ("actual_arrival_time", "service_start_time", "delivery_completed_time"):
                    if not is_verified:
                        states[field] = ("not_mature" if incomplete else "not_applicable",
                                         "Outcome follow-up is incomplete." if incomplete else "Cancelled trip; arrival target does not apply.")
                emit("eta_trip_outcome", {
                    "tenant_id": tenant, "outcome_version_id": f"{tid}-OUTCOME-V{version}", "trip_id": tid,
                    "actual_arrival_time": _stamp(arrival) if is_verified else None,
                    "service_start_time": _stamp(p["service_start"]) if is_verified else None,
                    "delivery_completed_time": _stamp(p["delivery_completed"]) if is_verified else None,
                    "cancellation_time": _stamp(cancellation) if cancellation else None,
                    "label_available_time": _stamp(available), "label_status": status,
                    "arrival_evidence_ref": f"validation/latent_truth.jsonl#trip_id={tid};source=independent_gate" if is_verified else None,
                    "arrival_time_precision": "second" if is_verified else "unknown",
                    "reviewer_id": "synthetic_oracle" if is_verified else None,
                    "cancellation_reason": "customer" if cancellation else "not_applicable",
                    "followup_through": _stamp(available),
                    "followup_end_reason": "completed" if is_verified else ("cancelled" if cancellation else "observation_cutoff"),
                    "revision_reason": reason,
                    "supersedes_outcome_version_id": f"{tid}-OUTCOME-V{supersedes}" if supersedes else None,
                }, occurred=arrival or cancellation or dep, available=available, scenario=scenario_id, null_reasons=states,
                     record_state="corrected" if version > 2 else "active")

            outcome(1, "pending", dep+timedelta(minutes=5))
            if p["cancellation"]:
                outcome(2, "cancelled", p["cancellation"]+timedelta(minutes=2), supersedes=1)
            elif scenario == "unfinished":
                outcome(2, "incomplete", dep+timedelta(hours=12, minutes=1), supersedes=1,
                        reason="Source follow-up incomplete; no verified gate evidence supplied.")
            else:
                known = max(p["arrival"]+timedelta(hours=48), gate_available+timedelta(minutes=1))
                if scenario == "outcome_revision":
                    # A reported gate time is initially five minutes too early;
                    # correction later restores the independent physical truth.
                    outcome(2, "verified", known, p["arrival"]-timedelta(minutes=5), supersedes=1,
                            reason="Initial simulated gate clock record; later corrected.")
                    outcome(3, "verified", p["arrival"]+timedelta(days=7), p["arrival"], supersedes=2,
                            reason="Independent synthetic gate-clock correction of minus-five-minute initial timestamp.")
                else:
                    outcome(2, "verified", known, p["arrival"], supersedes=1)

            end = p["cancellation"] or p["arrival"]
            if scenario == "unfinished":
                end = min(end, dep+timedelta(minutes=75))
            total_minutes = int((end-dep).total_seconds()//60)
            for minute in range(total_minutes+1):
                event_time = dep+timedelta(minutes=minute)
                if scenario == "gps_dropout" and 30 <= minute < 90:
                    continue
                # Latent motion and independent GNSS errors produce positions.
                # The gate timestamp is not recovered from these measurements.
                progress = min(1.0, max(0.0, minute/p["motion_minutes"]))
                progress = max(0.0, min(1.0, progress + .018*math.sin(progress*2*math.pi)))
                lat = p["origin"][0] + progress*(p["destination"][0]-p["origin"][0]) + r.gauss(0, .00012)
                lon = p["origin"][1] + progress*(p["destination"][1]-p["origin"][1]) + r.gauss(0, .00012)
                valid = "valid"
                if scenario == "invalid_position" and 25 <= minute < 55:
                    # Invalid fix preserves a plausible raw numeric reading;
                    # validity must be used, not just latitude range checking.
                    lat += .025
                    lon -= .025
                    valid = "invalid"
                delay_seconds = r.randint(5, 35)
                if scenario == "late_gps" and 15 <= minute < 75:
                    delay_seconds += 40*60
                available = event_time+timedelta(seconds=delay_seconds)
                device = p["default_device"]
                if "swap_at" in p and p["swap_at"] <= event_time < p["swap_back_at"]:
                    device = p["swap_device"]
                event_id = f"{tid}-GPS-{minute:04d}"
                speed = max(0.0, p["distance_km"]/(p["motion_minutes"]/60)+r.gauss(0, 2.4)) if progress < 1 else 0.0
                speed = round(speed, 3)
                emit("telemetry_event", {
                    "tenant_id": tenant, "canonical_event_id": event_id,
                    "event_type": "synthetic_gnss_telemetry", "gnss_valid": valid,
                    "event_time": _stamp(event_time), "latitude_deg": round(lat, 7), "longitude_deg": round(lon, 7),
                    "device_id": device, "vehicle_id": p["vehicle_id"],
                    "source_event_id": f"SRC-{event_id}", "source_system_id": "PortfolioTelemetry-Simulator",
                    "record_revision": "1", "raw_record_id": f"RAW-{event_id}", "mapping_version": VERSION,
                    "boot_session_id": f"{tid}-BOOT-{device}", "available_time": _stamp(available),
                    "vehicle_speed_kmh": speed, "gnss_speed_kmh": speed, "vehicle_speed_origin": "gnss",
                    "engine_distance_km": None, "engine_runtime_seconds": None, "engine_rpm": None,
                    "counter_epoch_id": None,
                }, occurred=event_time, available=available, scenario=scenario_id,
                     null_reasons={key: ("not_applicable", "GNSS-only synthetic source; no ECU measurement claimed.")
                                   for key in ("engine_distance_km", "engine_runtime_seconds", "engine_rpm", "counter_epoch_id")})

            truth_handle.write(_json({
                "trip_id": tid, "tenant_id": tenant, "global_ordinal": p["ordinal"], "scenario": scenario,
                "scenario_id": scenario_id, "origin_type": "synthetic", "validation_only": True,
                "vehicle_id": p["vehicle_id"], "route_id": p["route_id"],
                "true_departure_at": _stamp(dep),
                "true_gate_arrival_at": _stamp(p["physical_arrival"]) if p["physical_arrival"] else None,
                "true_service_start_at": _stamp(p["service_start"]) if p["service_start"] else None,
                "true_delivery_completed_at": _stamp(p["delivery_completed"]) if p["delivery_completed"] else None,
                "cancellation_at": _stamp(p["cancellation"]) if p["cancellation"] else None,
                "gate_report_available_at": _stamp(gate_available) if gate_available else None,
                "motion_minutes": p["motion_minutes"],
                "swap_at": _stamp(p["swap_at"]) if "swap_at" in p else None,
                "swap_back_at": _stamp(p["swap_back_at"]) if "swap_back_at" in p else None,
                "source_followup_incomplete": scenario == "unfinished",
                "truth_source": "independently seeded synthetic gate process; not a GPS geofence or fitted model",
            })+"\n")
    finally:
        _sync_close([*handles.values(), truth_handle])

    registry = {
        "generator_version": VERSION, "seed": int(config.get("seed", 20261021)), "namespace": namespace,
        "generated_at": generated_at, "extract_as_of_exclusive": _stamp(cutoff),
        "origin_type": "synthetic", "availability_basis": "simulated", "validation_only": True,
        "full_design_trips": 1800, "selected_trips": len(selected), "full_design_trips_per_month": 200,
        "selection_rule": "global_ordinal % 15 == 0" if sample_size == 120 else "all 1800 design ordinals",
        "selected_global_ordinals": [p["ordinal"] for p in selected],
        "scenario_counts": dict(sorted(scenario_counts.items())),
        "scenarios": [{"scenario": name, "selection_rule": f"global_ordinal % 11 == {i}"} for i, name in enumerate(SCENARIOS)],
        "table_counts": dict(counts),
        "physical_checks": {"vehicle_trip_overlap": 0, "spare_device_assignment_overlap": 0},
        "assumptions_document": "docs/generator_assumptions.md",
        "model_input_policy": "No scenario registry, latent truth, outcome data, or provenance scenario strings as model features.",
    }
    with (validation_dir / "scenario_registry.json").open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(registry, ensure_ascii=False, indent=2)+"\n")
        handle.flush()
        os.fsync(handle.fileno())
    return dict(counts)


def generate(out_dir: Path, config: dict) -> dict:
    """Stage, durably flush, verify and atomically replace complete files.

    The scenario registry is published last and contains the committed file
    counts and hashes. Each file replacement is atomic; the collection of
    several file replacements is not a filesystem-wide transaction. Callers
    must wait for successful return before consuming a new generation.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Staging inside the destination keeps os.replace on the same filesystem.
    with tempfile.TemporaryDirectory(prefix=".generator-stage-", dir=out_dir) as temporary:
        stage = Path(temporary)
        counts = _generate_staged(stage, config)
        files = {}
        for table in ACTIVE_TABLES:
            relative = Path("data") / "raw" / f"{table}.jsonl"
            files[str(relative)] = _inspect_jsonl(stage / relative, counts[table])
        truth_relative = Path("validation") / "latent_truth.jsonl"
        files[str(truth_relative)] = _inspect_jsonl(stage / truth_relative, counts["eta_trip"])

        registry_relative = Path("validation") / "scenario_registry.json"
        registry = json.loads((stage / registry_relative).read_text(encoding="utf-8"))
        registry["publication"] = {
            "strategy": "same-filesystem staging; flush+fsync; verify; atomic per-file replace; registry last",
            "persisted_counts_verified_before_publish": True,
            "persisted_counts_and_hashes_verified_after_publish": True,
            "files": files,
        }
        with (stage / registry_relative).open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(registry, ensure_ascii=False, indent=2)+"\n")
            handle.flush()
            os.fsync(handle.fileno())

        for relative, expected in files.items():
            destination = out_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage / relative, destination)
        for directory in (out_dir / "data" / "raw", out_dir / "validation"):
            _sync_directory(directory)

        persisted = {}
        for relative, expected in files.items():
            actual = _inspect_jsonl(out_dir / relative, expected["rows"])
            if actual != expected:
                raise IOError(f"Published file differs from durable staging bytes: {relative}")
            if relative.startswith("data/raw/"):
                persisted[Path(relative).stem] = actual["rows"]

        # A consumer can independently verify the new generation against this
        # final registry. A failed/partial publication never returns success.
        os.replace(stage / registry_relative, out_dir / registry_relative)
        _sync_directory(out_dir / "validation")
        published_registry = json.loads((out_dir / registry_relative).read_text(encoding="utf-8"))
        if published_registry != registry:
            raise IOError("Published scenario registry failed readback verification")
        return persisted
