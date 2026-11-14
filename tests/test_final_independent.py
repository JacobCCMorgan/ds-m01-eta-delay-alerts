"""Independent P08 hand calculations and adversarial boundary fixtures only."""
import ast
from copy import deepcopy
from datetime import timedelta
import importlib.util
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/check_final_evaluation.py'
SPEC = importlib.util.spec_from_file_location('p08_independent', SCRIPT)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)
ZERO = audit.clock('2026-08-01T07:00:00Z')


def at(seconds):
    return audit.stamp(ZERO + timedelta(seconds=seconds))


def row(trip='A', sid='S1', **kwargs):
    return {'tenant_id': 'T1', 'trip_id': trip, 'snapshot_id': sid, 'snapshot_time': at(0),
            'status': 'available', 'eligibility': 'eligible', 'target_seconds': 10,
            'lower_seconds': 8, 'median_seconds': 10, 'upper_seconds': 12,
            'probability': .8, 'delay_label': True, 'prediction_source': 'model',
            'calibration_status': 'fitted', **kwargs}


def feature(sid='S1', elapsed=3600, **numeric):
    return {'tenant_id': 'T1', 'trip_id': 'A', 'snapshot_id': sid, 'snapshot_time': at(elapsed),
            'numeric': {'elapsed_trip_seconds': elapsed, 'route_distance_km': 100,
                        'remaining_distance_km': 50, 'gnss_speed_kmh': 20,
                        'position_age_seconds': 0, **numeric},
            'categorical': {'route_id': 'R1', 'departure_time_bucket': 'morning', 'position_validity': 'valid'}}


def target_fixture(*, arrival=100, known=200, precision='second', state='verified'):
    opportunity = {'tenant_id': 'T1', 'trip_id': 'A', 'snapshot_id': 'S1',
                   'snapshot_time': at(0), 'promise_version_id': 'P1'}
    promise = {'tenant_id': 'T1', 'trip_id': 'A', 'promise_version_id': 'P1',
               'known_at': at(-100), 'window_start_at': at(50), 'window_end_at': at(100)}
    outcome = {'tenant_id': 'T1', 'trip_id': 'A', 'outcome_version_id': 'O1',
               'label_available_time': at(known), 'label_status': state,
               'actual_arrival_time': at(arrival), 'arrival_time_precision': precision,
               'arrival_evidence_ref': 'independent-gate', 'cancellation_time': None}
    return opportunity, promise, outcome


class FinalIndependentMathTests(unittest.TestCase):
    def test_only_stdlib_and_no_production_imports(self):
        tree = ast.parse(SCRIPT.read_text())
        names = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        names += [item.name for node in ast.walk(tree) if isinstance(node, ast.Import) for item in node.names]
        self.assertFalse(any(name and (name == 'ds_m01' or name.startswith('ds_m01.')) for name in names))
        self.assertFalse(any(name and name.split('.')[0] in {'numpy', 'pandas', 'sklearn', 'scipy'} for name in names))

    def test_eta_equal_trip_weights_and_interval_penalty_hand_calculation(self):
        rows = [row(), row(sid='S2', lower_seconds=12, median_seconds=14, upper_seconds=16),
                row(trip='B', sid='S3', lower_seconds=3, median_seconds=4, upper_seconds=5)]
        actual = audit.reference_eta(rows)
        self.assertEqual(actual['metrics']['median_mae_seconds'], 4)
        self.assertAlmostEqual(actual['metrics']['median_rmse_seconds'], math.sqrt(22))
        self.assertEqual(actual['metrics']['snapshot_coverage'], .25)
        self.assertEqual(actual['metrics']['mean_width_seconds'], 3)
        self.assertEqual(actual['metrics']['interval_score'], 33)

    def test_missing_interval_is_whole_trip_miss_not_denominator_deletion(self):
        rows = [row(), row(sid='S2', status='unavailable', lower_seconds=None,
                           median_seconds=None, upper_seconds=None), row(trip='B', sid='S3')]
        actual = audit.reference_eta(rows)
        self.assertEqual(actual['metrics']['snapshot_coverage'], 1)
        self.assertEqual(actual['metrics']['whole_trip_coverage'], .5)
        self.assertEqual(actual['metrics']['conditional_full_availability_coverage'], 1)
        self.assertEqual(actual['support']['eligible_snapshots'], 3)
        self.assertEqual(actual['support']['scored_snapshots'], 2)

    def test_empty_eta_and_no_available_forecasts_are_explicit(self):
        empty = audit.reference_eta([])
        self.assertIsNone(empty['metrics']['whole_trip_coverage'])
        self.assertEqual(empty['null_reasons']['whole_trip_coverage'], 'no_eligible_targets')
        unavailable = audit.reference_eta([row(status='unavailable')])
        self.assertEqual(unavailable['metrics']['whole_trip_coverage'], 0)
        self.assertIsNone(unavailable['metrics']['median_mae_seconds'])

    def test_probability_equal_trip_weights_reliability_uses_full_weights(self):
        rows = [row(), row(sid='S2', probability=.2, delay_label=False),
                row(trip='B', sid='S3', probability=.4)]
        actual = audit.reference_probability(rows)
        self.assertAlmostEqual(actual['metrics']['brier'], .2)
        self.assertAlmostEqual(actual['metrics']['log_loss'], (-math.log(.8)-math.log(.4))/2)
        self.assertEqual(actual['metrics']['roc_auc'], 1)
        self.assertEqual(actual['metrics']['average_precision'], 1)
        self.assertEqual(actual['reliability'][8]['weight'], .5)
        self.assertEqual(actual['reliability'][4]['weight'], 1)
        self.assertEqual(actual['reliability'][4]['weight_fraction'], .5)
        self.assertAlmostEqual(actual['metrics']['ece'], .4)

    def test_ranking_ties_are_grouped_with_half_auc_credit(self):
        rows = [row(probability=.5), row(trip='B', sid='S2', probability=.5, delay_label=False)]
        actual = audit.reference_probability(rows)
        self.assertEqual(actual['metrics']['roc_auc'], .5)
        self.assertEqual(actual['metrics']['average_precision'], .5)

    def test_ranking_ap_uses_trip_weights_not_snapshot_counts(self):
        rows = [row(probability=.6), row(sid='S2', probability=.6),
                row(trip='B', sid='S3', probability=.8, delay_label=False)]
        actual = audit.reference_probability(rows)
        self.assertEqual(actual['metrics']['roc_auc'], 0)
        self.assertEqual(actual['metrics']['average_precision'], .5)

    def test_reliability_exact_edges_and_clipping_count(self):
        rows = [row(trip=str(i), sid=str(i), probability=p, delay_label=bool(i % 2))
                for i, p in enumerate([0, .1, .2, .9, 1])]
        actual = audit.reference_probability(rows)
        self.assertEqual([b['snapshots'] for b in actual['reliability']], [1, 1, 1, 0, 0, 0, 0, 0, 0, 2])
        self.assertEqual(actual['support']['log_loss_clipped_snapshots'], 2)
        self.assertTrue(math.isfinite(actual['metrics']['log_loss']))

    def test_single_class_rank_metrics_null_not_perfect(self):
        actual = audit.reference_probability([row()])
        for name in ('roc_auc', 'average_precision'):
            self.assertIsNone(actual['metrics'][name])
            self.assertEqual(actual['null_reasons'][name], 'single_class')
        self.assertAlmostEqual(actual['metrics']['brier'], .04)

    def test_unknown_target_keeps_availability_but_not_negative_class(self):
        actual = audit.reference_probability([row(eligibility='excluded', delay_label=None)])
        self.assertEqual(actual['support']['opportunities'], 1)
        self.assertEqual(actual['support']['available_snapshots'], 1)
        self.assertEqual(actual['support']['negative_snapshots'], 0)
        self.assertIsNone(actual['metrics']['brier'])

    def test_fixed_paired_snapshot_support_reweights_each_common_trip(self):
        left = [row(), row(sid='S2'), row(trip='B', sid='S3')]
        right = deepcopy(left)
        right[1]['status'] = 'unavailable'
        a, b, counts = audit.common_support(left, right)
        self.assertEqual([r['snapshot_id'] for r in a], ['S1', 'S3'])
        self.assertEqual(counts['left_lost_snapshots'], 1)
        self.assertEqual(counts['common_trips'], 2)
        self.assertEqual(audit.reference_probability(a)['support']['positive_weight'], 2)

    def test_projection_half_even_outward_and_threshold_precision(self):
        self.assertEqual(audit.project_probability(.5499995), '0.550000')
        self.assertEqual(audit.project_probability(.5500005), '0.550000')
        self.assertEqual(audit.project_eta(1.01, 2.5, 3.01), (1, 2, 4))
        self.assertEqual(audit.project_eta(2.9, 3.5, 3.9), (2, 4, 4))
        with self.assertRaises(ValueError):
            audit.project_probability(float('nan'))

    def test_master_bootstrap_preserves_each_tenant_novelty_stratum(self):
        members = [{'tenant_id': 'T'+str(i//2), 'trip_id': str(i),
                    'novelty_group': audit.NOVELTY[i % 2]} for i in range(6)]
        draws = audit.bootstrap_draws(members, replicates=4)
        self.assertEqual(draws, audit.bootstrap_draws(list(reversed(members)), replicates=4))
        self.assertTrue(all(len(sample) == 6 for sample in draws))
        self.assertTrue(all(set(sample) == {audit.trip(r) for r in members} for sample in draws))

    def test_bootstrap_paired_delta_uses_same_draws_and_quantile_rule(self):
        keys = [('T1', str(i)) for i in range(20)]
        values = {key: i for i, key in enumerate(keys)}
        other = {key: value-3 for key, value in values.items()}
        result = audit.bootstrap_interval(values, [tuple(keys), tuple(reversed(keys))], other=other)
        self.assertEqual((result['lower'], result['upper']), (3, 3))
        self.assertEqual(audit.quantile([0, 10], .025), .25)

    def test_bootstrap_undefined_replicate_is_not_silently_dropped(self):
        values = {('T1', str(i)): 1 for i in range(20)}
        result = audit.bootstrap_interval(values, [tuple(values), (('T2', 'missing'),)])
        self.assertIsNone(result['lower'])
        self.assertEqual(result['undefined_replicates'], 1)
        self.assertEqual(result['reason'], 'undefined_replicate_denominator')
        sparse = audit.bootstrap_interval({('T1', 'one'): 1}, [tuple(values)])
        self.assertEqual(sparse['reason'], 'fewer_than_20_scored_trips')


class FinalIndependentTargetAndStressTests(unittest.TestCase):
    def test_cutoff_exclusive_and_latest_pending_withdrawal_blocks_old_label(self):
        opportunity, promise, outcome = target_fixture()
        pending = dict(outcome, outcome_version_id='O2', label_available_time=at(300), label_status='pending')
        exact_cutoff = dict(outcome, outcome_version_id='O3', label_available_time=at(400))
        actual = audit.reference_targets([opportunity], [outcome, pending, exact_cutoff], [promise], at(400))
        for name in ('eta_targets', 'probability_targets'):
            self.assertEqual(actual[name][0]['reason'], 'latest_outcome_pending')
            self.assertEqual(actual[name][0]['outcome_version_id'], 'O2')
            self.assertEqual(actual[name][0]['eligibility'], 'excluded')

    def test_unknown_literal_status_cannot_extend_frozen_contract(self):
        opportunity, promise, outcome = target_fixture(state='withdrawn')
        with self.assertRaisesRegex(ValueError, 'Unknown frozen'):
            audit.reference_targets([opportunity], [outcome], [promise], at(400))

    def test_eta_minute_valid_binary_second_only(self):
        opportunity, promise, outcome = target_fixture(precision='minute')
        actual = audit.reference_targets([opportunity], [outcome], [promise], at(400))
        self.assertEqual(actual['eta_targets'][0]['target_seconds'], 100)
        self.assertEqual(actual['probability_targets'][0]['reason'], 'insufficient_arrival_precision')

    def test_deadline_equality_on_time_and_snapshot_equality_post_arrival(self):
        opportunity, promise, outcome = target_fixture()
        actual = audit.reference_targets([opportunity], [outcome], [promise], at(400))
        self.assertIs(actual['probability_targets'][0]['delay_label'], False)
        outcome['actual_arrival_time'] = at(0)
        actual = audit.reference_targets([opportunity], [outcome], [promise], at(400))
        self.assertEqual(actual['eta_targets'][0]['reason'], 'already_arrived_at_snapshot')
        self.assertIsNone(actual['probability_targets'][0]['delay_label'])

    def test_promise_revision_does_not_rewrite_snapshot_deadline(self):
        opportunity, promise, outcome = target_fixture(arrival=101)
        later = dict(promise, promise_version_id='P2', known_at=at(1), window_end_at=at(1000))
        actual = audit.reference_targets([opportunity], [outcome], [promise, later], at(400))
        self.assertTrue(actual['probability_targets'][0]['delay_label'])
        self.assertEqual(actual['probability_targets'][0]['promise_version_id'], 'P1')

    def test_ambiguous_outcome_clock_rejected_and_foreign_payload_not_inspected(self):
        opportunity, promise, outcome = target_fixture()
        duplicate = dict(outcome, outcome_version_id='O2')
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            audit.reference_targets([opportunity], [outcome, duplicate], [promise], at(400))
        foreign = {'tenant_id': 'FOREIGN', 'trip_id': 'secret'}
        actual = audit.reference_targets([opportunity], [outcome, foreign], [promise], at(400))
        self.assertEqual(actual['eta_targets'][0]['eligibility'], 'eligible')

    def test_missing_position_and_stale_boundary_preserve_original_inputs(self):
        features = [feature('S1', 1799), feature('S2', 1800)]
        before = deepcopy(features)
        stale = audit.reference_stress(features, 'S02_stale_position')
        self.assertEqual(stale[0], features[0])
        self.assertEqual(stale[1]['numeric']['position_age_seconds'], 1800)
        self.assertIsNone(stale[1]['numeric']['remaining_distance_km'])
        missing = audit.reference_stress(features, 'S01_missing_position')
        self.assertTrue(all(r['categorical']['position_validity'] == 'no_position' for r in missing))
        self.assertEqual(features, before)

    def test_stationary_requires_consecutive_original_window_not_filtered_rows(self):
        features = [feature('S1', 3600), feature('S2', 5400), feature('S3', 7200), feature('S4', 9000)]
        features[1]['categorical']['position_validity'] = 'no_position'
        self.assertEqual(audit.reference_stress(features, 'S05_stationary_window'), features)
        features.append(feature('S5', 10800, remaining_distance_km=20))
        actual = audit.reference_stress(features, 'S05_stationary_window')
        self.assertEqual([r['numeric']['gnss_speed_kmh'] for r in actual], [20, 20, 0, 0, 0])
        self.assertEqual(actual[-1]['numeric']['remaining_distance_km'], 50)

    def test_stationary_earliest_three_window_does_not_expand(self):
        features = [feature('S'+str(i), 3600+i*1800, remaining_distance_km=50-i) for i in range(4)]
        actual = audit.reference_stress(features, 'S05_stationary_window')
        self.assertEqual([r['numeric']['gnss_speed_kmh'] for r in actual], [0, 0, 0, 20])
        self.assertEqual([r['numeric']['remaining_distance_km'] for r in actual], [50, 50, 50, 47])

    def test_unknown_route_and_exact_decimal_distance_preserve_identity_and_null(self):
        features = [feature(remaining_distance_km=None, route_distance_km=.1)]
        scaled = audit.reference_stress(features, 'S06_distance_scale')
        self.assertEqual(scaled[0]['numeric']['route_distance_km'], .125)
        self.assertIsNone(scaled[0]['numeric']['remaining_distance_km'])
        unknown = audit.reference_stress(features, 'S03_unseen_route_slot')
        self.assertEqual(unknown[0]['snapshot_id'], 'S1')
        self.assertEqual(unknown[0]['categorical']['route_id'], '__P08_UNSEEN_ROUTE__')


class FinalIndependentArtifactAndAdapterTests(unittest.TestCase):
    def test_historical_inventory_excludes_only_reserved_top_level_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ['reports/p07/retained.json', 'reports/.p08-attempt-abc/draft.json',
                     'reports/p08/final.json', 'reports/.p08-attempt-/must_remain.json',
                     'reports/p08_other/must_remain.json', 'reports/p07/.p08-attempt-nested/must_remain.json']
            for name in names:
                path = root/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('{}')
            expected = {names[i] for i in (0, 3, 4, 5)}
            self.assertEqual(audit.historical_report_paths(root), expected)
            reader = audit.Reader()
            for name in expected:reader.hash(root/name)
            (root/names[1]).write_text('{"pending_changed":true}')
            (root/names[2]).write_text('{"completed_changed":true}')
            added = root/'reports/.p08-attempt-next/new.json'; added.parent.mkdir(); added.write_text('{}')
            self.assertEqual(audit.historical_report_paths(root), expected)
            reader.stable()
            (root/names[0]).write_text('{"historical_changed":true}')
            with self.assertRaisesRegex(ValueError, 'changed during audit'):reader.stable()
            (root/names[3]).unlink()
            self.assertNotEqual(audit.historical_report_paths(root), expected)

    def test_reserved_looking_top_level_files_remain_historical(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'reports').mkdir()
            names = ['reports/p08', 'reports/.p08-attempt-file', 'reports/.p08-attempt-']
            for name in names:(root/name).write_text('historical file')
            self.assertEqual(audit.historical_report_paths(root), set(names))

    def registration(self):
        sources = {'scripts/check_final_evaluation.py': 'a'*64}
        settings = {'input_sha256': {'data/raw/eta_trip_outcome.jsonl': 'b'*64}}
        record = {'status': 'frozen_before_final_target_access', 'final_outcomes_accessed': False,
            'base_public_commit': 'e95dcd7f885e20af19f51fc645c9e3f577dedd97',
            'derived_reporting_exclusions': sorted(audit.DERIVED_EVIDENCE),
            'model_or_calibrator_refitted': False, 'policy_reselected': False,
            'quality_accepted': False, 'operational_approval': False,
            'july_model_calibration_policy_and_delivery_results_previously_observed': True,
            'metadata_inventory': {'fixture': True}, 'metadata_inventory_sha256': audit.digest_json({'fixture': True}),
            'source_sha256': sources, 'input_sha256': settings['input_sha256'], 'recorded_at_utc': at(30),
            'conformance': {'status': 'passed_pre_target_conformance', 'failures': 0, 'skips': 0,
                'unit_tests': 600, 'final_outcomes_accessed': False, 'source_sha256': sources,
                'started_at_utc': at(0), 'completed_at_utc': at(20),
                'access_guard': {'version': 'p08-python-outcome-access-v1',
                    'protected_outcome_opens_allowed': 0, 'guarded_processes': 1,
                    'scope': 'Synthetic fixture guard', 'events_sha256': 'c'*64}}}
        return record, settings

    def test_original_registration_requires_scope_clock_and_observed_guard(self):
        original, settings = self.registration()
        self.assertFalse(audit.verify_registration_record(original, settings))
        for change in ('refit', 'clock', 'guard', 'boolean_count', 'inventory', 'exclusions'):
            with self.subTest(change=change):
                record = deepcopy(original)
                if change == 'refit':record['model_or_calibrator_refitted'] = True
                elif change == 'clock':record['conformance']['completed_at_utc'] = at(40)
                elif change == 'guard':record['conformance']['access_guard']['protected_outcome_opens_allowed'] = 1
                elif change == 'boolean_count':record['conformance']['failures'] = False
                elif change == 'inventory':record['metadata_inventory']['extra'] = True
                else:record['derived_reporting_exclusions'].append('contracts/fields.json')
                with self.assertRaises(ValueError):audit.verify_registration_record(record, settings)

    def test_correction_requires_prior_exposure_and_no_untouched_claim(self):
        record, settings = self.registration()
        prior = {name: 'd'*64 for name in ('implementation_freeze.json', 'pretarget_receipt.json',
                                          'exposure_record.json', 'failure_record.json')}
        record.update(status='registered_correction_after_final_target_exposure', final_outcomes_accessed=True,
                      correction={'kind': 'engineering_correction_after_final_target_exposure',
                        'scientific_protocol_or_inputs_changed': False, 'untouched_final_test_claim': False,
                        'prior_evidence_sha256': prior, 'prior_implementation_freeze_sha256': 'd'*64,
                        'prior_exposure_started_at_utc': at(10)})
        self.assertTrue(audit.verify_registration_record(record, settings))
        for key in ('untouched_final_test_claim', 'scientific_protocol_or_inputs_changed'):
            changed = deepcopy(record); changed['correction'][key] = True
            with self.assertRaises(ValueError):audit.verify_registration_record(changed, settings)
        changed = deepcopy(record); changed['final_outcomes_accessed'] = False
        with self.assertRaises(ValueError):audit.verify_registration_record(changed, settings)

    def test_invalid_artifact_gate_prevents_all_retained_target_reads(self):
        fake = audit.Reader()
        with patch.object(audit, 'Reader', return_value=fake), \
                patch.object(fake, 'hash', return_value='wrong_protocol'), \
                patch.object(fake, 'rows', side_effect=AssertionError('Target IO must not occur')) as reading:
            with self.assertRaisesRegex(ValueError, 'scientific protocol changed'):
                audit.verify_report(SCRIPT.parents[1], Path('/synthetic/unopened-report'))
        reading.assert_not_called()

    def test_retained_artifact_mutation_and_path_escape_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'example.json'; path.write_text('{}')
            reader = audit.Reader(); reader.json(path)
            path.write_text('{"changed":true}')
            with self.assertRaisesRegex(ValueError, 'changed during audit'):reader.stable()
            with self.assertRaisesRegex(ValueError, 'leaves its root'):audit.contained(root, '../outside.json')

    def test_cli_refuses_existing_output_before_audit_and_exclusive_race(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'receipt.json'; output.write_text('preserve')
            args = ['--root', str(SCRIPT.parents[1]), '--report', str(Path(directory)/'report'), '--output', str(output)]
            with patch.object(audit, 'verify_report') as verify:
                with self.assertRaisesRegex(ValueError, 'output exists'):audit.main(args)
                verify.assert_not_called()
            output.unlink()
            def racing(*_):
                output.write_text('concurrent evidence')
                return {'status': 'passed', 'comparisons': 1, 'failures': 0}
            with patch.object(audit, 'verify_report', side_effect=racing):
                with self.assertRaises(FileExistsError):audit.main(args)
            self.assertEqual(output.read_text(), 'concurrent evidence')

    def test_nested_labels_are_rejected_from_forecast_policy_inputs(self):
        with self.assertRaisesRegex(ValueError, 'Target field'):
            audit.assert_label_free({'matrix': [{'nested': {'delay_label': False}}]})
        audit.assert_label_free({'trip_status_as_of': 'arrived', 'labels_used': False})

    def test_all_forecast_report_adapters_on_synthetic_only_methods_slices_pairs(self):
        from ds_m01.final_metrics import build_metrics
        from test_final_metrics import SETTINGS, row as production_fixture, variants
        eta = variants([production_fixture('A'), production_fixture('B', y=12)], 'eta')
        probability = variants([production_fixture('A', family='probability', y=0, p=.2),
                                production_fixture('B', family='probability', y=1, p=.8)], 'probability')
        document = build_metrics(eta, probability, SETTINGS)
        memberships = [{'tenant_id': 'T1', 'trip_id': key, 'novelty_group': audit.NOVELTY[0]} for key in ('A', 'B')]
        checked = audit.Audit()
        audit.verify_forecast_metrics(eta, probability, document, memberships, checked, 'synthetic-only')
        self.assertEqual(checked.failures, [])
        self.assertGreater(checked.comparisons, 1000)

    def test_full_policy_adapter_preserves_global_first_repeat_and_unknown_labels(self):
        from ds_m01.final_policy import run_policy_delivery
        from ds_m01.final_delivery_metrics import score_policy_delivery
        from test_final_policy import SETTINGS, opportunity
        from test_final_delivery_metrics import target
        refs = [audit.load_reference(SCRIPT.parents[1], name) for name in ('check_alert_policy', 'check_delivery_simulation')]
        for scenario in ('S00_unmodified', 'S07_transport_degraded'):
            rows = [opportunity(deadline=20000), opportunity('S2', 4000, deadline=7200, promise='P2')]
            rows[1]['position_validity'] = 'no_position'
            for item in rows:item['scenario_id'] = scenario
            labels = [target(r) for r in rows]
            bundle = run_policy_delivery(rows, SETTINGS, scenario)
            document = score_policy_delivery(rows, labels, bundle, SETTINGS, scenario)
            checked = audit.Audit()
            audit.verify_policy_delivery(rows, labels, bundle, document, SETTINGS, scenario, *refs, checked)
            self.assertEqual(checked.failures, [], scenario)

    def test_whole_trip_ci_requires_scored_support_even_with_many_eligible_trips(self):
        memberships = [{'tenant_id': 'T1', 'trip_id': str(i), 'novelty_group': audit.NOVELTY[0]} for i in range(25)]
        plan = audit.BootstrapReference(memberships)
        values = {audit.trip(r): 0. for r in memberships}
        result = plan.interval(values, 'trip_all_eligible_snapshots_coverage', scored_trips=0)
        self.assertIsNone(result['lower'])
        self.assertEqual(result['original_contributing_trips'], 25)
        self.assertEqual(result['original_scored_trips'], 0)
        self.assertEqual(result['undefined_reason'], 'fewer_than_20_original_scored_trips')


if __name__ == '__main__':
    unittest.main(verbosity=2)
