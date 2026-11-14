"""Independent runner fixtures test fold isolation and operational fallbacks."""

import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ds_m01.candidate_model import CANDIDATE_IDS
from ds_m01.candidate_run import train_and_predict, validate_retained_inputs
from ds_m01.candidate_targets import select_fit_outcomes
from ds_m01.common import parse_ts, utc
from ds_m01.feature_transform import fit_transformer, transform_rows
from ds_m01.features import FEATURE_DEFINITION_VERSION
from ds_m01.model_splits import validate_folds


PROJECT = Path(__file__).resolve().parents[1]


def fixture():
    source = json.loads((PROJECT / 'config/m2.json').read_text())
    settings = json.loads((PROJECT / 'config/m3a.json').read_text())
    settings.update(folds=settings['folds'][:1], candidate_ids=list(CANDIDATE_IDS), min_fit_trips=5)
    fold = validate_folds(settings['folds'], source)[0]
    features, matrices, memberships, transformers, history, outcomes = [], [], [], [], [], []
    for tenant in ('T01', 'T02'):
        fit, val = [], []
        for index in range(7):
            role = 'fit' if index < 5 else 'validation'
            trip = f'TRIP-{index}'
            day = '2026-01-10' if role == 'fit' else '2026-03-10'
            at = f'{day}T17:00:00Z'
            row = dict(tenant_id=tenant, trip_id=trip, snapshot_id='S-' + trip,
                       snapshot_time=at, feature_definition_version=FEATURE_DEFINITION_VERSION,
                       numeric=dict(elapsed_trip_seconds=3600.0, route_distance_km=100.0,
                                    remaining_distance_km=None, gnss_speed_kmh=None, position_age_seconds=None),
                       categorical=dict(route_id='R1', departure_time_bucket='1-08@America/Los_Angeles', position_validity='no_position'))
            (fit if role == 'fit' else val).append(row)
            memberships.append(dict(fold_id=fold['fold_id'], tenant_id=tenant, trip_id=trip,
                                    original_split_role='train', fold_role=role, training_cutoff_at=fold['training_cutoff_at']))
            outcome = dict(tenant_id=tenant, trip_id=trip, outcome_version_id='OUT-' + trip,
                           label_status='verified', arrival_time_precision='second',
                           actual_arrival_time=f'{day}T18:00:00Z', label_available_time=f'{day}T20:00:00Z')
            outcomes.append(outcome)
            if role == 'fit':
                history.append(dict(tenant_id=tenant, trip_id=trip, fold_id=fold['fold_id'],
                                    historical_outcome_version_id=outcome['outcome_version_id'],
                                    actual_arrival_time=outcome['actual_arrival_time'], label_known_at=outcome['label_available_time'],
                                    departure_time=f'{day}T16:00:00Z', duration_seconds=7200.0,
                                    training_cutoff_at=fold['training_cutoff_at'], route_id='R1',
                                    route_distance_km=100.0, departure_time_bucket='1-08@America/Los_Angeles'))
        state = fit_transformer(fit, tenant_id=tenant, fold_id=fold['fold_id'],
                                training_cutoff_at=fold['training_cutoff_at'],
                                fit_trip_keys={(tenant, row['trip_id']) for row in fit})
        transformers.append(state)
        features.extend(fit + val)
        for role, rows in (('fit', fit), ('validation', val)):
            matrices.extend(dict(row, status='available', reason=None) for row in transform_rows(rows, state, role=role))
    return dict(features=features, matrices=matrices, memberships=memberships,
                transformers=transformers, fallback_history=history), settings, source, outcomes


def fake_fit(rows, state, *, candidate_id, min_fit_trips):
    return dict(candidate_id=candidate_id, tenant_id=state['tenant_id'], fold_id=state['fold_id'], status='available')


def answer(status='available', reason=None, values=(1000, 2000, 3000)):
    return dict(status=status, reason=reason, raw_quantiles=list(values), quantile_crossing=False,
                p10_remaining_seconds=values[0] if status == 'available' else None,
                p50_remaining_seconds=values[1] if status == 'available' else None,
                p90_remaining_seconds=values[2] if status == 'available' else None)


class CandidateRunnerTests(unittest.TestCase):
    def execute(self, inputs, settings, source, outcomes, *, emitted=None):
        emitted = emitted or answer()
        calls = []
        def loader(keys, cutoff):
            calls.append((keys, cutoff))
            return select_fit_outcomes(outcomes, keys, cutoff)
        with patch('ds_m01.candidate_run.fit_candidate', side_effect=fake_fit) as fitted, \
                patch('ds_m01.candidate_run.predict_candidate', side_effect=lambda model, rows, state: [dict(copy.deepcopy(emitted), tenant_id=row['tenant_id'], snapshot_id=row['snapshot_id']) for row in sorted(rows, key=lambda row: row['snapshot_id'])]):
            result = train_and_predict(inputs, settings, source, loader)
        return result, calls, fitted.call_args_list

    def test_validation_opportunities_preserved_and_only_fit_targets_requested(self):
        inputs, settings, source, outcomes = fixture()
        result, calls, fitted = self.execute(inputs, settings, source, outcomes)
        self.assertEqual((len(result['models']), len(result['training_rows']), len(result['predictions'])), (4, 10, 8))
        self.assertEqual(result['summary']['validation_opportunities'], 4)
        self.assertTrue(all(row['prediction_source'] == 'candidate' for row in result['predictions']))
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(trip not in ('TRIP-5', 'TRIP-6') for _, trip in calls[0][0]))
        self.assertEqual(parse_ts(calls[0][1]), parse_ts(settings['folds'][0]['training_cutoff_at']))
        for call in fitted:
            self.assertEqual(len(call.args[0]), 5)
            self.assertTrue(all(row['sample_weight'] == 1.0 for row in call.args[0]))
        self.assertFalse(result['summary']['accuracy_evaluated'])
        self.assertFalse(result['summary']['validation_outcomes_used'])
        for row in result['predictions']:
            self.assertNotIn('remaining_duration_seconds', row)
            self.assertNotIn('outcome_version_id', row)

    def test_poisoning_validation_outcomes_cannot_change_models_or_predictions(self):
        inputs, settings, source, outcomes = fixture()
        original, _, _ = self.execute(inputs, settings, source, outcomes)
        poison = copy.deepcopy(outcomes)
        for row in poison:
            if row['trip_id'] in ('TRIP-5', 'TRIP-6'):
                row.update(actual_arrival_time=object(), label_available_time=object(), label_status='invented')
        changed, _, _ = self.execute(inputs, settings, source, poison)
        self.assertEqual(original, changed)

    def test_nonpositive_or_nonfinite_candidate_uses_same_fold_baseline(self):
        inputs, settings, source, outcomes = fixture()
        for reason in ('nonpositive_quantile', 'nonfinite_quantile', 'insufficient_fit_trips'):
            with self.subTest(reason=reason):
                result, _, _ = self.execute(inputs, settings, source, outcomes, emitted=answer('unsupported', reason))
                self.assertTrue(all(row['prediction_source'] == 'baseline_fallback' for row in result['predictions']))
                self.assertTrue(all(row['candidate_reason'] == reason for row in result['predictions']))
                self.assertTrue(all(row['p50_remaining_seconds'] == 3600 for row in result['predictions']))

    def test_finite_but_unrepresentable_eta_falls_back(self):
        inputs, settings, source, outcomes = fixture()
        result, _, _ = self.execute(inputs, settings, source, outcomes, emitted=answer(values=(1e300, 1e300, 1e300)))
        self.assertTrue(all(row['candidate_reason'] == 'eta_out_of_range' for row in result['predictions']))
        self.assertTrue(all(row['prediction_source'] == 'baseline_fallback' for row in result['predictions']))

    def test_no_usable_fallback_is_explicit_unavailable_without_dropping_rows(self):
        inputs, settings, source, outcomes = fixture()
        inputs['fallback_history'] = []
        result, _, _ = self.execute(inputs, settings, source, outcomes, emitted=answer('unsupported', 'nonpositive_quantile'))
        self.assertEqual(len(result['predictions']), 8)
        self.assertTrue(all(row['status'] == 'unavailable' and row['prediction_source'] == 'unavailable' for row in result['predictions']))
        self.assertTrue(all(row['eta_median'] is None and row['p50_remaining_seconds'] is None for row in result['predictions']))

    def test_absent_tenant_transformer_preserves_validation_rows_and_fallback(self):
        inputs, settings, source, outcomes = fixture()
        inputs['transformers'] = []
        for row in inputs['matrices']:
            row.update(status='unsupported', vector=None)
        result, _, _ = self.execute(inputs, settings, source, outcomes)
        self.assertEqual(len(result['predictions']), 8)
        self.assertTrue(all(row['prediction_source'] == 'baseline_fallback' for row in result['predictions']))

    def test_missing_matrix_and_duplicate_matrix_fail_before_fitting(self):
        for mutation in ('omit', 'duplicate'):
            with self.subTest(mutation=mutation):
                inputs, settings, source, _ = fixture()
                if mutation == 'omit':
                    inputs['matrices'].pop()
                else:
                    inputs['matrices'].append(inputs['matrices'][0])
                with self.assertRaises(ValueError):
                    validate_retained_inputs(inputs, validate_folds(settings['folds'], source))

    def test_fallback_wrong_fold_or_future_availability_is_rejected(self):
        for changes in ({'fold_id': 'FUTURE'}, {'label_known_at': '2026-04-01T00:00:00Z'}):
            with self.subTest(changes=changes):
                inputs, settings, source, _ = fixture()
                inputs['fallback_history'][0].update(changes)
                with self.assertRaises(ValueError):
                    validate_retained_inputs(inputs, validate_folds(settings['folds'], source))

    def test_changed_selected_outcome_invalidates_frozen_fallback_history(self):
        inputs, settings, source, outcomes = fixture()
        outcomes[0]['outcome_version_id'] = 'REPLACEMENT'
        with self.assertRaisesRegex(ValueError, 'Frozen fallback history differs'):
            self.execute(inputs, settings, source, outcomes)

    def test_cross_tenant_model_binding_preserved(self):
        inputs, settings, source, outcomes = fixture()
        result, _, fitted = self.execute(inputs, settings, source, outcomes)
        for call in fitted:
            self.assertTrue(all(row['tenant_id'] == call.args[1]['tenant_id'] for row in call.args[0]))
        digests = {tenant: {row['transformer_sha256'] for row in result['predictions'] if row['tenant_id'] == tenant}
                   for tenant in ('T01', 'T02')}
        self.assertEqual(len(digests['T01']), 1)
        self.assertFalse(digests['T01'] & digests['T02'])

    def test_prediction_join_is_keyed_when_matrix_order_differs_from_model_order(self):
        inputs, settings, source, outcomes = fixture()
        def predictor(model, rows, state):
            return [dict(answer(values=(100, 200 if row['trip_id'] == 'TRIP-5' else 300, 400)),
                         tenant_id=row['tenant_id'], snapshot_id=row['snapshot_id'])
                    for row in sorted(rows, key=lambda row: row['snapshot_id'])]
        loader = lambda keys, cutoff: select_fit_outcomes(outcomes, keys, cutoff)
        with patch('ds_m01.candidate_run.fit_candidate', side_effect=fake_fit), \
                patch('ds_m01.candidate_run.predict_candidate', side_effect=predictor):
            original = train_and_predict(inputs, settings, source, loader)
            inputs['matrices'].reverse()
            reordered = train_and_predict(inputs, settings, source, loader)
        self.assertEqual(original, reordered)
        for row in reordered['predictions']:
            self.assertEqual(row['p50_remaining_seconds'], 200 if row['trip_id'] == 'TRIP-5' else 300)

    def test_incomplete_prediction_keyset_is_rejected(self):
        inputs, settings, source, outcomes = fixture()
        with patch('ds_m01.candidate_run.fit_candidate', side_effect=fake_fit), \
                patch('ds_m01.candidate_run.predict_candidate', return_value=[]), \
                self.assertRaisesRegex(ValueError, 'cover every available input'):
            train_and_predict(inputs, settings, source, lambda keys, cutoff: select_fit_outcomes(outcomes, keys, cutoff))


if __name__ == '__main__':
    unittest.main()
