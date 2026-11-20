# Implementation change log

Entries describe implementation changes and observed test findings. Unless a timezone is stated, dates are actual execution dates in UTC; they do not substitute for version-control history.

## 2026-11-19 America/Los_Angeles — reconcile field, table and scenario evidence

P12 adds per-item references for the 110 field requirements, 28 mandatory tables and 14 validation scenarios. The [methods and results](p12_methods_results.md) bring the original evaluation denominators, synthetic workflow observations and storage limits together. A new P009 loopback HTTP receipt binds its retrieval clock to a synthetic payload. Five hand-specified tests exercise gaps in the validation cases, including unequal snapshot weights and a repeated clock hour. A separate column profile records SQL counts from a reconstructed M2 generation. The first preparation run placed new files in the historical contract and report directories. The P08 replay check detected that its file inventory had changed. Moving the P12 additions into [their own directory](../workflows/p12/) restored the original inventory, and the parent integrity check passed. Model-quality findings G14–G16 remain open.

The original P11 workflow failed during GitHub configuration validation because two job-level environment expressions referenced the runner before allocation. The signed [workflow fix](https://github.com/JacobCCMorgan/ds-m01-eta-delay-alerts/commit/2757d817b76a14f2d91da915302979a10560f09f) moved runner-specific settings into steps and updated the registered workflow digest. The [original failure](https://github.com/JacobCCMorgan/ds-m01-eta-delay-alerts/actions/runs/44489366252) and [successful corrected run](https://github.com/JacobCCMorgan/ds-m01-eta-delay-alerts/actions/runs/44578265478) document both executions.

## 2026-11-17 America/Los_Angeles — reconstruct the complete warehouse and exercise correction/recovery paths

Added a separately registered P11 path from published P10 commit
`151ad00095fa84a3c9c975a8a74546a798a4a135`. Two clean M2 builds regenerate all
13 raw and seven processed JSONL files with exact historical SHA256 agreement,
then reconstruct 17 populated and 11 typed empty DuckDB/Parquet tables. All-column
logical relations and both independent builds agree. Measured time, peak memory
and output size are retained as environment-specific observations.

An actual HTTP witness retains four revisions across two tenant/event identities,
ten snapshots and explicit versioned provenance. Numeric revision selection uses
availability time before validity filtering. Exact replay inserts no new rows;
conflicting payloads and four real database-constraint violations are rejected.
Five worker processes exit abruptly at publication boundaries on a small typed-store
fixture; complete old/new
generations remain readable and recovery agrees with an independent clean build.
The supported recovery protocol is restricted to one filesystem and cooperating
readers/writers. The original direct warehouse and snapshot entry points remain frozen.

Review identified and fixed optional-timezone dependency during DuckDB readback,
source precision loss hidden by typed insertion, an exit-137 full-size warehouse
probe, an explicit full-size DuckDB comparison memory failure, and mixed
progress/JSON output. The new P11 warehouse applies a bounded
DuckDB memory setting, one worker thread and an owned spill directory; the earlier entry point
remains unchanged.
The [P11 notes](p11_rebuild_recovery.md) record those development findings and the
scope of their regression checks. No production incident is implied.

The final preparation gate passes 1,021 tests: 727 original, 17 portability,
115 P09, 67 P10 and 95 P11. It also reruns retained P09 checks and actual P10
PostgreSQL/HTTP integration. P11 preserves 1,428 immutable files from the published
P10 tree. The six current documentation/evidence-map files remain explicitly
mutable; earlier scientific implementations, reports and registrations do not change.

The new workflow requires `P11 full integration` success at the actual published
commit. User-machine adoption, signing/push, signature verification and observed
GitHub CI remain separate steps. G14/G15/G16, scientific-quality acceptance and
operational approval are unchanged. See the [P11 evidence](../workflows/p11/evidence/README.md).

## 2026-11-15 America/Los_Angeles — verify native storage and HTTP input paths

Added a separately registered P10 integration check from the published P09
baseline. A fresh PostgreSQL 17 cluster exercises 11 operational source tables
and 35 retained P09 output/audit tables in separate schemas. Catalog inspection,
634 negative probes, transaction visibility, commit, reconnect readback and
owned-schema cleanup supplement the successful typed-row comparison. The
retained run contains 13,706 source rows and 32,128 P09 rows.

Actual loopback HTTP requests retrieve all 197,303 synthetic GPS records with
cursor paging, an injected transient failure and explicit page replay. Direct
snapshot, HTTP and PostgreSQL-plus-HTTP paths produce equal projections for
14,349 snapshots and labels and 1,800 split/membership records. Thirteen raw
files and the four direct/HTTP projection files also match historical SHA256
evidence. PostgreSQL-plus-HTTP equality is measured on all logical fields;
equivalent timestamp representations are not treated as byte-identical files.

Preparation review strengthened exact CHECK-definition inspection and
cross-tenant foreign-key probes so a rejection by the wrong constraint cannot
stand in for the intended test. The publication guide now stops when dependency
installation fails. The final version passed all required checks again.

All 926 tests passed on Linux: 727 original, 17 portability, 115 P09 and 67 P10.
The native run used PostgreSQL 17.11 and finished at
`2026-11-15T19:32:11.916642+00:00`. P10 preserves 1,393 frozen parent files.
The new code and registration live under `workflows/p10/`; current evidence
maps add bounded service scope without changing historical counts or statuses.

The small preprocessing fixture compares seven processed JSONL outputs, not a
DuckDB/Parquet warehouse build. Full clean warehouse reconstruction, canonical
multi-revision telemetry (G11), coordinated database/Parquet recovery (G13) and
actual CI remain P11 work. P10's revision guard rejects unsupported inputs
without claiming their semantics are implemented. macOS validation, signing
and publication remain separate user steps. Model quality and operational
approval are unchanged. See the [integration scope](p10_integration.md) and
[retained receipts](../workflows/p10/evidence/README.md).

## 2026-11-14 America/Los_Angeles — retain synthetic recipient and operator workflow

Added a separately registered P09 extension of the fixed primary P08 scenario.
Confirmed delivery, recipient opening, review, adoption or override, operator
action and follow-up have separate clocks and denominators. Failed delivery does
not establish rejection, and retries cannot create duplicate decisions for an
alert. Target-independent identity hashes select synthetic behavior; these
frequencies are engineering fixtures, not observed human response rates.

The separate typed store populates adoption, action and follow-up contracts while
preserving the other 24 inherited P08 tables. Synthetic opening is auxiliary; the original
notification rows remain unchanged. Follow-up retains the original promise and a
separate revised promise, using the same arrival. Pending source versions remain
pending at the observation endpoint even if a later verified revision is available
to the analyst. No unknown outcome is converted to zero delay or a successful
intervention.

Added a self-contained, read-only viewer for all primary trips, with tenant and
trip filtering, event timelines and knowledge-time filtering. Automated tests cover
the retained viewer data and JavaScript interactions; actual browser rendering and
visual review remain pending. The viewer does not send messages or edit evidence.

The first preparation run exposed a staging-directory collision with the P09
source inventory. Transient output is now built outside the checkout and moved
into place only after validation, so it cannot masquerade as registered source.
The repair precedes the first P09 publication and does not alter any P08 file.

Preparation passed 727 original tests, 17 portability tests and 115 P09 tests.
The independent workflow reference passed 26,524 comparisons. The retained
scenario has 42 adoption states, 33 action records including two explicit
no-actions, and 31 follow-up records with only one comparable mature arrival.

All new code, settings, tests and results live under `workflows/p09/`, outside the
historical scientific inventories. Original P08 source, registration, model states,
policy, targets and metrics remain intact. P09 is development after final-outcome
exposure, not a new untouched test. G14/G15/G16 and quality/operational approval
remain unchanged. See the [workflow scope and commands](p09_workflow.md) and
[retained results and engineering receipts](../workflows/p09/evidence/README.md).

## 2026-11-14 America/Los_Angeles — separate numerical replay from retained-store integrity

Native macOS P08 replay reached the typed-store check and rejected
`p08_opportunity_audit`. A subsequent diagnostic on that machine passed the original
forecast comparison rules in all nine scenarios and exactly reconstructed every saved
JSONL table from its retained inputs. The displayed opportunity differences were raw
probabilities, with a maximum absolute difference of approximately `1.11e-16`, within
the frozen tolerance. Those values also changed metadata and audit hashes.

The original verifier compared freshly rebuilt raw floats with exact stored JSON hashes
after accepting them under its numerical tolerance. Added a separately versioned
post-exposure verifier that keeps the original numerical comparisons and reconstructs
stored-table expectations from the verified retained inputs. The existing exact store
checks remain active. The original verifier, scientific sources, implementation freeze,
models, protocol, predictions, targets and metrics remain unchanged.

The supplemental verification registration and regression tests are separate from the
original 727-test pre-target conformance record. This is a verification correction, not
a new untouched final test, model refit or quality acceptance. The native diagnostic
is not evidence of a successful original strict replay or GitHub publication. See the
[verification revision](p08_portable_verification.md) for commands and evidence scope.

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
