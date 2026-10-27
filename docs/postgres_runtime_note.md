# PostgreSQL operational-source check

The M1 snapshot and local HTTP paths are separate from PostgreSQL server verification.
The PostgreSQL connector and typed DDL are supplied, but **the native PostgreSQL
roundtrip has not run in this execution environment**.

## Observed environment limitation

The workspace has no `postgres`, `initdb` or `pg_ctl` executable. The process is
UID 0. `/proc/self/uid_map` and `/proc/self/gid_map` each report `0 0 1`, exposing
only UID/GID 0. A normal `runuser -u nobody -- id` attempt fails with
`runuser: cannot set groups: Operation not permitted`. PostgreSQL initialization
requires a normal non-root execution account; that account cannot be started in
this sandbox. No root-check patch, privilege escalation, embedded substitute or
remote database was used. Workspace-only APT metadata was downloaded during the
availability investigation; no PostgreSQL package was installed.

This is an environment limitation, not a passed service test and not proof that
the connector is production-ready. Native PostgreSQL verification remains an
explicit pending engineering item.

## What the supplied check does

`ds_m01.postgres_check.check(root, dsn)` opens a real psycopg connection. It creates
a unique `ds_m01_m1_*` schema inside one transaction, loads 11 operational tables
with contract types, `NOT NULL` rules and primary keys, reads each table back,
and compares complete normalized rows and SHA256 digests. UTC representations
are normalized; decimal comparisons do not quantize away excess source precision.
All created database objects are rolled back on success or failure. Existing
schemas and application data are not dropped or overwritten.

The check covers typed operational-source transport. It does not claim to verify
the full production ETL, incremental polling, permission model, indexes or foreign
key coverage. GNSS telemetry is served by the separate HTTP input path.

## Run on a PostgreSQL-capable machine

After generating the M1 raw data and installing the project dependencies, set
`DS_M01_PG_DSN` for a dedicated local PostgreSQL database. Keep the DSN in your
shell's environment; do not add credentials to the repository or report.

```bash
python -m ds_m01.postgres_check --root . --output reports/postgres_roundtrip.json
```

An unset DSN returns `not_run` and exit code 2. A connection/load failure returns
`failed` and exit code 2. Only an actual complete equal roundtrip returns `passed`
and exit code 0. The report intentionally omits connection strings and credentials.

`sql/postgres_operational.sql` is the reviewable standalone DDL equivalent for the
fixed schema name `ds_m01_m1`. It intentionally fails if that schema already exists;
it contains no destructive `DROP` statements. The Python check uses a unique schema
and rolls it back instead.
