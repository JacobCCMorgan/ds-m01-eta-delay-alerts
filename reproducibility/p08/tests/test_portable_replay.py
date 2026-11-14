"""Portable verification regressions using synthetic rows and real typed stores.

These tests are later verifier evidence, not additions to the original pre-target
727-test conformance run. They never open the retained final outcome payload.
"""
from copy import deepcopy
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))
SPEC = importlib.util.spec_from_file_location('p08_portable_replay_under_test',
                                            ROOT / 'reproducibility/p08/portable_replay.py')
portable = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(portable)

from ds_m01.common import digest_file
from ds_m01 import final_store as store
from ds_m01.final_policy import run_policy_delivery
from test_final_policy import SETTINGS, opportunity, registry


GENERATED_AT = '2026-11-14T12:00:00Z'


class PortableStoreRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.rows = [opportunity(), opportunity('S2', 1800), opportunity('S3', 3600)]
        self.models = registry()
        self.bundle = run_policy_delivery(self.rows, SETTINGS)
        self.forecasts = {
            'eta_rows': [{'method_id': 'fixture', 'remaining_seconds': 4000.5}],
            'probability_rows': [{'method_id': 'fixture', 'probability': .7}],
            'opportunities': self.rows, 'model_registry': self.models,
            'matrices': [], 'features': [], 'projection_audit': [], 'stress_audit': [],
            'stress_summary': {'snapshots': 3},
        }
        self.scores = {'forecast_metrics': {'score': .125, 'eligible_count': 3},
                       'policy_delivery_metrics': {'captured_count': 1, 'rate': .25}}
        self.retained = deepcopy({**self.forecasts, 'policy_delivery': self.bundle, **self.scores})
        tables = store.build_tables(self.rows, self.models, self.bundle, GENERATED_AT, SETTINGS)
        store.write_store(ROOT, self.directory / 'store', tables, SETTINGS)

    def tearDown(self):
        self.temporary.cleanup()

    def verify(self, *, forecasts=None, bundle=None, scores=None, retained=None, settings=None):
        return portable.verify_scenario(ROOT, self.directory,
            self.retained if retained is None else retained,
            self.forecasts if forecasts is None else forecasts,
            self.bundle if bundle is None else bundle,
            self.scores if scores is None else scores,
            SETTINGS if settings is None else settings, 'S00_unmodified', GENERATED_AT)

    def assert_rejected_before_store(self, *, forecasts=None, bundle=None, scores=None, settings=None):
        with patch.object(store, 'verify_store') as exact_check:
            with self.assertRaises(ValueError):
                self.verify(forecasts=forecasts, bundle=bundle, scores=scores, settings=settings)
            exact_check.assert_not_called()

    def test_exact_replay_keeps_all_27_typed_tables_and_store_bytes(self):
        before = {str(p.relative_to(self.directory)): digest_file(p)
                  for p in self.directory.rglob('*') if p.is_file()}
        count, result, drift = self.verify()
        self.assertGreater(count, 0)
        self.assertEqual(result['differences'], 0)
        self.assertEqual(len(result['counts']), 27)
        self.assertTrue(result['read_only'])
        self.assertTrue(result['foreign_keys_verified'])
        self.assertEqual(drift['tolerated_nonzero_float_occurrences'], 0)
        self.assertEqual(before, {str(p.relative_to(self.directory)): digest_file(p)
                                 for p in self.directory.rglob('*') if p.is_file()})

    def test_one_ulp_inference_drift_passes_while_original_raw_store_comparison_fails(self):
        fresh = deepcopy(self.forecasts)
        fresh['opportunities'][0]['calibrated_probability'] = math.nextafter(.7, 1.0)
        fresh['opportunities'][0]['raw_probability'] = math.nextafter(.65, 1.0)
        fresh_bundle = run_policy_delivery(fresh['opportunities'], SETTINGS)
        native_tables = store.build_tables(fresh['opportunities'], fresh['model_registry'],
                                          fresh_bundle, GENERATED_AT, SETTINGS)
        with self.assertRaisesRegex(ValueError, 'Final JSONL differs from reconstructed rows'):
            store.verify_store(ROOT, self.directory / 'store', native_tables, SETTINGS)
        count, result, drift = self.verify(forecasts=fresh, bundle=fresh_bundle)
        self.assertGreater(count, 0)
        self.assertEqual(result['differences'], 0)
        self.assertEqual(drift['tolerated_nonzero_float_occurrences'], 2)
        self.assertGreater(drift['maximum_absolute_difference'], 0)
        self.assertLess(drift['maximum_absolute_difference'], 1e-10)

    def test_float_drift_outside_original_tolerance_fails(self):
        fresh = deepcopy(self.forecasts)
        fresh['opportunities'][0]['raw_probability'] += 1e-6
        self.assert_rejected_before_store(forecasts=fresh)

    def test_scoring_waits_for_forecast_and_policy_checks(self):
        fresh = deepcopy(self.forecasts)
        fresh['opportunities'][0]['raw_probability'] += 1e-6
        calls = []
        def scores():
            calls.append('scored')
            return self.scores
        with self.assertRaises(ValueError):
            self.verify(forecasts=fresh, scores=scores)
        self.assertEqual(calls, [])
        self.verify(scores=scores)
        self.assertEqual(calls, ['scored'])

    def test_identity_count_boolean_and_integer_changes_remain_exact(self):
        edits = [('snapshot_id', 'OTHER'), ('unseen_route', True),
                 ('median_seconds_int', 4001), ('median_seconds_int', 4000.0),
                 ('probability', '0.700001')]
        for field, value in edits:
            with self.subTest(field=field, value=value):
                fresh = deepcopy(self.forecasts)
                fresh['opportunities'][0][field] = value
                self.assert_rejected_before_store(forecasts=fresh)
        fresh = deepcopy(self.forecasts)
        fresh['opportunities'].pop()
        self.assert_rejected_before_store(forecasts=fresh)

    def test_fresh_decisions_and_model_registry_cannot_be_substituted(self):
        bundle = deepcopy(self.bundle)
        bundle['threshold_reselected'] = True
        self.assert_rejected_before_store(bundle=bundle)
        bundle = deepcopy(self.bundle)
        bundle['delivery']['alerts'].pop()
        self.assert_rejected_before_store(bundle=bundle)
        fresh = deepcopy(self.forecasts)
        fresh['model_registry'][0]['prediction_version'] = 'OTHER'
        self.assert_rejected_before_store(forecasts=fresh)

    def test_metrics_and_denominators_are_still_compared(self):
        for metric, value in [('score', .2), ('eligible_count', 4), ('eligible_count', 3.0)]:
            with self.subTest(metric=metric, value=value):
                scores = deepcopy(self.scores)
                scores['forecast_metrics'][metric] = value
                self.assert_rejected_before_store(scores=scores)
        scores = deepcopy(self.scores)
        scores['forecast_metrics']['score'] = math.nextafter(.125, 1.0)
        _, _, drift = self.verify(scores=scores)
        self.assertEqual(drift['tolerated_nonzero_float_occurrences'], 1)

    def test_missing_forecast_or_score_collection_is_not_silently_omitted(self):
        fresh = deepcopy(self.forecasts); fresh.pop('eta_rows')
        self.assert_rejected_before_store(forecasts=fresh)
        scores = deepcopy(self.scores); scores.pop('forecast_metrics')
        self.assert_rejected_before_store(scores=scores)

    def test_invalid_nonfinite_numeric_or_boolean_prediction_fails(self):
        for value in (float('nan'), float('inf'), True):
            with self.subTest(value=value):
                fresh = deepcopy(self.forecasts)
                fresh['opportunities'][0]['raw_probability'] = value
                self.assert_rejected_before_store(forecasts=fresh)

    def test_changed_tolerance_cannot_be_used_as_a_tuning_parameter(self):
        settings = deepcopy(SETTINGS)
        settings['comparison_tolerance']['absolute'] = .1
        fresh = deepcopy(self.forecasts)
        fresh['opportunities'][0]['raw_probability'] += .01
        self.assert_rejected_before_store(forecasts=fresh, settings=settings)

    def test_jsonl_duckdb_and_parquet_hash_tampering_still_fails(self):
        for relative in ('jsonl/p08_opportunity_audit.jsonl', 'experiment.duckdb',
                         'parquet/p08_opportunity_audit.parquet'):
            with self.subTest(relative=relative):
                path = self.directory / 'store' / relative
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b'\n')
                    with self.assertRaisesRegex(ValueError, 'Final store file changed'):
                        self.verify()
                finally:
                    path.write_bytes(original)

    def test_retained_metadata_edits_do_not_get_the_float_tolerance(self):
        retained = deepcopy(self.retained)
        retained['opportunities'][0]['raw_probability'] = math.nextafter(.65, 1.0)
        fresh = deepcopy(self.forecasts)
        fresh['opportunities'] = deepcopy(retained['opportunities'])
        with self.assertRaisesRegex(ValueError, 'Final JSONL differs from reconstructed rows'):
            self.verify(forecasts=fresh, retained=retained)

    def test_forged_store_acceptance_claim_remains_rejected(self):
        path = self.directory / 'store/verification.json'
        receipt = json.loads(path.read_text())
        receipt['quality_accepted'] = True
        path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, 'receipt claims differ'):
            self.verify()


class VerifierRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.report = self.root / 'reports/p08'; self.report.mkdir(parents=True)
        (self.report / 'run_receipt.json').write_text('{"original":"run"}\n')
        for relative, content in ((portable.VERIFIER_PATH, 'synthetic verifier\n'),
                                  (portable.TEST_PATH, 'synthetic tests\n')):
            path = self.root / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(content)
        self.path = self.root / portable.REGISTRATION_PATH
        self.path.parent.mkdir(parents=True)
        self.record = {
            'schema_version': 1, 'verifier_version': portable.VERIFIER_VERSION,
            'registered_at_utc': '2026-11-14T12:00:00Z',
            'post_exposure_verifier_revision': True, 'untouched_final_test_claim': False,
            'original_strict_native_store_replay_claim': False,
            'scientific_freeze_sha256': portable.SCIENTIFIC_FREEZE_SHA256,
            'original_delivery_tree': portable.ORIGINAL_DELIVERY_TREE,
            'original_run_receipt_sha256': digest_file(self.report / 'run_receipt.json'),
            'verifier_sha256': digest_file(self.root / portable.VERIFIER_PATH),
            'tests_sha256': {portable.TEST_PATH: digest_file(self.root / portable.TEST_PATH)},
        }
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.record) + '\n')

    def tearDown(self):
        self.temporary.cleanup()

    def verify(self):
        with patch.object(portable, '__file__', str(self.root / portable.VERIFIER_PATH)):
            return portable.verify_registration(self.root, self.report, '2026-11-14T06:06:33Z')

    def test_later_verifier_registration_has_its_own_evidence_binding(self):
        self.assertEqual(self.verify(), digest_file(self.path))

    def test_source_test_or_original_receipt_change_is_rejected(self):
        for path in (self.root / portable.VERIFIER_PATH, self.root / portable.TEST_PATH,
                     self.report / 'run_receipt.json'):
            with self.subTest(path=path):
                before = path.read_bytes()
                try:
                    path.write_bytes(before + b'changed\n')
                    with self.assertRaises(ValueError): self.verify()
                finally: path.write_bytes(before)

    def test_backdated_or_overclaiming_registration_is_rejected(self):
        edits = [('registered_at_utc', '2026-11-14T06:00:00Z'),
                 ('untouched_final_test_claim', True),
                 ('original_strict_native_store_replay_claim', True),
                 ('post_exposure_verifier_revision', False),
                 ('scientific_freeze_sha256', 'f' * 64),
                 ('original_delivery_tree', 'f' * 40)]
        for field, value in edits:
            with self.subTest(field=field):
                before = self.record[field]
                self.record[field] = value; self.save()
                with self.assertRaises(ValueError): self.verify()
                self.record[field] = before; self.save()


class DriftDisclosureTests(unittest.TestCase):
    def test_signed_zero_and_nonzero_float_occurrences_are_reported_separately(self):
        drift = portable.drift_summary()
        count = portable.checked_compare([-0.0, .7], [0.0, math.nextafter(.7, 1.0)],
                                         'fixture', SETTINGS['comparison_tolerance'], drift)
        self.assertEqual(count, 2)
        self.assertEqual(drift['tolerated_nonzero_float_occurrences'], 1)
        self.assertEqual(drift['float_representation_difference_occurrences'], 2)


if __name__ == '__main__':
    unittest.main()
