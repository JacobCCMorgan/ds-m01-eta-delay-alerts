# Point-in-time feature preparation

This M3b.1 stage prepares a reproducible feature interface for later ETA
candidates. It is an **Engineering and Method Demonstration — Synthetic Data**
using the frozen January–September 2026 M2 dataset. It does not train a candidate
model, estimate accuracy gains, calibrate intervals or score the final test.

## Information available to dispatch

At each snapshot, dispatch can know the elapsed time since a reported departure,
the planned route and the most recently available valid position. Actual gate
arrival, service completion, delivery completion and later corrections are not
feature inputs. Missing positioning does not remove a prediction opportunity.

The executable contract is [m3b1_features.json](../contracts/m3b1_features.json).
Five numeric features are elapsed trip seconds, planned route kilometers,
remaining projected kilometers, GNSS speed and position age. Three categorical
features are tenant-local route, departure weekday/hour bucket and position
quality. Identifiers and clocks are retained for audit and excluded from vectors.

The initial feature set deliberately excludes promise windows, vehicle/device
identifiers, raw coordinates, historical target aggregates, optional disabled
forecast/routing sources and all labels/simulator truth. These are declared
engineering choices, not selections based on validation scores.

## Temporal and source contract

The runner consumes exactly these six frozen files:

- `data/raw/eta_trip_event.jsonl`
- `data/raw/eta_route_version.jsonl`
- `data/processed/eta_snapshot.jsonl`
- `data/processed/eta_split_assignment.jsonl`
- `data/processed/trip_membership.jsonl`
- `data/processed/feature_lineage.jsonl`

Their SHA256 values must match the retained M2 rebuild receipt. Source config,
receipt and M3a folds are pinned in [m3b1.json](../config/m3b1.json). The runner
does not open outcomes, promises, snapshot labels, history references or hidden
validation truth. Original calibration, test and excluded groups are gated by
tenant/trip before their feature/event payloads are used. Shared file bytes are
still read and hashed; this is a use boundary, not storage access isolation.

Route joins include tenant and exact version. The version must be known and
valid at the snapshot. Lineage must match the snapshot, not exceed its clock and
not understate known route or position availability. The explicit departure must
have been available, and elapsed time must agree with that event. GPS event and
availability times, age and quality must agree. Heldout vehicle/route identifiers
are checked again on each snapshot, in addition to original trip membership.

These checks supplement the upstream pipeline and frozen hashes. A lineage
timestamp alone cannot prove that arbitrary externally supplied values were
derived correctly; the feature layer does not rebuild device/facility/status
joins from all raw telemetry.

## Missing and stale position

The upstream M2 snapshot retains historical GNSS speed when its position becomes
`low_confidence`. M3a never consumed that speed. Before candidate modeling, this
stage masks stale speed and remaining distance to missing, while retaining age
and quality as observation diagnostics. Age strictly greater than 900 seconds is
stale; the exact boundary remains valid. Zero speed is a measured zero.

`no_position` retains the opportunity with missing position-derived numbers.
Malformed values, future clocks, contradictory quality, incorrect lineage,
unknown versions and duplicate keys fail closed. They are not silently imputed.

## Fold-local preprocessing

The four expanding M3a folds and whole tenant/trip groups are unchanged. Each
fold has a separate fitted transformer for each tenant. Fit snapshots must be
strictly before the fit cutoff; validation snapshots must be at or after it and
cannot share a declared fit trip. No cross-tenant statistics or vocabularies are
pooled. A tenant without fit support receives an explicit unavailable vector.

All fit opportunities, regardless of label availability, inform this unsupervised
preprocessing. Each snapshot has equal weight here; supervised model fitting
and trip-balanced error evaluation must declare their own weighting later.
Numeric columns use fit medians, mean centering and population standard deviation
after imputation. An entirely missing column has the declared zero fallback and
unit scale; constant columns also use unit scale. Every numeric feature always
has a missing indicator. Imputation is not an observed measurement.

Categorical vocabularies are sorted and fit-only, with distinct missing and
unknown slots. Validation extrema or new categories never refit the transformer.
JSON state preserves the tenant, fold, cutoff, fit groups, fitted keys, statistics,
vocabularies and exact vector layout; its schema is validated on reuse. Vector
dimensions can differ across tenant/fold models and must not be mixed later.

## Reproduce

From the repository root, use the pinned environment in the main README. Obtain
the six frozen inputs by running the existing M2 generation/build workflow, or
restore an exact verified replay snapshot. No customer data or credentials are
needed. The feature stage itself needs only the Python standard library.

```bash
python -m unittest discover -s tests -v
python scripts/check_feature_replay.py --root . --output reports/m3b1_local
```

Use a new output directory each time. The replay checker checks code, config,
contract and retained evidence before recreating five deterministic artifacts.
It excludes run-specific execution timestamps from equality. For an explicitly
new experiment rather than exact retained replay, the direct command is:

```bash
python -m ds_m01.feature_run --root . --config config/m3b1.json --output reports/m3b1_local_new
```

The runner stages output atomically and refuses to overwrite existing output.
Input drift, frozen-fold changes, malformed input or interrupted writes cannot
publish a partially complete result. Large generated data remain ignored by Git.

## Evidence and remaining work

See [the retained results](../reports/m3b1/README.md), the machine-readable
[summary](../reports/m3b1/summary.json) and
[verification record](../reports/m3b1/verification.json). Independent handwritten
fixtures test target poisoning, future clocks, tenant collisions, temporal
boundaries, missingness, unknown categories and interrupted publication. Injected
failures are test scenarios, not claims about real operational incidents.

Synthetic motion is approximately linear along fictional routes; speed and
distance may encode that generator law particularly well. Temporal validity
does not establish real-road generalization. GPS-derived improvement in a later
candidate comparison must remain a synthetic-only result until real data support
it. Feature missingness and route/date vocabulary coverage are monitoring needs
for any real deployment.

M3b.2 must join cutoff-specific mature regression targets separately, retain all
validation opportunities before eligibility filtering, handle invalid outputs,
and bind the selected transformer to each model. Candidate comparison is M3b.3.
July calibration, August–September final scoring, alert policies, production
tables, PostgreSQL integration and CI remain later milestones.
