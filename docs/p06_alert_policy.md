# Alert-policy development and formal output protocol

P06 covers M3d.2 and M4.1: choose an experimental alert rule on the declared July development cohort, then produce typed, versioned offline outputs. It does not retrain a model, fit another calibrator, simulate notification delivery or approve operational use.

The protocol is [config/p06.json](../config/p06.json), based on public P05 commit `04354b54432fc19ffa445151ae8b9aef0a45a816`. Its frozen SHA256 is `7b7dbe70ec1d44199bd971607eeec9a029fdee5567a3c4fa1f42f7fac9ee0470`. The configuration was fixed on November 6, 2026, Los Angeles time, before the P06 threshold sweep. **The P05 July assessment outcomes and quality results were already known.** This is a prospective specification for this sweep, not a claim of a blind protocol or an independent policy test.

The two capacity limits below—50 candidates per 100 monitored trips and two candidates per tenant per local day—are hypothetical research settings. They are not measured dispatcher capacity, fleet requirements or monetary false-alert costs. They must not be relaxed or the threshold grid extended after inspecting this sweep's results.

## Population and protected data

Reuse only the P05 `assessment` cohort: 42 trips departing July 15–24, 2026, in `America/Los_Angeles`, with 341 retained prediction opportunities. A trip keeps its role across midnight. Policy labels retain the exclusive August 1 00:00 PDT cutoff (`2026-08-01T07:00:00Z`). All opportunities remain in the monitored population, including unavailable forecasts and unknown, immature or otherwise excluded targets.

The [P05 protocol](m3d_calibration.md) separately used original training trips with knowledge strictly before July 1, July 1–10 calibrator-fit trips with knowledge strictly before July 15, and a July 11–14 buffer. P06 preserves those models, learned preprocessing, calibrators and knowledge boundaries. Its later policy-label cutoff does not replace the calibrator-fit cutoff in a model-version record.

P06 reads the retained July probability targets already bound by P05. The binary target compares independent, eligible gate arrival with the promise version effective and known at that snapshot: arrival strictly after the deadline is delayed; equality is on time; grace is zero. A later promise revision does not rewrite an earlier candidate's label. Target eligibility is not an input to the decision rule.

The August–September final-test target values remain outside fitting, policy selection and scoring. Original heldout route and vehicle exclusions remain unchanged. Reading metadata or verifying the digest of a shared source file does not authorize protected outcome inspection. No independent final-policy performance estimate is obtained by selecting and describing a policy on this reused July cohort.

## Forecasts and decision inputs

Use the P05 calibrated pipeline without model or calibrator reselection. A candidate requires an available ETA and an available probability with a fitted probability calibrator. Unsupported calibration is not silently treated as calibrated risk or replaced with a threshold selected for raw probabilities. Keep the base native/fallback identity, calibration status and unavailable reason in the opportunity audit.

The policy receives one outcome-free record per `(tenant_id, snapshot_id)`. It includes the snapshot clock, trip status as known at that clock, effective promise/version, prediction version, forecast availability and calibrated probability. It contains no target eligibility, arrival outcome or delay label. The separate scorer joins targets only after verifying snapshot, trip and promise identities.

Convert the calibrated probability using `Decimal(str(value))`, rounded to six decimal places with `ROUND_HALF_EVEN`. This canonical decimal probability is the value used by the decision rule and stored in the formal prediction output. Retain the original precise probability alongside it in the audit. Decisions therefore do not depend on an unrecorded higher-precision value at a threshold boundary.

For canonical ETA output, round the median remaining time to whole seconds with `ROUND_HALF_EVEN`, floor the lower bound and ceil the upper bound, then expand the projected bounds if needed to include the rounded median. Derive arrival timestamps from the snapshot and these integer remaining times. Retain the precise source endpoints. This is an outward storage projection, not a new interval calibration or a change to P05's measured coverage.

## Candidate rules and state transitions

Evaluate one global threshold at each value from `0.00` through `1.00` in increments of `0.05`, plus an explicitly disabled reference. The decision comparison is `probability >= threshold`; probability exactly one can trigger a threshold-one rule. A disabled rule therefore needs `enabled=false` and `threshold=null`, rather than relying on threshold one alone.

| Rule component | Frozen behavior |
|---|---|
| Active trip | Only point-in-time `in_transit` status permits a candidate |
| Minimum notice | Snapshot-effective promise deadline minus snapshot time is at least 1,800 seconds; equality is admitted |
| Candidate clock | Historical snapshot time, assuming zero candidate-generation latency for research replay; this is not delivery latency |
| Processing order | Snapshot time, tenant ID, trip ID, snapshot ID |
| Cooldown | At least 1,800 seconds since the last admitted candidate for the same tenant/trip, across episodes and promise revisions |
| Daily capacity | At most two admitted candidates per tenant per `America/Los_Angeles` calendar day |
| Global burden limit | At most 50 admitted candidates per 100 monitored trips, counting all monitored trips and all admitted candidates |
| Duplicate inputs | Identical tenant/snapshot records are idempotent; conflicting duplicates fail |

Only admitted candidates consume cooldown and daily capacity. Unknown outcomes still consume both; post-hoc target exclusions cannot free capacity for another candidate. Report every opportunity's decision, including unavailable, below-threshold, insufficient-notice, suppressed and admitted states, using the implementation's documented status vocabulary.

An available, supported high probability opens or continues a risk episode. An available, supported probability below threshold, or a known terminal state (`arrived`, `cancelled`, `incomplete`, `lost`), closes it. Missing or unsupported forecasts do not resolve risk. An open episode at the end of observed opportunities is censored; do not use a later actual arrival to fabricate a historical closure.

A promise revision changes the deadline available to later snapshots, but does not reset the episode or cooldown. Every admitted candidate freezes its own effective promise version and deadline. Stable candidate identities prevent a replay from generating duplicate candidates.

These candidates and episodes are research audit records. Their suppression, repetition and prospective notice times are not delivered-notification counts or observed time available to a dispatcher.

## Threshold selection

Retain all 21 threshold trials and the disabled reference, with their rules, metrics and feasibility. Do not first remove unknown-label opportunities or restrict the candidate stream to scoreable trips.

Require at least ten scored trips, two actionable positive trips and two negative trips for selection. Here, a scored trip has at least one mature eligible target, regardless of forecast availability; this minimum is label-only support. Report trips with a usable forecast and label separately. A negative trip has at least one mature eligible negative snapshot; a trip may contain both positive and negative labels after promise changes, and that overlap must be disclosed. An actionable positive trip has at least one mature eligible positive snapshot whose prospective deadline lead is at least 1,800 seconds, including a snapshot whose forecast is unavailable. Missing forecasts do not reduce these target-defined support counts; if no candidate can capture a positive trip, the separate positive-capture requirement disables selection.

A feasible threshold must satisfy

```
100 * all_admitted_candidates <= 50 * all_monitored_trips
```

The daily cap is enforced before admission as part of the simulated rule, not averaged away by this global constraint. A threshold must capture at least one confirmed actionable true-alert trip to be selected. Rank feasible thresholds by:

1. Most confirmed actionable true-alert trips.
2. Fewest admitted candidates across all monitored opportunities.
3. Highest threshold when the preceding quantities tie.

A true-alert trip has an admitted candidate at an eligible positive snapshot, scored against that candidate's frozen promise. Insufficient support or no feasible threshold with confirmed positive utility yields an explicitly disabled selection. Keep the complete unsuccessful trial table; do not invent a satisfactory policy or adjust the budgets after the sweep.

## Development metrics and denominators

Report actionable-trip recall and recall over all observed positive trips separately. The latter includes positive trips without a 30-minute actionable opportunity. A trip is counted once in a recall numerator or denominator; extra snapshots do not create extra trips.

The first candidate is the chronologically earliest admitted candidate for each tenant/trip. Let `T` and `F` count first candidates with eligible positive and negative targets; let `U` count unresolved first candidates and `N` count all first candidates. Report:

| Measure | Definition |
|---|---|
| Conditional first-candidate precision | `T / (T + F)`; null when no first candidate has an eligible target |
| Candidate target coverage | Eligible positive or negative candidates divided by all admitted candidates, including repeats |
| First-candidate usefulness bounds | Lower `T / N`; upper `(T + U) / N`; null when there are no candidates |
| Candidate burden per 100 monitored trips | `100 * admitted candidates / all monitored trips` |
| Repeated candidates within episodes | Sum of `max(episode candidate count - 1, 0)` |
| Additional candidates per alerted trip | Total candidates minus alerted trips; separate from the within-episode repeat count |

Verified retrospectively post-arrival first candidates are known to be non-useful and do not enter the upper-bound numerator. The bounds describe unidentified usefulness among unresolved targets; they are not confidence intervals and have a different denominator from conditional precision. Unknown targets are not negative outcomes. All admitted candidates consume burden, including unresolved candidates and reminders.

Report candidates later found to occur after physical arrival, without retrospectively removing them from the candidate stream or burden. Such rows distinguish the state known at decision time from later ground truth. Mixed-promise-label trips are the intersection of trips with any eligible positive snapshot and trips with any eligible negative snapshot. Do not manufacture a trip-level confusion matrix by assigning one unchanging label across different promises. Retain per-tenant support and mark strata with fewer than 20 scored trips as sparse. These are development diagnostics on synthetic observations, not estimates of fleet alert precision, dispatcher workload, savings or operational benefit.

## Formal offline outputs

The formal-output phase requires typed JSONL, DuckDB and Parquet representations of five canonical tables. Actual row counts and successful validation belong to the run receipt, not this protocol.

| Canonical table | Meaning in this phase |
|---|---|
| `eta_model_version` | Existing refit, feature, split and calibration versions with their original knowledge cutoffs |
| `eta_evaluation_membership` | July policy-development membership, permitted strata and equal per-trip opportunity weight |
| `eta_prediction` | Available projected ETA values, independently calibrated decimal delay probability and prediction version |
| `eta_alert_rule_version` | Selected threshold and notice/suppression settings, bound to the full threshold-selection record |
| `eta_output_release` | Tenant-qualified, provenance-linked output identity with `release_mode=offline_replay` |

`generated_at` and `released_at` record actual execution time. Historical decision clocks remain separate. No row implies the model or policy was deployed in July. An unavailable ETA produces no canonical prediction row, while its explicitly null-valued opportunity audit remains visible.

Membership includes all 341 opportunities, not a target-filtered subset. Each trip's weights sum to one after nine-decimal projection and deterministic residual allocation. Canonical lead-time and evaluation strata must use the existing field contract's permitted values. These weights describe the complete opportunity cohort; target-specific scored denominators remain separately defined.

Bind model versions to the correct tenant and retained source states through an auxiliary typed registry. Bind outputs, snapshots, promises and provenance through a typed source-reference audit. Validate field shapes, required values, controlled categories, primary keys, tenant-qualified relationships, source and rule bindings, time/projection consistency and Parquet roundtrips. Write only to a fresh output location and publish the complete output directory atomically after validation. Preserve the original M2 database and exports.

For an explicitly disabled policy, the nonnullable canonical threshold stores `1` only as a placeholder. Consumers must join the rule audit and honor `enabled=false`. The threshold value alone cannot establish whether the rule is disabled.

The six later canonical tables—`eta_alert_episode`, `eta_alert`, `eta_notification_delivery`, `eta_adoption`, `eta_action` and `eta_action_followup`—remain reserved and empty. Auxiliary candidate/episode audits do not populate these tables. No delivery, retry receipt, opening, adoption, dispatcher action, handling time or intervention follow-up is simulated in P06.

The retained M2 row counts remain historical. A separate P06 formal-output store does not rewrite M2 receipts or prove a combined production warehouse or service integration.

## Verification and remaining quality limits

The run must bind the frozen configuration, field contract and P05 input digests. Retained replay should reconstruct the selected policy, candidate decisions, metrics and formal-output checks with their declared scope. Unit and independent checks must cover temporal promise identity, exact threshold/notice/cooldown boundaries, duplicate handling, unknown-label burden, capacity, disabled behavior and formal-output relationships. The actual receipts must say whether raw sources were opened and whether any fitting occurred; a generic pass is insufficient evidence of either.

P05's whole-trip interval coverage remains 19/29 (65.5%), below the nominal 80%. Its wider calibrated intervals have worse interval score, and probability calibration worsens Brier score and log loss. Selecting a rule does not repair those limitations, and successful engineering checks do not constitute quality acceptance.

Independent evaluation of a frozen policy, notification delivery and adoption/action workflows remain later work. The final-test boundary must be preserved until a separately authorized evaluation stage. Nothing in this protocol establishes operational approval or real fleet benefit.
