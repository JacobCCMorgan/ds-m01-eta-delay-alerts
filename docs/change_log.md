# Implementation change log

Entries describe implementation changes and observed test findings. Dates are actual execution dates in UTC; they do not substitute for version-control history.

## 2026-11-14 UTC — evaluate the frozen pipeline on the original final cohort

Registered new inference, target, metric, policy/delivery adapter and typed-store code
with passing pre-target conformance checks. Retained and hashed all nine label-free
scenario ledgers before opening final targets. Existing model, calibration, policy and
transport sources stayed fixed; July development results were already known.

The first execution stopped before final target access because the historical-report
inventory included its own temporary P08 output directory. The execution guard was
repaired to exclude only reserved P08 output/attempt directories and to check this
before inference; 727 tests then passed and the implementation was registered again
before target access. The scientific configuration, models, inputs and metadata
inventory did not change. All 97 compared input/scenario artifacts sealed before target
access had identical hashes. That comparison was recorded after the first target access
and does not move the original freeze clocks. This was a pre-target execution repair,
not a post-target scoring correction. The [repair
history](../reproducibility/p08/history/pre_target_revision.json) preserves the failed
attempt and earlier registered source.

Every scenario retains 400 original August–September trips and 3,200 opportunities,
including all four novelty groups (325/37/34/4 trips). Primary S00 whole-trip interval
coverage is 243/328 (74.1%); useful delivered-alert recall is 26/137 (19.0%);
conditional useful-first precision is 26/38 (68.4%). The full report retains every
scenario, exclusions, unresolved targets, paired supports and descriptive uncertainty.
No result promotes a model or changes the selected rule.

The suite passed 727 tests; the independent reference passed 10,349,858 comparisons with
zero failures, and retained replay found zero differences. Each scenario has 27 typed
tables, including eight canonical outputs and three reserved downstream tables. Actual
offline clocks remain distinct from synthetic event clocks. Quality/operational
acceptance stays false and G14/G15/G16 remain open. P09 adoption/action/follow-up and
the viewer are still future work. See [final findings](p08_final_evaluation.md) and
[results](../reports/p08/README.md).

## 2026-11-13 UTC — handle temporary-directory aliases in replay tests

Two delivery replay mock assertions failed on macOS because the fixture kept
the `/var/...` spelling while replay resolved the same directory to
`/private/var/...`. An aliased `TMPDIR` reproduced both failures on Linux.
The fixture now resolves its root; a regression also passes symlinked root and
report paths and checks that replay leaves evidence files unchanged.
All 577 tests pass with the aliased temporary directory on Linux. Revalidation
on the reporting Mac remains pending.

The test-only change is delivered as a separate follow-up commit. The original
readiness record is retained in `reproducibility/p07/history/`; the current
record identifies that predecessor and updates only this test's artifact pin
with the actual amendment time. Production sources, protocol assumptions,
models, retained results, and their pins are unchanged. No final evaluation,
model refit, policy reselection, or quality acceptance is implied.

## 2026-11-11 UTC — simulate delivery and freeze final-evaluation requirements

Retained the P06 policy, models, calibrators and July development population.
Froze transport assumptions and the P08 evaluation specification before the
first P07 simulation. The protocol does not pretend July outcomes were unseen:
the P05/P06 assessment results were already known.

The fixed scenario turned nine candidate alerts into eight attempts and seven
confirmed synthetic deliveries across 42 monitored trips. Two alerts were
suppressed by delivery cooldown; one transient failure led to one successful
retry. There were no delivered repeats. Four of 18 actionable delayed trips
received a useful delivery. Conditional useful-first precision was 4/6; one
of seven first deliveries had an unresolved target. These are simulated July
development results, not real notifications or independent final performance.

Added three canonical alert/episode/delivery tables to the five P06 output
tables, while retaining the adoption/action/follow-up schemas without rows.
Delivery clocks belong to the synthetic July scenario; inherited offline output
releases retain their actual November execution timestamps. Transport receipts
do not establish recipient opening or adoption.

The reference run passed 576 unit tests. Both replay modes found zero differences
in 9,093 cross-format logical row comparisons, and an independent reference
passed 9,226 comparisons with no failures. Two executions reproduced eight core
files exactly and all 26 tables after normalizing only P07 materialization and
provenance recording clocks. Scenario clocks and inherited P06 records remained
exact; database/Parquet byte identity and GitHub CI are not implied.

The [P08 protocol](p08_evaluation_protocol.md) fixes final cohorts, target
knowledge cutoffs, frozen pipeline versions, metrics, descriptive uncertainty
and outcome-independent stress transformations. A later readiness record binds
the validated P07 implementation to that specification. The future P08 evaluator
still needs its own implementation and passing pre-target conformance gate.
No August–September final outcome was used for fitting, selection or scoring.
P05 undercoverage and probability loss, plus P06 low recall, remain unresolved.
See the [delivery protocol](p07_delivery.md) and [run results](../reports/p07/README.md).

## 2026-11-07 UTC — develop an alert rule and materialize offline outputs

Reused the declared July assessment cohort for a frozen 21-threshold policy
sweep plus a disabled reference. The 30-minute notice/suppression settings,
two-candidate tenant/day cap and 50-candidates-per-100-trips budget are research
assumptions fixed before this sweep, after the P05 assessment results were known.
Neither models nor calibrators were refitted or reselected.

The selected 0.55 threshold generated nine candidates across 42 monitored trips
and captured 4/18 actionable delayed trips. Conditional first-candidate precision
was 4/6; one of seven first candidates had an unresolved target. Candidate burden
includes that unknown outcome and two repeats. These are development measurements
subject to selection optimism, not independent policy performance or delivered
notification metrics.

Materialized five canonical output tables in a separate DuckDB/Parquet/JSONL
store, retaining all 341 opportunities in typed audits and membership. Six later
alert/delivery/adoption/action tables remain empty. Added complete policy-trial
replay, source-context reconstruction, key/relationship/rounding checks and fresh
output publication. Historical M2 and P05 receipts remain unchanged.

The reference environment passed 489 unit tests, both retained/source replays
with zero differences, and 163,968 independent reference comparisons with no
failures. Two executions reproduced 14 deterministic files byte-for-byte;
20 JSONL tables matched after documented execution-clock and derived-digest
normalization. Database/Parquet byte identity, CI and user-machine execution
are not implied by these checks.

The [results](../reports/p06/README.md) retain sparse tenant slices, the missed
actionable trips and the unresolved P05 coverage/probability limitations.
August–September targets remain protected; there is no operational approval,
notification delivery or measured fleet benefit.

## 2026-11-05 UTC — assess fixed July interval and probability calibration

Refitted the selected ETA model and independent delay classifier on the original
training cohort with a July 1 knowledge cutoff. Froze separate July calibration
fit, maturity-buffer and assessment roles before using their targets. A tenant's
interval calibrator uses one maximum residual score per fit trip; its probability
calibrator uses a nonnegative-slope sigmoid with a fixed identity penalty.

On 143 scored snapshots from 29 later July trips, interval expansion increased
whole-trip coverage from 9/29 to 19/29, still below the nominal 80% target.
Trip-weighted snapshot coverage increased from 76.6% to 92.1%; mean width grew
from 62.53 to 89.75 minutes and interval score worsened. Median forecasts were
unchanged. Brier score increased from 0.2129 to 0.2208 and log loss from 0.6120
to 0.6303. The fixed methods were retained without a results-driven switch.

All six tenant calibrators had sufficient support in this run. Support failure
retains raw output with an explicit unsupported calibration status; invalid
inputs or optimization failures stop execution. The [results](../reports/m3d/README.md)
separate fitted transformations, measured assessment behavior and remaining
limits. P06 may use this assessment cohort for policy development. No alert
threshold, operational release or August–September final evaluation follows
from this experiment.

## 2026-11-05 UTC — classify delay against the snapshot promise

Added an independent logistic classifier and tenant/fold prevalence baseline
with a fixed, versioned promise-deadline feature. The target preserves the
promise known at each snapshot and uses the latest verified gate-arrival
outcome strictly before the relevant fold cutoff. Unknown, cancelled and
already-arrived outcomes remain explicit. Binary deadline labels require
second precision and add no grace period.

The recorded run produced 10,326 raw probabilities. On 511 scored trips,
logistic regression reduced Brier error from 0.2393 to 0.2061. Stale/missing GPS
and 0–30-minute advance snapshots remained weaker than the baseline. Results
retain those regressions, fixed-bin reliability and class support. No warning
threshold or subgroup switch was selected.

Development review strengthened fit-time visibility, complete preprocessing
membership, equal per-trip snapshot weights and identity-keyed target joins.
Portable JSON inference and source-derived label/score replay are checked
separately from fitting. See the [delay results](../reports/m3c/README.md) and
[independent checks](../reports/m3c/independent_checks.json). July calibration
and final-test scoring remain pending.

## 2026-11-04 UTC — compare the ETA candidates across chronological folds

Both candidates met the fixed availability, error and uncertainty requirements on
the 511-trip baseline-supported cohort. The boosted model had the lowest P50
mean absolute error, with larger errors for low-confidence or missing GPS.
The [model comparison](m3b3_comparison.md) includes scores for all three methods,
the common unrounded target, the paired trip bootstrap and the decision rule.
The source-clock fix preserves fractional event times for freshness tests and
derives stored whole seconds consistently across the data stages.

## 2026-11-01 UTC — integrated training replay verified

On October 31 in Los Angeles, the integrated command at `0814fa1` passed on Mac
Docker Desktop: two new Linux/amd64 containers reproduced the reference models,
predictions, training rows and summary byte-for-byte. Local checks at the same
commit passed 285 tests and reproduced 10,326 predictions without refitting.
GitHub subsequently reported valid signatures on all five commits in the batch.
No GitHub Actions run was recorded for that commit. See the
[verification summary](../reports/m3b2_integrated_replay_20261031.json) for
the tree, image and output hashes. The earlier native macOS fitting failure
still applies.

## 2026-11-01 — M3b.2 fixed-container replay integration

- Native macOS fitting failed the original numerical replay despite unchanged
  training rows, summary, numerical package versions and fixed seed. Retained
  JSON inference passed separately. The observed fitting difference does not
  establish a unique CPU, compiler or library root cause.
- A pinned Linux/amd64 runtime on Mac Docker Desktop completed two fresh full
  replays with the original model parameters, source bytes and tolerances.
  Each retained 24 model states and 10,326 predictions; models, predictions,
  training rows and summary matched the original reference byte-for-byte.
- Added an independent container entry point with exact source and payload
  verification, immutable local image selection, isolated read-only snapshots,
  new output directories, full replay and direct pair comparison. Failed child
  processes preserve diagnostics and stop subsequent runs.
- Kept the original receipt-bound training implementation and retained outputs
  unchanged. At this point only the earlier runtime experiment and host guard
  tests had run. The separate entry above records the later execution of the
  integrated command.
- Documented the observed Mac Docker scope, unsupported native macOS exact
  fitting, separate retained-inference check and untested additional hosts in
  the [reproducibility guide](reproducibility.md). No model-quality, calibration,
  final-test, CI or operational-performance claim is added.

## 2026-10-30 — M3b.2 quantile candidate training and inference

- Added two fixed quantile model families with per-tenant/fold fitting,
  trip-balanced supervised weights and validated JSON model persistence.
- Joined only mature fit-group outcome versions strictly before each fit cutoff;
  retained all 18,343 fit opportunities with explicit eligibility reasons.
- Trained 24 model states from 8,417 eligible fit rows and emitted 10,326
  predictions across both families. Preserved all validation opportunities,
  including baseline fallbacks and unavailable results, without joining labels.
- Retained raw quantile crossings and nonpositive estimates. Positive crossed
  estimates are sorted; invalid estimates follow the explicit fallback policy.
  Availability is not an accuracy comparison or a model-selection decision.
- During development review, corrected an incomplete outcome-status allowlist
  and replaced a positional prediction join with an identity-keyed join. Their
  regression tests cover withdrawn `lost`/`conflict` labels and reordered input.
  These were prepublication development findings, not real-fleet incidents.
- Added source/configuration/code drift guards, atomic publication, exact model
  and transformer binding, and retained-inference plus numerical training replay.
  See [M3b.2 evidence](../reports/m3b2/README.md) for results and limitations.

Candidate comparison, calibration, final evaluation and operational integration
remain later stages. The existing M3a/M3b.1 evidence has not been rewritten.

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
