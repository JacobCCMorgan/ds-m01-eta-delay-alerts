"""Exercise native adapters against unchanged synthetic source and parent data."""
from datetime import datetime, timezone
import json
import platform
from pathlib import Path
import subprocess

from ds_m01.generate import generate
from ds_m01.common import write_source_manifest, verify_source_manifest
from .registry import digest_file, verify_registration
from .cluster import ownedcluster
from .postgres import check as check_postgres
from .http_check import check_http_snapshot


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def require_complete(postgres, http, lifecycle):
    """A successful process is insufficient without every native proof."""
    require(postgres.get('status') == 'passed', 'Native PostgreSQL checks did not pass')
    for key in ('commit_completed', 'fresh_connection_exact_readback',
                'uncommitted_changes_hidden_from_second_connection',
                'owned_schema_cleanup_verified', 'same_key_changed_payload_rejected'):
        require(postgres.get(key) is True, 'Missing native PostgreSQL proof: ' + key)
    groups = postgres.get('groups', {})
    for group, count in (('operational', 11), ('p09', 35)):
        require(groups.get(group, {}).get('table_count') == count, 'Native table scope incomplete: ' + group)
        require(len(groups[group].get('tables', {})) == count, 'Native row comparison incomplete: ' + group)
        for table in groups[group]['tables'].values():
            require(table.get('exact') is True
                    and table.get('input_rows') == table.get('returned_rows')
                    and table.get('input_normalized_sha256') == table.get('returned_normalized_sha256'),
                    'Native typed rows differ: ' + group)
        for key in ('failed_writes_rolled_back', 'exact_retry_preserved'):
            require(groups[group].get(key) is True, 'Missing native transaction proof: ' + key)
    require(len(postgres.get('operational_export', {})) == 11, 'Native readback export incomplete')
    require(http.get('status') == 'passed' and http.get('source_unchanged') is True,
            'HTTP source reconciliation did not pass')
    require(http.get('postgres_http_projection_checked') is True, 'Combined PostgreSQL/HTTP projection missing')
    require(set(http.get('comparisons', {})) == {'snapshot_vs_http', 'snapshot_vs_postgres_http'},
            'Three source paths were not reconciled')
    transport = http.get('transport', {})
    for key in ('full_payload_set_equal', 'event_and_available_clocks_equal',
                'tenant_event_revision_identity_preserved', 'server_closed'):
        require(transport.get(key) is True, 'Missing HTTP proof: ' + key)
    require(transport.get('transport') == 'actual_loopback_http' and transport.get('retries') == 1,
            'Actual HTTP transport/retry proof missing')
    require(lifecycle.get('status') == 'passed_owned_cluster_lifecycle', 'Owned cluster lifecycle did not pass')
    for key in ('readiness_verified', 'owned_data_directory_confirmed', 'graceful_shutdown', 'listener_closed'):
        require(lifecycle.get(key) is True, 'Missing cluster lifecycle proof: ' + key)


def check_historical_oracle(root, manifest, http):
    """Anchor the new projector to retained results from the earlier full build."""
    receipt_path = Path(root) / 'reports/rebuild_report.json'
    historical = json.loads(receipt_path.read_text())
    require(historical.get('status') == 'passed', 'Historical M2 rebuild receipt is not a pass')
    entries = {entry['path']: entry for entry in historical['files']}
    require(len(entries) == len(historical['files']), 'Historical M2 receipt contains duplicate paths')
    raw = {name: entry['original_sha256'] for name, entry in entries.items() if name.startswith('data/raw/')}
    actual = {entry['path']: entry['sha256'] for entry in manifest['files']}
    require(len(raw) == 13 and actual == raw, 'Fresh raw source differs from the retained historical M2 bytes')
    mapping = {'snapshots': 'eta_snapshot', 'labels': 'eta_snapshot_label',
               'split_assignments': 'eta_split_assignment', 'memberships': 'trip_membership'}
    comparisons = {}
    for name, filename in mapping.items():
        path = 'data/processed/' + filename + '.jsonl'
        entry = entries[path]
        require(entry['equal'] is True and entry['original_sha256'] == entry['rebuilt_sha256'],
                'Historical M2 projection was not reproduced: ' + name)
        for mode in ('snapshot', 'http'):
            observed = http['projections'][mode]['tables'][name]['serialized_jsonl_sha256']
            require(observed == entry['original_sha256'], 'Historical M2 projection bytes differ: ' + mode + '/' + name)
        comparisons[path] = entry['original_sha256']
    return {'status': 'passed', 'historical_receipt_path': 'reports/rebuild_report.json',
            'historical_receipt_sha256': digest_file(receipt_path),
            'raw_file_count': 13, 'raw_sha256': raw, 'projection_file_count': 4,
            'historical_projection_sha256': comparisons, 'byte_exact_modes': ['snapshot', 'http'],
            'postgres_http_comparison': 'All fields and logical values against the historical-byte-verified direct projection.',
            'full_scale_history_lineage_warehouse_rebuilt': False}


def verify_native(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    require(not output.exists(), 'Choose a fresh output directory; existing evidence is retained')
    require(not output.is_relative_to(root) and not root.is_relative_to(output),
            'Native execution and logs must be outside the checkout')
    registration = verify_registration(root)
    started = now()
    output.mkdir(parents=True)
    try:
        write_json(output / 'parent_integrity.json', registration)
        source = output / 'source'
        config_path = root / 'config/m2.json'
        config = json.loads(config_path.read_text())
        settings = json.loads((root / 'workflows/p10/settings.json').read_text())
        counts = generate(source, config)
        require(counts['eta_trip'] == settings['expected_source_trip_count']
                and counts['telemetry_event'] == settings['expected_source_telemetry_rows'],
                'Generated source population differs from the registered full M2 scope')
        write_source_manifest(source, counts)
        source_manifest = verify_source_manifest(source)
        write_json(output / 'source_generation.json', {
            'status': 'passed', 'generated_counts': counts, 'config_sha256': digest_file(config_path),
            'source_manifest_sha256': digest_file(source / 'data/source_manifest.json'),
            'actual_completed_at_utc': now(),
            'pinned_generated_at_semantics': 'Original deterministic source timestamp; not this execution time.'})
        with ownedcluster(output / 'cluster') as cluster:
            postgres = check_postgres(root, cluster.dsn, operational_root=source, export_root=output / 'postgres_export')
            write_json(output / 'postgres.json', postgres)
            http = check_http_snapshot(root, source, output / 'http', config,
                                      operational_root=output / 'postgres_export')
            write_json(output / 'http_snapshot.json', http)
        lifecycle = cluster.metadata
        require_complete(postgres, http, lifecycle)
        for mode in settings['source_modes']:
            projection = http['projections'][mode]
            require(projection['snapshot_count'] == settings['expected_snapshot_count']
                    and projection['label_count'] == settings['expected_label_count'],
                    'Full M2 projection population differs: ' + mode)
        oracle = check_historical_oracle(root, source_manifest, http)
        write_json(output / 'historical_oracle.json', oracle)
        require(verify_source_manifest(source) == source_manifest, 'Raw source changed during native checks')
        require(verify_registration(root) == registration, 'Registered parent or integration code changed during execution')
        result = {
            'schema_version': 1, 'status': 'passed_native_integration',
            'native_postgresql_executed': True, 'real_http_executed': True,
            'parent_unchanged': True, 'registered_code_unchanged': True,
            'started_at_utc': started, 'completed_at_utc': now(),
            'registration_sha256': registration['registration_sha256'],
            'source_manifest_sha256': digest_file(source / 'data/source_manifest.json'),
            'generated_counts': counts, 'postgresql_version': postgres['postgresql_version'],
            'postgres_tables': {name: value['table_count'] for name, value in postgres['groups'].items()},
            'postgres_rows': {name: sum(t['returned_rows'] for t in value['tables'].values()) for name, value in postgres['groups'].items()},
            'native_commit_and_reconnect_exact': True, 'owned_cluster_lifecycle': lifecycle,
            'transport': http['transport'], 'projection_comparisons': http['comparisons'],
            'projections': http['projections'],
            'historical_oracle': oracle,
            'runtime': {'python': platform.python_version(), 'system': platform.system(), 'machine': platform.machine()},
            'full_scale_history_lineage_warehouse_rebuilt': False,
            'unified_28_table_warehouse_accepted': False, 'multiple_canonical_revisions_supported': False,
            'new_untouched_final_test': False, 'model_refitted': False, 'quality_accepted': False,
            'ci_executed': False, 'operational_approval': False,
            'scope': 'Native loopback PostgreSQL/HTTP adapter and snapshot/label parity over frozen synthetic inputs. Separate operational and P09 schemas; full warehouse integration remains P11.'}
        write_json(output / 'native_integration.json', result)
        head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
        # Bind compact evidence plus actual runtime logs; never expose connection credentials.
        evidence_names = ('parent_integrity.json', 'source_generation.json', 'postgres.json',
                          'http_snapshot.json', 'historical_oracle.json', 'native_integration.json', 'cluster/lifecycle.json')
        write_json(output / 'run_receipt.json', {
            'status': 'passed_native_integration', 'execution_git_head': head,
            'execution_checkout_changes_present': bool(subprocess.check_output(
                ['git', '-C', str(root), 'status', '--porcelain'], text=True).strip()),
            'execution_git_head_scope': 'Observed HEAD; registered file hashes bind executed integration code.',
            'started_at_utc': started, 'completed_at_utc': now(),
            'registration_sha256': registration['registration_sha256'],
            'output_sha256': {name: digest_file(output / name) for name in evidence_names},
            'native_postgresql_executed': True, 'real_http_executed': True,
            'quality_accepted': False, 'operational_approval': False})
        return result
    except BaseException as exc:
        # Retain diagnostics, never label a partial execution as success.
        for name in ('native_integration.json', 'run_receipt.json'):
            (output / name).unlink(missing_ok=True)
        write_json(output / 'failure.json', {'status': 'failed', 'failure_type': type(exc).__name__,
                   'started_at_utc': started, 'failed_at_utc': now(), 'success_receipt_created': False})
        raise
