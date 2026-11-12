# Frozen final-evaluation specification

P08 (M3e) will evaluate the fixed ETA, delay-probability, candidate and synthetic
delivery pipeline on the original August–September test period. This document
and [config/p08_evaluation.json](../config/p08_evaluation.json) are specified in
P07 before any P07 delivery simulation or final-test target use. The P05/P06
July findings are already known; this is not a claim of blind July development.
Writing the specification does not mean the final evaluator exists or final
evaluation has run.

The P07 protocol-freeze record binds the specification and existing artifacts
before the first delivery simulation. A later evaluation-readiness record binds
the validated delivery code and P07 run evidence to the unchanged protocol,
explicitly marking July delivery results as already observed. P08 must still
register its new final-evaluation implementation and conformance tests before
accessing final targets. These are separate evidence stages, not a retrospective
claim that all code existed when the specification was written.

## Frozen population and artifacts

Use every original `test` trip departing August 1–September 30, 2026, inclusive,
in `America/Los_Angeles`. Group by tenant/trip throughout; a snapshot across
midnight keeps its trip's role. The final outcome-knowledge cutoff is
`2026-10-15T07:00:00Z`, exclusive. The metadata-only trip and snapshot inventories
must be hashed before target access, with no target-based pruning. July's
42-trip/341-opportunity assertions do not define the final population.

Report four groups: seen vehicle/seen route, unseen vehicle/seen route,
seen vehicle/unseen route, and unseen vehicle/unseen route. Preserve the original
`ROUTE-12`, `T01-V10`, `T02-V10` and `T03-V10` holdouts. Keep empty or sparse
groups visible with counts and null reasons; do not combine them after seeing
their errors.

The configuration pins the existing source files, original split, field and
feature contracts, P05 R01 models, transformers, frozen training history,
calibrators, and selected P06 rule. No model, vocabulary, imputation value,
history pool, calibrator, threshold or fallback rule is refitted using July or
test labels. The policy remains calibrated probability `>=0.550000`, with
1,800-second candidate notice/suppression and two candidates per tenant/day.

P08 needs a separate label-free test inference path. Historical P05 loaders
deliberately reject test/heldout feature payloads; P06 binds the July population.
Do not remove those historical guards to make a final evaluation work. New
code must apply the exact frozen states to the original test inventory and
register its source hashes and conformance tests before final outcomes are
opened. Existing core source hashes in the configuration remain binding.

## Order of execution and targets

First verify all pins and metadata inventories. Generate and retain every raw
and calibrated forecast, canonical projection, policy decision and transport
ledger without target fields. Freeze their hashes. Only then join the authorized
final target groups and calculate all specified results. Generator outcome
truth, future arrival or a target eligibility flag cannot enter inference,
policy, delivery draws or stress-case selection.

Choose the latest outcome version known strictly before the final cutoff,
including revisions that withdraw or cancel earlier evidence. Ambiguous
same-known-time versions fail. An older verified version cannot replace a
later cutoff-known withdrawal. ETA uses independent verified gate arrival with
second or minute precision and exact positive remaining time. Binary delay uses
second-precision arrival and the original snapshot-effective promise:
`arrival > deadline`, equality on time, zero grace. Service start and delivery
completion are not arrival substitutes.

Keep ETA and binary eligibility separate. Cancelled, incomplete, pending,
withdrawn, unknown and already-arrived targets retain explicit reasons; they
are never filled with on-time labels or zero-duration targets. A later promise
revision cannot rewrite an earlier candidate's label or delivered notice.

## Forecast metrics and denominators

Availability uses all test opportunities. For each scored metric, cohort and
slice, first average snapshots within each scored trip, then give each trip
equal weight. Publish eligible-before-availability and scored-after-availability
counts, native/fallback/unavailable output, and fitted/unsupported calibration.

ETA reporting includes median MAE and RMSE, whole-trip interval coverage,
trip-weighted snapshot coverage, width, and interval score at alpha 0.2.
Whole-trip coverage uses all trips with an eligible ETA target: every eligible
snapshot must have an interval and be covered; a missing interval is a miss.
Conditional coverage among fully available trips has a separate denominator.
Raw pinball scores at 0.1/0.5/0.9 may be reported for the raw quantiles; expanded
calibrated endpoints are coverage bounds, not re-estimated quantiles.

Probability reporting includes Brier score, log loss, ROC AUC, average precision,
ten fixed reliability bins, ECE and class support. Use epsilon `1e-15` for log
loss and count clipping. Bins are left-closed/right-open except the final bin
includes one; use full scored-cohort trip weights within bins. AUC and average
precision are null with an explicit reason for a single-class group.

Score precise retained forecasts for proper-score/calibration diagnostics.
Keep the canonical six-decimal half-even probability used by the policy
separate. Formal ETA storage rounds median seconds half-even, floors the lower
bound, ceils the upper bound and contains the rounded median. Report the effect
of this storage projection separately; do not call it a new calibration.

The saved historical ETA and probability-prevalence baselines, plus raw versus
calibrated variants, are explanatory references. Use fixed paired support for
differences and report lost support separately. Final comparisons cannot promote
a new model or switch a supported calibrator to identity because it scored worse.

## Candidate and successful-delivery evaluation

Retain P06 candidate measures for comparability, then report successful-delivery
measures separately. All monitored trips count toward burden. Actionable positive
support means a mature eligible positive snapshot with at least 1,800 seconds
of prospective deadline lead, even when its forecast is unavailable or its
candidate is blocked. Mixed-promise trips can contribute both positive and
negative snapshots; there is no invented single trip-level confusion matrix.

First successful delivery means the earliest confirmed service receipt for a
tenant/trip, chosen without targets. It carries its own candidate's frozen
snapshot/promise. A failed first candidate does not select the label for a later
successful alert. A useful delivery needs an eligible positive target,
`deadline - delivered_at >= 1800`, and actual arrival strictly after delivery.
Delivered actionable recall divides captured trips by the same label-defined
actionable-positive trips used for candidate recall. No successful delivery or
insufficient delivered notice is a miss.

Useful-first-delivery precision divides useful first successful deliveries by
all known-assessable first successful deliveries: mature eligible targets plus
verified exact `already_arrived_at_snapshot` targets. Those verified post-arrival
rows are excluded from binary modeling but are known non-useful deliveries.
Known false, too-late and post-arrival deliveries remain in the denominator.
Known insufficient prospective lead or verified post-arrival timing establishes
non-usefulness even if the binary delay target is unavailable. Only still
potentially useful deliveries without the required outcome evidence remain
unresolved; this timing classification does not invent an on-time label.
Show target coverage and unresolved outcomes. Across all first deliveries `N`,
with confirmed useful `T` and unresolved `U`, usefulness bounds are `T/N` to
`(T+U)/N`; known non-useful cases do not enter the upper numerator. These are
identification bounds, not confidence intervals. The measure differs from
P06's positive-first-candidate precision.

Use the frozen P07 transport law, bounded retries and successful-delivery
cooldown. Set receipt accounting to `2026-10-15T07:00:00Z`; receipts known **at or
before** that boundary count, while final outcome knowledge remains strictly
**before** it. Retain queued/unknown late confirmations as censored. Failed
attempts are not delivered burden or rejected adoption. All successful
deliveries, including repeated successes, count toward notifications per 100
monitored trips. Candidate/day capacity is distinct from receipt-day volume.

Report candidate lead, first-successful delivered lead, actual-arrival advance,
and transport latency as different clocks and distributions. First-effective
delivery lead is a secondary description among captured trips, never a
replacement for full recall or first-successful-delivery denominators. The
actual offline execution timestamps remain separate from simulated event time.

## Predeclared stress cases

All transformations below use metadata/features, never labels. Keep the original
opportunity inventory, model/calibrator states and targets; record exact
before/after values and affected/not-applicable counts. These feature perturbations
test input sensitivity. They are not new physical source observations or causal
predictions of what arrival would have been under an intervention.

| ID | Frozen case |
|---|---|
| S00 | Unmodified final cohort and standard frozen P07 transport; primary result |
| S01 | Every opportunity has `no_position`; remaining distance, speed and age become null with the corresponding missing indicators |
| S02 | For originally positioned opportunities with elapsed time at least 1,800 seconds, set age to 1,800 and validity to `low_confidence`, mask distance/speed; other rows stay unchanged |
| S03 | Send the route categorical input through the existing unknown slot using the fixed absent token `__P08_UNSEEN_ROUTE__`; retain original source identity and novelty group separately |
| S04 | Report all four original route/vehicle holdout groups without transforming them; vehicle IDs are not model predictors |
| S05 | Per trip, the earliest three consecutive originally valid-position opportunities after 3,600 elapsed seconds and spanning at least 3,600 seconds hold the first remaining distance, speed zero and age zero; otherwise unchanged/not applicable |
| S06 | Scale route distance and nonnull remaining distance by exactly 1.25, preserving nulls and valid bounds; do not refit the saved preprocessing |
| S07 | Transport outcomes 60% delivered, 25% transient failure, 10% permanent failure and 5% unknown; latency 60–180 seconds and receipt lag 0–30 seconds |
| S08 | Apply S01 and S07 together, as one fixed combination |

S02 keeps age at most elapsed time; S05 requires known feature values, retains
all clocks and does not invent changed arrival targets; S06 retains positive
route distance and bounded nonnegative remaining distance. S03 must assert the
fixed token is absent from each saved vocabulary before scoring. Stress feature
audits distinguish overrides from original raw source and lineage claims.

S07 preserves the P07 seed, version, independent dimension keys, outcome-bin
order, maximum three attempts, 30/120-second backoffs, 120-second unknown timeout,
600-second lifetime, notice and successful-delivery cooldown. It transforms
the same hash-derived exogenous integers through the declared scenario ranges;
it never redraws until success. All scenarios are retained; none can replace
S00 because it produces a more convenient result.

Also report August versus September and known elapsed time below versus at
least 7,200 seconds. These metadata-defined views expose temporal and duration
shifts without selecting cases by future arrival or model error.

## Subgroups, uncertainty and conclusions

Besides the four novelty groups, report tenant, route, departure month, original
GPS validity, early trip (`elapsed <3600`), fixed snapshot deadline-advance bins,
native/fallback/unavailable source, calibration support and delivery scenario.
Flag groups with fewer than 20 scored trips. Keep zero/single-class groups with
null reasons. Do not introduce outcome-selected intersections or subgroup-specific
policies after evaluation.

For forecast metrics only, use the fixed 2,000-replicate whole-trip bootstrap
with seed `20261111`, sorted keys and tenant/novelty strata. Keep each trip's
snapshots and paired methods together. Use the same resamples for paired deltas
and linear 2.5/97.5 percentile interpolation. Suppress intervals below 20
original scored trips or when required replicate denominators are undefined,
recording the reason. Do not bootstrap AUC/AP in this protocol. These intervals
are descriptive, conditional on the fixed synthetic trajectories; they do not
guarantee future-fleet coverage or account fully for route/vehicle dependence.

Policy and delivery admissions share tenant/day capacity and cross-midnight trip
state. Do not apply independent-trip binomial confidence claims or redraw
transport while resampling. Report exact support, slices and unresolved-outcome
identification bounds instead.

Engineering completion requires unchanged pins, complete inventories, no target
leakage, correct temporal/tenant/version semantics, independent reference checks,
bounded delivery behavior and reproducible fresh outputs. Quality is separate.
Report whether observed whole-trip coverage reaches the nominal 80%, plus raw/
calibrated error changes, low recall and burden violations. No measured fleet
acceptance thresholds for precision, recall, width, costs or service latency
exist; do not invent them after scoring. P05/P06 limitations remain open and
operational approval remains false.

Unknown or unsupported outputs retain their denominators; undefined metrics
remain null. An engineering defect stops publication of a successful result.
A poor quality result is completed and retained without tuning. Preserve the
first final result and any subsequent correction separately; a corrected rerun
after target exposure is not another untouched test. P09 adoption/action
follow-up and later service integration remain outside this evaluation.
