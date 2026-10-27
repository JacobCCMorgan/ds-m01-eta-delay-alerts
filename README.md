# DS-M01 — ETA intervals and delay alerts

[繁體中文](README.zh-TW.md)

This delivery implements **M0 source review and the M1 data trial**. It contains original fictional 2026 logistics scenarios, point-in-time features, independently observed arrival labels, a local HTTP transport check, and a typed DuckDB/Parquet warehouse. It is not real Rollingtrans data, a trained ETA model, or evidence of operational performance.

## Delivered result

| Item | Current M1 result |
|---|---:|
| Main-period trial trips, January–September 2026 | 120 |
| GNSS telemetry records | 12,834 |
| Snapshots and corresponding labels | 937 each |
| Active canonical tables / original required tables | 17 / 28 |
| Eligible / not mature / cancelled / already arrived labels | 457 / 288 / 11 / 181 |
| Valid / low-confidence / missing positions | 413 / 465 / 59 |
| Train / excluded / calibration / test trips | 65 / 16 / 13 / 26 |

The 33 unit tests pass for this implementation. The warehouse runs 24 checks; their current result is recorded in `reports/quality_report.json`. The actual loopback HTTP run fetched 94 pages, retried one injected 503, and replayed 137 records without adding duplicate canonical events. Complete received records match the canonical source content. See [scope and evidence](docs/m1_scope.md) for limits and report locations.

## Run on a Mac

Use Python 3.11 or later. The delivered checks ran with Python 3.12.14 on Linux; macOS execution is not claimed as tested. Open Terminal in the extracted project directory:

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install -e . --no-deps
python -m ds_m01.cli run --root .
```

`run` regenerates the configured 120-trip dataset, builds snapshots/labels/history, and validates DuckDB and Parquet outputs. It replaces generated project outputs; keep the supplied archive if you want an unchanged reference copy.

Exercise the real local HTTP path and rebuild from the received telemetry:

```bash
python -m ds_m01.cli http-check --root .
python -m ds_m01.cli build --root . --telemetry-mode http_snapshot
python -m ds_m01.cli validate --root .
python -m unittest discover -s tests -v
python scripts/check_rebuild.py --root .
```

Continue with `http_snapshot` only when `reports/last_http-check.json` says `passed`. The server uses an ephemeral `127.0.0.1` port and closes after the check. A runtime that cannot bind the port receives a `blocked` report. No external API, credentials, or database is needed for these commands.

Optional generator expansion/stability check:

```bash
python scripts/check_generator_stability.py --root .
```

This generates temporary 120- and 1,800-trip raw datasets to check schema, subset stability, schedules and assignment histories. Allow tens of seconds depending on the machine. It writes `reports/generator_validation.json`; it does not retain or deliver an M2 full pipeline.

## Files to review

| Path | Purpose |
|---|---|
| `config/m1.json` | Dataset seed, namespace, periods, cutoffs, held-out entities and settings |
| `contracts/` | Original requirements, table register and field contract |
| `data/raw/` | 13 source tables as JSONL |
| `data/ingested/` | Actual HTTP-received canonical telemetry |
| `data/processed/` | Four derived tables and combined provenance |
| `data/parquet/`, `data/ds_m01_m1.duckdb` | 17 canonical table exports and typed warehouse |
| `reports/` | Counts, quality checks, command receipts and replay evidence |
| `validation/` | Isolated synthetic truth for generator checks; not feature input |
| `tests/` | Independent point-in-time, build and transport fixtures |

The pinned `generated_at` in the delivered configuration is the original synthetic source acquisition receipt. Keeping it fixed permits byte-reproducible replay; command reports record each execution's actual finish time. For a new dataset, create a new configuration with an actual acquisition timestamp and a new dataset namespace. The historical scenario dates do not claim that a model was deployed in the past.

## Next stages and handoff

M2 still needs the retained 1,800-trip end-to-end dataset and its validation. Model fitting, interval/probability calibration, final-test evaluation, alert episodes, action follow-up and the complete O01–O03 deliverables remain pending. Eleven required tables are reserved for those later stages; the two optional tables remain disabled.

The PostgreSQL adapter and DDL are supplied, but the native PostgreSQL roundtrip has **not run** in this environment. See [PostgreSQL runtime note](docs/postgres_runtime_note.md) for the separate local check. PostgreSQL credentials are not needed to review or run the delivered snapshot/HTTP trial.

No Git commit or push was created. The user owns signed commits, Verified status and GitHub publication. DA-E01 remains unchanged and pending its separate acceptance.
