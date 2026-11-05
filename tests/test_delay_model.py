"""Independent classifier probability, weighting, membership and JSON checks."""

from copy import deepcopy
import json
import unittest

from ds_m01.delay_model import (PARAMETERS, fit_deadline_transformer, fit_delay_model,
                               predict_delay_model, extend_vector, sigmoid)
from test_candidate_model import fixture as quantile_fixture


def fixture():
    state, fit, validation = quantile_fixture()
    for index, row in enumerate(fit):
        row.update(status='available', promise_seconds_remaining=300.0 + index * 40, delay_label=(index // 8) % 2 == 0,
                   eligibility='eligible', label_status='verified', actual_arrival_time='2026-02-01T10:00:00Z',
                   label_known_at='2026-02-02T00:00:00Z')
    for index, row in enumerate(validation):
        row['status'] = 'available'
        row['promise_seconds_remaining'] = -100 + index * 450
    horizon = fit_deadline_transformer(fit, state)
    return state, horizon, fit, validation


class DelayModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state, cls.horizon, cls.fit, cls.validation = fixture()
        cls.models = {m: fit_delay_model(cls.fit, cls.state, cls.horizon, m) for m in ('fold_prevalence', 'logistic_delay')}

    def test_portable_json_matches_independently_fitted_sklearn(self):
        from sklearn.linear_model import LogisticRegression
        estimator = LogisticRegression(**PARAMETERS)
        estimator.fit([extend_vector(r, self.state, self.horizon) for r in self.fit],
                      [r['delay_label'] for r in self.fit], sample_weight=[r['sample_weight'] for r in self.fit])
        expected = estimator.predict_proba([extend_vector(r, self.state, self.horizon) for r in self.validation])[:,1]
        restored = json.loads(json.dumps(self.models['logistic_delay']))
        answers = predict_delay_model(restored, self.validation, self.state)
        for predicted, reference in zip(answers, expected):
            self.assertAlmostEqual(predicted['probability'], reference, delta=1e-12)
        self.assertEqual(predict_delay_model(self.models['logistic_delay'], self.validation, self.state), answers)

    def test_prior_uses_equal_total_trip_weight(self):
        prior = self.models['fold_prevalence']
        self.assertEqual(prior['weighted_training_prevalence'], .5)
        self.assertEqual(prior['total_training_weight'], 8)
        self.assertEqual(prior['class_support']['true'], {'snapshots':32, 'trips':4})
        self.assertEqual({r['probability'] for r in predict_delay_model(prior, self.validation, self.state)}, {.5})

    def test_scaler_uses_every_fit_opportunity_before_labels(self):
        with self.assertRaisesRegex(ValueError, 'every frozen fit snapshot'):
            fit_deadline_transformer(self.fit[:8], self.state)
        poisoned = [dict(row, delay_label=object(), eligibility='excluded', label_known_at=object()) for row in self.fit]
        self.assertEqual(fit_deadline_transformer(poisoned, self.state), self.horizon)
        changed = deepcopy(self.validation)
        for row in changed:
            row['promise_seconds_remaining'] = 1e12
        self.assertEqual(fit_deadline_transformer(self.fit, self.state), self.horizon)

    def test_unequal_within_trip_weights_and_unavailable_labels_fail(self):
        rows = deepcopy(self.fit)
        rows[0]['sample_weight'], rows[1]['sample_weight'] = .2, .05
        cases = [(rows, 'shared equally'), ([dict(self.fit[0], eligibility='excluded'), *self.fit[1:]], 'eligible'),
                 ([dict(self.fit[0], label_known_at='2099-01-01T00:00:00Z'), *self.fit[1:]], 'cutoff')]
        for rows, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                fit_delay_model(rows, self.state, self.horizon, 'logistic_delay')

    def test_single_class_and_low_support_have_explicit_states(self):
        single = [dict(r, delay_label=False) for r in self.fit]
        prior = fit_delay_model(single, self.state, self.horizon, 'fold_prevalence')
        self.assertEqual((prior['status'], prior['weighted_training_prevalence']), ('available', 0.0))
        classifier = fit_delay_model(single, self.state, self.horizon, 'logistic_delay')
        self.assertEqual(classifier['reason'], 'insufficient_class_trip_support')
        self.assertTrue(all(r['probability'] is None for r in predict_delay_model(classifier, self.validation, self.state)))
        for method in ('fold_prevalence','logistic_delay'):
            self.assertEqual(fit_delay_model(self.fit[:32], self.state, self.horizon, method)['reason'], 'insufficient_fit_trips')

    def test_future_or_fit_overlap_and_malformed_vectors_fail(self):
        for row in (dict(self.validation[0], snapshot_time='2025-01-01T00:00:00Z'),
                    dict(self.validation[0], trip_id=self.fit[0]['trip_id']),
                    dict(self.validation[0], tenant_id='OTHER'),
                    dict(self.validation[0], vector=[float('nan')] * len(self.validation[0]['vector']))):
            with self.subTest(row=row), self.assertRaises(ValueError):
                predict_delay_model(self.models['logistic_delay'], [row], self.state)

    def test_model_metadata_and_horizon_tampering_fail(self):
        changes = [('feature_names', ['wrong']), ('base_transformer_sha256', '0'*64),
                   ('status', 'unsupported'), ('coefficients', [1.0]), ('training_cutoff_at', '2099-01-01T00:00:00Z')]
        for key, value in changes:
            bad = dict(self.models['logistic_delay'], **{key:value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                predict_delay_model(bad, self.validation, self.state)
        bad = deepcopy(self.models['logistic_delay'])
        bad['deadline_transformer']['fit_snapshot_keys'].pop()
        bad['deadline_transformer']['fit_rows'] -= 1
        with self.assertRaisesRegex(ValueError, 'every frozen'):
            predict_delay_model(bad, self.validation, self.state)

    def test_validation_labels_and_eta_quantiles_cannot_change_probabilities(self):
        altered = [dict(r, delay_label=object(), actual_arrival_time=object(), p10_remaining_seconds=-1,
                        p90_remaining_seconds=1e10) for r in self.validation]
        self.assertEqual(predict_delay_model(self.models['logistic_delay'], self.validation, self.state),
                         predict_delay_model(self.models['logistic_delay'], altered, self.state))
        self.assertEqual(self.models['logistic_delay'], fit_delay_model(list(reversed(self.fit)), self.state, self.horizon, 'logistic_delay'))

    def test_sigmoid_is_stable_at_extreme_finite_scores(self):
        self.assertEqual(sigmoid(1000), 1.0)
        self.assertEqual(sigmoid(-1000), 0.0)
        self.assertEqual(sigmoid(0), .5)
        with self.assertRaises(ValueError):
            sigmoid(float('inf'))


if __name__ == '__main__':
    unittest.main()
