"""Independent timelines for supervised target maturity and weighting."""

import copy
import unittest

from ds_m01.candidate_targets import prepare_training_rows, select_fit_outcomes


CUTOFF = '2026-03-01T08:00:00Z'


def outcome(trip='FIT', **changes):
    return dict(tenant_id='T01', trip_id=trip, outcome_version_id=changes.pop('outcome_version_id', trip + '-V1'),
                label_available_time=changes.pop('label_available_time', '2026-01-11T00:00:00Z'),
                actual_arrival_time=changes.pop('actual_arrival_time', '2026-01-10T18:00:00.250Z'),
                label_status=changes.pop('label_status', 'verified'),
                arrival_time_precision=changes.pop('arrival_time_precision', 'second'), **changes)


def observation(trip='FIT', sid='S1', at='2026-01-10T17:00:00Z', elapsed=3600):
    metadata = dict(tenant_id='T01', trip_id=trip, snapshot_id=sid, snapshot_time=at)
    return (dict(metadata, fold_id='F01', fold_role='fit', status='available', vector=[1.0]),
            dict(metadata, numeric={'elapsed_trip_seconds': elapsed}))


def targets(observations, outcomes, keys=None):
    keys = keys or {('T01', row[0]['trip_id']) for row in observations}
    selected = select_fit_outcomes(outcomes, keys, CUTOFF)
    return prepare_training_rows([row[0] for row in observations], [row[1] for row in observations], selected,
                                 fold_id='F01', fit_trip_keys=keys, training_cutoff_at=CUTOFF)


class CandidateTargetTests(unittest.TestCase):
    def test_fractional_seconds_and_equal_trip_not_snapshot_weights(self):
        rows = targets([observation(), observation(sid='S2', at='2026-01-10T17:30:00Z', elapsed=5400),
                        observation(trip='OTHER', sid='S3')], [outcome(), outcome('OTHER')])
        self.assertEqual([row['sample_weight'] for row in rows], [0.5, 0.5, 1.0])
        self.assertEqual([row['remaining_duration_seconds'] for row in rows], [3600.25, 1800.25, 3600.25])

    def test_later_correction_and_equal_cutoff_do_not_replace_known_target(self):
        original = outcome()
        future = outcome(outcome_version_id='FUTURE', label_available_time=CUTOFF, actual_arrival_time='malformed')
        self.assertEqual(targets([observation()], [original, future]), targets([observation()], [original]))

    def test_latest_known_withdrawal_excludes_old_verified_label(self):
        for status in ('cancelled', 'pending', 'incomplete', 'lost', 'conflict'):
            with self.subTest(status=status):
                later = outcome(outcome_version_id='V2', label_available_time='2026-02-01T00:00:00Z',
                                label_status=status, actual_arrival_time=None)
                row = targets([observation()], [outcome(), later])[0]
                self.assertEqual((row['eligibility'], row['reason'], row['sample_weight']),
                                 ('excluded', 'latest_outcome_not_verified', 0.0))
                self.assertEqual(row['outcome_version_id'], 'V2')

    def test_later_known_verified_correction_replaces_earlier_target(self):
        later = outcome(outcome_version_id='V2', label_available_time='2026-02-01T00:00:00Z',
                        actual_arrival_time='2026-01-10T17:45:00Z')
        row = targets([observation()], [outcome(), later])[0]
        self.assertEqual((row['remaining_duration_seconds'], row['outcome_version_id']), (2700, 'V2'))

    def test_validation_other_tenant_and_heldout_payloads_are_not_inspected(self):
        poison = [dict(tenant_id='T01', trip_id='VALIDATION', label_available_time=object()),
                  dict(tenant_id='T02', trip_id='FIT', label_available_time=object()),
                  dict(tenant_id='T01', trip_id='TEST', actual_arrival_time=object())]
        self.assertEqual(targets([observation()], [outcome(), *poison]), targets([observation()], [outcome()]))

    def test_duplicate_known_version_and_simultaneous_versions_fail_closed(self):
        for other in (outcome(), outcome(outcome_version_id='OTHER')):
            with self.subTest(other=other['outcome_version_id']), self.assertRaises(ValueError):
                targets([observation()], [outcome(), other])

    def test_arrival_known_before_it_happened_is_invalid(self):
        with self.assertRaisesRegex(ValueError, 'after it was known'):
            targets([observation()], [outcome(label_available_time='2026-01-10T17:00:00Z')])

    def test_missing_label_coarse_label_and_arrived_snapshots_remain_exclusion_rows(self):
        self.assertEqual(targets([observation()], [])[0]['reason'], 'no_outcome_known_before_fit')
        self.assertEqual(targets([observation()], [outcome(arrival_time_precision='day')])[0]['reason'], 'insufficient_arrival_precision')
        late = observation(at='2026-01-10T18:30:00Z', elapsed=9000)
        self.assertEqual(targets([late], [outcome()])[0]['reason'], 'already_arrived_at_snapshot')
        self.assertEqual(targets([observation()], [outcome(actual_arrival_time='2026-01-10T15:00:00Z')])[0]['reason'], 'nonpositive_trip_duration')

    def test_unknown_status_and_missing_precise_verified_arrival_fail(self):
        for changes in ({'label_status': 'invented'}, {'actual_arrival_time': None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                targets([observation()], [outcome(**changes)])

    def test_duplicate_or_wrong_role_matrix_and_future_snapshot_fail(self):
        for obs in ([observation(), observation()], [observation(at=CUTOFF)]):
            with self.subTest(obs=obs), self.assertRaises(ValueError):
                targets(obs, [outcome()])
        wrong = observation()
        wrong[0]['fold_role'] = 'validation'
        with self.assertRaises(ValueError):
            targets([wrong], [outcome()])

    def test_missing_feature_is_not_silently_dropped(self):
        matrix, _ = observation()
        with self.assertRaises(ValueError):
            prepare_training_rows([matrix], [], {}, fold_id='F01', fit_trip_keys={('T01', 'FIT')}, training_cutoff_at=CUTOFF)

    def test_unsupported_vector_preserves_fit_record_without_target_weight(self):
        obs = observation()
        obs[0].update(status='unsupported', vector=None)
        row = targets([obs], [outcome()])[0]
        self.assertEqual((row['reason'], row['sample_weight']), ('unsupported_feature_vector', 0.0))

    def test_inputs_not_mutated(self):
        observations, outcomes = [observation()], [outcome()]
        before = copy.deepcopy((observations, outcomes))
        targets(observations, outcomes)
        self.assertEqual((observations, outcomes), before)


if __name__ == '__main__':
    unittest.main()
