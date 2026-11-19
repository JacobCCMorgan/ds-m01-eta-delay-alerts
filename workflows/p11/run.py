"""Execute P11 checks without changing the registered scientific parent."""
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import subprocess
import time

from workflows.p11.registry import digest_file, require, verify_registration

STATUS = 'passed_p11_clean_rebuild_revision_and_recovery'


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n')


def check_revision(value):
    require(value.get('status') == 'passed_revision_contract'
            and value.get('canonical_events') == 2 and value.get('retained_revisions') == 4
            and value.get('source_duplicate_rows') == 1
            and value.get('http_server_closed') is True
            and len(value.get('snapshot_witnesses', [])) == 10,
            'Revision witness did not finish the registered population')
    store = value.get('store', {})
    require(store.get('status') == 'passed_typed_revision_store'
            and len(store.get('actual_constraint_rejections', [])) == 4
            and all(store.get(k) is True for k in ('fresh_connection_exact',
                'all_column_parquet_exact', 'snapshot_revision_foreign_key_enforced')),
            'Revision store did not finish actual typed checks')
    require(store.get('retained_revision_replay', {}).get('all_typed_payloads_unchanged') is True,
            'Retained revision replay changed source payloads')


def check_recovery(value):
    require(value.get('status') == 'passed_abrupt_failure_and_recovery'
            and value.get('faults_executed') == 5
            and value.get('database_and_parquet_executed') is True
            and value.get('subprocess_abrupt_exit') is True
            and len(value.get('cases', [])) == 5,
            'The abrupt failure/recovery matrix is incomplete')
    expected = {'after_database', 'after_first_parquet', 'after_validation',
                'after_generation_rename', 'after_pointer_replace'}
    require({case.get('fault') for case in value['cases']} == expected,
            'The recovery fault boundaries differ')
    for case in value['cases']:
        require(case.get('process_exit') == 86
                and case.get('old_database_and_all_parquet_preserved') is True
                and case.get('recovery_mutated_artifacts') is False
                and case.get('clean_and_recovered_logical_tables_equal') is True,
                'A recovery case failed its preservation or repeat check')


def check_rebuild(value):
    require(value.get('status') == 'passed_two_full_frozen_m2_rebuilds'
            and len(value.get('runs', [])) == 2, 'Two complete rebuilds were not executed')
    for run in value['runs']:
        require(run.get('status') == 'passed_full_frozen_m2_rebuild'
                and run.get('materialized_schemas') == 28 and run.get('populated_data_tables') == 17
                and len(run.get('typed_empty_tables', [])) == 11 and len(run.get('tables', {})) == 28,
                'A full rebuild has an incomplete warehouse population')
        oracle = run.get('historical_oracle', {})
        require(oracle.get('status') == 'passed' and oracle.get('raw_files_compared') == 13
                and oracle.get('processed_files_compared') == 7,
                'A rebuild did not match the complete historical source/projection oracle')
        checks = run.get('warehouse_checks', [])
        require(len(checks) == 27 and all(item.get('passed') is True for item in checks),
                'A rebuild did not pass all warehouse invariants')
        archive = run.get('source_archive', {})
        require(archive.get('status') == 'passed_lossless_owned_source_archive'
                and len(archive.get('files', {})) == 21
                and archive.get('all_archives_verified_before_any_plain_stream_removed') is True
                and all(item.get('full_decode_byte_identity_verified') is True
                        for item in archive['files'].values()),
                'Generated source bytes were not completely retained and verified')
    repeat = value.get('repeatability', {})
    require(repeat.get('status') == 'passed' and repeat.get('canonical_relations_compared') == 28
            and repeat.get('schema_count_and_all_row_digests_equal') is True,
            'The clean rebuilds did not agree exactly')


def verify(root, output):
    root, requested = Path(root).resolve(), Path(output)
    require(not requested.is_symlink(), 'P11 output must not be a symlink')
    output = requested.resolve()
    require(not output.exists() and not output.is_relative_to(root),
            'Choose a new P11 output directory outside the checkout')
    require(Path(__file__).resolve() == root / 'workflows/p11/run.py',
            'P11 runner belongs to another checkout')
    before = verify_registration(root)
    output.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    start = time.monotonic()
    phases = {}
    try:
        from workflows.p11.recovery import verify_recovery
        from workflows.p11.revision import verify_revision_contract
        from workflows.p11.rebuild import run_rebuild
        # Fail inexpensive conformance witnesses before allocating full rebuilds.
        phases['recovery'] = verify_recovery(output / 'recovery')
        check_recovery(phases['recovery'])
        write_json(output / 'recovery.json', phases['recovery'])
        phases['revision'] = verify_revision_contract(root, output / 'revision')
        check_revision(phases['revision'])
        write_json(output / 'revision.json', phases['revision'])
        phases['rebuild'] = run_rebuild(root, output / 'rebuild')
        check_rebuild(phases['rebuild'])
        write_json(output / 'rebuild.json', phases['rebuild'])
        after = verify_registration(root)
        require(before == after, 'Registered parent or P11 source changed during execution')
        write_json(output / 'parent_integrity.json', after)
        summary = {
            'status': STATUS,
            'two_clean_rebuilds_executed': True,
            'revision_http_and_store_executed': True,
            'abrupt_recovery_executed': True,
            'parent_integrity_verified': True,
            'ci_verified': False,
            'quality_accepted': False,
            'operational_approval': False,
            'historical_entrypoints_changed': False,
            'unified_populated_28_table_postgres_claimed': False,
            'distributed_postgres_parquet_atomicity_claimed': False,
            'new_untouched_final_test_claimed': False,
            'registration_sha256': before['registration_sha256'],
            'parent_file_count': before['parent_file_count'],
            'checks': phases,
            'scope': 'Two complete frozen M2 rebuilds; P11 revision-aware HTTP/snapshot/store witness; '
                     'single-filesystem DuckDB/Parquet generation publication and process-crash recovery. '
                     'Existing scientific results and historical entry points remain unchanged. '
                     'Actual GitHub CI requires a separate exact-commit run after publication.',
        }
        write_json(output / 'summary.json', summary)
        bound = {name: digest_file(output / name) for name in
                 ('summary.json', 'parent_integrity.json', 'rebuild.json', 'revision.json', 'recovery.json')}
        receipt = {
            'status': STATUS,
            'started_at_utc': started,
            'completed_at_utc': datetime.now(timezone.utc).isoformat(),
            'elapsed_seconds': time.monotonic() - start,
            'head': subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip(),
            'execution_checkout_changes_present': bool(subprocess.check_output(
                ['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=all'], text=True)),
            'registration_sha256': before['registration_sha256'],
            'output_sha256': bound,
            'runtime': {'python': platform.python_version(), 'platform': platform.platform()},
            'ci_verified': False, 'quality_accepted': False, 'operational_approval': False,
        }
        write_json(output / 'receipt.json', receipt)
        return summary
    except BaseException as exc:
        write_json(output / 'failure.json', {
            'status': 'failed_no_success_receipt', 'started_at_utc': started,
            'failed_at_utc': datetime.now(timezone.utc).isoformat(),
            'error': str(exc), 'completed_checks': list(phases),
        })
        raise
