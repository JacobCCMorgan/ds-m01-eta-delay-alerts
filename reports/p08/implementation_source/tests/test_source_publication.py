"""Reject incomplete, malformed or altered source sets before point-in-time joins."""
from pathlib import Path
import json
import tempfile
import unittest
from ds_m01.common import SOURCE_TABLES, write_source_manifest, verify_source_manifest


class SourcePublicationTests(unittest.TestCase):
    def source(self, root):
        raw = root / 'data/raw'
        raw.mkdir(parents=True)
        for table in SOURCE_TABLES:
            (raw / f'{table}.jsonl').write_text('{"key":"source-record"}\n')
        return {table: 1 for table in SOURCE_TABLES}

    def change_manifest(self, root, change):
        path = root / 'data/source_manifest.json'
        manifest = json.loads(path.read_text())
        change(manifest)
        path.write_text(json.dumps(manifest))

    def test_truncated_and_changed_sources_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            counts = self.source(root)
            counts['eta_trip'] = 2
            path = root / 'data/raw/eta_trip.jsonl'
            with self.assertRaises(IOError):
                write_source_manifest(root, counts)
            path.write_text('{"trip_id":"T1"}\n{"trip_id":"T2"}\n')
            write_source_manifest(root, counts)
            verify_source_manifest(root)
            path.write_text('{"trip_id":"T1"}\n{"trip_id":"CHANGED"}\n')
            with self.assertRaises(IOError):
                verify_source_manifest(root)

    def test_publication_requires_all_canonical_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            counts = self.source(root)
            del counts['telemetry_event']
            with self.assertRaises(ValueError):
                write_source_manifest(root, counts)
            self.assertFalse((root / 'data/source_manifest.json').exists())

    def test_omitted_manifest_entry_cannot_hide_changed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            counts = self.source(root)
            write_source_manifest(root, counts)
            self.change_manifest(root, lambda m: m['files'].pop())
            (root / 'data/raw/telemetry_event.jsonl').write_text('{"key":"changed"}\n')
            with self.assertRaises(ValueError):
                verify_source_manifest(root)

    def test_duplicate_entry_cannot_replace_another_required_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_source_manifest(root, self.source(root))
            self.change_manifest(root, lambda m: m['files'].__setitem__(-1, m['files'][0]))
            with self.assertRaises(ValueError):
                verify_source_manifest(root)

    def test_manifest_row_count_is_verified_even_when_hash_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_source_manifest(root, self.source(root))
            self.change_manifest(root, lambda m: m['files'][0].__setitem__('rows', 2))
            with self.assertRaises(IOError):
                verify_source_manifest(root)

    def test_unexpected_path_is_rejected_before_file_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_source_manifest(root, self.source(root))
            self.change_manifest(root, lambda m: m['files'][0].__setitem__('path', 'data/raw/../../secret'))
            with self.assertRaises(ValueError):
                verify_source_manifest(root)

    def test_partial_blank_or_nonobject_jsonl_is_not_published(self):
        for content in ['{"key":"source-record"}', '\n', '{"key":\n', '[1,2]\n']:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                counts = self.source(root)
                (root / 'data/raw/eta_trip.jsonl').write_text(content)
                with self.assertRaises(IOError):
                    write_source_manifest(root, counts)
                self.assertFalse((root / 'data/source_manifest.json').exists())
