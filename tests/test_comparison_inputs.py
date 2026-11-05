"""Small independent clocks, joins and failure paths for training-only scoring."""

from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from ds_m01.common import parse_ts, utc
from ds_m01.comparison_inputs import (BASELINE, CANDIDATES, ETA_FIELDS, QUANTILES, _prepare_rows,
                                      _select_validation_outcomes, _validation_label, load_comparison_inputs)


FIT = '2026-03-01T08:00:00Z'
AT = '2026-03-10T12:00:00Z'
END = '2026-04-01T07:00:00Z'
FOLD = dict(fold_id='F01', training_cutoff_at=FIT, validation_label_cutoff_at=END,
            train_departure_dates=['2026-01-01', '2026-02-21'], validation_departure_dates=['2026-03-01', '2026-03-24'])


def forecast_values(seconds):
    return dict(zip(QUANTILES + ETA_FIELDS, [*seconds, *(utc(parse_ts(AT) + timedelta(seconds=x)) for x in seconds)]))


def example():
    identity = dict(tenant_id='T', trip_id='A', snapshot_id='S', snapshot_time=AT)
    categorical = dict(route_id='R1', departure_time_bucket='3-05@America/Los_Angeles', position_validity='missing')
    base = dict(identity, fold_id='F01', training_cutoff_at=FIT, status='available',
                pool='distance_band', route_id='R1', departure_time_bucket=categorical['departure_time_bucket'],
                **forecast_values([10, 60.25, 90]))
    member = dict(tenant_id='T', trip_id='A', fold_id='F01', fold_role='validation', original_split_role='train',
                  departure_date='2026-03-10', training_cutoff_at=FIT, label_cutoff_at=END)
    feature = dict(identity, categorical=categorical)
    reference = dict(base, label_eligibility='eligible', outcome_version_id='V1', remaining_duration_seconds=60,
                     position_validity='missing', elapsed_slice='0-60m', evaluation_label_cutoff_at=END)
    candidates = [dict(base, candidate_id=method, candidate_status='available', position_validity='missing',
                       prediction_source='candidate') for method in CANDIDATES]
    candidates[1].update(candidate_status='unsupported', prediction_source='baseline_fallback')
    outcome = dict(tenant_id='T', trip_id='A', outcome_version_id='V1', label_status='verified', arrival_time_precision='second',
                   actual_arrival_time='2026-03-10T12:01:00.750Z', label_available_time='2026-03-10T18:00:00Z')
    return dict(memberships=[member], features=[feature], baseline_rows=[base], reference_rows=[reference],
                candidate_rows=candidates, folds={'F01': FOLD}, outcomes=[outcome])


class ComparisonInputTests(unittest.TestCase):
    def test_shared_fractional_target_and_native_and_fallback_sources(self):
        row, = _prepare_rows(**example())
        self.assertEqual((row['eligible'], row['target_seconds'], row['route_support']), (True, 60.75, 'distance_band'))
        self.assertEqual(row['position_validity'], 'missing')
        self.assertEqual([row['predictions'][m]['source'] for m in (BASELINE, *CANDIDATES)], ['native', 'native', 'baseline_fallback'])
        self.assertEqual(row['predictions'][CANDIDATES[1]]['quantiles'], row['predictions'][BASELINE]['quantiles'])

    def test_protected_groups_and_equal_or_later_versions_never_expose_payload(self):
        # Missing/junk clocks on protected groups are never inspected. Versions
        # at or beyond a fold cutoff deliberately have no id or target fields.
        args = example()
        args['outcomes'] += [dict(tenant_id='T', trip_id=t, label_available_time='poison') for t in ('JULY', 'FINAL', 'FIT')]
        args['outcomes'] += [dict(tenant_id='OTHER', trip_id='A', label_available_time='poison'),
                             dict(tenant_id='T', trip_id='A', label_available_time=END),
                             dict(tenant_id='T', trip_id='A', label_available_time='2026-08-01T00:00:00Z')]
        self.assertEqual(_prepare_rows(**args), _prepare_rows(**example()))

    def test_latest_known_withdrawal_and_unknown_remain_opportunities(self):
        for state, expected_status in [('cancelled', 'cancelled'), ('pending', 'not_mature'), ('lost', 'lost')]:
            with self.subTest(state=state):
                args = example()
                withdrawal = dict(args['outcomes'][0], outcome_version_id='V2', label_available_time='2026-03-15T00:00:00Z',
                                  label_status=state, actual_arrival_time=None)
                args['outcomes'].insert(0, withdrawal)  # File order cannot resurrect the earlier verified version.
                args['reference_rows'][0].update(outcome_version_id='V2', label_eligibility=expected_status, remaining_duration_seconds=None)
                row, = _prepare_rows(**args)
                self.assertEqual((row['eligible'], row['target_seconds'], row['label_status']), (False, None, expected_status))
                self.assertEqual(row['predictions'][BASELINE]['source'], 'native')

    def test_known_ties_or_duplicate_revision_fail_before_outcome_selection(self):
        row = example()['outcomes'][0]
        for changes in ({'outcome_version_id': 'V2'}, {'label_available_time': '2026-03-12T00:00:00Z'}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'ambiguous'):
                _select_validation_outcomes([row, dict(row, **changes)], {('T', 'A'): FOLD})

    def test_invalid_clocks_or_frozen_reference_mismatch_fail(self):
        args = example()
        ref, outcome = args['reference_rows'][0], args['outcomes'][0]
        for changes in ({'label_available_time': END}, {'actual_arrival_time': '2026-03-11T00:00:00Z'},
                        {'outcome_version_id': 'V2'}, {'actual_arrival_time': '2026-03-10T12:01:01.010Z'},
                        {'tenant_id': 'T2'}, {'actual_arrival_time': None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                _validation_label(ref, dict(outcome, **changes), END)
        with self.assertRaisesRegex(ValueError, 'cutoff'):
            _validation_label(dict(ref, evaluation_label_cutoff_at=FIT), outcome, END)
        with self.assertRaisesRegex(ValueError, 'disagree'):
            _validation_label(dict(ref, label_eligibility='conflict', remaining_duration_seconds=None), outcome, END)

    def test_one_fractional_second_precision_rule_for_all_methods(self):
        args = example()
        ref = dict(args['reference_rows'][0], label_eligibility='already_arrived', remaining_duration_seconds=None)
        outcome = args['outcomes'][0]
        for at, expected in [('2026-03-10T12:00:00.500Z', ('eligible', 0.5)), (AT, ('already_arrived', None))]:
            self.assertEqual(_validation_label(ref, dict(outcome, actual_arrival_time=at), END), expected)
        ref.update(label_eligibility='insufficient_precision')
        self.assertEqual(_validation_label(ref, dict(outcome, arrival_time_precision='day'), END), ('insufficient_precision', None))

    def test_key_completeness_rejects_missing_extra_duplicate_and_cross_tenant_rows(self):
        for group in ('baseline_rows', 'reference_rows', 'candidate_rows'):
            for operation in ('remove', 'extra_trip', 'duplicate', 'other_tenant'):
                with self.subTest(group=group, operation=operation), self.assertRaises(ValueError):
                    args = example()
                    rows = args[group]
                    if operation == 'remove':
                        rows.pop()
                    elif operation == 'extra_trip':
                        rows.append(dict(rows[0], trip_id='B'))
                    elif operation == 'duplicate':
                        rows.append(rows[0].copy())
                    else:
                        rows[0]['tenant_id'] = 'T2'
                    _prepare_rows(**args)

    def test_membership_guards_precede_outcome_use(self):
        def raises_if_opened():
            raise AssertionError('Outcomes reached before cohort was validated')
            yield
        for changes in ({'original_split_role': 'calibration'}, {'departure_date': '2026-08-10'}, {'label_cutoff_at': FIT}):
            args = example()
            args['memberships'][0].update(changes)
            args['outcomes'] = raises_if_opened()
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'Membership'):
                _prepare_rows(**args)
        args = example()
        args['features'][0]['snapshot_time'] = END
        with self.assertRaisesRegex(ValueError, 'Snapshot clock'):
            _prepare_rows(**args)

    def test_fallback_values_status_and_row_clocks_must_match_retained_records(self):
        for changes in (forecast_values([8, 62, 98]), {'candidate_status': 'available'}, {'status': 'unavailable'},
                        {'snapshot_time': END}, {'eta_lower': END}, {'position_validity': 'valid'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                args = example()
                args['candidate_rows'][1].update(changes)
                _prepare_rows(**args)

    def test_changed_trust_root_rejected_even_when_it_contains_well_formed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'reports/m3b2/run_receipt.json'
            path.parent.mkdir(parents=True)
            path.write_text('{}\n')
            with self.assertRaisesRegex(ValueError, 'input hash mismatch'):
                load_comparison_inputs(tmp, {})
