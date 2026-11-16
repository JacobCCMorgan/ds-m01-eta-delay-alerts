# Native PostgreSQL and HTTP/snapshot integration

P10 checks the storage and input-service boundary around the existing synthetic
pipeline. It extends the published P09 tree at
`8ae479f5391e6b2f0e3b6ad1131e411629f90966` without changing the frozen scientific
implementation, final evaluation, retained P09 workflow or their registrations.
All new implementation, fixtures and service evidence live under
`workflows/p10/`.

## Compared populations

| Check | Population and boundary |
| --- | --- |
| Native operational-source readback | All 11 typed operational source tables in a temporary PostgreSQL schema. These are source contracts, not the complete derived M2 warehouse. |
| Native workflow readback | All 35 retained P09 output/audit tables in a separate temporary PostgreSQL schema. The 11 canonical output contracts remain distinct from auxiliary audit tables. |
| HTTP source reconciliation | All 197,303 synthetic GPS rows served by a real local HTTP server and retrieved through the service adapter. |
| Full-source downstream projections | All 14,349 snapshots and their labels, plus 1,800 split assignments and membership records, compared across direct source, HTTP, and PostgreSQL-plus-HTTP input paths. |
| Small preprocessing fixture | A bounded fixture compares all seven processed JSONL files produced by the frozen preprocessing pipeline, including history, provenance and lineage. It does not build DuckDB/Parquet warehouse outputs. |

Counts from separate schemas are not added into a claimed 28-table canonical
deployment. Historical M2 row counts, the P08 scientific freeze and the 35-table
P09 store remain separate evidence populations.

## Service checks

The native PostgreSQL check requires a successful connection to a running
server. Generated SQL, mocks, an installed driver or a skipped service test
cannot produce a native-success receipt. The verifier owns a fresh, isolated
loopback cluster. It creates uniquely named schemas, checks that uncommitted
changes are hidden from a second connection, commits the retained rows and
compares them again from a fresh connection. Negative probes roll back their
changes. The owned schemas are removed after verification, and the owned server
is shut down with its logs retained. Existing application services are not
selected or reset. Credentials and connection strings are not written to the
public receipt.

Typed comparisons preserve identifiers, booleans, integer counts, nullable
fields, exact decimal contracts and UTC timestamp precision. Catalog checks and
negative primary-key, foreign-key and value-constraint tests supplement
successful readback. Those checks prove
only the constraints exercised by the retained receipt; they do not imply
production indexing, migrations, high availability or access-control acceptance.

The HTTP check runs against an actual local server, not a patched client. It
exercises cursor paging, transient failure and retry behavior while preserving
tenant identity, event clocks and source-availability clocks. Tenant and
knowledge-time filtering are checked in the downstream snapshot projections;
the local HTTP endpoint itself is not claimed to enforce those filters. Payloads
are compared before downstream processing, so a matching final count cannot
conceal changed or omitted source records. Malformed and incomplete responses
fail the check.

Historical telemetry identity does not represent multiple revisions of one
event consistently across canonical warehouse and lineage records. P10 rejects
unsupported duplicate revisions before claiming input equivalence. This guard
limits supported input; it does not resolve G11 or establish multi-revision
end-to-end support.

## Evidence and reproduction

The retained Linux preparation used Python 3.12.14 and PostgreSQL 17.11. All
926 tests passed: 727 original tests, 17 portability tests, 115 P09 tests and
67 P10 tests. Native PostgreSQL retained 13,706 source rows and 32,128 P09 rows
through commit and exact readback from a new connection. All 634 negative
probes were rejected by their intended constraints. The full native run
completed at `2026-11-15T19:32:11.916642+00:00`.

The actual HTTP run used 1,441 pages and 1,443 requests, including one injected
503 retry and an explicit first-page replay. All 197,303 source payloads were
preserved. Snapshot, HTTP and PostgreSQL-plus-HTTP projections agreed on all
fields and logical values. The 13 raw source files and the four serialized
projection files from direct/HTTP modes matched the historical SHA256 records.
The PostgreSQL-plus-HTTP comparison is logical equality against that verified
baseline; equivalent timestamp spellings are not a byte-identity claim.

The [retained evidence index](../workflows/p10/evidence/README.md) identifies the
actual runtime, executed checks, input hashes and comparison scope. A fresh run
must produce its own receipt. Saved preparation evidence is not proof that a
different machine ran the services successfully.

Use Python 3.12 with the unchanged model dependencies and the P10 integration
driver. Install PostgreSQL 17 separately, then place its matching `postgres`,
`initdb`, `pg_ctl` and `psql` executables on `PATH`, or set `P10_PG_BIN` to that
installation's `bin` directory. The verifier checks the executable versions;
it does not install software or start a system service. A PostgreSQL 17.x minor
version is recorded in each new receipt rather than assumed to match the saved
preparation environment.

From the repository root:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-models.lock \
  -r workflows/p10/requirements-integration.lock
python -m pip install -e . --no-deps

python -I -B workflows/p10/cli.py check-parent --root .
P10_CHECK_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/ds_m01_p10.XXXXXX")"
python -I -B workflows/p10/cli.py verify-native \
  --root . --output "$P10_CHECK_ROOT/run"
```

The output directory must be new and outside the checkout. The verifier generates
the registered synthetic source there, owns its temporary PostgreSQL cluster and
HTTP server, and retains runtime logs and comparison receipts. A failed run keeps
diagnostics and does not produce a successful integration receipt. The package's
complete publication guide also runs all required suites before generating its
receipt-bound push command.

The earlier [PostgreSQL runtime note](postgres_runtime_note.md) is preserved as
historical evidence bound by the P09 registration. Its M2-era absence of native
execution is superseded only for the explicitly tested P10 scopes, not for an
entire production deployment.

## Remaining work

P11/M5.2 still owns full clean-environment reconstruction, revision-aware
telemetry identity and lineage, coordinated database/Parquet publication and
failure recovery, repeat-run resource measurements and actual CI execution.
G11 and G13 remain open until their separate acceptance conditions are observed.

P10 neither changes model/calibrator/policy selection nor creates a new untouched
final test. Synthetic input parity and typed readback do not establish forecast
quality, causal action benefit, real notification delivery or operational
approval. G14/G15/G16 and the final-evaluation limitations remain unchanged.
