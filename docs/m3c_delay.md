# Delay classification against the snapshot promise

M3c.1 fits an independent binary delay classifier. M3c.2 checks its raw
probabilities on the four original training-period validation folds. The
configuration is retained for the later July calibration stage.

## What counts as a delay

For each snapshot, select the latest promise version known at or before that
snapshot and require its ID to match the snapshot's recorded promise ID. The
promise window describes the promised arrival time; its start is not the date
when the promise version became available. A later reschedule cannot change an
earlier snapshot's deadline.

The binary target is `actual gate arrival > snapshot promise window end`.
Equality is on time and no grace period is added. Arrival must have independent
gate evidence and be verified strictly before the fold's label cutoff. Select
the latest outcome known before that cutoff, including revisions that withdraw
an earlier verified observation. Delivery completion and partial proof of
delivery do not substitute for gate arrival.

Every prediction opportunity is retained. Cancelled, pending, incomplete,
withdrawn, missing and already-arrived outcomes have explicit reasons and no
binary target. This classifier accepts only second-precision arrivals. Unlike
the earlier duration experiment, minute-resolution observations are excluded:
their rounding convention does not define a reliable binary deadline boundary.
The frozen synthetic source has second or unknown precision, so this policy
does not discard a known minute-resolution group from the recorded run.

## Features and fitting

The classifier uses the frozen M3b.1 tenant/fold feature vectors plus signed
seconds from the snapshot to its effective promise deadline. This added feature
has its own definition version. The deadline mean and population standard
deviation are fitted on available fold-fit opportunities before joining labels.
Negative deadline values are retained. ETA quantiles, future outcomes and
generator truth are absent from classifier inputs.

Each eligible training trip has total weight one across its usable snapshots.
A tenant/fold prevalence baseline estimates the weighted training delay rate.
Logistic regression uses fixed L2 regularization, `C=1`, the `lbfgs` solver and
no class rebalancing. Both methods require at least five eligible fit trips;
logistic fitting additionally requires at least two contributing trips in each
class. Insufficient classifier support uses the recorded prevalence fallback
when that baseline is supported. Invalid input or fitting errors stop the run.

The four expanding folds preserve the M2 split, tenant/trip grouping and
exclusive label cutoffs. Later folds may train on earlier folds' validation
trips. Those repeated fit memberships are not independent observations. July
calibration and August–September final-test groups remain outside fitting and
scoring. All predictions are produced before the evaluation step joins
validation outcomes.

## Probability checks

Within every scored cohort or slice, each tenant/trip receives equal total
weight. Brier score and log loss measure probability error. Average precision
and ROC AUC describe ranking when both classes are represented. Single-class
ranking results are null with a reason. Log-loss clipping is counted explicitly.

Reliability uses ten fixed probability bins. Each bin retains its contributing
trips, snapshots, average probability and observed delay fraction. Trip weights
come from the full scored cohort before binning; each bin does not independently
renormalize every trip. Reliability is descriptive and does not fit a calibrator.

Separate views cover native model output, delivered output including fallback,
common support and baseline support. Availability denominators retain unscored
opportunities. Slices cover fold, tenant, route, departure period, GPS quality,
the first hour of a trip and time before the effective promise. Advance strata
use the known deadline rather than the future arrival: overdue, 0–30 minutes,
30–120 minutes and more than two hours. Small cohorts are marked explicitly.
If a trip has not arrived and its promise deadline has already passed, lateness
is already observable. The overdue slice therefore does not demonstrate useful
advance warning. Read the pre-deadline strata separately from overall ranking.

No probability threshold, operational warning rule or GPS-dependent model
switch is selected in this experiment. The Brier score combines several aspects
of probability quality; a lower value alone does not establish calibration.

## Results and reproduction

The completed run, independent checks and replay commands are recorded with the
retained experiment outputs in `reports/m3c/`.

## What follows

M3d.1 assigns distinct July roles for fitting and assessing probability and ETA
interval calibration. That plan must be fixed before using July outcomes for
calibration or assessment.
Final evaluation, alert thresholds, delivery, action follow-up and canonical
model/prediction table population remain later work. These results use original
synthetic data and do not establish real-fleet delay prevalence or benefit.

Implementation references: [scikit-learn 1.8 logistic regression](https://scikit-learn.org/1.8/modules/generated/sklearn.linear_model.LogisticRegression.html),
[probability calibration](https://scikit-learn.org/1.8/modules/calibration.html)
and [average precision](https://scikit-learn.org/1.8/modules/generated/sklearn.metrics.average_precision_score.html).
