# Final evaluation findings and interpretation

This is an engineering and method demonstration on fixed synthetic trajectories, not
customer telemetry or a fleet trial. G14 (July interval undercoverage/width), G15 (July
probability-calibration deterioration) and G16 (low July policy recall and selection
optimism) remain open. A final measurement does not itself resolve their causes or
establish acceptable costs, precision, recall or latency. `quality_accepted` and
`operational_approval` remain false.

S00 whole-trip coverage is 243/328 (74.1%), below the nominal 80%. The descriptive 95%
whole-trip bootstrap range is 69.97%–78.09%. Its 92.0% weighted snapshot coverage
measures something different. Interval expansion changes mean width from 62.85 to 95.69
minutes and interval score from 96.05 to 115.04 minutes (lower is better). Probability
Brier score changes from 0.195852 to 0.197842 and log loss from 0.576995 to 0.581029;
both point estimates increase after calibration.

Paired calibrated-minus-raw changes are Brier +0.001990 [-0.003051, 0.006628] and log
loss +0.004034 [-0.009236, 0.015918]; brackets are descriptive 95% trip-bootstrap
ranges. Both ranges include zero, so the small point-estimate deterioration is not
statistically established inferiority. The paired interval-score change is +1139.09
[980.55, 1299.06] seconds. These comparisons stay conditional on the fixed synthetic
sample and do not select a replacement pipeline.

The original unseen route, ROUTE-12, has whole-trip coverage of 0/33 (0.0%). The
seen-vehicle/unseen-route group covers 0/29 eligible trips, and the joint holdout covers
0/4. Available or native model output does not demonstrate successful generalization.
The four-trip joint group is sparse; no bootstrap interval is reported for it. A zero
observed rate or a degenerate [0, 0] resampling range for the larger route group
describes this sample, not proof that future-population coverage is exactly zero.

When all position inputs are removed in S01, whole-trip coverage is 47/328 (14.3%). This
shows substantial sensitivity to the declared missing-position perturbation; it does not
estimate a causal effect of a real outage on arrival time.

P08 / M3e applies the fixed pipeline to every original August–September test trip. S00
is the primary result: whole-trip interval coverage is 243/328 (74.1%); useful
delivered-alert recall is 26/137 (19.0%); useful-first precision is 26/38 (68.4%). The
[full report](../reports/p08/README.md) retains all nine scenarios, exact supports,
excluded targets and uncertainty.

The original P05 R01 models, preprocessing, history pools and calibrators, P06 0.550000
rule, and P07 transport algorithm are unchanged. July development results were already
observed. The P08 implementation and conformance evidence were registered before final
target access, then all nine forecast/policy/transport ledgers were retained and hashed
before targets were joined. No final result selects a model, calibrator, threshold,
fallback or preferred stress scenario. S00 remains primary.

## Reading the results

ETA scoring has 1,579 eligible snapshots from 328 trips before availability; 1,579
snapshots from 328 trips are scored. Binary scoring independently has 1,579 eligible
snapshots from 328 trips and scores 1,579 snapshots from 328 trips. The remaining 1,621
ETA and 1,621 binary opportunities keep explicit exclusion reasons.

| ETA output | Available snapshots | Scored snapshots / trips | MAE (min) | Whole-trip coverage | Weighted snapshot coverage | Width (min) | Interval score (min) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Historical ETA baseline | 2,142/3,200 | 1,569/328 | 21.23 | 196/328 (59.8%) | 74.2% | 69.38 | 122.75 |
| Raw pipeline | 3,200/3,200 | 1,579/328 | 17.54 | 149/328 (45.4%) | 79.4% | 62.85 | 96.05 |
| Calibrated pipeline | 3,200/3,200 | 1,579/328 | 17.54 | 243/328 (74.1%) | 92.0% | 95.69 | 115.04 |
| Stored projection | 3,200/3,200 | 1,579/328 | 17.54 | 243/328 (74.1%) | 92.0% | 95.70 | 115.05 |

| Probability output | Available snapshots | Scored snapshots / trips | Brier | Log loss | Fixed-bin ECE | ROC AUC | Average precision |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Training prevalence | 3,200/3,200 | 1,579/328 | 0.2360 | 0.6647 | 0.0171 | 0.5717 | 0.4404 |
| Raw classifier | 3,200/3,200 | 1,579/328 | 0.1959 | 0.5770 | 0.0415 | 0.7505 | 0.6800 |
| Calibrated classifier | 3,200/3,200 | 1,579/328 | 0.1978 | 0.5810 | 0.0497 | 0.7480 | 0.6775 |
| Six-decimal projection | 3,200/3,200 | 1,579/328 | 0.1978 | 0.5810 | 0.0497 | 0.7480 | 0.6775 |

The primary run has 42 first successful deliveries, of which 38 are known-assessable and
4 remain unresolved. Conditional useful-first precision is 26/38 (68.4%);
all-first-success usefulness is bounded by 61.9%–71.4%. These are identification bounds,
not confidence intervals. Known post-arrival or insufficient-notice deliveries are
non-useful even when their binary modeling target is excluded. Unknown targets are never
relabeled on time. The count of attempts with unknown transport status is 1, separate
from the 4 unresolved first-delivery targets.

Neither a high snapshot coverage rate nor a low burden rate establishes acceptable
whole-trip coverage or useful-alert recall. The interval target is nominal 80%; there
are no measured fleet acceptance thresholds for width, precision, recall, cost or
service latency. Negative results remain part of the experiment.

## Design boundaries

Whole-trip membership follows local Los Angeles departure dates, including snapshots
crossing midnight. Outcome versions must be known strictly before 2026-10-15T07:00:00Z;
receipt knowledge at that exact clock counts. Binary delay means independent
second-precision gate arrival strictly after the original snapshot promise, with zero
grace. ETA also accepts minute precision. Service start, completion and partial proof of
delivery are not arrival substitutes.

Forecast-metric uncertainty ranges use the fixed 2,000-replicate
tenant/novelty-stratified whole-trip bootstrap. The predictive ETA bounds themselves
come from the frozen calibration states. Each trip keeps its snapshots and paired
methods together. These are descriptive intervals conditional on this synthetic sample,
not future-fleet confidence guarantees or a complete treatment of route/vehicle
dependence. Intervals are null below 20 original scored trips or with an undefined
replicate denominator. Policy/delivery measures use exact counts and identification
bounds; they are not trip-binomial confidence estimates.

Original novelty support is 325/37/34/4 trips. All groups, including sparse or empty
scoring cohorts, remain visible. Stress cases modify metadata-defined features or the
fixed transport law without changing the source targets; they do not represent physical
interventions. No case, subgroup or interval chooses a new policy.

The earlier [specification](p08_evaluation_protocol.md) is retained as written. It
describes what was frozen before execution; the implementation registration and [run
receipt](../reports/p08/run_receipt.json) establish when that work actually happened.
July results were already known. Subsequent replay uses exposed final targets and is not
a new untouched evaluation.

The first execution stopped before final target access because the historical-report
inventory included its own temporary P08 output directory. The execution guard was
repaired to exclude only reserved P08 output/attempt directories and to check this
before inference; 727 tests then passed and the implementation was registered again
before target access. The scientific configuration, models, inputs and metadata
inventory did not change. All 97 compared input/scenario artifacts sealed before target
access had identical hashes. That comparison was recorded after the first target access
and does not move the original freeze clocks. This was a pre-target execution repair,
not a post-target scoring correction. See the [repair
record](../reproducibility/p08/history/pre_target_revision.json).

P09 is the next workflow stage: adoption/override, actions, follow-up and the viewer.
Eight canonical outputs are materialized per scenario, while the three downstream tables
remain empty. No operator behavior or business benefit is inferred from synthetic
service receipt.
