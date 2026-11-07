# July calibration protocol

M3d calibrates the fixed ETA interval pipeline and the independent delay classifier, then compares raw and calibrated outputs on a separate July assessment cohort. It does not choose another model, select an alert threshold, or populate production prediction tables.

The protocol is [config/m3d.json](../config/m3d.json), based on public commit `0a1255222dd82a14596d2a033b779d55b633542c`. Its frozen SHA256 is `1c898e5a4e6f095f58d1b6321deb029b4a34ce4cc6456ecaf15e58582147aa87`. Preparation and freeze occurred on November 5, 2026, before this phase used July outcomes. The dates below describe simulated information boundaries; they do not claim that these models were deployed or run in July.

## Cohorts and knowledge boundaries

Assign each entire `(tenant_id, trip_id)` using its original M2 split and actual departure date in `America/Los_Angeles`. A snapshot after midnight retains its trip's role. For example, a July 10 departure with a July 11 snapshot remains a calibration-fit trip. Every original trip remains in the membership inventory, including buffer, excluded and final-test trips.

| Role | Local departure dates, inclusive | Exclusive target knowledge cutoff | Use |
| --- | --- | --- | --- |
| `train` | January 1–June 23 | July 1 00:00 PDT (`2026-07-01T07:00:00Z`) | Refit fixed base models and preprocessing |
| `calibration_fit` | July 1–10 | July 15 00:00 PDT (`2026-07-15T07:00:00Z`) | Fit calibration parameters only |
| `calibration_buffer` | July 11–14 | None | Retained maturity buffer; no target use |
| `assessment` | July 15–24 | August 1 00:00 PDT (`2026-08-01T07:00:00Z`) | Raw/calibrated comparison; later P06 policy development |
| `test` | August 1–September 30 | None in P05 | Protected final test |
| `excluded` | Original M2 exclusions | None in P05 | Retain original reason; never promote into an active cohort |

For each active role, snapshots and selected outcome knowledge must precede that role's cutoff. Equality is excluded. A later revision cannot replace what was known at the relevant cutoff. An immature or otherwise ineligible target remains an explicit exclusion, never a negative label.

The original route `ROUTE-12` and vehicles `T01-V10`, `T02-V10`, and `T03-V10` remain protected. Neither a matching July date nor an incorrect false flag can admit their trips to training, calibration fitting, or assessment. Split assignment uses metadata only. Reading the inventory of all trip identities is separate from permitting their outcome or feature payloads into a model.

[calibration_split.py](../src/ds_m01/calibration_split.py) validates the original split, the fixed windows and heldouts, and equal metadata key sets before producing roles. The input loader must also verify the exact eight source digests and four parent-configuration digests declared in the protocol. The splitter does not open outcome files.

## Fixed base models and targets

Refit `R01` uses only the original training cohort and July 1 knowledge boundary. Refit the selected `gradient_boosting_quantile` ETA model, its `historical_baseline` fallback, the independent `logistic_delay` classifier, and its `fold_prevalence` fallback. All learned preprocessing uses training opportunities only. The preceding candidate and classifier configurations remain fixed; July provides no model-family, feature, or hyperparameter search.

Calibrate the delivered pipeline within each tenant, including its declared fallback predictions. Retain native/fallback identities in every prediction and report their assessment strata. Do not silently pool tenants or borrow another tenant's calibration parameters.

The ETA target retains verified gate-arrival evidence with second or minute precision and positive exact remaining time. Service or delivery completion is not a substitute. The binary target preserves M3c's stricter second-precision gate-arrival requirement and independent arrival evidence. It compares arrival with the promise version effective and known at that snapshot: `arrival > promise_window_end` is delayed, equality is on time, and grace is zero. Eligibility and exclusions are retained separately for the two targets; a row valid for one need not be valid for the other.

## ETA interval expansion

For an eligible calibration-fit snapshot with delivered raw interval `[L, U]` and remaining-time target `y`, compute

```
snapshot_score = max(L - y, y - U, 0)
trip_score = max(snapshot_score over that trip's eligible supported snapshots)
k = ceil((n_fit_trips + 1) * 0.8)
delta = kth smallest trip_score
```

Each trip contributes one score regardless of its number of snapshots. Require at least 10 contributing trips for that tenant and an observed rank `k <= n_fit_trips`; use no interpolation. Apply `[max(0, L - delta), U + delta]`. The raw median is unchanged, so median MAE is unchanged on the same cohort. These endpoints are coverage bounds, not newly estimated P10/P90 quantiles. If support fails, retain the raw interval with `calibration_status=unsupported`; do not label it calibrated.

This is a nonnegative, whole-trip adaptation of [conformalized quantile regression](https://proceedings.neurips.cc/paper_files/paper/2019/file/5103c3584b063c431bd1268e9b5e76fb-Paper.pdf). The finite-sample coverage argument requires exchangeable calibration and future scores. Temporal dependence, shared routes or vehicles, and maturity-based selection do not establish that premise here. Report measured assessment coverage, including simultaneous coverage within a trip, without claiming guaranteed 80% coverage on a future fleet.

## Delay probability calibration

For raw probability `p`, fit one map per tenant:

```
z = logit(clip(p, 1e-6, 1 - 1e-6))
calibrated_probability = sigmoid(a * z + b), a >= 0
objective = sum_i(weight_i * binary_cross_entropy_i)
            + 0.5 * ((a - 1)^2 + b^2)
```

Each eligible supported trip has total fit weight one, divided among its eligible snapshots. The objective is a weighted sum, without division by trip count or class balancing. Require at least 10 contributing trips and at least two contributing trips for each class. A trip can contribute to both classes when its effective promise changes between snapshots. Support failure produces an exact identity fallback: preserve the original raw probability, including zero or one, and mark it unsupported/uncalibrated. Clipping belongs only inside a fitted map.

Use L-BFGS-B with analytic gradient, initial `(a, b) = (1, 0)`, nonnegative slope bound, and the fixed options in the protocol. Stable cross-entropy and sigmoid calculations avoid numerical overflow. Invalid inputs, nonfinite values, or solver nonconvergence stop the run; they are not support fallbacks.

The [probability calibration guide](https://scikit-learn.org/1.8/modules/calibration.html) explains why base-model fitting and calibration require separate data and why a probability loss alone does not isolate calibration quality. Our constrained, regularized map is specified above rather than delegated to a changing default calibrator. Zero slope, clipping, and numerical saturation can create ties; separate tenant maps can change ranking across tenants. No pooled AUC invariance is asserted. Solver options follow the documented [SciPy L-BFGS-B interface](https://docs.scipy.org/doc/scipy-1.17.0/reference/optimize.minimize-lbfgsb.html).

## Assessment and retained evidence

Run before/after metrics only on `assessment`. Calibration application must reject calibration-fit trip identities. Fit records retain objective, trip scores, support counts, source identities and parameters as fitting diagnostics; they do not contain a before/after fit-cohort evaluation.

Use identical eligible cohorts for each raw/calibrated comparison. Within each reported snapshot metric or slice, each scored trip has total weight one. Report all-opportunity availability, target exclusions and unsupported calibration separately so unavailable forecasts cannot disappear from the accounting.

ETA assessment includes trip-weighted snapshot coverage, interval width, and median MAE. Also report:

- **All-eligible-trip coverage:** a trip succeeds only if every eligible snapshot has an interval and is inside it; missing intervals count as misses.
- **Coverage given full availability:** restrict to trips with intervals at every eligible snapshot, then require all those snapshots to be covered.
- **Mean interval score at alpha 0.2:** width plus `10 * (L - y)` below the interval, or `10 * (y - U)` above it.

Probability assessment includes Brier score, log loss, fixed-bin reliability, ROC AUC and average precision. Retain class support; single-class ranking metrics are null with a reason, while probability losses remain defined. Reliability uses the frozen ten equal-width probability bins. Report tenant, route, GPS validity, early-trip, deadline-advance, and native/fallback source strata. Mark slices below 20 scored trips as sparse. These are descriptive checks, not a search for a slice-specific method or alert policy.

Assessment outcomes cannot change calibration methods, fit windows, support thresholds, or the decision to apply a supported calibrator. A worse assessment result is still reported. Later P06 may use this predeclared assessment cohort for policy development; it cannot then claim that cohort as an independent final-policy test. Re-fitting on all July data would require new provenance and would invalidate the present held-out assessment claim. August–September target payloads remain outside all fitting, selection, and scoring in this phase. Shared source files are hashed and decoded; this does not authorize use of protected target values.

The protocol defines experimental calibration and its reproducible evidence. It does not establish production readiness, operational benefit, conditional coverage for every slice, or completed alert-policy validation.
