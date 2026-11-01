# Quantile ETA candidates

This stage trains candidate P10/P50/P90 remaining gate-arrival time models and
records their predictions on the frozen training-period validation opportunities.
It is an **Engineering and Method Demonstration — Synthetic Data**, using the
January–September 2026 M2 dataset. Candidate accuracy comparison and selection
belong to M3b.3. July calibration and August–September final scoring remain
unexecuted. Predictions here are uncalibrated quantiles, not delay probabilities.

## Fixed experiment design

Two model families test different functional forms: a regularized linear quantile
regressor and a shallow gradient-boosted quantile regressor. Each fits separate
0.1, 0.5 and 0.9 quantiles for each of the twelve tenant/fold combinations. The
four expanding chronological folds, original split roles, feature definitions
and fitted preprocessing states are retained unchanged from M3a/M3b.1.

Parameters are engineering defaults fixed before producing candidate predictions:

| Candidate | Parameters |
|---|---|
| Linear quantile | L1 penalty `alpha=0.01`, intercept, HiGHS solver |
| Gradient boosting quantile | 64 trees, depth 2, minimum 20 samples per leaf, learning rate 0.05, full-sample fitting, seed 20261030, no early stopping |

The minimum is five eligible training trips per model. That threshold is a
support guard, not evidence of adequate predictive accuracy or statistical power.
There is no hyperparameter search, winner selection or final production refit in
this batch. The baseline remains available independently.

## Supervised targets and weights

For each fold, outcome records are gated to its fit trips before target payloads
are examined. Among versions known strictly before the fit cutoff, the latest
version determines eligibility. A withdrawn or unverified latest version cannot
resurrect an earlier verified target. Duplicate known versions and ambiguous
equal-clock revisions fail closed.

Eligible outcomes require verified second/minute-precision gate arrival known
before fitting, positive total trip duration and positive remaining time at the
snapshot. Regression targets retain fractional seconds from the source clocks.
M3a's evaluation helper uses integer seconds; M3b.3 must apply one shared
evaluation definition to all candidates and the baseline. Neither service
completion nor delivery completion supplies an arrival target.

Every fit opportunity receives an explicit eligibility record. Each eligible
trip carries total supervised weight one, divided among its eligible snapshots.
The unsupervised M3b.1 preprocessing remains snapshot-weighted, as originally
declared. Repeated trip memberships in expanding folds are not independent trips.

## Prediction and fallback contract

Every validation opportunity is emitted for each candidate before any outcome
join. Validation target eligibility therefore cannot control prediction
availability. Missing/stale GPS and unseen categories retain the M3b.1 missing
and unknown indicators. They do not silently remove opportunities.

Each model binds its tenant, fold, cutoff, ordered feature layout and full
transformer digest. Wrong bindings, malformed vectors and corrupted model state
stop execution. Insufficient training support or nonpositive/nonfinite predicted
remaining time trigger the declared historical-baseline fallback. That fallback
uses only the same fold's frozen fit history; insufficient surviving history
produces an explicit unavailable result. Training failures are errors and are
not concealed as a successful fallback.

Three finite positive raw estimates are sorted into nondecreasing order.
Raw estimates, crossing flags and postprocessing decisions are retained. Sorting
fixes ordering; it does not establish calibrated coverage. Nonpositive estimates
are not clipped into apparently valid arrivals. Final ETA timestamps add the
published remaining seconds to the prediction clock.

## Reproducibility and artifact use

Install the model environment from `requirements-models.lock`, then install the
repository in editable mode with `--no-deps`. Models are recorded as validated
JSON coefficients or tree arrays. Inference does not unpickle executable Python
objects. Tree inference follows the training library's float32 input semantics.
Source hashes, frozen artifact hashes, library versions and execution receipts
are kept with the experiment. New runs use a new output directory; failed runs
must not publish partial results or overwrite retained evidence.

Training replay, retained-model inference replay and numerical tolerances are
documented with the retained results. A numerical replay pass is distinct from
byte-for-byte model identity and from predictive performance. Use the
[fixed-container replay guide](reproducibility.md) for exact retraining. Native
macOS fitting failed the original contract; passing the unit suite or retained
JSON inference does not make that native training path supported.

```bash
python -m pip install -r requirements-models.lock
python -m pip install -e . --no-deps
python -m unittest discover -s tests -v
```

The candidate replay requires only the frozen raw
`data/raw/eta_trip_outcome.jsonl`; feature matrices, transformers, memberships
and fallback history are retained in the repository. The complete M2 workflow
can reproduce the source, or an exact verified replay snapshot can restore it.
The expected outcome SHA256 is
`4de63a3302783534bcc6368591721f7c22048a5cd3040567ba5b7d59c7f58a58`.
Shared source bytes include protected periods and are hashed; only each fold's
fit-group target payloads enter training. This is a use boundary, not physical
storage isolation.

For retained-model inference without opening the outcome source or retraining:

```bash
python scripts/check_candidate_replay.py --root . --inference-only
```

Structural fields, identities, counts and decisions must match exactly.
Floating values use absolute tolerance `1e-6` or relative tolerance `1e-9`;
ETA timestamps allow at most 50 microseconds. A mismatch stops verification;
do not increase tolerances after observing a failure. The original native Linux
replay was byte-identical. A later fixed Linux/amd64 runtime on Mac Docker Desktop
also produced byte-identical core artifacts in two fresh containers; native
macOS fitting remains unsupported for exact replay. The
[environment evidence and integrated-runner acceptance boundary](reproducibility.md#evidence-and-execution-scope)
keep those results separate.
Malformed vectors, including finite values beyond float32 tree-input range,
fail closed rather than silently entering the numerical-output fallback branch.

The pinned API references are scikit-learn 1.8's
[QuantileRegressor](https://scikit-learn.org/1.8/modules/generated/sklearn.linear_model.QuantileRegressor.html)
and
[GradientBoostingRegressor](https://scikit-learn.org/1.8/modules/generated/sklearn.ensemble.GradientBoostingRegressor.html).

## Limits and next decision

Synthetic route motion is approximately linear. GPS distance/speed may capture
the generator's assumptions especially well. A temporally valid model can still
generalize poorly to actual traffic, stoppages and operational observation delays.
No real-world accuracy, savings or dispatch improvement is established here.

M3b.3 will compare both fixed candidates with the historical baseline on the same
validation opportunities, report trip-balanced errors and availability, inspect
subgroups and decide what to carry to calibration. Preserving unavailable and
fallback rows is necessary for that comparison. Production model/alert tables,
notifications, PostgreSQL integration and CI remain later milestones.
