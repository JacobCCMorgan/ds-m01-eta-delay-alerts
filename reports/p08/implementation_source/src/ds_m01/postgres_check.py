"""Typed operational-source PostgreSQL roundtrip check.

This is an optional service check, separate from the snapshot/DuckDB pipeline.
It never reports success without an actual PostgreSQL connection. All database
objects are created in a unique schema inside one transaction that is rolled
back, including on success. Existing application schemas are never dropped.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any
import uuid


TABLE_KEYS = {
    "core_tenant": ("tenant_id",),
    "core_vehicle": ("tenant_id", "vehicle_id"),
    "core_device": ("tenant_id", "device_id"),
    "core_vehicle_device_assignment": ("tenant_id", "assignment_id"),
    "eta_trip": ("tenant_id", "trip_id"),
    "eta_facility_version": ("tenant_id", "facility_version_id"),
    "eta_route_version": ("tenant_id", "route_version_id"),
    "eta_trip_plan_version": ("tenant_id", "trip_version_id"),
    "eta_promise_version": ("tenant_id", "promise_version_id"),
    "eta_trip_event": ("tenant_id", "trip_event_id"),
    "eta_trip_outcome": ("tenant_id", "outcome_version_id"),
}


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in ("True", "true"):
        return True
    if value in ("False", "false"):
        return False
    raise ValueError("Contract boolean must be True or False")


def _fields(root: Path) -> dict[str, list[dict]]:
    source = json.loads((root / "contracts" / "fields.json").read_text())
    grouped = {table: [] for table in TABLE_KEYS}
    for field in source:
        if field["table"] in grouped:
            grouped[field["table"]].append(field)
    if not all(grouped.values()):
        raise ValueError("Operational-table contract is incomplete")
    for table, fields in grouped.items():
        names = [f["name"] for f in fields]
        if len(names) != len(set(names)):
            raise ValueError(f"Duplicate field definitions in {table}")
        if not set(TABLE_KEYS[table]) <= set(names):
            raise ValueError(f"Missing key definition in {table}")
    return grouped


def _pg_type(name: str) -> str:
    mapped = {
        "VARCHAR": "TEXT", "TIMESTAMPTZ": "TIMESTAMPTZ",
        "BIGINT": "BIGINT", "INTEGER": "INTEGER", "BOOLEAN": "BOOLEAN",
        "DOUBLE": "DOUBLE PRECISION", "DATE": "DATE", "JSON": "JSONB",
    }
    if name in mapped:
        return mapped[name]
    if re.fullmatch(r"DECIMAL\(\d+,\d+\)", name):
        return name
    raise ValueError(f"Unsupported contract type: {name}")


def _quote(name: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z_0-9]*", name):
        raise ValueError("Invalid SQL identifier")
    return '"' + name + '"'


def schema_sql(root: Path, schema: str = "ds_m01_m1") -> str:
    """Return reviewable DDL. Names and types come from a strict allowlist."""
    parts = [f"CREATE SCHEMA {_quote(schema)};"]
    for table, fields in _fields(root).items():
        columns = [
            f"  {_quote(f['name'])} {_pg_type(f['type'])}"
            + ("" if _bool(f["nullable"]) else " NOT NULL")
            for f in fields
        ]
        columns.append("  PRIMARY KEY (" + ", ".join(map(_quote, TABLE_KEYS[table])) + ")")
        parts.append(f"CREATE TABLE {_quote(schema)}.{_quote(table)} (\n" + ",\n".join(columns) + "\n);")
    return "\n\n".join(parts) + "\n"


def _timestamp(value: Any) -> str:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Naive timestamps are not allowed")
    return result.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normal_value(value: Any, typ: str) -> Any:
    if value is None:
        return None
    if typ == "TIMESTAMPTZ":
        return _timestamp(value)
    if typ.startswith("DECIMAL"):
        # No quantization here: excess source precision must cause a mismatch.
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError("Non-finite numeric input")
        return format(result.normalize(), "f")
    if typ == "DOUBLE":
        return format(float(value), ".17g")
    if typ in ("INTEGER", "BIGINT"):
        if isinstance(value, bool) or Decimal(str(value)) != Decimal(int(value)):
            raise ValueError("Non-integer value in integer field")
        return int(value)
    if typ == "BOOLEAN":
        return _bool(value)
    if typ == "JSON":
        return json.loads(value) if isinstance(value, str) else value
    if typ == "DATE":
        return value.isoformat() if isinstance(value, date) else str(value)
    return str(value)


def _normal_rows(rows: list[dict], fields: list[dict], keys: tuple[str, ...]) -> list[dict]:
    names = {f["name"] for f in fields}
    result = []
    for row in rows:
        if set(row) != names:
            raise ValueError("Rows must contain exactly the canonical contract columns")
        result.append({f["name"]: _normal_value(row[f["name"]], f["type"]) for f in fields})
    return sorted(result, key=lambda row: tuple(str(row[k]) for k in keys))


def _hash(rows: list[dict]) -> str:
    raw = "".join(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)
    return hashlib.sha256(raw.encode()).hexdigest()


def check(root: Path, dsn: str) -> dict:
    """Actually roundtrip all operational tables; rollback all DB objects.

    ``dsn`` should be supplied from DS_M01_PG_DSN, never committed to a file.
    This checks transport, SQL types, nulls, primary keys and exact normalized
    record equality. It does not claim a complete incremental production ETL.
    """
    import psycopg
    from psycopg.types.json import Jsonb

    root = Path(root)
    fields = _fields(root)
    source: dict[str, list[dict]] = {}
    for table in TABLE_KEYS:
        path = root / "data" / "raw" / f"{table}.jsonl"
        source[table] = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if not source[table]:
            raise ValueError(f"No source records for required operational table {table}")
        _normal_rows(source[table], fields[table], TABLE_KEYS[table])

    started = datetime.now(timezone.utc)
    tick = time.perf_counter()
    schema = "ds_m01_m1_" + uuid.uuid4().hex[:12]
    report = {
        "scope": "PostgreSQL operational-source typed roundtrip; not full incremental ETL",
        "status": "running", "started_at_utc": started.isoformat(),
        "schema": schema, "writes_rolled_back": False,
        "credentials_recorded": False, "tables": {},
    }
    # Connection errors propagate to the caller; no pass result is invented.
    conn = psycopg.connect(dsn, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL TIME ZONE 'UTC'")
            cur.execute("SELECT version()")
            report["server_version"] = cur.fetchone()[0]
            cur.execute(schema_sql(root, schema))
            for table, table_fields in fields.items():
                names = [field["name"] for field in table_fields]
                query = f"INSERT INTO {_quote(schema)}.{_quote(table)} (" + ",".join(map(_quote, names)) + ") VALUES (" + ",".join(["%s"] * len(names)) + ")"
                values = []
                for row in source[table]:
                    values.append(tuple(
                        (Jsonb(row[f["name"]]) if f["type"] == "JSON" and row[f["name"]] is not None
                         else Decimal(str(row[f["name"]])) if f["type"].startswith("DECIMAL") and row[f["name"]] is not None
                         else row[f["name"]]) for f in table_fields
                    ))
                cur.executemany(query, values)
                cur.execute(f"SELECT " + ",".join(map(_quote, names)) + f" FROM {_quote(schema)}.{_quote(table)}")
                returned = [dict(zip(names, row)) for row in cur.fetchall()]
                before = _normal_rows(source[table], table_fields, TABLE_KEYS[table])
                after = _normal_rows(returned, table_fields, TABLE_KEYS[table])
                report["tables"][table] = {
                    "input_rows": len(before), "returned_rows": len(after),
                    "input_normalized_sha256": _hash(before),
                    "returned_normalized_sha256": _hash(after),
                    "equal": before == after,
                }
        report["status"] = "passed" if all(t["equal"] for t in report["tables"].values()) else "failed"
    finally:
        conn.rollback()
        report["writes_rolled_back"] = True
        conn.close()
    report["elapsed_seconds"] = round(time.perf_counter() - tick, 3)
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--emit-sql", action="store_true")
    args = parser.parse_args()
    if args.emit_sql:
        print(schema_sql(args.root), end="")
        return 0
    dsn = os.environ.get("DS_M01_PG_DSN")
    if not dsn:
        report = {"status": "not_run", "reason": "DS_M01_PG_DSN is not configured; no PostgreSQL execution claimed."}
    else:
        try:
            report = check(args.root, dsn)
        except Exception as exc:
            report = {"status": "failed", "error_class": type(exc).__name__, "reason": "PostgreSQL check did not complete; connection details suppressed to avoid leaking credentials."}
    content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content)
    print(content, end="")
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
