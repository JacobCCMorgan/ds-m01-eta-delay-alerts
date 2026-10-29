# DS-M01 — ETA intervals and delay alerts

A reproducible data foundation for estimating fleet arrival times and identifying delay risk. The pipeline reconstructs what dispatch could know at each prediction time, separates gate arrival from service and delivery completion, and preserves source lineage for every derived record.

**Current stage: M3a historical ETA baseline on the v0.2.0 M2 data foundation.** Four training-period chronological folds now evaluate a conditional historical-duration baseline. Candidate models, calibrated intervals, delay probabilities, final-test model evaluation and alert policies remain pending.

All operational observations are original synthetic data for January–September 2026. They are not Rollingtrans customer telemetry or real fleet operating records. Scenario frequencies are engineering fixtures, not estimates of real-world prevalence.

## Historical ETA baseline

The retained M3a experiment predicts remaining gate-arrival time from strictly
prior, tenant-local durations. Route/departure groups fall back to distance bands
or tenant history; insufficient surviving support produces an unavailable result.

| Training-period validation result | Value |
|---|---:|
| Prediction opportunities / trips | 5,163 / 654 |
| Available predictions over all opportunities | 3,357 / 5,163 (65.02%) |
| Eligible, supported snapshots / scored trips | 2,347 / 511 |
| Trip-balanced median-ETA MAE | 17.43 minutes |
| Trip-bootstrap MAE interval (95%, synthetic sample) | 15.96–19.00 minutes |
| Raw P10–P90 coverage / mean width | 77.84% / 67.53 minutes |

The error denominator excludes unsupported and unavailable outcomes, while the
availability denominator retains all opportunities. These synthetic,
training-period results do not measure final-test or operational performance.
The P10/P90 interval has not been calibrated. See the [protocol](docs/m3a_baseline.md)
and [results, denominators and limitations](reports/m3a/README.md).

## Data and engineering results

| Item | M2 result |
|---|---:|
| Trips | 1,800 (200 per month) |
| GPS events | 197,303 |
| Snapshots and corresponding labels | 14,349 each |
| Historical training-trip references | 2,536,045 |
| Provenance records | 2,775,752 |
| Canonical schemas and typed Parquet exports | 28 each |
| Populated data-stage tables | 17 |
| Reserved, empty model and alert tables | 11 |

The pipeline evaluates event time and availability time separately. It selects only promises, positions, device assignments and status versions known at the snapshot. Gate observations supply arrival labels independently of GPS. Historical references are limited to eligible training trips with mature observations available at that time.

Of 14,349 snapshots, 6,966 have eligible labels, 4,272 are not mature, 164 are cancelled and 2,947 were already at the destination before arrival status became known. The eligible total includes 1,122 snapshots assigned to excluded trips; those are not model input. Position quality is valid for 6,394 snapshots, stale (`low_confidence`) for 7,096 and unavailable for 859.

| Split | Trips | Eligible trips | Eligible snapshots |
|---|---:|---:|---:|
| Train | 992 | 805 | 3,670 |
| Calibration | 171 | 130 | 596 |
| Test | 400 | 328 | 1,578 |
| Excluded | 237 | 195 | 1,122 |

See the [data validation guide](docs/data_validation.md) for figures and interpretation, and [generator assumptions](docs/generator_assumptions.md) for route geometry, observation delays and scenario design.

## Validation evidence

The retained M2 execution receipts record:

- **49 unit tests, 27 warehouse checks and 22 readiness checks passed.** Readiness is `ready_for_m3_with_cautions`, not a model or deployment approval.
- **M1 continuity:** the 120-trip M1 business rows and all 937 snapshot/label rows remain exact subsets. Under the original M1 configuration, the optimized pipeline reproduces all seven M1 processed JSONL files byte-for-byte.
- **HTTP transport:** 1,441 pages received, one injected 503 retried and 137 duplicate records replayed, with no added canonical rows. All 197,303 received GPS records match the canonical source.
- **Fresh rebuild:** SHA256 equality for 48 source, processed and Parquet files, plus equality for seven HTTP-derived outputs.

The evidence is in [unit test results](reports/unittest_results.txt), [warehouse checks](reports/quality_report.json), [M2 readiness](reports/m2_readiness.json), [M1 pipeline compatibility](reports/pipeline_m1_compatibility.json), [HTTP results](reports/last_http-check.json) and [rebuild results](reports/rebuild_report.json). Historical M1 receipts are retained separately in [reports/m1_reference/](reports/m1_reference/).

These are historical M1/M2 execution results. The documentation and reporting cleanup did not rerun the full M2 dataset; its current checks are recorded separately in [public cleanup validation](reports/public_cleanup_validation.json).

M3a's fresh M2 replay, extended tests, input-hash checks and deterministic baseline
replay are recorded separately in [M3a verification](reports/m3a/verification.json).
The older receipts above have not been rewritten to describe the new run.

## Run locally

Use Python 3.11 or later. The recorded full run used Linux and Python 3.12.14; a full macOS run has not been verified. The snapshot and local HTTP workflow requires no company files, paid APIs or credentials.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install -e . --no-deps
python -m unittest discover -s tests -v
```

Generate the complete M2 dataset and run its checks from the repository root:

```bash
python -m ds_m01.cli run --root . --config config/m2.json
python -m ds_m01.cli http-check --root . --config config/m2.json
python scripts/check_m2_readiness.py --root .
python scripts/check_rebuild.py --root . --config config/m2.json
```

`run` replaces generated data in the selected root. Explicit `--config` selects the reproducible configuration; the CLI otherwise defaults to `config/m2.json` when present. The smaller historical configuration is [config/m1.json](config/m1.json); use a separate project copy for M1 experiments to keep M2 outputs distinct.

With the frozen M2 model inputs available, run training-only baseline validation:

```bash
python -m ds_m01.baseline_run --root . --config config/m3a.json --output reports/m3a_local
```

Choose a new output directory for a subsequent run. See the [M3a protocol](docs/m3a_baseline.md)
for the exact required inputs, output files and figure command.

The complete dataset occupies approximately 12.6 GB before compression. The recorded full-run peak RSS was about 7.23 GiB; resource use varies by machine. Allow approximately 30 GB of free disk for a complete fresh rebuild and HTTP parity checks. Generated data are excluded from the source repository.

Use [plot_data_quality.py](scripts/plot_data_quality.py) to regenerate the English validation figures from the retained summary reports:

```bash
python -m pip install -e '.[plots]'
python scripts/plot_data_quality.py --root .
```

## Scope and limitations

The 11 reserved tables have declared types and nullability but no model, alert, notification or action records. Their population and later-stage behavior remain unimplemented. Native PostgreSQL verification is also pending; see the [PostgreSQL runtime note](docs/postgres_runtime_note.md). DuckDB and HTTP validation do not establish PostgreSQL integration.

For the next modeling stage, the unseen-route-only test cohort has 29 eligible trips and the joint unseen-vehicle/unseen-route cohort has four. These sparse cohorts support descriptive reporting, not reliable deployment claims. A threshold of 30 trips is a coverage warning, not a power analysis. The training set also contains 97 eligible snapshots without a prior historical pool, requiring a defined fallback or unavailable result.

Keep split boundaries and heldout IDs frozen. Do not tune on final test outcomes or count snapshots from the same trip as independent trips. Subsequent work covers candidate models, interval/probability calibration, frozen final evaluation, and alert delivery and action follow-up. The empirical baseline and its training-only validation are now available; they do not complete M3.

## Repository guide

| Location | Purpose |
|---|---|
| [src/ds_m01/](src/ds_m01/) | Data pipeline, historical baseline, chronological validation, transport and PostgreSQL adapter |
| [config/](config/), [contracts/](contracts/), [sql/](sql/) | Reproducible configurations, requirements and typed schema definitions |
| [tests/](tests/) | Independent temporal fixtures, build invariants, transport and publication checks |
| [scripts/](scripts/) | Rebuild, readiness, generator stability and figure generation |
| [reports/](reports/) | Execution receipts, data summaries and validation figures |
| [docs/m1_scope.md](docs/m1_scope.md), [docs/m2_scope.md](docs/m2_scope.md) | Historical trial scope and full data milestone scope |
| [docs/m3a_baseline.md](docs/m3a_baseline.md) | Historical baseline, fold cutoffs, protected periods and metric denominators |
| [docs/change_log.md](docs/change_log.md) | Implementation changes and technical findings |

A full run creates `data/raw/`, `data/processed/`, `data/parquet/` and `data/ds_m01_m2.duckdb`. `data/source_manifest.json` guards the 13 raw sources with counts and SHA256 digests. Generated `validation/` artifacts contain engineering-only synthetic truth and must never become model features.
