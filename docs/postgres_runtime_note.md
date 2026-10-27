# PostgreSQL operational-source check — M2

The M2 snapshot, local HTTP and DuckDB/Parquet validations are separate from
PostgreSQL server verification. The PostgreSQL connector and typed DDL are
supplied, but **the native PostgreSQL roundtrip has not run in this execution
environment**. A data-pipeline pass does not change this status.

## Historical M2 environment inspection

The M2 availability inspection found no `postgres`, `initdb`, `pg_ctl` or `psql`
executable on PATH, and no `docker` or `podman`. `DS_M01_PG_DSN` is not configured.
The process is UID/GID 0; `/proc/self/uid_map` and `/proc/self/gid_map` each contain
`0 0 1`, exposing only UID/GID 0. PostgreSQL initialization requires an ordinary
non-root service account. The earlier M1 attempt to use an ordinary account was
blocked by this runtime; that attempt was not repeated for M2.

The inspection did not execute a PostgreSQL connection or SQL roundtrip.
The historical inspection time and evidence are in
[reports/postgres_check.json](../reports/postgres_check.json). Native PostgreSQL
verification remains a pending engineering item. The documentation cleanup does
not change that status.

## What the supplied check does

`ds_m01.postgres_check.check(root, dsn)` opens a real psycopg connection. It creates
a unique `ds_m01_m1_*` schema inside one transaction, loads 11 operational tables
with contract types, `NOT NULL` rules and primary keys, reads each table back,
and compares complete normalized rows and SHA256 digests. The schema prefix is
a retained implementation name; the data read come from the supplied `--root`.
UTC representations are normalized; decimal comparisons do not quantize away
excess source precision.

All created database objects are rolled back on success or failure. Existing
schemas and application data are not dropped or overwritten. The check covers
typed operational-source transport. It does not claim to verify incremental
polling, production permissions, indexes, foreign-key coverage, the HTTP telemetry
path, or all 28 canonical warehouse tables.

## Run on a PostgreSQL-capable machine

Use a dedicated local PostgreSQL database and a role allowed to create schemas.
Install the project dependencies and PostgreSQL extra. Generate the retained
M2 raw data only if they are not already present:

```bash
python -m pip install -e '.[postgres]'
python -m ds_m01.cli run --root . --config config/m2.json
```

Set `DS_M01_PG_DSN` in your shell environment for that database. Use your actual
local role and database details; keep passwords and connection strings out of
Git and reports. Then run:

```bash
python -m ds_m01.postgres_check --root . --output reports/postgres_roundtrip.json
```

An unset DSN returns `not_run` and exit code 2. A connection/load failure returns
`failed` and exit code 2. Only an actual complete equal roundtrip returns `passed`
and exit code 0. The report intentionally omits connection strings and credentials.

`sql/postgres_operational.sql` is the reviewable standalone DDL for the retained
fixed schema name `ds_m01_m1`. It intentionally fails if that schema exists and
contains no destructive `DROP` statements. Prefer the Python check for validation;
it uses a unique schema and rolls it back.
