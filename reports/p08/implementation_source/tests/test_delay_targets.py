"""Hand-calculated binary labels, independent of simulator or prediction output."""

import copy
import unittest

from ds_m01.delay_targets import build_delay_targets, prepare_promise_features


KEYS = {('T1', 'TRIP')}
CUTOFF = '2026-03-01T00:00:00Z'


def snapshot(**changes):
    return dict(dict(tenant_id='T1', trip_id='TRIP', snapshot_id='S1',
                     snapshot_time='2026-01-10T10:00:00Z', promise_version_id='P1'), **changes)


def promise(**changes):
    return dict(dict(tenant_id='T1', trip_id='TRIP', promise_version_id='P1',
                     window_start_at='2026-01-10T10:30:00Z', window_end_at='2026-01-10T11:00:00Z',
                     known_at='2026-01-09T00:00:00Z', supersedes_promise_version_id=None,
                     provenance_id='PP1'), **changes)


def outcome(**changes):
    return dict(dict(tenant_id='T1', trip_id='TRIP', outcome_version_id='O1', label_status='verified',
                     actual_arrival_time='2026-01-10T11:00:00Z', label_available_time='2026-01-11T00:00:00Z',
                     arrival_time_precision='second', arrival_evidence_ref='gate-observation-1',
                     provenance_id='PO1', cancellation_time=None), **changes)


def target(observations=None, outcomes=None, promises=None, cutoff=CUTOFF, keys=KEYS):
    return build_delay_targets([snapshot()] if observations is None else observations, keys,
                               [outcome()] if outcomes is None else outcomes,
                               [promise()] if promises is None else promises, cutoff)


class DelayTargetTests(unittest.TestCase):
    def test_zero_grace_and_exact_microsecond_boundary(self):
        for arrival, delayed in [('10:59:59.999999', False), ('11:00:00', False), ('11:00:00.000001', True)]:
            with self.subTest(arrival=arrival):
                row, = target(outcomes=[outcome(actual_arrival_time=f'2026-01-10T{arrival}Z')])
                self.assertEqual(row['eligibility'], 'eligible')
                self.assertIs(row['delay_label'], delayed)
                self.assertEqual(row['grace_seconds'], 0)
                self.assertEqual(row['deadline_at'], row['promise_window_end_at'])
        with self.assertRaises(TypeError):
            build_delay_targets([snapshot()], KEYS, [outcome()], [promise()], CUTOFF, grace_seconds=60)

    def test_offset_and_fractional_promise_boundary_are_not_rounded(self):
        row, = target(promises=[promise(window_end_at='2026-01-10T03:00:00.500000-08:00')],
                      outcomes=[outcome(actual_arrival_time='2026-01-10T11:00:00.500001Z')])
        self.assertIs(row['delay_label'], True)
        self.assertAlmostEqual(row['delay_seconds'], .000001)
        self.assertEqual(row['promise_seconds_remaining'], 3600.5)

    def test_promise_horizon_is_label_free_and_can_be_negative(self):
        rows = prepare_promise_features([snapshot(snapshot_time='2026-01-10T11:00:00.250Z')], KEYS, [promise()])
        self.assertEqual(rows[0]['promise_seconds_remaining'], -.25)
        self.assertEqual(rows[0]['status'], 'available')
        self.assertEqual(rows[0]['promise_provenance_id'], 'PP1')
        self.assertNotIn('delay_label', rows[0])
        self.assertNotIn('actual_arrival_time', rows[0])

    def test_snapshot_promise_changes_only_when_revision_becomes_known(self):
        revision = promise(promise_version_id='P2', known_at='2026-01-10T10:30:00Z',
                           window_end_at='2026-01-10T12:00:00Z', supersedes_promise_version_id='P1')
        observations = [snapshot(), snapshot(snapshot_id='S2', snapshot_time='2026-01-10T10:30:00Z', promise_version_id='P2')]
        rows = target(observations, outcomes=[outcome(actual_arrival_time='2026-01-10T11:30:00Z')], promises=[promise(), revision])
        self.assertEqual([r['delay_label'] for r in rows], [True, False])
        self.assertEqual([r['promise_version_id'] for r in rows], ['P1', 'P2'])
        for wrong in (snapshot(promise_version_id='P2'), dict(observations[1], promise_version_id='P1')):
            with self.subTest(wrong=wrong), self.assertRaisesRegex(ValueError, 'latest version'):
                target([wrong], promises=[promise(), revision])

    def test_future_promise_and_foreign_payloads_are_ignored(self):
        poison_promises = [dict(tenant_id='T2', trip_id='TRIP', known_at=object()),
                           dict(tenant_id='T1', trip_id='HOLDOUT', known_at=object()),
                           dict(tenant_id='T1', trip_id='TRIP', known_at='2026-01-10T10:00:00.000001Z', window_end_at=object())]
        poison_outcomes = [dict(tenant_id='T2', trip_id='TRIP', label_available_time=object()),
                           dict(tenant_id='T1', trip_id='HOLDOUT', label_status=object()),
                           dict(tenant_id='T1', trip_id='TRIP', label_available_time=CUTOFF, actual_arrival_time=object())]
        self.assertEqual(target(), target(promises=[promise(), *poison_promises], outcomes=[outcome(), *poison_outcomes]))

    def test_label_cutoff_is_strict_and_revisions_are_as_of_each_fold(self):
        earlier, = target(cutoff='2026-01-11T00:00:00Z')
        self.assertIsNone(earlier['delay_label'])
        self.assertEqual(earlier['reason'], 'no_outcome_known_before_cutoff')
        revised = outcome(outcome_version_id='O2', label_available_time='2026-02-01T00:00:00Z',
                          actual_arrival_time='2026-01-10T11:00:01Z')
        before, = target(outcomes=[outcome(), revised], cutoff='2026-02-01T00:00:00Z')
        after, = target(outcomes=[outcome(), revised], cutoff='2026-02-01T00:00:00.000001Z')
        self.assertIs(before['delay_label'], False)
        self.assertIs(after['delay_label'], True)
        self.assertEqual((before['outcome_version_id'], after['outcome_version_id']), ('O1', 'O2'))

    def test_latest_withdrawn_label_never_resurrects_old_success(self):
        for status in ('pending', 'incomplete', 'cancelled', 'lost', 'conflict'):
            with self.subTest(status=status):
                row, = target(outcomes=[outcome(), outcome(outcome_version_id='O2', label_status=status,
                                                         label_available_time='2026-02-01T00:00:00Z', actual_arrival_time=None)])
                self.assertEqual((row['eligibility'], row['label_status'], row['reason']),
                                 ('excluded', status, 'latest_outcome_' + status))
                self.assertIsNone(row['delay_label'])
                self.assertEqual(row['outcome_version_id'], 'O2')

    def test_minute_date_interval_and_unknown_precision_never_become_on_time(self):
        for precision in ('minute', 'date_only', 'interval', 'unknown', None):
            with self.subTest(precision=precision):
                row, = target(outcomes=[outcome(arrival_time_precision=precision)])
                self.assertEqual(row['reason'], 'insufficient_arrival_precision')
                self.assertIsNone(row['delay_label'])

    def test_partial_pod_and_service_completion_do_not_supply_gate_arrival(self):
        for change, reason in [
            ({'actual_arrival_time': None, 'delivery_completed_time': '2026-01-10T10:30:00Z',
              'service_start_time': '2026-01-10T10:20:00Z', 'pod_status': 'partial'}, 'missing_gate_arrival'),
            ({'arrival_evidence_ref': None}, 'missing_arrival_evidence'),
            ({'cancellation_time': '2026-01-10T10:45:00Z'}, 'conflicting_cancellation_evidence'),
            ({'label_status': 'incomplete', 'pod_status': 'partial'}, 'latest_outcome_incomplete'),
        ]:
            with self.subTest(change=change):
                row, = target(outcomes=[outcome(**change)])
                self.assertEqual(row['reason'], reason)
                self.assertIsNone(row['delay_label'])
        original = target()
        self.assertEqual(original, target(outcomes=[outcome(delivery_completed_time='2099-01-01T00:00:00Z',
                                                          service_start_time='1900-01-01T00:00:00Z')]))

    def test_after_arrival_snapshot_retained_but_not_evaluated(self):
        for arrival in ('2026-01-10T10:00:00Z', '2026-01-10T09:59:59.999999Z'):
            row, = target(outcomes=[outcome(actual_arrival_time=arrival)])
            self.assertEqual(row['reason'], 'already_arrived_at_snapshot')
            self.assertIsNone(row['delay_label'])
        row, = target(outcomes=[outcome(actual_arrival_time='2026-01-10T10:00:00.000001Z')])
        self.assertEqual(row['remaining_duration_seconds'], .000001)
        self.assertIs(row['delay_label'], False)

    def test_bad_clocks_statuses_and_snapshot_scope_fail_closed(self):
        for changed in (outcome(label_available_time='2026-01-10T10:30:00Z'),
                        outcome(label_status='invented'), outcome(actual_arrival_time='2026-01-10T11:00:00')):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                target(outcomes=[changed])
        for changed in (snapshot(snapshot_time=CUTOFF), snapshot(trip_id='HOLDOUT'), snapshot(snapshot_time='2026-01-10T10:00:00')):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                target([changed])

    def test_duplicate_snapshot_versions_and_simultaneous_known_times_fail_closed(self):
        with self.assertRaises(ValueError):
            target([snapshot(), snapshot()])
        for other in (promise(), promise(promise_version_id='P2')):
            with self.subTest(other=other), self.assertRaises(ValueError):
                target(promises=[promise(), other])
        for other in (outcome(), outcome(outcome_version_id='O2')):
            with self.subTest(other=other), self.assertRaises(ValueError):
                target(outcomes=[outcome(), other])
        with self.assertRaises(ValueError):
            target(promises=[])
        with self.assertRaises(ValueError):
            target(promises=[promise(window_start_at='2026-01-10T12:00:00Z')])

    def test_tenant_identity_is_retained_even_with_reused_ids(self):
        rows = build_delay_targets([snapshot(), snapshot(tenant_id='T2')], {('T1', 'TRIP'), ('T2', 'TRIP')},
                                   [outcome(), outcome(tenant_id='T2', actual_arrival_time='2026-01-10T11:00:01Z')],
                                   [promise(), promise(tenant_id='T2')], CUTOFF)
        self.assertEqual([(r['tenant_id'], r['delay_label']) for r in rows], [('T1', False), ('T2', True)])

    def test_audit_fields_are_complete_and_inputs_are_not_mutated(self):
        inputs = ([snapshot(fold_id='F01', fold_role='fit')], [outcome()], [promise()])
        before = copy.deepcopy(inputs)
        row, = target(inputs[0], inputs[1], inputs[2])
        self.assertEqual(inputs, before)
        self.assertEqual((row['fold_id'], row['fold_role']), ('F01', 'fit'))
        self.assertEqual((row['outcome_provenance_id'], row['promise_provenance_id']), ('PO1', 'PP1'))
        self.assertEqual(row['arrival_evidence_ref'], 'gate-observation-1')
        self.assertEqual(row['label_cutoff_at'], CUTOFF)


if __name__ == '__main__':
    unittest.main()
