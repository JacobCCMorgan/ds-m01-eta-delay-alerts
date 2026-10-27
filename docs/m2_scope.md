# M2 data milestone scope

M2 retains the full 1,800-trip January–September 2026 synthetic design using the original seed, namespace and frozen feature/split definitions. It has its own batch receipt and pipeline version. The 120-trip M1 business rows, snapshots and labels remain exact subsets; historical pools intentionally grow as more eligible training trips become available.

M2 instantiates 28 canonical field schemas, populates 17 data-stage tables, and leaves 11 future model and alert tables empty. Empty schema creation does not implement their downstream behavior. The original 110 requirements remain in scope; M2 does not assert that all are satisfied.

The retained execution produced 197,303 GPS records, 14,349 snapshots and labels, 2,536,045 historical references and 2,775,752 provenance records. Its receipts record 49 passing unit tests, 27 passing warehouse checks and 22 passing readiness checks. Fresh rebuild and HTTP-derived parity also passed. See the [README evidence links](../README.md#validation-evidence) and [data validation guide](data_validation.md).

## Dataset and evaluation boundaries

No optional weather forecasts, commercial routing, extra historical warmup data, fitted models or operating-performance claims are introduced. December 2025 valid-from support metadata defines fictional sites and assignments; observed trips remain January–September 2026, with an exclusive outcome extraction cutoff of `2026-10-15T07:00:00Z`. Early snapshots can explicitly lack historical support.

Test cohorts are inspected for validity and support only, not model choice. Training, calibration and test dates, heldout IDs, the 12-hour horizon and target cutoff remain frozen. The unseen-route-only cohort has 29 eligible trips, and the joint unseen-vehicle/unseen-route cohort has four. These sparse cohorts require cautious descriptive reporting. There are also 97 eligible training snapshots without prior-history support. The next modeling stage must define a fallback or unavailable result and account for dependence among snapshots from the same trip.

Native PostgreSQL verification remains an unresolved service check. Local HTTP verification covers telemetry transport and replay parity; it does not establish PostgreSQL integration or production deployment.

## Shared catalog and effective configuration

[contracts/fields.json](../contracts/fields.json) is a shared field catalog: 341 field records across 34 table names. Human-readable labels are descriptive metadata; `name`, `table`, field IDs, types and units define the machine-facing contract. DS-M01's implemented scope is the 28 required tables listed in [config/m2.json](../config/m2.json), with 17 populated and 11 reserved empty tables.

Some inherited `supply_rule` and `planned_stage` descriptions retain shared planning references, including a January–April/May/June split and optional 2025 forecasts. These descriptions do not select data for the current run. The effective M2 configuration specifies 2026 training departures from January 1 through June 23, calibration departures from July 1 through July 24, and test departures from August 1 through September 30, with buffer intervals and label-availability cutoffs defined in that configuration. Forecast and routing tables remain disabled; no forecast or commercial routing records were generated.

## Evidence chronology

The M2 JSON receipts and test logs document the original data execution. M1 receipts are retained under [reports/m1_reference/](../reports/m1_reference/). The subsequent documentation and reporting cleanup did not rerun the complete M2 dataset. Its limited compatibility and reporting checks are recorded separately in [public cleanup validation](../reports/public_cleanup_validation.json).
