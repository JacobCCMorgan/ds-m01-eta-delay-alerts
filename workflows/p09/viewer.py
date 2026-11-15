"""Read-only, self-contained trip review for the synthetic P09 workflow.

The viewer retains the full primary population, including trips with no alert.
Recipient behavior is an explicit later simulation. Knowledge-time filtering
hides evidence unavailable at the selected snapshot; retrospective outcome
comparisons never rewrite the frozen P08 prediction-time promise or label.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path


VERSION = "p09-trip-review-v1"
PARENT_COMMIT = "12b904696b87d90369d85dabeee01eadadc854cd"


def _rows(value, *names):
    for name in names:
        rows = value.get(name)
        if rows is not None:
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("Viewer requires a row list: " + name)
            return deepcopy(rows)
    return []


def _clock(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Viewer clock must be an aware ISO timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid viewer clock") from exc
    if result.tzinfo is None:
        raise ValueError("Viewer clock must have a timezone")
    return value


def _first(row, *names):
    for name in names:
        if row.get(name) is not None:
            return row[name]
    return None


def _identity(row, name):
    value = row.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Viewer requires nonempty " + name)
    return value


def build_data(inputs, simulation, metrics, settings):
    """Return a display projection, retaining evidence rows and full membership."""
    opportunities = _rows(inputs, "opportunities")
    parent = inputs.get("parent_tables", {})
    if not opportunities:
        opportunities = [deepcopy(r["opportunity"]) for r in parent.get("p08_metadata_audit", [])]
    memberships = _rows(inputs, "memberships")
    trips = {}
    for row in memberships + opportunities:
        key = (_identity(row, "tenant_id"), _identity(row, "trip_id"))
        if key not in trips:
            trips[key] = {"tenant_id": key[0], "trip_id": key[1],
                          "route_id": row.get("route_id"), "vehicle_id": row.get("vehicle_id"),
                          "novelty_group": row.get("novelty_group"),
                          **{name: [] for name in ("opportunities", "alerts", "attempts", "adoptions", "actions",
                              "followups", "openings", "decision_audit", "followup_audit", "promise_versions",
                              "outcomes", "promises", "events")}}
        else:
            for field in ("route_id", "vehicle_id", "novelty_group"):
                if trips[key].get(field) is None and row.get(field) is not None:
                    trips[key][field] = row[field]
    snapshots = {}
    for row in opportunities:
        tenant, trip = row["tenant_id"], row["trip_id"]
        snapshot = _identity(row, "snapshot_id")
        if (tenant, snapshot) in snapshots:
            raise ValueError("Duplicate viewer snapshot identity")
        _clock(row.get("snapshot_time"))
        snapshots[tenant, snapshot] = (tenant, trip)
        trips[tenant, trip]["opportunities"].append(row)

    bundle = inputs.get("policy_delivery", inputs.get("bundle", {}))
    delivery = bundle.get("delivery", {}) if isinstance(bundle, dict) else {}
    alerts = _rows(inputs, "alerts") or deepcopy(delivery.get("alerts", []))
    attempts = _rows(inputs, "attempts") or deepcopy(delivery.get("attempts", []))
    if not alerts:
        alerts = [deepcopy(row.get("record", row)) for row in parent.get("p08_delivery_alert_audit", [])]
    if not attempts:
        attempts = [deepcopy(row.get("record", row)) for row in parent.get("p08_delivery_attempt_audit", [])]
    collections = [
        ("alerts", alerts, "alert_id"), ("attempts", attempts, "delivery_id"),
        ("adoptions", _rows(simulation, "eta_adoption"), "adoption_id"),
        ("actions", _rows(simulation, "eta_action"), "action_id"),
        ("followups", _rows(simulation, "eta_action_followup"), None),
        ("openings", _rows(simulation, "opening_events"), None),
        ("decision_audit", _rows(simulation, "decision_audit"), None),
        ("followup_audit", _rows(simulation, "followup_audit"), None),
        ("promise_versions", _rows(simulation, "promise_versions"), "promise_version_id"),
        ("outcomes", _rows(inputs, "outcomes"), "outcome_version_id"),
        ("promises", _rows(inputs, "promises"), "promise_version_id"),
    ]
    links = {"snapshot_id": snapshots}

    def resolve(row):
        tenant = _identity(row, "tenant_id")
        if row.get("trip_id"):
            key = tenant, row["trip_id"]
            if key not in trips:
                raise ValueError("Viewer evidence references an unknown trip")
            return key
        for field in ("snapshot_id", "alert_id", "delivery_id", "adoption_id", "action_id", "promise_version_id"):
            if row.get(field) is not None and (tenant, row[field]) in links.get(field, {}):
                return links[field][tenant, row[field]]
        raise ValueError("Viewer evidence has no tenant-scoped trip lineage")

    for name, rows, identity in collections:
        if identity:
            links.setdefault(identity, {})
        for row in rows:
            key = resolve(row)
            if identity:
                token = key[0], _identity(row, identity)
                if token in links[identity] and links[identity][token] != key:
                    raise ValueError("Viewer identity crosses trip ownership")
                links[identity][token] = key
            trips[key][name].append(row)

    def event(trip, kind, title, happened, known, record, *, detail=None, status=None):
        if happened is None and known is None:
            return
        _clock(happened)
        _clock(known)
        # Unknown knowledge time remains unknown. It must not pass an as-of filter.
        trip["events"].append({"kind": kind, "title": title, "event_at": happened,
                               "known_at": known, "detail": detail, "status": status,
                               "record": deepcopy(record)})

    for trip in trips.values():
        for row in trip["opportunities"]:
            event(trip, "prediction", "Prediction snapshot", row.get("snapshot_time"), row.get("snapshot_time"), row,
                  detail=row.get("snapshot_id"), status=row.get("eta_status", "unknown"))
        for row in trip["promises"]:
            event(trip, "promise", "Source promise became known", row.get("known_at"), row.get("known_at"), row,
                  detail=row.get("promise_version_id"))
        for row in trip["alerts"]:
            # A final retained alert row includes future transport results. The
            # candidate event exposes only the fields known at its creation.
            candidate = {k: v for k, v in row.items() if k not in
                         {"first_delivery_id", "first_delivered_at", "attempt_count", "alert_status", "status_reason"}}
            event(trip, "alert", "Alert candidate", row.get("created_at"), row.get("created_at"), candidate,
                  detail=row.get("alert_id"), status="candidate")
        for row in trip["attempts"]:
            event(trip, "delivery", "Transport attempt " + str(row.get("attempt_index", "")),
                  row.get("sent_at"), row.get("sent_at"),
                  {k: row.get(k) for k in ("tenant_id", "trip_id", "delivery_id", "alert_id", "candidate_id",
                   "alert_episode_id", "snapshot_id", "attempt_index", "sent_at", "recipient_role", "result_origin")},
                  detail=row.get("delivery_id"), status="sent")
            if row.get("receipt_known_at") is not None:
                event(trip, "delivery", "Service receipt became known", row.get("delivered_at") or row.get("service_at"),
                      row.get("receipt_known_at"), row, detail=row.get("delivery_id"), status=row.get("delivery_status"))
        for row in trip["openings"]:
            happened = _first(row, "opened_at", "received_or_opened_at", "event_at")
            known = _first(row, "known_at", "opening_known_at", "recorded_at", "observed_at")
            event(trip, "opening", "Simulated recipient opening", happened, known, row, detail=row.get("delivery_id"))
        decision_by_id = {row.get("adoption_id"): row for row in trip["decision_audit"]}
        for row in trip["adoptions"]:
            audit = decision_by_id.get(row["adoption_id"], {})
            known = _first(audit, "decision_known_at", "known_at", "recorded_at", "observed_at", "counts_as_of")
            if row.get("adoption_status") == "not_reviewed" and known is None:
                known = settings.get("observation_cutoff_at")
            event(trip, "adoption", "Review decision" if row.get("decided_at") else "Review status observation",
                  row.get("decided_at"), known, {"canonical": row, "audit": audit},
                  detail=row.get("adoption_reason"), status=row.get("adoption_status"))
        for row in trip["actions"]:
            event(trip, "action", "Recorded operator action", row.get("action_time"),
                  _first(row, "known_at", "action_known_at") or row.get("action_time"), row,
                  detail=row.get("action_id"), status=row.get("action_type"))
        canonical_by_action = {row.get("action_id"): row for row in trip["followups"]}
        followup_by_action = {row.get("action_id"): row for row in trip["followup_audit"]}
        # Unknown outcomes have no canonical row because no real source outcome
        # identifier can be supplied. Keep their auxiliary observations visible.
        for action_id in sorted(set(canonical_by_action) | set(followup_by_action)):
            row = canonical_by_action.get(action_id)
            audit = followup_by_action.get(action_id, {})
            observation = audit or row
            happened = observation.get("outcome_observation_end")
            if audit.get("followup_status") == "no_action":
                happened = audit.get("action_time") or happened
            known = _first(observation, "known_at", "followup_known_at", "recorded_at", "observed_at") or happened
            event(trip, "followup", "Follow-up not applicable" if audit.get("followup_status") == "no_action" else "Follow-up observation", happened, known,
                  {"canonical": row, "audit": audit}, detail=action_id,
                  status=audit.get("followup_status") or row.get("review_outcome"))
        for row in trip["promise_versions"]:
            known = _first(row, "known_at", "recorded_at", "created_at")
            event(trip, "promise", "Simulated follow-up promise", known, known, row, detail=row.get("promise_version_id"))
        for row in trip["outcomes"]:
            event(trip, "outcome", "Retained arrival outcome version",
                  row.get("actual_arrival_time") or row.get("followup_through"), row.get("label_available_time"), row,
                  detail=row.get("outcome_version_id"), status=row.get("label_status"))
        for name, rows in trip.items():
            if isinstance(rows, list):
                rows.sort(key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False))
        trip["opportunities"].sort(key=lambda r: (r.get("snapshot_time") or "", r["snapshot_id"]))
        trip["events"].sort(key=lambda r: (r.get("known_at") or "9999", r.get("event_at") or "9999", r["kind"]))
    ordered = [trips[key] for key in sorted(trips)]
    return {"version": VERSION, "scenario_id": "S00_unmodified", "parent_commit": PARENT_COMMIT,
            "trip_count": len(ordered), "opportunity_count": len(opportunities), "trips": ordered,
            "metrics": deepcopy(metrics), "workflow_summary": deepcopy(simulation.get("summary", {})),
            "protocol_version": settings.get("protocol_version", "p09-workflow"),
            "disclosures": {"synthetic_data": True, "read_only": True, "operational_approval": False,
                "quality_accepted": False, "causal_effect_estimated": False,
                "original_p08_outcomes_changed": False, "primary_scenario_only": True}}


def build_viewer(inputs, simulation, metrics, settings):
    """Build a deterministic offline document without external dependencies."""
    data = build_data(inputs, simulation, metrics, settings)
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    # JSON is inert application/json content, but a literal closing script token
    # still terminates an HTML element. Escape every HTML-sensitive character.
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    payload = payload.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return _HTML.replace("__PAYLOAD__", payload)


def write_viewer(path, inputs, simulation, metrics, settings):
    """Write one fresh view; refuse to replace prior evidence."""
    path = Path(path)
    if path.is_symlink() or path.exists():
        raise ValueError("Viewer output exists; choose a fresh path")
    content = build_viewer(inputs, simulation, metrics, settings).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)
    return {"status": "written_read_only_viewer", "version": VERSION,
            "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>DS-M01 · Trip review</title>
<style>
:root{color-scheme:light;--bg:#f3f5f3;--panel:#fff;--ink:#192c2b;--muted:#5c6d6c;--line:#dce4df;--soft:#edf2ed;--brand:#176455;--tint:#e2f0e9;--warn:#865c23;--warning:#fcf3df;--shadow:0 8px 30px #193c2a08;--blue:#426479;--red:#9a4741}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input,select{font:inherit}button{cursor:pointer}a{color:var(--brand)}button,input,select,summary{outline-offset:4px}button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #529e89}h1,h2,h3,p{margin-top:0}h1{font-size:32px;letter-spacing:-1.1px;line-height:1.15;margin-bottom:9px}h2{font-size:21px;letter-spacing:-.5px;margin-bottom:6px}h3{font-size:14px;margin-bottom:8px}small,.muted{color:var(--muted)}.topbar{padding:18px 32px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:16px}.brand{display:flex;align-items:center;gap:11px;font-weight:700;letter-spacing:-.3px}.brand-mark{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;background:var(--brand);color:white;font-size:15px}.top-meta{display:flex;align-items:center;gap:14px;font-size:12px;color:var(--muted)}.tag{font-size:11px;text-transform:uppercase;letter-spacing:1px;border-radius:6px;padding:4px 7px;background:var(--tint);color:var(--brand);font-weight:700}.plain-button{border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);padding:6px 11px}.wrap{max-width:1500px;margin:auto;padding:28px 32px}.intro{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin-bottom:22px}.eyebrow{font-size:11px;letter-spacing:1.5px;text-transform:uppercase;font-weight:700;color:var(--brand);margin-bottom:9px}.intro p{max-width:790px;color:var(--muted);margin-bottom:0}.source-label{font:11px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;color:var(--muted);text-align:right;white-space:nowrap}.notice{border:1px solid #e5d3a9;background:var(--warning);color:var(--warn);padding:12px 16px;border-radius:11px;display:flex;gap:12px;margin-bottom:22px;font-size:12px}.notice strong{white-space:nowrap}.summary-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;box-shadow:var(--shadow)}.stat-label{font-size:12px;color:var(--muted);margin-bottom:5px}.stat-value{font-size:28px;font-weight:650;letter-spacing:-1px;line-height:1.2}.stat-note{font-size:11px;color:var(--muted);margin-top:6px}.workspace{display:grid;grid-template-columns:285px minmax(0,1fr);gap:20px;align-items:start}.sidebar{position:sticky;top:18px;border:1px solid var(--line);border-radius:13px;background:var(--panel);overflow:hidden}.filters{padding:17px;border-bottom:1px solid var(--line)}label{display:block;font-size:11px;font-weight:650;color:var(--muted);margin:0 0 5px}input,select{width:100%;background:var(--panel);border:1px solid #cfdad3;border-radius:7px;padding:9px 10px;color:var(--ink);min-height:39px}input::placeholder{color:#85928c}.field+.field{margin-top:11px}.filter-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:13px}.filter-head h3{margin:0}.filter-head button{border:0;background:none;font-size:11px;color:var(--brand);padding:4px}.results-label{padding:11px 17px;font-size:11px;color:var(--muted);background:var(--soft);display:flex;justify-content:space-between}.trip-list{list-style:none;margin:0;padding:6px;max-height:560px;overflow:auto}.trip-button{border:0;border-radius:8px;width:100%;text-align:left;background:transparent;padding:12px 11px;color:var(--ink);margin-bottom:3px}.trip-button:hover{background:var(--soft)}.trip-button[aria-current=true]{background:var(--tint);box-shadow:inset 3px 0 var(--brand)}.trip-id{font-weight:650;font-size:12px;display:block;overflow-wrap:anywhere}.trip-line{display:flex;gap:8px;justify-content:space-between;color:var(--muted);font-size:11px;margin-top:5px}.mini-status{font-size:10px;letter-spacing:.3px}.detail{min-width:0}.trip-header{padding:22px 24px;background:var(--panel);border:1px solid var(--line);border-radius:13px;margin-bottom:16px}.trip-heading{display:flex;justify-content:space-between;gap:16px;align-items:start}.trip-heading h2{font-size:23px;margin-bottom:8px;overflow-wrap:anywhere}.metadata{display:flex;flex-wrap:wrap;gap:8px 22px;color:var(--muted);font-size:12px}.metadata strong{color:var(--ink);font-weight:550}.pills{display:flex;flex-wrap:wrap;gap:6px;margin:15px 0 0}.pill{border:1px solid var(--line);background:var(--soft);border-radius:20px;font-size:11px;padding:4px 9px}.view-controls{border-top:1px solid var(--line);margin-top:18px;padding-top:16px;display:grid;grid-template-columns:minmax(0,1fr) minmax(190px,.65fr);gap:16px;align-items:start}.view-note{font-size:11px;color:var(--muted);margin-top:7px}.tabs{display:flex;gap:5px;margin-bottom:14px;border-bottom:1px solid var(--line)}.tab{border:0;border-bottom:2px solid transparent;background:none;padding:10px 14px;color:var(--muted);font-size:13px}.tab[aria-selected=true]{border-color:var(--brand);color:var(--brand);font-weight:650}.panel{background:var(--panel);border:1px solid var(--line);border-radius:13px;overflow:hidden}.panel-head{display:flex;align-items:start;justify-content:space-between;gap:16px;padding:20px 23px;border-bottom:1px solid var(--line)}.panel-head p{font-size:12px;color:var(--muted);margin:0}.panel-head h3{font-size:15px;margin:0 0 4px}.panel-count{font:11px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;color:var(--muted);white-space:nowrap}.timeline{list-style:none;padding:9px 23px;margin:0}.event{display:grid;grid-template-columns:145px 20px minmax(0,1fr);gap:10px;padding:17px 0;position:relative}.event+.event{border-top:1px solid var(--line)}.event-clock{font:11px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;color:var(--muted)}.event-clock span{display:block}.event-dot{width:9px;height:9px;background:var(--brand);border-radius:50%;margin-top:5px;box-shadow:0 0 0 4px var(--tint)}.event[data-kind=prediction] .event-dot{background:var(--blue);box-shadow:0 0 0 4px #e9f0f5}.event[data-kind=outcome] .event-dot,.event[data-kind=followup] .event-dot{background:var(--warn);box-shadow:0 0 0 4px var(--warning)}.event-title{display:flex;flex-wrap:wrap;gap:9px;align-items:center;font-size:13px;font-weight:650}.event-detail{font-size:11px;color:var(--muted);overflow-wrap:anywhere;margin:4px 0}.event-known{font-size:10px;color:var(--muted);margin-top:5px}details{font-size:11px;color:var(--muted);margin-top:7px}summary{cursor:pointer;display:list-item}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:360px;overflow:auto;background:var(--soft);padding:12px;border-radius:7px;border:1px solid var(--line);color:var(--ink);font:11px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px}th,td{text-align:left;padding:13px 18px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:10px;text-transform:uppercase;letter-spacing:.7px;color:var(--muted);background:var(--soft);font-weight:650}td .sub{display:block;font-size:10px;color:var(--muted);margin-top:3px;overflow-wrap:anywhere}tr:last-child td{border-bottom:0}.followup-cards{padding:20px 23px;display:grid;gap:14px}.followup-card{border:1px solid var(--line);border-radius:9px;padding:16px}.comparison-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:13px}.comparison{background:var(--soft);padding:12px;border-radius:7px}.comparison .number{font-size:23px;font-weight:600;letter-spacing:-.5px}.comparison .caption{font-size:10px;color:var(--muted)}.reference{font:10px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere;color:var(--muted);margin-top:7px}.empty{padding:40px 24px;text-align:center;color:var(--muted);font-size:13px}.empty strong{display:block;color:var(--ink);font-size:16px;margin-bottom:6px}.footnote{font-size:11px;color:var(--muted);padding:16px 23px;background:var(--soft);border-top:1px solid var(--line)}.page-footer{display:flex;justify-content:space-between;gap:16px;margin-top:25px;font-size:11px;color:var(--muted)}[hidden]{display:none!important}.live{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}
body.dark{color-scheme:dark;--bg:#101b1a;--panel:#172523;--ink:#e2ece6;--muted:#a2b5aa;--line:#30433a;--soft:#20332b;--brand:#8bd1b5;--tint:#264b3b;--warn:#e2c28e;--warning:#382f20;--shadow:none;--blue:#90b6ce;--red:#e4a59d}body.dark .brand-mark{background:#467e65;color:white}body.dark input,body.dark select{border-color:var(--line)}body.dark .notice{border-color:#615039}body.dark .event[data-kind=prediction] .event-dot{box-shadow:0 0 0 4px #263e48}
@media(max-width:1000px){.wrap{padding:24px 20px}.workspace{grid-template-columns:245px minmax(0,1fr);gap:14px}.source-label{display:none}.event{grid-template-columns:112px 15px minmax(0,1fr)}.event-clock{font-size:10px}.trip-header{padding:18px}.view-controls{grid-template-columns:1fr}.topbar{padding:16px 20px}.stat{padding:14px}.summary-grid{gap:9px}}
@media(max-width:720px){.wrap{padding:20px 14px}.topbar{padding:14px}.top-meta>span{display:none}.intro{margin-bottom:18px}h1{font-size:28px}.intro p{font-size:13px}.notice{display:block}.notice strong{display:block;margin-bottom:5px}.summary-grid{grid-template-columns:repeat(2,1fr);gap:9px;margin-bottom:18px}.stat-value{font-size:25px}.workspace{display:block}.sidebar{position:static;margin-bottom:18px}.filters{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:14px}.filter-head{grid-column:1/-1;margin:0}.field+.field{margin:0}.field.search{grid-column:1/-1}.trip-list{max-height:190px}.trip-heading{display:block}.trip-heading>.tag{display:inline-block;margin-top:6px}.trip-header{padding:17px}.event{grid-template-columns:1fr;gap:4px;padding:16px 0}.event-dot{display:none}.event-clock{display:flex;gap:8px}.timeline{padding:5px 18px}.panel-head{padding:17px}.panel-count{font-size:10px}.comparison-grid{grid-template-columns:1fr}.tabs{gap:0}.tab{padding:10px 11px;font-size:12px}.page-footer{display:block}.followup-cards{padding:17px}.metadata{gap:6px 14px}}
</style></head><body>
<header class="topbar"><div class="brand"><span class="brand-mark" aria-hidden="true">↗</span><span>DS-M01 <span class="muted" style="font-weight:400">/ Trip review</span></span></div><div class="top-meta"><span>Offline · Read only</span><span class="tag">P09 workflow</span><button class="plain-button" id="theme" aria-label="Toggle dark theme" aria-pressed="false">Dark</button></div></header>
<main class="wrap"><section class="intro"><div><div class="eyebrow">From forecast to follow-up</div><h1>A complete trip, in context.</h1><p>Trace the frozen forecast, service delivery and simulated operator workflow. Inspect what was known at a snapshot, or review the complete evidence afterwards.</p></div><div class="source-label">PRIMARY SCENARIO<br>S00_unmodified<br>PARENT 12b9046</div></section>
<aside class="notice" aria-label="Evidence limitations"><strong>Synthetic demonstration</strong><span>Recipient behavior and actions are simulated. P08 whole-trip coverage is 243/328 (74.1%), below 80%; unseen-route coverage is 0/33. G14/G15/G16 remain open. No deployment approval or causal fleet benefit is established.</span></aside>
<section class="summary-grid" aria-label="Complete primary population"><div class="stat"><div class="stat-label">Primary trips</div><div class="stat-value" id="total-trips">—</div><div class="stat-note">Includes trips with no alert</div></div><div class="stat"><div class="stat-label">Prediction snapshots</div><div class="stat-value" id="total-snapshots">—</div><div class="stat-note">Repeated snapshots are not independent trips</div></div><div class="stat"><div class="stat-label">Confirmed delivery alerts</div><div class="stat-value" id="total-delivered">—</div><div class="stat-note">Distinct alerts, not transport attempts</div></div><div class="stat"><div class="stat-label">Recorded workflow actions</div><div class="stat-value" id="total-actions">—</div><div class="stat-note">Excludes explicit no-action records</div></div></section>
<div class="workspace"><aside class="sidebar" aria-label="Trip selection"><div class="filters"><div class="filter-head"><h3>Find a trip</h3><button id="reset">Reset filters</button></div><div class="field search"><label for="search">Trip, route or vehicle</label><input id="search" type="search" placeholder="Search the full population…" autocomplete="off"></div><div class="field"><label for="tenant">Tenant</label><select id="tenant"><option value="">All tenants</option></select></div><div class="field"><label for="state">Workflow state</label><select id="state"><option value="">All states</option><option value="no_alert">No alert</option><option value="not_delivered">No confirmed delivery</option><option value="not_reviewed">Not reviewed</option><option value="reviewed">Reviewed</option><option value="action">Action recorded</option><option value="overridden">Overridden</option></select></div></div><div class="results-label"><span id="results-count">Loading trips…</span><span>Retrospective states</span></div><ul class="trip-list" id="trip-list"></ul></aside>
<section class="detail" aria-label="Selected trip"><div id="no-selection" class="panel empty" hidden><strong>No matching trips</strong>Try a different tenant, state or search.</div><div id="selected"><section class="trip-header"><div class="trip-heading"><div><div class="eyebrow" id="trip-tenant">Trip evidence</div><h2 id="trip-title">—</h2><div class="metadata" id="trip-meta"></div></div><span class="tag" id="trip-state">—</span></div><div class="pills" id="trip-pills"></div><div class="view-controls"><div><label for="asof">Knowledge-time view</label><select id="asof"><option value="">Complete audit · retrospective</option></select><div class="view-note" id="asof-note">Retrospective evidence includes later outcomes. It was not all known at prediction time.</div></div><div><label for="event-kind">Timeline event type</label><select id="event-kind"><option value="">All event types</option><option value="prediction">Predictions</option><option value="promise">Promises</option><option value="alert">Alerts</option><option value="delivery">Transport and receipts</option><option value="opening">Recipient opening</option><option value="adoption">Adoption decisions</option><option value="action">Operator actions</option><option value="followup">Follow-up</option><option value="outcome">Retained outcomes</option></select></div></div></section>
<div class="tabs" role="tablist" aria-label="Trip evidence views"><button class="tab" role="tab" id="tab-timeline" aria-controls="view-timeline" aria-selected="true" data-tab="timeline">Timeline</button><button class="tab" role="tab" id="tab-predictions" aria-controls="view-predictions" aria-selected="false" tabindex="-1" data-tab="predictions">Predictions</button><button class="tab" role="tab" id="tab-followup" aria-controls="view-followup" aria-selected="false" tabindex="-1" data-tab="followup">Follow-up</button></div>
<section class="panel" id="view-timeline" role="tabpanel" aria-labelledby="tab-timeline"><div class="panel-head"><div><h3>Evidence timeline</h3><p>Ordered by when each record became known. Expand a row for its exact source identifiers.</p></div><span class="panel-count" id="event-count"></span></div><ol class="timeline" id="timeline"></ol><div class="footnote">Transport receipt ≠ recipient opening ≠ adoption ≠ action. The original P08 delivery records remain unchanged.</div></section>
<section class="panel" id="view-predictions" role="tabpanel" aria-labelledby="tab-predictions" hidden><div class="panel-head"><div><h3>Frozen prediction snapshots</h3><p>Intervals are remaining travel time. Promises are the versions known at each prediction.</p></div><span class="panel-count" id="prediction-count"></span></div><div class="table-wrap"><table><thead><tr><th>Snapshot · UTC</th><th>ETA interval</th><th>Delay probability</th><th>Original promise</th><th>Evidence</th></tr></thead><tbody id="prediction-rows"></tbody></table></div></section>
<section class="panel" id="view-followup" role="tabpanel" aria-labelledby="tab-followup" hidden><div class="panel-head"><div><h3>Follow-up, without rewriting history</h3><p>Compare the original deadline and a later simulated promise against the same retained arrival.</p></div><span class="panel-count" id="followup-count"></span></div><div class="followup-cards" id="followup-cards"></div><div class="footnote">A later deadline may reduce measured delay without changing arrival. This is a promise comparison, not estimated time saved or intervention benefit. Unknown and immature outcomes remain unscored.</div></section></div></section></div>
<footer class="page-footer"><span>Fixed P08 evidence · Separate P09 synthetic workflow · Primary scenario only</span><span>No external services, analytics or write actions</span></footer><p class="live" id="live" aria-live="polite"></p></main>
<script id="p09-data" type="application/json">__PAYLOAD__</script>
<script>
'use strict';
const DATA=JSON.parse(document.getElementById('p09-data').textContent);
const $=id=>document.getElementById(id);
const el=(name,cls,text)=>{const n=document.createElement(name);if(cls)n.className=cls;if(text!==undefined)n.textContent=String(text);return n;};
const value=v=>(v===null||v===undefined||v==='')?'Unknown':String(v);
const human=v=>value(v).replaceAll('_',' ');
const fmt=v=>{if(!v)return 'Unknown';const d=new Date(v);return Number.isNaN(d.valueOf())?'Unknown':d.toISOString().replace('T',' ').replace('.000Z',' UTC').replace('Z',' UTC');};
const shortTime=v=>fmt(v).replace(/\.\d+ UTC$/,' UTC');
const minutes=v=>v===null||v===undefined?'Unknown':Number.isFinite(Number(v))?(Number(v)/60).toFixed(1)+' min':'Unknown';
const known=(time,cutoff)=>!cutoff||Boolean(time&&new Date(time)<=new Date(cutoff));
const key=t=>JSON.stringify([t.tenant_id,t.trip_id]);
const delivered=t=>new Set(t.attempts.filter(r=>r.delivery_status==='delivered'&&r.receipt_observed!==false&&r.receipt_known_at).map(r=>r.alert_id));
const actions=t=>t.actions.filter(r=>r.action_type!=='no_action'&&r.action_time);
const reviewed=t=>t.adoptions.filter(r=>r.adoption_status!=='not_reviewed'&&r.decided_at);
const state=t=>!t.alerts.length?'No alert':!delivered(t).size?'Not delivered':actions(t).length?'Action recorded':reviewed(t).length?'Reviewed':'Not reviewed';
let active=null, activeTab='timeline';
$('total-trips').textContent=DATA.trip_count.toLocaleString();$('total-snapshots').textContent=DATA.opportunity_count.toLocaleString();
$('total-delivered').textContent=DATA.trips.reduce((n,t)=>n+delivered(t).size,0).toLocaleString();$('total-actions').textContent=DATA.trips.reduce((n,t)=>n+actions(t).length,0).toLocaleString();
[...new Set(DATA.trips.map(t=>t.tenant_id))].sort().forEach(v=>{const n=el('option',null,v);n.value=v;$('tenant').append(n);});
function matches(t){const search=$('search').value.trim().toLowerCase(),tenant=$('tenant').value,s=$('state').value;return(!tenant||tenant===t.tenant_id)&&(!search||[t.tenant_id,t.trip_id,t.route_id,t.vehicle_id].join(' ').toLowerCase().includes(search))&&(!s||(s==='no_alert'&&!t.alerts.length)||(s==='not_delivered'&&t.alerts.length&&!delivered(t).size)||(s==='not_reviewed'&&delivered(t).size&&t.adoptions.some(r=>r.adoption_status==='not_reviewed'))||(s==='reviewed'&&reviewed(t).length)||(s==='action'&&actions(t).length)||(s==='overridden'&&t.adoptions.some(r=>r.adoption_status==='overridden')));}
function renderList(){const rows=DATA.trips.filter(matches);$('trip-list').replaceChildren();$('results-count').textContent=rows.length+' of '+DATA.trip_count+' trips';if(!rows.some(t=>key(t)===active)){active=rows.length?key(rows[0]):null;selectTrip(false);}rows.forEach(t=>{const li=el('li'),b=el('button','trip-button');b.type='button';b.setAttribute('aria-current',String(key(t)===active));b.append(el('span','trip-id',t.trip_id));const line=el('span','trip-line');line.append(el('span',null,value(t.route_id)),el('span','mini-status',state(t)));b.append(line);b.addEventListener('click',()=>{active=key(t);selectTrip(true);renderList();});li.append(b);$('trip-list').append(li);});$('live').textContent=rows.length+' matching trips';}
function current(){return DATA.trips.find(t=>key(t)===active);}
function selectTrip(reset=true){const t=current();$('selected').hidden=!t;$('no-selection').hidden=Boolean(t);if(!t)return;$('trip-title').textContent=t.trip_id;$('trip-tenant').textContent=t.tenant_id+' / PRIMARY S00';$('trip-state').textContent=state(t);$('trip-meta').replaceChildren();[['Route',t.route_id],['Vehicle',t.vehicle_id],['Population',human(t.novelty_group)]].forEach(([label,v])=>{const n=el('span');n.append(document.createTextNode(label+' '),el('strong',null,value(v)));$('trip-meta').append(n);});$('trip-pills').replaceChildren();[t.opportunities.length+' snapshots',t.alerts.length+' alerts',t.attempts.length+' transport attempts',delivered(t).size+' confirmed deliveries'].forEach(v=>$('trip-pills').append(el('span','pill',v)));const old=reset?'':$('asof').value;$('asof').replaceChildren();const full=el('option',null,'Complete audit · retrospective');full.value='';$('asof').append(full);t.opportunities.forEach((r,i)=>{const o=el('option',null,'Snapshot '+(i+1)+' · '+shortTime(r.snapshot_time));o.value=r.snapshot_time;$('asof').append(o);});$('asof').value=old;if(reset)$('event-kind').value='';renderDetails();}
function detailRecord(record){const d=el('details'),s=el('summary',null,'Inspect evidence and identifiers'),p=el('pre',null,JSON.stringify(record,null,2));d.append(s,p);return d;}
function visibleEvents(t){const cutoff=$('asof').value;return t.events.filter(e=>known(e.known_at,cutoff));}
function renderDetails(){const t=current();if(!t)return;const cutoff=$('asof').value,kind=$('event-kind').value,all=visibleEvents(t),rows=all.filter(e=>!kind||e.kind===kind);renderKnownHeader(t,all,cutoff);$('asof-note').textContent=cutoff?'Only evidence known by '+shortTime(cutoff)+'. Later outcomes and unknown knowledge clocks are hidden.':'Retrospective evidence includes later outcomes. It was not all known at prediction time.';$('event-count').textContent=rows.length+' events'+(cutoff?' · as of snapshot':'');$('timeline').replaceChildren();if(!rows.length){const n=el('li','empty');n.append(el('strong',null,'No visible events'),'No events of this type were known at the selected time.');$('timeline').append(n);}rows.forEach(e=>{const li=el('li','event');li.dataset.kind=e.kind;const clock=el('div','event-clock');clock.append(el('span',null,e.event_at?shortTime(e.event_at):'No decision time'));const body=el('div');const title=el('div','event-title',e.title);if(e.status)title.append(el('span','pill',human(e.status)));body.append(title);if(e.detail)body.append(el('div','event-detail',e.detail));body.append(el('div','event-known','Known: '+shortTime(e.known_at)),detailRecord(e.record));li.append(clock,el('div','event-dot'),body);$('timeline').append(li);});renderPredictions(t,cutoff);renderFollowup(t,all);}
function renderKnownHeader(t,events,cutoff){if(!cutoff){$('trip-state').textContent=state(t);$('trip-pills').replaceChildren();[t.opportunities.length+' snapshots',t.alerts.length+' alerts',t.attempts.length+' transport attempts',delivered(t).size+' confirmed deliveries'].forEach(v=>$('trip-pills').append(el('span','pill',v)));return;}const alerts=events.filter(e=>e.kind==='alert'),receipts=events.filter(e=>e.kind==='delivery'&&e.status==='delivered'),decisions=events.filter(e=>e.kind==='adoption'&&e.status!=='not_reviewed'),actionEvents=events.filter(e=>e.kind==='action'&&e.status!=='no_action');$('trip-state').textContent=actionEvents.length?'Action known':decisions.length?'Review known':receipts.length?'Delivery known':alerts.length?'Alert known':'No alert yet';$('trip-pills').replaceChildren();[events.filter(e=>e.kind==='prediction').length+' known snapshots',alerts.length+' known alerts',receipts.length+' known delivery receipts','As-of view'].forEach(v=>$('trip-pills').append(el('span','pill',v)));}
function renderPredictions(t,cutoff){const rows=t.opportunities.filter(r=>known(r.snapshot_time,cutoff));$('prediction-count').textContent=rows.length+' snapshots';$('prediction-rows').replaceChildren();rows.forEach(r=>{const tr=el('tr'),stamp=el('td',null,shortTime(r.snapshot_time));stamp.append(el('span','sub',r.snapshot_id));const eta=el('td',null,minutes(r.lower_seconds)+' – '+minutes(r.upper_seconds));eta.append(el('span','sub','Median '+minutes(r.median_seconds)+' · '+human(r.eta_status)));const probability=r.calibrated_probability??r.probability;const prob=el('td',null,probability===null||probability===undefined?'Unknown':(Number(probability)*100).toFixed(1)+'%');prob.append(el('span','sub',human(r.probability_status)));const promise=el('td',null,shortTime(r.deadline_at));promise.append(el('span','sub',value(r.promise_version_id)));const evidence=el('td');evidence.append(el('span','sub',value(r.prediction_version)),detailRecord(r));tr.append(stamp,eta,prob,promise,evidence);$('prediction-rows').append(tr);});}
function renderFollowup(t,events){const rows=events.filter(e=>e.kind==='followup');$('followup-count').textContent=rows.length+' observations';$('followup-cards').replaceChildren();if(!rows.length){const n=el('div','empty');n.append(el('strong',null,'No follow-up visible'),'No follow-up record is available at this knowledge time. This does not imply an on-time outcome.');$('followup-cards').append(n);return;}rows.forEach(e=>{const a=e.record.audit||{},r=e.record.canonical||a,card=el('article','followup-card');const head=el('div','event-title',human(a.followup_status||r.review_outcome));head.append(el('span','pill',r.observed_delay_seconds===null||r.observed_delay_seconds===undefined?'Outcome unscored':'Observed comparison'));card.append(head,el('div','reference','Action '+r.action_id),el('div','event-known','Workflow review cutoff '+shortTime(r.outcome_observation_end)),el('div','event-known','Source observations through '+shortTime(a.source_followup_through)),el('div','event-detail',value(a.reason)));const grid=el('div','comparison-grid');[['Original promise',r.original_promise_delay_seconds,r.original_promise_version_id],['Follow-up promise',r.observed_delay_seconds,r.followup_promise_version_id]].forEach(([label,n,id])=>{const box=el('div','comparison');box.append(el('div','caption',label),el('div','number',minutes(n)),el('div','caption','Positive delay against the same retained arrival'),el('div','reference',id));grid.append(box);});card.append(grid,el('div','reference','Outcome version '+value(r.outcome_version_id)),detailRecord({canonical:r,audit:a}));$('followup-cards').append(card);});}
function tab(name){activeTab=name;document.querySelectorAll('[data-tab]').forEach(b=>{const on=b.dataset.tab===name;b.setAttribute('aria-selected',String(on));b.tabIndex=on?0:-1;$('view-'+b.dataset.tab).hidden=!on;});}
document.querySelectorAll('[data-tab]').forEach(b=>{b.addEventListener('click',()=>tab(b.dataset.tab));b.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const names=['timeline','predictions','followup'],i=names.indexOf(activeTab);tab(e.key==='Home'?names[0]:e.key==='End'?names[2]:names[(i+(e.key==='ArrowRight'?1:2))%3]);$('tab-'+activeTab).focus();});});
['search','tenant','state'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',renderList));$('reset').addEventListener('click',()=>{['search','tenant','state'].forEach(id=>$(id).value='');renderList();});$('asof').addEventListener('change',renderDetails);$('event-kind').addEventListener('change',renderDetails);$('theme').addEventListener('click',()=>{const on=document.body.classList.toggle('dark');$('theme').textContent=on?'Light':'Dark';$('theme').setAttribute('aria-pressed',String(on));});
renderList();
</script></body></html>'''
