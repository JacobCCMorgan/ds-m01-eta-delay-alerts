"""Fresh output and factual execution clocks protect prior evidence."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
from ds_m01.delivery_run import run
from ds_m01.delivery_replay import execution_clock


class DeliveryRunGuardTests(unittest.TestCase):
    def test_existing_output_is_untouched_before_protocol_or_source_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'retained'
            output.mkdir(); marker = output / 'keep.txt'; marker.write_text('previous evidence')
            with patch('ds_m01.delivery_run.verify_settings') as settings:
                with self.assertRaisesRegex(ValueError, 'Output exists'):
                    run(directory, output)
                settings.assert_not_called()
            self.assertEqual(marker.read_text(), 'previous evidence')

    def test_broken_symlink_output_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'linked'
            output.symlink_to(Path(directory) / 'absent')
            with self.assertRaisesRegex(ValueError, 'Output exists'):
                run(directory, output)
            self.assertTrue(output.is_symlink())

    def test_failed_store_write_leaves_no_partial_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            freeze_path = root / 'reproducibility/p07/protocol_freeze.json'
            freeze_path.parent.mkdir(parents=True)
            freeze_path.write_text(json.dumps({'frozen_at_utc':'2026-11-11T00:00:00Z'}))
            marker = root / 'earlier.txt'; marker.write_text('retain prior evidence')
            output = root / 'new-result'
            settings = {'source_report':'parent'}
            simulation = dict(alerts=[],attempts=[],episodes=[],events=[],summary={})
            inputs = dict(candidates=[],episodes=[],opportunities=[],targets=[],parent_tables={})
            with ExitStack() as stack:
                for name,value in [('verify_settings',settings),('code_hashes',{}),
                                   ('verify_freeze',{'frozen_at_utc':'2026-11-11T00:00:00Z'}),
                                   ('verify_policy',{}),('load_inputs',inputs),
                                   ('simulate_delivery',simulation),('score_delivery',{}),('build_tables',{})]:
                    stack.enter_context(patch('ds_m01.delivery_run.'+name,return_value=value))
                stack.enter_context(patch('ds_m01.delivery_run.write_store',side_effect=RuntimeError('export failed')))
                with self.assertRaisesRegex(RuntimeError,'export failed'):
                    run(root,output)
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob('.new-result-*')),[])
            self.assertEqual(marker.read_text(),'retain prior evidence')

    def test_freeze_and_materialization_clock_order_is_enforced(self):
        freeze = {'frozen_at_utc':'2026-11-11T19:00:00Z'}
        receipt = {'started_at_utc':'2026-11-11T20:00:00Z',
                   'materialized_at_utc':'2026-11-11T20:00:02Z',
                   'completed_at_utc':'2026-11-11T20:00:04Z'}
        self.assertEqual(execution_clock(receipt, freeze), receipt['materialized_at_utc'])
        for field,value in [('started_at_utc','2026-11-11T18:00:00Z'),
                            ('materialized_at_utc','2026-11-11T20:00:05Z'),
                            ('completed_at_utc','2026-11-11T20:00:01Z'),
                            ('started_at_utc','2026-11-11T20:00:00')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                execution_clock(dict(receipt, **{field:value}), freeze)


if __name__ == '__main__':
    unittest.main()
