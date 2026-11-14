"""Recheck frozen P08 numbers and exact retained stores as separate invariants.

This verifier was added after final outcomes were exposed. It does not replace
the original scientific implementation, rewrite its freeze, or claim that the
original bitwise native-store replay passed. It uses the already-frozen numeric
tolerance for fresh calculations and the original exact store verifier for
tables reconstructed from hash-verified retained label-free artifacts.
"""
import argparse
import json
from pathlib import Path
import sys


VERIFIER_VERSION = 'p08-portable-replay-v1'
SCIENTIFIC_FREEZE_SHA256 = 'd6cb17c36cb59812388a385fe61a6d0db7448770a7f4c916a0b1f40cab86f202'
FORECAST_KEYS = frozenset(('eta_rows', 'probability_rows', 'opportunities', 'model_registry',
                           'matrices', 'features', 'projection_audit', 'stress_audit', 'stress_summary'))
SCORE_KEYS = frozenset(('forecast_metrics', 'policy_delivery_metrics'))
REGISTRATION_PATH = 'reproducibility/p08/portability_v1/registration.json'
VERIFIER_PATH = 'reproducibility/p08/portable_replay.py'
TEST_PATH = 'reproducibility/p08/tests/test_portable_replay.py'
ORIGINAL_DELIVERY_TREE = 'a82bee55df60c0e1235de25bc802a6e9a6e6f9c4'


def drift_summary():
    return {'tolerated_nonzero_float_occurrences': 0,
            'float_representation_difference_occurrences': 0,
            'maximum_absolute_difference': 0.0}


def checked_compare(expected, actual, path, tolerance, drift):
    """Keep the frozen comparator authoritative; then report accepted float drift."""
    from ds_m01.final_replay import compare
    count = compare(expected, actual, path, tolerance=tolerance)

    def record(left, right):
        if isinstance(left, dict):
            for key in left:
                record(left[key], right[key])
        elif isinstance(left, list):
            for a, b in zip(left, right):
                record(a, b)
        elif isinstance(left, float):
            difference = abs(left - right)
            if difference:
                drift['tolerated_nonzero_float_occurrences'] += 1
                drift['maximum_absolute_difference'] = max(drift['maximum_absolute_difference'], difference)
            if type(left) is not type(right) or repr(left) != repr(right):
                drift['float_representation_difference_occurrences'] += 1

    record(expected, actual)
    return count


def verify_registration(root, report, run_started_at):
    """Bind this later verifier and its tests without editing the scientific freeze."""
    from ds_m01.common import digest_file, parse_ts
    from ds_m01.delivery_inputs import require
    path = root / REGISTRATION_PATH
    record = json.loads(path.read_text())
    require(Path(__file__).resolve() == root / VERIFIER_PATH,
            'Portable verifier does not belong to the requested checkout')
    require(record.get('schema_version') == 1 and record.get('verifier_version') == VERIFIER_VERSION,
            'Unsupported portable verifier registration')
    require(record.get('post_exposure_verifier_revision') is True
            and record.get('untouched_final_test_claim') is False
            and record.get('original_strict_native_store_replay_claim') is False,
            'Portable verifier registration overclaims its scope')
    require(record.get('scientific_freeze_sha256') == SCIENTIFIC_FREEZE_SHA256
            and record.get('original_delivery_tree') == ORIGINAL_DELIVERY_TREE,
            'Portable verifier original evidence binding differs')
    require(record.get('original_run_receipt_sha256') == digest_file(report / 'run_receipt.json'),
            'Portable verifier original run receipt differs')
    require(record.get('verifier_sha256') == digest_file(root / VERIFIER_PATH),
            'Portable verifier source differs from registration')
    require(record.get('tests_sha256') == {TEST_PATH: digest_file(root / TEST_PATH)},
            'Portable verifier tests differ from registration')
    require(parse_ts(record['registered_at_utc']) > parse_ts(run_started_at),
            'Portable verifier registration must disclose its later creation')
    return digest_file(path)


def verify_scenario(root, directory, retained, forecasts, bundle, scores, settings, scenario_id,
                    materialized_at):
    """Accept numeric reproduction only before checking the exact retained store.

Raw inferred floats must not substitute for the original stored float values:
that would change nested JSON and its provenance hashes even within tolerance.
Canonical identities, decisions, types and integer/decimal projections remain
subject to the original comparator, and retained store bytes remain exact.
    """
    from ds_m01.delivery_inputs import require
    from ds_m01.final_store import build_tables, verify_store

    require(set(forecasts) == FORECAST_KEYS, 'Portable replay forecast inventory differs')
    tolerance = settings['comparison_tolerance']
    comparisons = 0
    drift = drift_summary()
    for name, rows in forecasts.items():
        comparisons += checked_compare(retained[name], rows, scenario_id + '/' + name, tolerance, drift)
    comparisons += checked_compare(retained['policy_delivery'], bundle,
                                   scenario_id + '/policy_delivery', tolerance, drift)
    # Production scoring remains after the fresh forecast/policy checks, as in
    # the original replay. Synthetic tests may supply already-computed scores.
    if callable(scores):
        scores = scores()
    require(set(scores) == SCORE_KEYS, 'Portable replay score inventory differs')
    for name, metrics in scores.items():
        comparisons += checked_compare(retained[name], metrics, scenario_id + '/' + name, tolerance, drift)

    # All three retained inputs are covered by the original run/pretarget hashes
    # in verify(), and each has just been compared to its fresh reconstruction.
    tables = build_tables(retained['opportunities'], retained['model_registry'],
                          retained['policy_delivery'], materialized_at, settings)
    store = verify_store(root, Path(directory) / 'store', tables, settings)
    return comparisons, store, drift


def verify(root, report, *, input_root=None):
    root, report = Path(root).resolve(), Path(report).resolve()
    # Direct script execution must use this checkout's frozen scientific code.
    sys.path.insert(0, str(root / 'src'))
    from ds_m01.common import digest_file, read_jsonl
    from ds_m01.delivery_inputs import require
    from ds_m01.final_gate import verify_pins, verify_registered, verify_correction_history, OUTCOME_PATH
    from ds_m01.final_inputs import prepare_inputs
    from ds_m01.final_inference import load_states
    from ds_m01.final_targets import build_final_targets
    from ds_m01.final_run import build_scenario, read_payload, score_scenario

    settings = verify_pins(root)
    receipt = json.loads((report / 'run_receipt.json').read_text())
    require(receipt['status'] == 'engineering_complete_final_evaluation', 'Final run is not complete')
    registration_sha256 = verify_registration(root, report, receipt['started_at_utc'])
    for path, expected in receipt['output_sha256'].items():
        require(digest_file(report / path) == expected, 'Retained final output changed: ' + path)
    freeze_path = report / 'implementation_freeze.json'
    freeze = json.loads(freeze_path.read_text())
    require(digest_file(freeze_path) == SCIENTIFIC_FREEZE_SHA256,
            'Portable verifier targets a different original scientific freeze')
    verify_registered(root, freeze, run_started_at=receipt['started_at_utc'])
    if freeze['status'] == 'registered_correction_after_final_target_exposure':
        verify_correction_history(freeze, report / 'correction_history')
        verify_correction_history(freeze, report / 'prior_attempt')
    require(digest_file(freeze_path) == receipt['implementation_freeze_sha256'],
            'Implementation freeze differs from run receipt')
    pretarget = json.loads((report / 'pretarget_receipt.json').read_text())
    require(digest_file(report / 'pretarget_receipt.json') == receipt['pretarget_receipt_sha256'],
            'Pre-target receipt differs from run receipt')
    for path, expected in pretarget['output_sha256'].items():
        require(digest_file(report / path) == expected, 'Label-free retained artifact changed: ' + path)
    stored_inputs = read_payload(report / 'inputs')
    inputs = prepare_inputs(root, input_root, settings) if input_root is not None else stored_inputs
    non_scenario_drift = drift_summary()
    comparisons = checked_compare(stored_inputs, inputs, 'inputs', settings['comparison_tolerance'],
                                  non_scenario_drift)
    require(inputs['inventory'] == freeze['metadata_inventory'] == pretarget['metadata_inventory'],
            'Final inventory differs from its pre-target freeze')
    outcomes = read_jsonl(report / 'outcome_source.jsonl')
    source_bound = False
    if input_root is not None:
        source = Path(input_root) / OUTCOME_PATH
        require(digest_file(source) == settings['input_sha256'][OUTCOME_PATH], 'Final source outcome hash differs')
        trips = {(r['tenant_id'], r['trip_id']) for r in inputs['memberships']}
        selected = [r for r in read_jsonl(source) if (r['tenant_id'], r['trip_id']) in trips]
        comparisons += checked_compare(outcomes, selected, 'outcome_source', settings['comparison_tolerance'],
                                       non_scenario_drift)
        source_bound = True
    targets = build_final_targets(inputs['snapshots'], outcomes, inputs['promises'], settings)
    for name, rows in targets.items():
        comparisons += checked_compare(read_jsonl(report / (name + '.jsonl')), rows, name,
                                       settings['comparison_tolerance'], non_scenario_drift)
    states = load_states(root, settings)
    source_config = json.loads((root / 'config/m2.json').read_text())
    stores = {}
    scenario_drift = {}
    for case in settings['stress']['scenarios']:
        sid = case['id']
        directory = report / 'scenarios' / sid
        retained = read_payload(directory, forecasts=True)
        forecasts, bundle = build_scenario(inputs, states, source_config, settings, sid)
        count, stores[sid], scenario_drift[sid] = verify_scenario(
            root, directory, retained, forecasts, bundle,
            lambda: score_scenario(forecasts, targets, bundle, settings, sid),
            settings, sid, receipt['materialized_at_utc'])
        comparisons += count
    verify_registered(root, freeze)
    require(verify_registration(root, report, receipt['started_at_utc']) == registration_sha256,
            'Portable verifier registration changed during replay')
    all_drift = [non_scenario_drift, *scenario_drift.values()]
    numerical_drift = {
        'tolerated_nonzero_float_occurrences': sum(d['tolerated_nonzero_float_occurrences'] for d in all_drift),
        'float_representation_difference_occurrences': sum(d['float_representation_difference_occurrences'] for d in all_drift),
        'maximum_absolute_difference': max(d['maximum_absolute_difference'] for d in all_drift),
        'counting_unit': 'Compared scalar occurrences, not unique predictions or independent observations',
        'input_and_target_checks': non_scenario_drift, 'scenarios': scenario_drift,
    }
    return {
        'status': 'passed', 'differences': 0, 'violations': 0, 'logical_comparisons': comparisons,
        'scenario_count': len(settings['stress']['scenarios']), 'source_outcomes_bound': source_bound,
        'mode': 'source_assisted_portable_final_replay' if source_bound else 'retained_portable_final_replay',
        'verifier_version': VERIFIER_VERSION, 'verifier_sha256': digest_file(Path(__file__).resolve()),
        'verifier_registration_sha256': registration_sha256,
        'scientific_freeze_sha256': SCIENTIFIC_FREEZE_SHA256,
        'comparison_tolerance': settings['comparison_tolerance'], 'numeric_tolerance_changed': False,
        'numerical_drift': numerical_drift,
        'stored_artifact_hashes_exact': True, 'retained_store_integrity_exact': True,
        'store_reconstruction_input': 'hash_verified_retained_label_free_artifacts',
        'stores': stores, 'model_or_calibrator_refitted': False, 'policy_reselected': False,
        'post_exposure_verifier_revision': True, 'untouched_final_test_claim': False,
        'original_strict_native_store_replay_claim': False,
        'quality_accepted': False, 'operational_approval': False,
        'scope': 'Numerical reproduction under the original frozen tolerance plus exact retained '
                 'store reconstruction and JSONL/DuckDB/Parquet/schema/constraint checks. '
                 'Zero differences means no violations of these two declared checks; it does not '
                 'mean all freshly inferred raw float bits equal the stored raw float bits. '
                 'Added after outcome exposure; not a new untouched final test or an original strict replay pass.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--input-root', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.output is not None:
        destination = args.output.expanduser().resolve()
        if args.output.exists() or args.output.is_symlink() or destination.is_relative_to(args.root.resolve()):
            raise ValueError('Choose a fresh output outside the frozen checkout')
    result = verify(args.root, args.report, input_root=args.input_root)
    if args.output is not None:
        from ds_m01.final_gate import write_json
        write_json(args.output, result, exclusive=True)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
