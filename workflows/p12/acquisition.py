"""Measure when a synthetic telemetry batch is received over loopback HTTP.

The source event and availability clocks come from the frozen point-in-time
fixture. Only the receiving process supplies retrieved_at. A new run therefore
produces a new receipt for the same source, with a different acquisition time.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from ds_m01 import event_api
from ds_m01.common import parse_ts

SOURCE_PATH = "tests/fixtures/point_in_time_case.json"


def source_batch(root):
    data = (root / SOURCE_PATH).read_bytes()
    rows = json.loads(data)["tables"]["telemetry_event"]
    source = event_api.EventStore()
    source.ingest(rows)
    if not rows or len(source.rows) != len(rows):
        raise ValueError("Expected a nonempty source batch with unique versioned events")
    return sha256(data).hexdigest(), source.ordered()


def expected_records(rows, retrieved_at):
    return [{
        "tenant_id": row["tenant_id"],
        "canonical_event_id": row["canonical_event_id"],
        "source_system_id": row["source_system_id"],
        "source_record_id": row["source_event_id"],
        "source_provenance_id": row["provenance_id"],
        "record_version": row["record_revision"],
        "event_time": row["event_time"],
        "available_time": row["available_time"],
        "retrieved_at": retrieved_at,
    } for row in rows]


def verify_receipt(root, receipt):
    """Check a retained acquisition against the registered fixture and its clocks."""
    digest, rows = source_batch(root)
    start = parse_ts(receipt["request_started_at_utc"])
    end = parse_ts(receipt["retrieved_at"])
    if not all(value.endswith("+00:00") for value in
               (receipt["request_started_at_utc"], receipt["retrieved_at"])):
        raise ValueError("Acquisition clocks must be UTC")
    if end < start or any(start <= parse_ts(row["available_time"]) for row in rows):
        raise ValueError("Acquisition clocks inconsistent with this historical fixture")
    if (receipt["source_path"] != SOURCE_PATH or receipt["source_sha256"] != digest
            or receipt["scope"] != "synthetic_fixture_over_loopback_http"
            or receipt["payload_sha256"] != event_api._rows_hash(rows)
            or receipt["provenance_records"] != expected_records(rows, receipt["retrieved_at"])):
        raise ValueError("Acquisition receipt does not describe the registered source")
    if receipt["http_statuses"] != [200, 200] or receipt["replayed_rows"] != len(rows):
        raise ValueError("Expected a successful fetch and replay of the fixture page")


def capture(root=ROOT):
    digest, source = source_batch(root)
    with event_api._running_server(source, inject_503=False) as (url, _):
        started = datetime.now(timezone.utc).isoformat()
        received, transport = event_api.fetch_paginated(url)
        completed = datetime.now(timezone.utc).isoformat()
    rows = received.ordered()
    receipt = {
        "scope": "synthetic_fixture_over_loopback_http",
        "source_path": SOURCE_PATH,
        "source_sha256": digest,
        "payload_sha256": event_api._rows_hash(rows),
        "request_started_at_utc": started,
        "retrieved_at": completed,
        "http_statuses": [status for page in transport["page_records"]
                          for status in page["http_statuses"]] + transport["replay"]["http_statuses"],
        "replayed_rows": transport["duplicate_rows_replayed"],
        "provenance_records": expected_records(rows, completed),
    }
    verify_receipt(root, receipt)
    return receipt


if __name__ == "__main__":
    print(json.dumps(capture(), indent=2, sort_keys=True))
