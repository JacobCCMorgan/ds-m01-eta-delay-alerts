"""Hand-derived independent transport/scoring fixtures; no retained July runs."""
import ast
from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/check_delivery_simulation.py'
SPEC = importlib.util.spec_from_file_location('independent_delivery_audit', SCRIPT)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)
ZERO = audit.clock('2026-07-20T12:00:00Z')


def at(seconds):
    return audit.stamp(ZERO+timedelta(seconds=seconds))


def settings():
    return {'delivery': {'version': 'p07-delivery-v1', 'seed': 'DS-M01-P07-20261111',
        'counts_as_of': at(20000), 'latency_seconds': [5, 60], 'receipt_lag_seconds': [0, 15],
        'outcome_basis_points': {'delivered': 8000, 'transient_failure': 1500, 'permanent_failure': 300, 'unknown': 200},
        'max_attempts': 3, 'retry_backoff_seconds': [30, 120], 'ttl_seconds': 600,
        'unknown_timeout_seconds': 120, 'minimum_action_lead_seconds': 1800, 'repeat_suppression_seconds': 1800},
        'metrics': {'sparse_trip_threshold': 20},
        'population': {'label_cutoff_exclusive': '2026-08-01T07:00:00Z'}}


def candidate(cid='C1', *, issued=0, deadline=5000, trip='TR1', episode='E1', promise='P1', tenant='T1'):
    return {'candidate_id': cid, 'tenant_id': tenant, 'trip_id': trip, 'episode_id': episode,
        'snapshot_id': 'S-'+cid, 'snapshot_time': at(issued), 'simulated_issue_at': at(issued),
        'deadline_at': at(deadline), 'lead_seconds': deadline-issued, 'prediction_version': 'M1',
        'policy_version': 'RULE1', 'promise_version_id': promise, 'probability': '0.800000',
        'candidate_status': 'admitted', 'reason': 'fixture'}


def episodes(rows):
    result = {}
    for row in rows:
        key = row['tenant_id'], row['episode_id']
        if key not in result:
            result[key] = {'tenant_id': row['tenant_id'], 'trip_id': row['trip_id'],
                'episode_id': row['episode_id'], 'episode_started_at': at(-100),
                'episode_last_observed_at': at(19000), 'episode_closed_at': None,
                'episode_status': 'censored', 'closure_reason': None, 'censored_at': at(19000),
                'candidate_count': 0, 'policy_version': 'RULE1', 'start_snapshot_id': row['snapshot_id'],
                'start_promise_version_id': row['promise_version_id']}
        result[key]['candidate_count'] += 1
    return list(result.values())


def draw(outcome='delivered', latency=0, lag=0):
    return lambda *_: {'outcome': outcome, 'latency_seconds': latency, 'receipt_lag_seconds': lag}


def simulate(rows, *, source_episodes=None, config=None, cutoff=None, transport=None):
    return audit.reference_delivery(rows, source_episodes or episodes(rows), config or settings(),
                                    counts_as_of=cutoff, draw=transport or draw())


def label_inputs(rows, labels):
    opportunities, targets = [], []
    for row, specification in zip(rows, labels):
        opportunities.append({**row, 'promise_known_at': at(-1000)})
        kind, arrival = specification
        targets.append({'tenant_id': row['tenant_id'], 'trip_id': row['trip_id'], 'snapshot_id': row['snapshot_id'],
            'promise_version_id': row['promise_version_id'], 'deadline_at': row['deadline_at'],
            'eligibility': 'eligible' if kind in ('positive', 'negative') else 'excluded',
            'delay_label': kind=='positive' if kind in ('positive', 'negative') else None,
            'reason': 'already_arrived_at_snapshot' if kind=='post_arrival' else 'pending' if kind=='unknown' else 'verified_gate_arrival',
            'actual_arrival_time': at(arrival) if arrival is not None else None,
            'label_status': 'pending' if kind=='unknown' else 'verified', 'arrival_time_precision': 'second',
            'label_known_at': at(19000) if kind!='unknown' else None,
            'label_cutoff_at': '2026-08-01T07:00:00Z'})
    return opportunities, targets


class IndependentDeliveryAuditTests(unittest.TestCase):
    def test_only_standard_library_imports_and_no_production_imports(self):
        tree = ast.parse(SCRIPT.read_text())
        imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        imports += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        self.assertFalse(any(name and (name=='ds_m01' or name.startswith('ds_m01.')) for name in imports))
        self.assertNotIn('__import__', SCRIPT.read_text())

    def test_golden_sha256_transport_vectors(self):
        row = candidate()
        self.assertEqual(audit.transport_draw(row, 1, settings()),
                         {'outcome': 'delivered', 'latency_seconds': 16, 'receipt_lag_seconds': 13})
        self.assertEqual(audit.transport_draw(row, 2, settings()),
                         {'outcome': 'transient_failure', 'latency_seconds': 54, 'receipt_lag_seconds': 12})

    def test_exact_dispatch_and_service_lead_boundaries(self):
        self.assertEqual(simulate([candidate(deadline=1800)])['summary']['delivered_attempts'], 1)
        low = simulate([candidate(deadline=1799)])
        self.assertEqual(low['attempts'], [])
        self.assertEqual(low['alerts'][0]['alert_status'], 'no_action_lead')
        exact = simulate([candidate(deadline=1802)], transport=draw(latency=2))
        late = simulate([candidate(deadline=1801)], transport=draw(latency=2))
        self.assertEqual(exact['attempts'][0]['delivery_status'], 'delivered')
        self.assertEqual(late['attempts'][0]['service_outcome'], 'gate_blocked')
        self.assertEqual(late['attempts'][0]['error_reason'], 'insufficient_action_lead')

    def test_ttl_equality_passes_and_later_service_is_blocked(self):
        self.assertEqual(simulate([candidate()], transport=draw(latency=600))['summary']['delivered_attempts'], 1)
        blocked = simulate([candidate()], transport=draw(latency=601))
        self.assertEqual(blocked['attempts'][0]['error_reason'], 'ttl_expired')
        self.assertIsNone(blocked['attempts'][0]['delivered_at'])

    def test_same_time_episode_closure_precedes_service(self):
        rows = [candidate()]
        source = episodes(rows)
        source[0].update(episode_closed_at=at(5), closure_reason='risk_resolved', episode_status='closed')
        result = simulate(rows, source_episodes=source, transport=draw(latency=5))
        self.assertEqual(result['attempts'][0]['error_reason'], 'episode_closed')
        self.assertEqual([r['event_type'] for r in result['event_core']], ['candidate', 'dispatch', 'closure', 'service'])

    def test_cooldown_survives_promise_and_episode_changes(self):
        first = candidate('C1')
        for second_at, delivered in [(1809, 1), (1810, 2)]:
            with self.subTest(second_at=second_at):
                rows = [first, candidate('C2', issued=second_at, episode='E2', promise='P2', deadline=6000)]
                transport = lambda row, *_: {'outcome': 'delivered', 'latency_seconds': 10 if row['candidate_id']=='C1' else 0, 'receipt_lag_seconds': 0}
                self.assertEqual(simulate(rows, transport=transport)['summary']['delivered_attempts'], delivered)

    def test_service_success_suppresses_repeat_before_late_receipt(self):
        rows = [candidate('C1'), candidate('C2', issued=20)]
        result = simulate(rows, cutoff=at(30), transport=draw(latency=10, lag=90))
        self.assertEqual(result['summary']['delivered_attempts'], 0)
        self.assertEqual(result['summary']['attempts'], 1)
        self.assertEqual(result['attempts'][0]['service_outcome'], 'delivered')
        self.assertIsNone(result['attempts'][0]['delivered_at'])
        self.assertEqual(next(r for r in result['alerts'] if r['candidate_id']=='C2')['status_reason'], 'delivery_cooldown')

    def test_receipt_cutoff_is_inclusive_and_unknown_never_retries(self):
        before = simulate([candidate()], cutoff=at(14), transport=draw(latency=5, lag=10))
        exact = simulate([candidate()], cutoff=at(15), transport=draw(latency=5, lag=10))
        self.assertEqual(before['attempts'][0]['delivery_status'], 'queued')
        self.assertEqual(exact['attempts'][0]['delivery_status'], 'delivered')
        for cutoff, expected in [(119, 'queued'), (120, 'unknown'), (20000, 'unknown')]:
            result = simulate([candidate()], cutoff=at(cutoff), transport=draw('unknown', latency=900))
            self.assertEqual(len(result['attempts']), 1)
            self.assertEqual(result['attempts'][0]['delivery_status'], expected)
            self.assertIsNone(result['attempts'][0]['scheduled_service_at'])
            self.assertIsNone(result['attempts'][0]['delivered_at'])

    def test_transient_retries_start_after_receipt_plus_backoff(self):
        transport = lambda row, index, cfg: {'outcome': 'transient_failure' if index==1 else 'delivered',
                                           'latency_seconds': 5, 'receipt_lag_seconds': 7}
        result = simulate([candidate()], transport=transport)
        self.assertEqual([r['sent_at'] for r in result['attempts']], [at(0), at(42)])
        self.assertEqual(result['alerts'][0]['first_delivered_at'], at(47))
        self.assertEqual(result['attempts'][0]['retry_scheduled_at'], at(42))

    def test_maximum_attempts_and_retry_ttl_bound_work(self):
        result = simulate([candidate()], transport=draw('transient_failure', latency=1))
        self.assertEqual([r['sent_at'] for r in result['attempts']], [at(0), at(31), at(152)])
        self.assertEqual(result['alerts'][0]['status_reason'], 'maximum_attempts_exhausted')
        cfg = settings(); cfg['delivery']['retry_backoff_seconds'] = [600, 120]
        result = simulate([candidate()], config=cfg, transport=draw('transient_failure', latency=1))
        self.assertEqual(len(result['attempts']), 1)
        self.assertEqual(result['alerts'][0]['status_reason'], 'ttl_expired_before_retry')
        self.assertEqual(len(simulate([candidate()], transport=draw('permanent_failure'))['attempts']), 1)

    def test_zero_lag_receipt_is_causal_and_same_clock_identity_order_stable(self):
        rows = [candidate('B'), candidate('A')]
        result = simulate(rows)
        self.assertEqual([r['event_type'] for r in result['event_core']],
                         ['candidate', 'candidate', 'dispatch', 'dispatch', 'service', 'receipt', 'service'])
        self.assertEqual(next(r for r in result['alerts'] if r['alert_status']=='delivered')['candidate_id'], 'A')
        self.assertEqual(result, simulate(list(reversed(rows))))

    def test_future_candidates_closures_and_zero_candidate_episodes_are_retained(self):
        rows = [candidate(issued=100)]
        source = episodes(rows)
        source.append({**source[0], 'episode_id': 'EMPTY', 'candidate_count': 0})
        source[0].update(episode_closed_at=at(200), closure_reason='risk_resolved', episode_status='closed')
        result = simulate(rows, source_episodes=source, cutoff=at(50))
        self.assertEqual(result['attempts'], [])
        self.assertEqual(result['alerts'][0]['status_reason'], 'candidate_not_yet_observed')
        self.assertEqual(len(result['episodes']), 2)
        self.assertTrue(all(r['episode_closed_at'] is None for r in result['episodes']))

    def test_duplicate_idempotence_conflicts_and_label_injection(self):
        row = candidate(); source = episodes([row])
        result = simulate([row, deepcopy(row)], source_episodes=source+deepcopy(source))
        self.assertEqual(result['summary']['attempts'], 1)
        self.assertEqual(result['summary']['duplicate_candidate_records_ignored'], 1)
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            simulate([row, {**row, 'deadline_at': at(7000)}], source_episodes=source)
        with self.assertRaisesRegex(ValueError, 'Truth fields'):
            simulate([{**row, 'delay_label': True}], source_episodes=source)
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            simulate([], source_episodes=source)

    def test_first_success_uses_its_own_promise_after_first_candidate_failed(self):
        rows = [candidate('A', deadline=4000), candidate('B', issued=1800, deadline=6000, promise='P2')]
        transport = lambda row, *_: {'outcome': 'permanent_failure' if row['candidate_id']=='A' else 'delivered',
                                    'latency_seconds': 0, 'receipt_lag_seconds': 0}
        sim = simulate(rows, transport=transport)
        opp, labels = label_inputs(rows, [('positive', 5000), ('negative', 5000)])
        metrics = audit.reference_metrics(opp, labels, rows, sim, settings())
        self.assertEqual(metrics['counts']['first_successful_deliveries'], 1)
        self.assertEqual(metrics['delivery_rows'][0]['promise_version_id'], 'P2')
        self.assertEqual(metrics['delivery_rows'][0]['classification'], 'known_false')
        self.assertEqual(metrics['metrics']['conditional_first_delivery_useful_precision'], 0)

    def test_known_postarrival_and_unknown_too_late_are_assessable_not_useful(self):
        rows = [candidate('A', trip='TR1', episode='E1'), candidate('B', trip='TR2', episode='E2'),
                candidate('C', trip='TR3', episode='E3'), candidate('D', trip='TR4', episode='E4')]
        source = episodes(rows)
        sim = simulate(rows, source_episodes=source)
        opp, labels = label_inputs(rows, [('positive', 7000), ('post_arrival', -1), ('unknown', None), ('unknown', None)])
        # Scoring fixture only: a legacy/malformed transport can be late, so the
        # metric must never improve precision by dropping its known non-usefulness.
        for row in sim['attempts']:
            if row['candidate_id']=='C':
                row.update(delivered_at=at(3300), receipt_known_at=at(3300))
        metrics = audit.reference_metrics(opp, labels, rows, sim, settings())
        self.assertEqual(metrics['counts']['known_assessable_first_deliveries'], 3)
        self.assertEqual(metrics['metrics']['conditional_first_delivery_useful_precision'], 1/3)
        self.assertEqual(metrics['metrics']['first_delivery_target_coverage'], 1/4)
        self.assertEqual(metrics['metrics']['first_delivery_assessable_coverage'], 3/4)
        self.assertEqual(metrics['metrics']['first_delivery_usefulness_bounds'], {'lower': 1/4, 'upper': 1/2})
        self.assertEqual(metrics['counts']['negative_trips'], 0)

    def test_missing_delivery_stays_in_actionable_denominator_and_empty_precision_is_null(self):
        rows = [candidate()]
        sim = simulate(rows, transport=draw('unknown'))
        opp, labels = label_inputs(rows, [('positive', 7000)])
        result = audit.reference_metrics(opp, labels, rows, sim, settings())
        self.assertEqual(result['denominators']['delivered_actionable_trip_recall'], 1)
        self.assertEqual(result['metrics']['delivered_actionable_trip_recall'], 0)
        self.assertIsNone(result['metrics']['conditional_first_delivery_useful_precision'])
        self.assertEqual(result['counts']['unknown_attempts'], 1)
        self.assertTrue(result['sparse'])

    def test_semantic_mutation_is_detected_and_boolean_is_not_numeric(self):
        checker = audit.Audit()
        checker.compare({'attempts': 2, 'delivered': True}, {'attempts': 1, 'delivered': 1}, 'fixture')
        self.assertEqual(len(checker.failures), 2)

    def test_cli_preserves_existing_file_and_dangling_symlink_before_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); output = root/'evidence.json'; output.write_text('preserved')
            with patch.object(audit, 'audit_report', side_effect=AssertionError('must not read')):
                with self.assertRaisesRegex(SystemExit, 'Refusing to overwrite'):
                    audit.main(['--root', str(root), '--output', str(output)])
                self.assertEqual(output.read_text(), 'preserved')
                link = root/'link.json'; link.symlink_to(root/'missing.json')
                with self.assertRaisesRegex(SystemExit, 'Refusing to overwrite'):
                    audit.main(['--root', str(root), '--output', str(link)])

    def test_exclusive_output_creation_protects_race(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); output = root/'evidence.json'
            def concurrent_writer(*_):
                output.write_text('other evidence')
                return {'status': 'passed', 'comparisons': 1, 'failure_count': 0}
            with patch.object(audit, 'audit_report', side_effect=concurrent_writer):
                with self.assertRaises(FileExistsError):
                    audit.main(['--root', str(root), '--output', str(output)])
            self.assertEqual(output.read_text(), 'other evidence')


if __name__ == '__main__':
    unittest.main()
