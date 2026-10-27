# M1 trial scope and evidence

M1 established the point-in-time pipeline on a fixed 120-trip subset of the January–September 2026 synthetic design. This document describes that historical trial. The full 1,800-trip data milestone is documented in [M2 scope](m2_scope.md).

## Implemented foundation

- **Shared runtime audit:** checked stable entity identifiers and deterministic seed semantics against the earlier source implementation. The audit reused identifiers and narrow utility behavior, not 2025 trip observations or fuel-analysis logic. See [shared runtime audit](shared_runtime_audit.md).
- **Synthetic source generation:** retained 120 trips across three tenants, 30 vehicles, 12 routes and 11 scenario families, using the DS-M01 dataset namespace.
- **Point-in-time reconstruction:** selected inputs using event time, availability time, promise versions, device assignment versions and known trip status. Labels were joined only after features were constructed.
- **Independent arrival evidence:** simulated gate/sign-in observations established arrival independently of the last GPS point, service start and delivery completion. Historical references used mature observations from eligible training trips known at the snapshot.
- **HTTP ingestion:** exercised local pagination, 503 retries, cursor checks, replay deduplication and conflicting-content rejection, then supplied received telemetry to the same feature pipeline.
- **Typed storage:** built 17 canonical DuckDB/Parquet tables and validated temporal availability, labels, lineage, heldout membership and structure.

## Historical trial size

The retained sources for these counts are [M1 summary](../reports/m1_reference/m1_summary.json) and [M1 quality report](../reports/m1_reference/quality_report.json).

| Item | Count |
|---|---:|
| Trips / GPS records | 120 / 12,834 |
| Snapshots / labels / historical references | 937 / 937 / 11,594 |
| Labels: eligible / not mature / cancelled / already arrived | 457 / 288 / 11 / 181 |
| Position: valid / low confidence / unavailable | 413 / 465 / 59 |
| Trips: train / excluded / calibration / test | 65 / 16 / 13 / 26 |

Unknown, immature and cancelled outcomes remain explicit; they are not filled with zero remaining seconds or on-time labels. Snapshots created after physical arrival but before arrival status was known remain in the data as `already_arrived`, excluded from remaining-time regression labels.

The candidate horizon is 12 hours after departure. Position age above 900 seconds is low confidence. The first snapshot is triggered when the departure event becomes available, followed by 30-minute intervals. In this source, departure is known 20 seconds after the event, preserving a snapshot for trips cancelled after only 25 minutes.

## Tables and stage boundaries

| Group | M1 populated tables |
|---|---|
| Shared entities and provenance | `core_tenant`, `core_vehicle`, `core_device`, `core_vehicle_device_assignment`, `provenance_record` |
| ETA sources | `eta_trip`, `eta_facility_version`, `eta_route_version`, `eta_trip_plan_version`, `eta_promise_version`, `eta_trip_event`, `eta_trip_outcome` |
| Telemetry | `telemetry_event` |
| Derived records | `eta_snapshot`, `eta_snapshot_label`, `eta_split_assignment`, `eta_history_reference` |

The project retains 28 required table definitions and 110 requirements. M1 populated 17 tables. The remaining 11 model and alert tables are `eta_model_version`, `eta_evaluation_membership`, `eta_prediction`, `eta_alert_rule_version`, `eta_output_release`, `eta_alert_episode`, `eta_alert`, `eta_notification_delivery`, `eta_adoption`, `eta_action` and `eta_action_followup`. M2 creates their empty typed schemas; their behavior remains future work. Optional `eta_forecast_version` and `eta_routing_response` are not enabled.

## Retained validation evidence

| Evidence | Historical result and boundary |
|---|---|
| [Quality report](../reports/m1_reference/quality_report.json) | 24 checks passed for the 937-snapshot trial and its 17 populated tables. |
| [Unit test results](../reports/m1_reference/unittest_results.txt) | 33 tests passed: 20 temporal cases, four build invariants, eight HTTP cases and one source-publication guard. The current suite has subsequently expanded. |
| [HTTP results](../reports/m1_reference/last_http-check.json) | 94 pages, one retry, 137 replayed records and zero added duplicates; all 12,834 canonical records matched, and the local server closed. |
| [Rebuild results](../reports/m1_reference/rebuild_report.json) | Fresh-directory generation, point-in-time outputs, typed warehouse and Parquet rebuild passed, including HTTP input parity. |
| [Generator validation](../reports/m1_reference/generator_validation.json) | Same-directory stability and temporary 1,800-trip source generation passed. This source-only expansion was separate from the later complete M2 pipeline. |
| [PostgreSQL inspection](../reports/m1_reference/postgres_check.json) | Native PostgreSQL roundtrip was not run; supplying an adapter and DDL did not establish service integration. |

HTTP response ordering can differ from source ordering. Validation compares complete event identities and content, then checks the SHA256 digest of canonical sorted JSONL. Deduplication keys include tenant, event and revision; different content under the same key is rejected.

## Reproduction and subsequent work

Run instructions are in the [README](../README.md). Exact reproduction preserves the recorded source receipt `generated_at` in the frozen configuration; actual execution time is recorded separately. A newly generated dataset version requires its own receipt metadata and dataset identity.

M2 subsequently completed the retained 1,800-trip pipeline and expanded quality, coverage and continuity checks. Neither milestone trained models or established accuracy, interval coverage, alert precision/recall or operational benefit. Native PostgreSQL remains pending; its [runtime note](postgres_runtime_note.md) describes the independent service check.

Historical receipts remain unchanged as evidence of those runs. Current documentation and reporting compatibility checks are separate in [public cleanup validation](../reports/public_cleanup_validation.json); they do not represent another full M2 rebuild.
