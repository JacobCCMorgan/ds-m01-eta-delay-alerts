# Clean reconstruction, revision identity and recoverable publication

P11 adds a separately registered engineering path on the published P10 tree at
`151ad00095fa84a3c9c975a8a74546a798a4a135`. It preserves the earlier scientific
implementation, model/calibrator/policy states, retained results and their
registrations. It does not fit another model or create a new untouched final
evaluation.

## Complete reconstruction and repeatability

Two fresh external runs regenerate the fixed M2 synthetic population and its
complete preprocessing outputs. Each run compares all 13 raw and seven processed
JSONL files with their historical SHA256 records, including history, provenance
and lineage. The warehouse has all 28 required typed schemas: 17 populated M2
tables and 11 reserved empty downstream tables. Each database relation is checked
against its corresponding Parquet relation using all columns and logical values.
The separately retained P09 output store is not merged into a claimed 28-table
operational deployment.

Elapsed time, peak resident memory and output sizes describe the executed runs
and environment. They are not service-level guarantees or fleet-scale throughput
estimates. After a run succeeds, its newly generated raw/processed JSONL working
files and the generated `validation/latent_truth.jsonl` are retained as lossless
`JSONL.gz` archives. The 21 archives comprise the 20 historical-oracle inputs
plus latent truth, whose separate binding comes from the generated scenario
registry. Decompression must reproduce
both the exact SHA256 and byte length before only those owned plain working
copies are removed. The archive manifest records the original paths and checks;
manual reuse first restores the inputs outside the checkout. Frozen repository
inputs and retained scientific evidence are not replaced by this compaction.
Dynamic execution times and generation directory names are recorded
separately from deterministic data. Parquet file byte identity is reported
separately from typed relation equality; writer-specific serialization is not a
reason to ignore a changed value.

## Revision-aware telemetry

The P11 telemetry key includes tenant, canonical event ID and numeric record
revision. An identical replay is idempotent. Reusing a key for a different payload
is rejected. The source identity mapping remains explicit, so a source event
cannot silently change canonical identity.

At each snapshot, P11 chooses the highest eligible revision whose availability
time is at or before the snapshot. It performs that selection before applying
vehicle and validity filters. A correction that invalidates a position must not
resurrect an older usable version. Revision 10 follows revision 2 numerically;
late delivery of an older revision does not replace a newer known version.

The retained witness sends multiple revisions through actual loopback HTTP,
constructs snapshots on both sides of correction availability, and stores explicit
snapshot-to-event/revision provenance. DuckDB and Parquet retain the versioned
key and all compared payload fields. Tenant isolation, replay, conflicting
payloads and invalidating corrections have separate checks.

This is the supported P11 entry point for G11. The earlier frozen pipeline and
its historical evidence remain unchanged. The original M2 population contains
revision-1 telemetry; reproducing it alone cannot establish correction behavior.

## One complete database and Parquet generation

P11 builds into a new generation directory and validates the closed DuckDB
database, complete Parquet set and manifest before changing the active reference.
An atomic reference replacement makes one complete generation visible to readers.
Readers pin that reference rather than mixing files discovered independently.
Previously published generations are retained.

Five abrupt worker exits exercise publication boundaries on a small real
DuckDB/Parquet fixture (`source_event`, `derived_snapshot`, `reserved_action`),
including database creation and the first Parquet export. The two full M2 runs
separately validate complete warehouse publication; each fault case does not
rebuild the full M2 population. Recovery identifies incomplete staging directories
and complete unreferenced generations without automatically promoting them.
A subsequent clean rebuild is compared with the recovered result. An exclusive
writer lock and expected-current check reject conflicting publication attempts.

The guarantee applies to the P11 publication protocol on one local filesystem.
It is not a distributed PostgreSQL/filesystem transaction, a power-loss guarantee
for every storage device, or retroactive protection of the frozen direct warehouse
entry point. The original [G11/G13 issue description](known_issues.md) remains a
historical record; this later evidence states exactly which path was exercised.

## Reproduction and CI

Start from a complete checkout, including the published P10 commit in Git
history, with Python 3.12 installed. Allow at least 20 GiB of free space for a
fresh full run in addition to the checkout and existing results. The following
commands create a project environment, install the locked dependencies, verify
the parent binding and run P11 into a new directory outside the checkout:

```bash
(
  set -e
  python3.12 -m venv .venv
  .venv/bin/python -m pip install -r requirements-models.lock \
    -r workflows/p10/requirements-integration.lock
  .venv/bin/python -m pip install -e . --no-deps
  .venv/bin/python -m pip check
  P11_RUN_PARENT="$(mktemp -d /tmp/ds-m01-p11.XXXXXX)"
  .venv/bin/python -I -B workflows/p11/cli.py check-parent --root .
  .venv/bin/python -I -B workflows/p11/cli.py verify --root . \
    --output "$P11_RUN_PARENT/run"
  printf 'P11 evidence: %s\n' "$P11_RUN_PARENT/run"
)
```

The P11 command executes the registered revision witness, process-crash recovery
and two complete M2 rebuilds. The output child directory must not exist; the
verifier creates it. Preserve the directory on failure for diagnosis. An earlier
receipt does not verify a later commit.

The complete publication gate runs all five test suites, retained P09 replay,
actual P10 PostgreSQL/HTTP integration and the complete P11 checks in GitHub
Actions. The [workflow](../.github/workflows/p11-integration.yml) records the Linux
PostgreSQL setup, pinned action revisions, exact dependency installation,
complete gate and retained artifact. Its required job is `P11 full integration`.
The `ci.py` runner requires the genuine GitHub execution context and is not a
local reproduction command. The [P10 integration guide](p10_integration.md)
provides the separate local native-service reproduction commands.

A successful P11-only run is narrower than that complete gate. Local success and
valid commit signatures do not establish a successful Actions run: CI acceptance
requires a completed successful run and the required job/artifact for the exact
published commit. It remains pending before that run is observed.

## Findings from implementation review

Development checks changed the P11 implementation before the retained run:

- Fetching DuckDB `TIMESTAMPTZ` values directly required optional `pytz` in the
  locked environment. P11 normalizes comparisons to exact UTC values while preserving
  the original database schema and without adding a dependency.
- A typed readback alone could conceal source rounding during `DECIMAL` insertion.
  The revision witness now compares the source value with the database value
  before accepting a row; a `62.0004` regression input exercises this rejection.
- A full-size warehouse probe exited with status 137 after producing all source
  files. That observation alone does not identify the operating-system cause.
  P11 now applies an explicit DuckDB memory limit, one database worker thread and a
  dedicated spill directory while retaining the frozen schema and source SQL.
  A 1 GB warehouse export and a separate 512 MB/two-thread full-size comparison
  explicitly raised DuckDB out-of-memory. The comparison now orders by the unique key while
  comparing every typed column, with a 2 GB/one-thread DuckDB setting. That
  setting is not a ceiling on total process RSS. The completed full rerun and
  measured resources are reported below.
- Progress output mixed with JSON could mislead a caller parsing standard output.
  Machine verification now reads the explicit `summary.json`; progress goes to
  standard error.

These were development findings in this preparation run, not reported incidents
in a deployed system. The retained successful gate follows the corrections.

## Retained results and limits

The preparation environment passed 1,021 tests across the original (727),
portability (17), P09 (115), P10 (67) and P11 (95) suites, with no skips. P09
retained replay had zero differences. The P10 native rerun used PostgreSQL 17.11
and actual loopback HTTP; its private cluster closed normally.

| P11 check | Observed result |
| --- | --- |
| Fresh full M2 rebuilds | 2 complete, independent workers |
| Historical JSONL files per run | 13 raw + 7 processed, byte-exact SHA256 |
| Typed warehouse per run | 28 schemas: 17 populated, 11 reserved empty |
| Original warehouse invariants per run | 27 passed |
| Complete DB/Parquet row comparison per run | 5,553,304 rows, all typed columns |
| Repeat comparison | All 28 schemas, counts and complete logical-row digests equal |
| Revision witness | 4 versions of 2 identities; 10 snapshots; 4 real constraint rejections |
| Abrupt process exits | 5 boundaries, each preserving the prior published generation |
| GitHub CI for the user publication | Pending publication and observed exact-commit run |

| Execution | Wall time | Peak worker RSS | Retained source bytes | Generation bytes |
| --- | ---: | ---: | ---: | ---: |
| Fresh run 1 | 571.17 s | 2.460 GiB | 434,381,652 | 978,623,686 |
| Fresh run 2 | 576.00 s | 2.400 GiB | 434,381,652 | 977,837,286 |

P11 integration elapsed time was 1165.39 seconds on Linux
x86_64 with Python 3.12.14. RSS is the fresh worker's OS high-water mark, not an
engine budget or whole-machine measurement. Disk columns are retained file sizes,
not maximum temporary spill usage. Each successful run retained 21 lossless gzip
streams: the 20 historical raw/processed comparisons plus the freshly generated
synthetic truth file, whose hash is checked against that run's scenario registry.
Every archive was fully decoded and byte-verified before removing only its owned
plain working copy. These are observations on the preparation host, not performance
guarantees for another computer.


See the [P11 evidence index](../workflows/p11/evidence/README.md) for the executed
checks and file bindings. Preparation evidence, user-machine execution,
publication/signatures and GitHub CI have separate statuses.

The fixed P08 quality limitations remain open: G14/G15/G16 are not resolved by
reconstruction, revision identity or crash recovery. No new fleet data, causal
intervention effect, live notifications, production security acceptance or
operational deployment is claimed. Whole-project acceptance and final handover
remain subsequent work.
