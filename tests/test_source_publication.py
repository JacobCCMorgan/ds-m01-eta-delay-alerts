"""Reject an incomplete source or post-publication alteration before joins."""
from pathlib import Path
import tempfile
import unittest
from ds_m01.common import write_source_manifest,verify_source_manifest


class SourcePublicationTests(unittest.TestCase):
    def test_truncated_and_changed_sources_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'data/raw/eta_trip.jsonl';path.parent.mkdir(parents=True)
            path.write_text('{"trip_id":"T1"}\n')
            with self.assertRaises(IOError):write_source_manifest(root,{'eta_trip':2})
            path.write_text('{"trip_id":"T1"}\n{"trip_id":"T2"}\n')
            write_source_manifest(root,{'eta_trip':2});verify_source_manifest(root)
            path.write_text('{"trip_id":"T1"}\n{"trip_id":"CHANGED"}\n')
            with self.assertRaises(IOError):verify_source_manifest(root)
