"""Actual acquisition, historical availability and source identity stay separate."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from workflows.p12.acquisition import capture, verify_receipt


class AcquisitionTests(unittest.TestCase):
    def test_received_clock_is_observed_after_real_http_fetch(self):
        before = datetime.now(timezone.utc)
        receipt = capture(ROOT)
        after = datetime.now(timezone.utc)
        end = datetime.fromisoformat(receipt["retrieved_at"])
        self.assertLessEqual(before, datetime.fromisoformat(receipt["request_started_at_utc"]))
        self.assertLessEqual(end, after)
        self.assertEqual(receipt["provenance_records"][0]["retrieved_at"], receipt["retrieved_at"])
        self.assertEqual(receipt["http_statuses"], [200, 200])

    def test_retained_receipt_rejects_substitution_of_availability_or_identity(self):
        path = ROOT / "workflows/p12/evidence/acquisition_receipt.json"
        receipt = json.loads(path.read_bytes())
        verify_receipt(ROOT, receipt)
        row = receipt["provenance_records"][0]
        for name, replacement in (("retrieved_at", row["available_time"]), ("record_version", "99")):
            with self.subTest(field=name):
                changed = dict(receipt, provenance_records=[dict(row, **{name: replacement})])
                with self.assertRaises(ValueError):
                    verify_receipt(ROOT, changed)
