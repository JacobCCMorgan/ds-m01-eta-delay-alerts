# Implementation change log

Entries describe implementation changes and observed test findings. Dates are actual execution dates in UTC; they do not substitute for version-control history.

## 2026-10-30 — M3b.1 feature contracts and leakage checks

- Added a positive feature contract with five numeric and three categorical
  inputs, tenant/version/time guards and explicit stale-position masking.
- Added tenant/fold-local median imputation, scaling, missing indicators and
  training-only categorical vocabularies with persisted state validation.
- Added a feature-only runner over six frozen inputs; it never opens outcome,
  label, promise or simulator-truth files. All four M3a folds remain unchanged.
- Retained 7,850 original-training feature rows, 12 transformers and all 5,163
  validation opportunities. Validation rows are not filtered by future labels.
- Documented the upstream stale-speed behavior and synthetic motion limitation.
  The former is handled at the feature boundary; it did not affect M3a's
  route-and-elapsed historical baseline. Neither item is a real-fleet incident.
- Added independent temporal/tenant/target-poisoning fixtures, source drift and
  interrupted-publication checks. See the actual test and deterministic replay
  results in [M3b.1 verification](../reports/m3b1/verification.json).

Candidate models, calibration, final scoring and operational outputs remain
pending; this stage establishes a checked interface for those later milestones.

## 2026-10-28 — M3a historical baseline

- Added four expanding, trip-grouped validation folds inside the original M2
  training period, with separate exclusive fit and evaluation-label cutoffs.
- Reconstructed raw outcome versions at each fit cutoff so later corrections
  cannot replace what was known when a historical pool was frozen.
- Implemented conditional remaining-duration P10/P50/P90, tenant-local
  route/departure and distance/global fallbacks, and explicit unsupported output.
- Added trip-balanced error, raw interval and support metrics with a fixed-seed
  trip bootstrap. Predictions are emitted before evaluation labels are joined.
- Replayed the full M2 pipeline and verified the eight consumed input hashes.
  All 102 tests passed; six core baseline outputs matched across two executions.
- Retained 5,163 opportunities. Of these, 3,357 had predictions; the error
  denominator was 2,347 eligible supported snapshots from 511 trips. MAE was
  17.43 minutes on this synthetic training-period validation only.
- Exact route/departure history was sparse: 44 predictions used that pool,
  compared with 2,961 distance-band and 352 tenant-global fallbacks. No tuning
  was performed on July calibration or August–September final-test outcomes.

The M3a experimental outputs do not populate the reserved model/alert schemas.
Candidate-model fitting, calibration, final-test scoring and alerts remain
pending. See [M3a scope](m3a_baseline.md) and [results](../reports/m3a/README.md).

## 2026-10-27 — M0 and M1 foundation

- Audited shared entity identifiers and named seed behavior without modifying the reference source.
- Built an original 120-trip synthetic 2026 trial with independent observation sources and isolated simulator truth, retaining the full 1,800-trip design for expansion.
- Implemented point-in-time snapshots, independent gate-arrival labels, trip splits, restricted historical references and source-parent lineage.
- Implemented local HTTP pagination, retry, replay deduplication, content-conflict rejection and received-telemetry input mode.
- Built 17 typed DuckDB/Parquet tables, 24 quality checks and 33 unit tests.
- Added generator stability and rebuild checks, plus a PostgreSQL adapter and typed DDL.

### Temporal findings and corrections

| Finding | Implemented behavior |
|---|---|
| Departure is observed after the event. Starting a 30-minute grid at physical departure can omit a trip cancelled after 25 minutes. | Trigger the first snapshot when departure becomes known, 20 seconds later in this source. An independent build test verifies the short cancelled trip retains its first snapshot. |
| A historical arrived status may later be corrected. | Use the latest status known at each snapshot. A later-known correction can resume snapshots but cannot alter earlier knowledge. Selected status records enter source lineage. |
| Device validity and knowledge intervals differ. | Check both intervals, retain missing positions when relationships are unknown or unresolved, and test revisions and half-open intervals. |
| Planned departure or late-known outcomes can contaminate historical durations. | Use independent departure and mature gate-arrival observations, require temporal availability and adequate precision, and restrict history to non-self training trips with full lineage. |
| Arrival status may arrive after physical arrival. | Preserve snapshots consistent with available knowledge, label them `already_arrived`, and exclude them from remaining-time regression. Hidden future truth cannot stop the earlier snapshot process. |

The resulting trial had 937 snapshots, including 11 cancelled labels and 59 unavailable-position snapshots. The [historical M1 summary](../reports/m1_reference/m1_summary.json) records the full distribution. Changed row counts represent corrected temporal behavior, not improved model performance.

### Source publication and integrity

During an overwrite and complete pipeline execution, observed on-disk row counts differed from the generator's returned counts, causing downstream relationship checks to fail. An isolated rerun did not reproduce the discrepancy. A single root cause was not established.

Generator version `ds-m01-synthetic-generator-1.0.1` stages files on the same filesystem, flushes and fsyncs them, checks row counts and SHA256 digests, publishes with per-file atomic replacement, verifies the published content, and publishes the scenario registry last. This guarantees atomic replacement of individual files, not one transaction across the entire project. Execution must still follow generation, ingestion, build and validation order.

A source manifest records verified counts and hashes. When enabled, pipeline and warehouse entry points validate the manifest before processing and stop if generated sources have changed. An independent publication-guard test covers this rejection. The [historical rebuild](../reports/m1_reference/rebuild_report.json) and [generator validation](../reports/m1_reference/generator_validation.json) record the completed checks after the configuration was frozen.

## 2026-10-27 — M2, application 0.2.0

- Retained all 1,800 synthetic trips and complete point-in-time outputs and historical references.
- Indexed trip inputs and streamed history/provenance with staged per-file publication. All seven original M1 processed files remained byte-identical under the M1 configuration.
- Added exhaustive manifest coverage for all 13 raw sources, with count and SHA256 checks. Malformed or truncated rows fail closed.
- Created 28 typed schemas and Parquet exports, keeping 11 future model and alert tables explicitly empty.
- Replaced repeated full-snapshot scans and Python provenance retention with indexed lookup and SQL validation.
- Added maturity, cohort support, historical support and M1 continuity checks.
- Excluded staging and generated build outputs from source distribution.
- Recorded 49 passing unit tests, 27 passing warehouse checks and 22 passing readiness checks.
- Completed a fresh rebuild with 48 equal source/processed/Parquet hashes and seven equal HTTP-derived outputs; see [rebuild results](../reports/rebuild_report.json).
- The initial profiling launcher could not start because `/usr/bin/time` was unavailable. Python resource profiling then succeeded; [runtime profile](../reports/runtime_profile.json) records the measured run.

Model training, interval and probability calibration, final model evaluation, alert/action workflows and native PostgreSQL roundtrip remain future work. Empty downstream schemas are not evidence of implemented model or alert behavior.

## 2026-10-27 — Public documentation and reporting cleanup

- Reorganized public documentation around the ETA problem, implemented data pipeline, evidence and limitations.
- Standardized technical explanations in English and retained historical M1 references separately from M2 evidence.
- Added standalone English data-quality figure generation and a validation guide.
- Kept historical data receipts distinct from the [current cleanup validation](../reports/public_cleanup_validation.json). The complete M2 dataset was not rebuilt during this cleanup.

## 2026-10-27 — English contract metadata

- Published English field labels, definitions, requirement descriptions and validation cases while retaining machine identifiers, types, nullability and input relationships.
- Corrected time labels that conflicted with the existing seconds units; runtime units and values are unchanged.
- Clarified inherited planning descriptions against the effective 2026 configuration.
- Removed unused configuration metadata outside the pipeline scope.
- Verified 341 field definitions, runtime configuration equivalence and a 120-trip compatibility replay. The replay reproduced all 48 raw, processed and Parquet files byte-for-byte; see the current cleanup validation receipt.
