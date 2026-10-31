# M3b.2 retained candidate evidence

**Engineering and Method Demonstration — Synthetic Data.** These results cover
fixed candidate training and inference on original-training chronological folds
of the January–September 2026 M2 scenarios. No candidate accuracy evaluation,
winner selection, July calibration or August–September final scoring occurred.

## Population and output contract

| Item | Count |
|---|---:|
| Candidate families | 2 |
| Tenant/fold combinations per family | 12 |
| Model states / quantile estimators | 24 / 72 |
| Fit opportunity rows across expanding folds | 18,343 |
| Eligible positive remaining-duration fit rows | 8,417 |
| Eligible trip/fold memberships | 1,869 |
| Validation opportunities per family | 5,163 |
| Total emitted prediction rows | 10,326 |

Fit counts include reuse across expanding folds. Each eligible trip has total
supervised weight one within its tenant/fold. All validation opportunities are
emitted before any evaluation-label join; unavailable predictions remain present.

| Output diagnostic | Linear quantile | Gradient boosting quantile |
|---|---:|---:|
| Candidate predictions | 4,669 | 5,162 |
| Historical-baseline fallbacks | 24 | 1 |
| Unavailable predictions | 470 | 0 |
| Nonpositive raw quantile cases | 494 | 1 |
| Raw crossed quantile cases | 902 | 1 |
| Positive crossed cases actually sorted | 408 | 1 |

The linear model's 494 invalid cases also have raw crossings. They are rejected,
not counted again as 494 published sorted predictions. Raw estimates, sorting,
fallback causes and support remain auditable in `predictions.jsonl`.
Every available final output is positive and ordered; unavailable ETA fields are
null. These checks establish inference behavior, **not accuracy, calibrated
coverage, operational improvement or a preferred model**.

## Verification

- All **247 tests passed**, including 82 added candidate-stage tests.
- An independent target reconstruction agrees on 8,417 eligible rows and 1,869
  trip/fold memberships. Model/transformer binding, target arithmetic, weights,
  opportunity keys and ETA arithmetic reconcile.
- Retained JSON model inference reproduces all 10,326 predictions without
  opening the outcome source. The maximum numerical and ETA differences are zero.
- Fresh training replay reproduces `training_rows.jsonl` and `summary.json`
  exactly. Models and predictions pass the frozen numerical tolerances and were
  also byte-identical in this Linux execution.
- A fresh isolated Python 3.12.14 environment installs the pinned model/data
  dependencies and passes `pip check`. Initial fitting/inference took 30.29
  seconds with approximately 357.5 MiB peak RSS; the full replay took 22.90
  seconds with approximately 431.7 MiB peak RSS. These are single-machine
  measurements, not production throughput or latency guarantees.

The [verification record](verification.json), [quality review](quality_review.json),
[test log](unittest_results.txt) and [replay receipt](replay_receipt.json) retain
the detailed evidence. `run_receipt.json` binds the source, fixed settings,
implementation, dependencies and four core artifacts.

## Artifact map

| File | Purpose |
|---|---|
| `models.json` | Validated JSON states, fixed hyperparameters, fit identities and transformer hashes |
| `training_rows.jsonl` | Every fit opportunity, selected mature target version or exclusion reason, trip weight |
| `predictions.jsonl` | All candidate opportunities, raw and published quantiles, model binding and fallback decisions |
| `summary.json` | Counts and explicit evaluation-stage boundaries |
| `run_receipt.json` | Version and input/output integrity evidence |

Use the [candidate protocol](../../docs/m3b2_candidates.md) for installation,
required input, training replay, inference-only replay and the exact tolerance
policy. Generated replay outputs belong in a new ignored `reports/m3b2_local*`
directory. Retained results are never overwritten by the runner.

## Remaining uncertainty

Linear nonpositive estimates and quantile crossings are observed limitations.
Their predictive consequences require M3b.3's shared-label comparison. Sorting
does not calibrate intervals. Synthetic near-linear route motion limits external
validity, and missing/stale GPS behavior still requires real operating data.
The eleven reserved production tables remain empty; these experiment artifacts
do not constitute an integrated alert service. macOS training verification,
PostgreSQL integration, CI and deployment are not established by this evidence.
