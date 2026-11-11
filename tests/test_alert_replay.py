"""Receipt claims must agree with rebuilt evidence, beyond matching file hashes."""
import copy
import unittest

from ds_m01.alert_replay import execution_clock, verify_upstream_receipt, verify_store_receipts


class AlertReplayReceiptTests(unittest.TestCase):
    def test_generation_is_after_selection_and_inside_execution(self):
        receipt = dict(started_at_utc='2026-11-06T20:00:00-08:00',
                       selection_completed_at_utc='2026-11-07T04:00:02Z',
                       canonical_generated_at='2026-11-07T04:00:02+00:00',
                       completed_at_utc='2026-11-07T04:00:05Z')
        self.assertEqual(execution_clock(receipt), receipt['canonical_generated_at'])
        for field, value in [('selection_completed_at_utc', '2026-11-07T04:00:06Z'),
                             ('started_at_utc', '2026-11-07T04:00:03Z'),
                             ('canonical_generated_at', '2026-11-07T04:00:00Z'),
                             ('completed_at_utc', '2026-11-07T04:00:05')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                execution_clock(dict(receipt, **{field: value}))

    def test_original_source_claim_is_distinct_from_current_replay_scope(self):
        replayed = dict(status='passed', differences=0, assessment_opportunities=341,
                        source_targets_verified=False, outcome_source_opened=False)
        original = dict(replayed, source_targets_verified=True, outcome_source_opened=True)
        verify_upstream_receipt(original, replayed)
        verify_upstream_receipt(original, original)
        self.assertFalse(replayed['source_targets_verified'])
        for field, value in [('assessment_opportunities', 340), ('source_targets_verified', False),
                             ('differences', 1)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_upstream_receipt(dict(original, **{field: value}), replayed)

    def test_store_claims_must_match_independent_checks(self):
        checks = [dict(name='example_fk_check', violations=0, passed=True)]
        replayed = dict(counts={'eta_prediction': 2}, checks=checks)
        receipt = dict(counts=replayed['counts'], checks=checks + [
            dict(name='parquet_roundtrip_all_tables', violations=0, passed=True)],
            foreign_keys_enforced=True, parquet_roundtrip_verified=True)
        verify_store_receipts(receipt, copy.deepcopy(receipt), replayed)
        for field, value in [('counts', {'eta_prediction': 1}), ('checks', []),
                             ('foreign_keys_enforced', False)]:
            mutated = dict(receipt, **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_store_receipts(mutated, copy.deepcopy(mutated), replayed)
        with self.assertRaises(ValueError):
            verify_store_receipts(receipt, dict(receipt, extra_claim=True), replayed)


if __name__ == '__main__':
    unittest.main()
