# DS-M01 — ETA intervals and delay alerts

[繁體中文](README.zh-TW.md)

**M2 data milestone, application v0.2.0.** This project provides a reproducible, original synthetic 2026 fleet dataset and point-in-time feature/label pipeline. It is an independent portfolio exercise, not real Rollingtrans operational data. Models and alert policies have not been trained or evaluated.

## Delivered data

| Item | M2 result |
|---|---:|
| Trips, January–September 2026 | 1,800 (200/month) |
| GPS events | 197,303 |
| Snapshots and corresponding labels | 14,349 each |
| Historical training-trip references | 2,536,045 |
| Provenance records | 2,775,752 |
| Canonical schemas | 28 |
| Populated data-stage tables / reserved empty future tables | 17 / 11 |

Labels: 6,966 eligible; 4,272 not mature; 164 cancelled; 2,947 already arrived before the system knew it. Of the eligible snapshots, 1,122 belong to excluded trips and are not model-training/evaluation input. Position quality: 6,394 valid, 7,096 stale, 859 missing. Counts reflect deliberately varied engineering scenarios, not real-world prevalence or model performance.

| Role | Trips | Eligible trips | Eligible snapshots |
|---|---:|---:|---:|
| Train | 992 | 805 | 3,670 |
| Calibration | 171 | 130 | 596 |
| Test | 400 | 328 | 1,578 |
| Excluded | 237 | 195 | 1,122 |

## Validation and readiness

49 unit tests pass. The full warehouse passes 27 checks; M2 coverage and point-in-time readiness passes 22 checks. The 120-trip M1 business rows and all 937 M1 snapshot/label rows are preserved exactly. The optimized pipeline also reproduces all seven M1 processed JSONL files byte-for-byte under the original configuration.

The actual loopback HTTP run receives 1,441 pages, retries one injected 503, and replays 137 duplicate records with no added canonical rows. See `reports/last_http-check.json`. Fresh-directory source/processed/Parquet reproducibility and HTTP-derived parity are recorded in `reports/rebuild_report.json` once completed; the delivery includes its actual result.

M3 may proceed with explicit limits: unseen-route-only testing has 29 eligible trips; both unseen vehicle and unseen route has only 4. These are descriptive coverage warnings, not deployment gates or a power analysis. The training set has 97 eligible snapshots without a prior historical pool; M3 must define a fallback or unavailable result. Split boundaries and heldouts remain fixed. Never tune on test outcomes or treat same-trip snapshots as independent trips.

## Run

Use Python 3.11+. Tested here on Linux/Python 3.12.14; macOS execution is not claimed. No company files, paid API, or credentials are needed for this snapshot/HTTP workflow.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install -e . --no-deps
python -m ds_m01.cli run --root . --config config/m2.json
python -m ds_m01.cli http-check --root . --config config/m2.json
python scripts/check_m2_readiness.py --root .
python -m unittest discover -s tests -v
python scripts/check_rebuild.py --root . --config config/m2.json
```

Default CLI configuration is `config/m2.json` when present; use explicit `--config` for reproducible selection. `run` replaces generated data in the selected root. Use a separate root for M1 experiments. M1 configuration remains in `config/m1.json`; historical M1 receipts are in `reports/m1_reference/`.

The complete dataset occupies about 12.6 GB before ZIP compression. The observed full-run peak RSS was about 7.23 GiB; local usage varies. Allow approximately 30 GB free disk for the complete fresh rebuild and HTTP parity checks. The rebuild releases its temporary first-pass JSONL before the HTTP pass to avoid retaining a third full copy.

## Layout

- `src/ds_m01/`: generator, as-of pipeline, typed warehouse, HTTP transport, PostgreSQL adapter.
- `config/`, `contracts/`, `sql/`: frozen configurations, field contracts and all 28 schema definitions.
- `data/raw/`, `data/processed/`, `data/parquet/`, `data/ds_m01_m2.duckdb`: full data, 28 typed exports and warehouse (complete archive).
- `data/source_manifest.json`: exhaustive 13-source row-count and SHA256 guard.
- `validation/`: engineering-only synthetic scenario truth; never a feature source.
- `reports/`: actual run receipts, readiness, compatibility and reproducibility evidence.
- `scripts/build_m2_report.py`: Chinese report authoring after all required receipts pass.

The 11 reserved tables are empty placeholders with contract types/nullability. Their later-stage population, final keys and model/alert behavior still need validation. Native PostgreSQL has not run in this environment; see `docs/postgres_runtime_note.md` for the real-service roundtrip command. DuckDB and HTTP do not stand in for that verification.

## GitHub handoff

M2 builds on user-owned signed M1 commit `ff9ad674434064366c789b5d7de944bfbdb5b0d4`, verified on GitHub. No assistant commit or push was performed. Use the source archive to update the existing repository and make a new signed commit; retain `.git` and the M1 history. See `docs/commit_handoff.txt`.

Next: chronological baseline/candidate model development, interval/probability calibration, frozen final evaluation, then alert delivery/adoption/action follow-up. DA-E01 remains unchanged and pending its separate acceptance.
