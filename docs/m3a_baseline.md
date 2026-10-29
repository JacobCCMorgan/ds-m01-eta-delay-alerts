# M3a: historical ETA baseline

M3a establishes a reproducible historical baseline before fitting a candidate
remaining-time model. It uses the frozen synthetic M2 dataset and evaluates only
chronological windows inside the original training period. July calibration and
August–September final test outcomes do not enter fitting or scoring.

## Forecast and information boundary

The target is positive remaining time until independently observed gate arrival,
not service completion or proof of delivery. A prediction opportunity is the M2
snapshot selected using status known at that time. Its existence does not depend
on the eventual outcome label. Predictions are generated first; targets are
joined in a separate evaluation pass.

The model input allowlist is `tenant_id`, `trip_id`, `snapshot_id`,
`snapshot_time`, `route_id`, `departure_time_bucket`, `route_distance_km` and
`elapsed_trip_seconds`. The route version and planned total route distance must
be known and valid at the snapshot. This baseline does not use position, speed,
remaining GPS-derived distance, future arrival, generator truth or delay labels.
Missing or stale GPS alone therefore does not block it.

The source files contain all M2 groups. The loader selects original-training
tenant/trip keys before exposing snapshot or outcome payloads to the experiment.
This is an exclusion from model/evaluation use, not a claim that shared file
bytes were never read. Tests alter protected July/test payloads and confirm that
the training experiment is unchanged.

## Four chronological folds

All dates below are local to America/Los_Angeles. Departure endpoints are
inclusive; label cutoffs are exclusive local midnight and retain the correct
UTC offset across daylight saving time.

| Fold | Fit departures | Fit label cutoff | Validation departures | Validation label cutoff |
|---|---|---|---|---|
| F01 | Jan 1–Feb 21 | Mar 1 | Mar 1–24 | Apr 1 |
| F02 | Jan 1–Mar 24 | Apr 1 | Apr 1–23 | May 1 |
| F03 | Jan 1–Apr 23 | May 1 | May 1–24 | Jun 1 |
| F04 | Jan 1–May 24 | Jun 1 | Jun 1–23 | Jul 1 |

Only M2 `train` trips can enter these folds. Existing heldout vehicles/routes,
excluded trips and buffer dates remain excluded. Each tenant/trip stays together;
validation windows are disjoint. Earlier validation trips may enter a later
expanding fit window, but never their own fold's historical pool.

The history pool is rebuilt from raw outcome versions for each fit cutoff. The
latest version known before that cutoff must be verified, have second/minute
arrival precision, and yield a positive duration. A later correction cannot
replace an earlier known version; a known conflict cannot resurrect an older
verified outcome. Grouping uses the first point-in-time snapshot's route and
departure bucket. The pool remains frozen throughout the validation window.

Validation labels are independently rebuilt at each validation-label cutoff.
Reusing the M2 July label for all folds would incorrectly make later revisions
available in earlier evaluations.

## Conditional historical baseline

For elapsed time `e`, retain historical total durations `T > e` and calculate
remaining durations `T - e`. Select the first supported pool:

1. Same tenant, route and exact M2 departure bucket (weekday, hour and timezone).
2. Same tenant and 50-km planned-route-distance band.
3. All eligible historical trips in the same tenant.

Each pool requires at least five surviving, distinct historical trips. Both the
outcome availability time and actual arrival must be strictly before the fit
cutoff and prediction time. Own-trip history and other tenants are excluded.
If no pool meets the requirement, return `unsupported` with a reason and support
counts. Do not substitute zero remaining time or a fabricated probability.

P10, P50 and P90 use linear interpolation at `(n - 1) * p`. They are raw empirical
quantiles, **not calibrated intervals or guaranteed 80% coverage**. Five trips
and 50-km bands are declared engineering defaults fixed before this run's
scores. They are not production acceptance thresholds. There is no delay
classifier, probability calibration or notification policy in this batch.

## Metrics and denominators

Keep all prediction opportunities in the audit output, including unknown,
cancelled and already-arrived outcomes. An already-arrived label can be observed
later when the arrival notice was unavailable at prediction time. Do not remove
that opportunity using hindsight.

Error and interval metrics use only eligible, supported snapshots. Each scored
trip has weight one, divided equally among its scored snapshots. This prevents
long trips with more snapshots from dominating the result. Report availability
over **all opportunities** separately from support among eligible targets.

The MAE interval uses 500 fixed-seed bootstrap resamples of complete trip-level
contributions. It describes sampling variation within this synthetic evaluation;
it does not establish fleet generalization and does not model additional
cross-trip vehicle or temporal dependence. Small slices remain descriptive.

## Reproduce

Run the existing environment setup in the README first. The experiment adds no
runtime dependency beyond the standard library. It needs the eight model input
files listed in `baseline_run.INPUT_FILES`, `config/m2.json` and the retained
`reports/rebuild_report.json`. Full M2 generation also supplies these files.

```bash
python -m ds_m01.baseline_run --root . --config config/m3a.json --output reports/m3a_local
```

The command checks the frozen configuration and receipt hashes, then compares
each consumed input against the retained M2 hashes. Drift fails closed. A new
dataset needs a separately reviewed protocol; silently relaxing these checks
would mix results from different data versions.

Use a new output directory for each execution. Existing evidence is never
overwritten. The six core result files are deterministic; `run_receipt.json`
records the actual execution time and environment separately. To regenerate the
figure after installing the existing `plots` extra:

```bash
python scripts/plot_baseline_validation.py --input reports/m3a_local --output reports/m3a_local/baseline_validation.png
```

## Artifacts and remaining scope

`memberships.jsonl` records whole-trip fold roles. `frozen_history.jsonl` records
the historical outcome versions and grouping snapshots. `predictions.jsonl`
contains no evaluation targets. `evaluation_rows.jsonl` joins predictions to
cutoff-specific labels. `metrics.json`, `model_manifest.json` and
`run_receipt.json` provide results, protocol and provenance.

These are experiment artifacts under `reports/m3a/`. The 11 reserved canonical
model/alert tables remain unpopulated. A candidate model, interval/probability
calibration, frozen final-test comparison, alerts and follow-up remain later
work. See the [retained M3a result](../reports/m3a/README.md).
