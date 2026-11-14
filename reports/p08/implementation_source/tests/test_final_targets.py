"""Synthetic boundary cases for the predeclared final-target join."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from ds_m01.final_targets import build_final_targets


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = json.loads((ROOT / 'config/p08_evaluation.json').read_text())
CUTOFF = '2026-10-15T07:00:00Z'


def opportunity(**changes):
    return dict(dict(tenant_id='T1', trip_id='TRIP', snapshot_id='S1',
                     snapshot_time='2026-08-01T10:00:00Z', promise_version_id='P1',
                     deadline_at='2026-08-01T11:00:00Z', promise_known_at='2026-07-31T23:00:00Z',
                     promise_provenance_id='PP1'), **changes)


def promise(**changes):
    return dict(dict(tenant_id='T1', trip_id='TRIP', promise_version_id='P1',
                     window_start_at='2026-08-01T10:30:00Z', window_end_at='2026-08-01T11:00:00Z',
                     known_at='2026-07-31T23:00:00Z', provenance_id='PP1'), **changes)


def outcome(**changes):
    return dict(dict(tenant_id='T1', trip_id='TRIP', outcome_version_id='O1',
                     label_available_time='2026-08-02T00:00:00Z', label_status='verified',
                     actual_arrival_time='2026-08-01T11:00:00Z', arrival_evidence_ref='independent-gate',
                     arrival_time_precision='second', provenance_id='PO1', cancellation_time=None), **changes)


def build(opportunities=None, outcomes=None, promises=None, settings=SETTINGS):
    return build_final_targets([opportunity()] if opportunities is None else opportunities,
                               [outcome()] if outcomes is None else outcomes,
                               [promise()] if promises is None else promises, settings)


class FinalTargetTests(unittest.TestCase):
    def test_exact_zero_grace_deadline_boundary_and_subsecond_remaining(self):
        for arrival, delayed in [('10:59:59.999999', False), ('11:00:00', False), ('11:00:00.000001', True)]:
            with self.subTest(arrival=arrival):
                result = build(outcomes=[outcome(actual_arrival_time='2026-08-01T' + arrival + 'Z')])
                eta, binary = result['eta_targets'][0], result['probability_targets'][0]
                self.assertIs(binary['delay_label'], delayed)
                self.assertEqual(binary['grace_seconds'], 0)
                self.assertEqual(eta['target_seconds'], binary['remaining_duration_seconds'])
                self.assertEqual(eta['eligibility'], 'eligible')
        result = build(outcomes=[outcome(actual_arrival_time='2026-08-01T10:00:00.000001Z')])
        self.assertEqual(result['eta_targets'][0]['target_seconds'], .000001)
        self.assertIs(result['probability_targets'][0]['delay_label'], False)

    def test_minute_arrival_scores_eta_but_never_becomes_binary_negative(self):
        result = build(outcomes=[outcome(arrival_time_precision='minute')])
        self.assertEqual(result['eta_targets'][0]['target_seconds'], 3600)
        binary = result['probability_targets'][0]
        self.assertEqual(binary['reason'], 'insufficient_arrival_precision')
        self.assertIsNone(binary['delay_label'])
        self.assertIsNone(binary['actual_arrival_time'])

    def test_unknown_precision_is_excluded_from_both_targets(self):
        for precision in ('unknown', 'date_only', 'interval', None):
            with self.subTest(precision=precision):
                result = build(outcomes=[outcome(arrival_time_precision=precision)])
                for rows in result.values():
                    self.assertEqual(rows[0]['reason'], 'insufficient_arrival_precision')
                    self.assertEqual(rows[0]['eligibility'], 'excluded')

    def test_strict_cutoff_excludes_equal_known_revision(self):
        revision = outcome(outcome_version_id='O2', label_status='cancelled', label_available_time=CUTOFF)
        result = build(outcomes=[outcome(), revision])
        self.assertEqual(result['eta_targets'][0]['outcome_version_id'], 'O1')
        result = build(outcomes=[dict(revision, label_available_time='2026-10-15T06:59:59.999999Z'), outcome()])
        self.assertEqual(result['eta_targets'][0]['reason'], 'latest_outcome_cancelled')
        self.assertIsNone(result['eta_targets'][0]['target_seconds'])
        result = build(outcomes=[revision])
        self.assertEqual(result['probability_targets'][0]['reason'], 'no_outcome_known_before_cutoff')

    def test_latest_withdrawal_never_resurrects_older_verified_evidence(self):
        for status in ('pending', 'cancelled', 'incomplete', 'lost', 'conflict'):
            revision = outcome(outcome_version_id='O2', label_status=status,
                               label_available_time='2026-10-14T00:00:00Z', actual_arrival_time=None)
            with self.subTest(status=status):
                result = build(outcomes=[revision, outcome()])
                for rows in result.values():
                    self.assertEqual(rows[0]['outcome_version_id'], 'O2')
                    self.assertEqual(rows[0]['reason'], 'latest_outcome_' + status)
                    self.assertEqual(rows[0]['eligibility'], 'excluded')
                    self.assertIsNone(rows[0]['actual_arrival_time'])
                self.assertIsNone(result['probability_targets'][0]['delay_label'])

    def test_latest_verified_revision_with_missing_evidence_still_blocks_old_label(self):
        revision = outcome(outcome_version_id='O2', label_available_time='2026-10-14T00:00:00Z',
                           actual_arrival_time=None)
        result = build(outcomes=[outcome(), revision])
        self.assertEqual(result['eta_targets'][0]['reason'], 'missing_gate_arrival')
        self.assertIsNone(result['eta_targets'][0]['target_seconds'])

    def test_equal_known_clocks_and_duplicate_version_fail_instead_of_arbitrary_tie(self):
        for revision in (outcome(), outcome(outcome_version_id='O2'),
                         outcome(label_available_time='2026-08-03T00:00:00Z')):
            with self.subTest(revision=revision), self.assertRaisesRegex(ValueError, 'Duplicate.*ambiguous'):
                build(outcomes=[outcome(), revision])

    def test_postarrival_evidence_remains_available_for_nonuseful_delivery_accounting(self):
        for arrival in ('2026-08-01T10:00:00Z', '2026-08-01T09:59:59.999999Z'):
            with self.subTest(arrival=arrival):
                result = build(outcomes=[outcome(actual_arrival_time=arrival)])
                for rows in result.values():
                    self.assertEqual(rows[0]['reason'], 'already_arrived_at_snapshot')
                    self.assertEqual(rows[0]['actual_arrival_time'], arrival)
                    self.assertEqual(rows[0]['arrival_evidence_ref'], 'independent-gate')
                self.assertIsNone(result['probability_targets'][0]['delay_label'])
                self.assertIsNone(result['eta_targets'][0]['target_seconds'])

    def test_partial_pod_service_start_completion_and_cancel_conflict_never_substitute(self):
        cases = [({'actual_arrival_time': None, 'service_start_time': '2026-08-01T10:30:00Z',
                   'delivery_completed_time': '2026-08-01T10:40:00Z', 'pod_status': 'partial'}, 'missing_gate_arrival'),
                 ({'arrival_evidence_ref': ''}, 'missing_arrival_evidence'),
                 ({'cancellation_time': '2026-08-01T10:00:00Z'}, 'conflicting_cancellation_evidence')]
        for changes, reason in cases:
            with self.subTest(reason=reason):
                for rows in build(outcomes=[outcome(**changes)]).values():
                    self.assertEqual(rows[0]['reason'], reason)
        self.assertEqual(build(), build(outcomes=[outcome(service_start_time='1900-01-01T00:00:00Z',
                                                       delivery_completed_time='2099-01-01T00:00:00Z')]))

    def test_source_promise_revision_applies_at_known_time_and_never_relabels_earlier_snapshot(self):
        revision = promise(promise_version_id='P2', known_at='2026-08-01T10:30:00Z',
                           window_end_at='2026-08-01T12:00:00Z', provenance_id='PP2')
        second = opportunity(snapshot_id='S2', snapshot_time='2026-08-01T10:30:00Z', promise_version_id='P2',
                             deadline_at='2026-08-01T12:00:00Z', promise_known_at='2026-08-01T10:30:00Z',
                             promise_provenance_id='PP2')
        result = build(opportunities=[opportunity(), second], outcomes=[outcome(actual_arrival_time='2026-08-01T11:30:00Z')],
                       promises=[promise(), revision])
        self.assertEqual([r['delay_label'] for r in result['probability_targets']], [True, False])
        self.assertEqual([r['promise_version_id'] for r in result['eta_targets']], ['P1', 'P2'])
        with self.assertRaisesRegex(ValueError, 'latest version'):
            build(opportunities=[dict(second, promise_version_id='P1')], promises=[promise(), revision])

    def test_retained_promise_bindings_are_checked_even_when_no_label_can_be_scored(self):
        for changes in ({'deadline_at': '2026-08-01T11:00:00.000001Z'},
                        {'promise_known_at': '2026-07-31T23:00:00.000001Z'},
                        {'promise_provenance_id': 'OTHER'}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'promise'):
                build(opportunities=[opportunity(**changes)], outcomes=[])

    def test_offset_fractional_boundary_and_crossmidnight_snapshot_are_not_rounded(self):
        row = opportunity(snapshot_time='2026-08-01T23:59:59.750-07:00',
                          deadline_at='2026-08-02T00:00:00.500-07:00')
        p = promise(window_start_at='2026-08-01T23:00:00-07:00', window_end_at=row['deadline_at'])
        result = build(opportunities=[row], promises=[p], outcomes=[outcome(
            actual_arrival_time='2026-08-02T00:00:00.500001-07:00', label_available_time='2026-08-03T00:00:00Z')])
        self.assertEqual(result['eta_targets'][0]['target_seconds'], .750001)
        self.assertIs(result['probability_targets'][0]['delay_label'], True)
        self.assertEqual(result['probability_targets'][0]['delay_seconds'], .000001)

    def test_foreign_future_and_superseded_payloads_are_not_inspected(self):
        class Protected(dict):
            def get(self, field, *args):
                if field not in ('tenant_id', 'trip_id', 'label_available_time', 'outcome_version_id'):
                    raise AssertionError('Protected outcome payload was inspected')
                return super().get(field, *args)
        protected = [Protected(tenant_id='T2', trip_id='TRIP'),
                     Protected(tenant_id='T1', trip_id='TRAIN'),
                     Protected(tenant_id='T1', trip_id='TRIP', label_available_time=CUTOFF),
                     Protected(tenant_id='T1', trip_id='TRIP', outcome_version_id='OLD',
                               label_available_time='2026-08-01T23:59:59Z')]
        self.assertEqual(build(), build(outcomes=[*protected, outcome()]))

    def test_tenant_qualified_reused_trip_snapshot_and_version_ids_remain_distinct(self):
        result = build(opportunities=[opportunity(), opportunity(tenant_id='T2')],
                       promises=[promise(), promise(tenant_id='T2')],
                       outcomes=[outcome(), outcome(tenant_id='T2', actual_arrival_time='2026-08-01T11:00:01Z')])
        self.assertEqual([(r['tenant_id'], r['delay_label']) for r in result['probability_targets']],
                         [('T1', False), ('T2', True)])

    def test_duplicate_missing_or_ambiguous_promises_and_opportunities_fail_closed(self):
        for opps in ([opportunity(), opportunity()], [opportunity(snapshot_time=CUTOFF)]):
            with self.assertRaises(ValueError): build(opportunities=opps)
        for promises in ([], [promise(), promise(promise_version_id='P2')],
                         [promise(window_start_at='2026-08-01T12:00:00Z')]):
            with self.assertRaises(ValueError): build(promises=promises)

    def test_invalid_status_precision_and_impossible_verification_clocks_fail(self):
        for changes in ({'label_status': 'withdrawn'}, {'arrival_time_precision': 'invented'},
                        {'actual_arrival_time': '2026-08-02T00:00:00.000001Z'},
                        {'actual_arrival_time': '2026-08-01T11:00:00'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build(outcomes=[outcome(**changes)])

    def test_protocol_cutoff_is_not_a_tunable_argument_and_inputs_are_unchanged(self):
        inputs = [opportunity()], [outcome()], [promise()]
        before = deepcopy(inputs)
        result = build_final_targets(inputs[0], inputs[1], inputs[2], SETTINGS)
        self.assertEqual(inputs, before)
        for rows in result.values():
            self.assertEqual(rows[0]['label_cutoff_at'], CUTOFF)
            self.assertEqual(rows[0]['outcome_provenance_id'], 'PO1')
            self.assertEqual(rows[0]['promise_provenance_id'], 'PP1')
        wrong = deepcopy(SETTINGS); wrong['population']['label_cutoff_exclusive'] = '2026-10-16T07:00:00Z'
        with self.assertRaisesRegex(ValueError, 'frozen P08'):
            build(settings=wrong)

    def test_empty_population_returns_both_empty_inventories(self):
        self.assertEqual(build(opportunities=[]), {'eta_targets': [], 'probability_targets': []})


if __name__ == '__main__':
    unittest.main()
