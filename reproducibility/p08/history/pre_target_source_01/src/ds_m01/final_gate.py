"""Register tested final-evaluation code before reading held-out outcomes."""
import argparse
import json
import os
import re
import subprocess
import tempfile
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

from .common import digest_file, parse_ts
from .delivery_inputs import require, verify_evaluation_readiness, verify_freeze

PROTOCOL_SHA256 = 'b9d07fee882785b2de3325ec78048c481e6dc6ee0553bc1830786e597187d194'
BASE_PUBLIC_COMMIT = 'e95dcd7f885e20af19f51fc645c9e3f577dedd97'
OUTCOME_PATH = 'data/raw/eta_trip_outcome.jsonl'
DERIVED_EVIDENCE = frozenset('contracts/current_' + name + '_evidence.csv'
                           for name in ('requirement', 'table', 'validation'))
GUARD_ENV = 'DS_M01_P08_CONFORMANCE_GUARD'
GUARD_VERSION = 'p08-python-outcome-access-v1'
GUARD_SCOPE = ('Python audit hooks reject outcome-named file access outside the fresh '
               'conformance temporary root, resolving symlink aliases. Child processes '
               'must use the same Python interpreter and inherited sitecustomize guard; '
               'native/shell execution and guard-bypassing Python flags are rejected. '
               'This is an observed Python-level test boundary, not an OS sandbox against '
               'hostile native extensions, renamed copies or previously opened descriptors.')


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value, *, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), 'Refusing a symlink evidence destination')
    with path.open('x' if exclusive else 'w') as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n')


def verify_pins(root):
    root = Path(root).resolve()
    require(digest_file(root / 'config/p08_evaluation.json') == PROTOCOL_SHA256,
            'Frozen final-evaluation protocol changed')
    settings = json.loads((root / 'config/p08_evaluation.json').read_text())
    for path, expected in settings['artifact_sha256'].items():
        require(digest_file(root / path) == expected, 'Frozen artifact changed: ' + path)
    for name, expected in settings['existing_core_sha256'].items():
        require(digest_file(root / 'src/ds_m01' / name) == expected,
                'Frozen source changed: ' + name)
    verify_freeze(root, json.loads((root / 'reproducibility/p07/protocol_freeze.json').read_text()))
    verify_evaluation_readiness(root)
    require(Path(__file__).resolve() == root / 'src/ds_m01/final_gate.py',
            'Executed final gate does not belong to the requested checkout')
    return settings


def source_inventory(root):
    """Bind production, independent references, contracts and conformance tests."""
    root = Path(root).resolve()
    paths = set()
    for folder in ('src', 'scripts', 'tests'):
        paths.update(p for p in (root / folder).rglob('*.py') if '__pycache__' not in p.parts)
    for folder in ('config', 'contracts'):
        paths.update(p for p in (root / folder).rglob('*') if p.is_file())
    return {str(p.relative_to(root)): digest_file(p) for p in sorted(paths)
            if str(p.relative_to(root)) not in DERIVED_EVIDENCE}


def historical_inventory(root):
    """Retain every existing M2/P05/P06/P07 report, not just selected models."""
    root = Path(root).resolve()
    return {str(p.relative_to(root)): digest_file(p)
            for p in sorted((root / 'reports').rglob('*'))
            if p.is_file() and p.relative_to(root / 'reports').parts[0] != 'p08'}


def _timestamp(value):
    require(isinstance(value, str) and value.strip(), 'Evidence timestamp must be a timezone-aware string')
    return parse_ts(value)


def _digest_mapping(value, name):
    require(isinstance(value, dict) and value, name + ' must be a nonempty digest map')
    for key, digest in value.items():
        require(isinstance(key, str) and key and not Path(key).is_absolute()
                and '..' not in Path(key).parts
                and isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest),
                'Invalid ' + name + ' entry')


def _inventory(value):
    require(isinstance(value, dict), 'Missing metadata inventory')
    for key in ('outcomes_opened', 'target_based_pruning'):
        require(value.get(key) is False, 'Metadata inventory has invalid exposure/pruning claim')
    for field, count in (('trip_keys', 'trips'), ('snapshot_keys', 'opportunities')):
        rows = value.get(field)
        require(isinstance(rows, list) and type(value.get(count)) is int and value[count] >= 0
                and len(rows) == value[count], 'Invalid metadata ' + field + ' count')
        require(all(isinstance(row, list) and len(row) == 2
                    and all(isinstance(x, str) and x.strip() for x in row) for row in rows),
                'Invalid metadata identity keys')
        require(rows == sorted(rows) and len({tuple(row) for row in rows}) == len(rows),
                'Metadata keys must be complete unique sorted identities')
    for key in ('membership_sha256', 'snapshot_context_sha256', 'feature_sha256', 'promise_features_sha256'):
        require(isinstance(value.get(key), str) and re.fullmatch('[0-9a-f]{64}', value[key]),
                'Missing metadata digest: ' + key)
    return value


def metadata_digest(value):
    _inventory(value)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def _check_test_log(log, expected=None):
    counts = re.findall(r'^Ran (\d+) tests? in ', log, re.MULTILINE)
    require(len(counts) == 1 and log.rstrip().endswith('\nOK'),
            'Conformance log must contain exactly one complete suite with no skips')
    count = int(counts[0])
    require(count > 577 and (expected is None or count == expected), 'Unexpected conformance test count')
    require('test_final_independent.' in log, 'Independent final fixtures did not execute')
    return count


def _validate_conformance(qa, *, before=None):
    require(isinstance(qa, dict) and qa.get('status') == 'passed_pre_target_conformance',
            'Passing pre-target conformance is required')
    for key in ('failures', 'skips'):
        require(type(qa.get(key)) is int and qa[key] == 0, 'Conformance ' + key + ' must be zero')
    require(type(qa.get('unit_tests')) is int and qa['unit_tests'] > 577, 'Invalid conformance test count')
    require(qa.get('final_outcomes_accessed') is False, 'Conformance exposure claim differs')
    require(_timestamp(qa['started_at_utc']) <= _timestamp(qa['completed_at_utc']),
            'Conformance completion precedes its start')
    if before is not None:
        require(_timestamp(qa['completed_at_utc']) <= _timestamp(before), 'Tests must complete before registration')
    _digest_mapping(qa.get('source_sha256'), 'tested source inventory')
    require(isinstance(qa.get('log_sha256'), str) and re.fullmatch('[0-9a-f]{64}', qa['log_sha256']),
            'Invalid conformance log digest')
    guard = qa.get('access_guard')
    require(isinstance(guard, dict) and guard.get('version') == GUARD_VERSION
            and guard.get('scope') == GUARD_SCOPE
            and guard.get('protected_outcome_opens_allowed') == 0
            and type(guard.get('protected_outcome_opens_allowed')) is int
            and type(guard.get('guarded_processes')) is int and guard['guarded_processes'] >= 1
            and isinstance(guard.get('events_sha256'), str)
            and re.fullmatch('[0-9a-f]{64}', guard['events_sha256']),
            'Missing observed conformance access guard')
    return qa


def install_access_guard(config):
    """Install before test discovery; child Python startup uses the same hook.

    Temporary fixture outcomes are allowed only inside this new run's root.
    The closure retains its original settings if a test mutates the environment.
    """
    temporary = Path(config['temporary_root']).resolve()
    guard_dir = Path(config['guard_directory']).resolve()
    interpreter = Path(config['python']).resolve()
    journal = Path(config['journal']).resolve() if config.get('journal') else None
    token = json.dumps(config, sort_keys=True, separators=(',', ':'))
    protected_name = Path(OUTCOME_PATH).name
    startup_digest = config.get('startup_sha256')

    def record(kind, **extra):
        if journal is not None:
            line = json.dumps(dict(event=kind, pid=os.getpid(), **extra), sort_keys=True) + '\n'
            descriptor = os.open(journal, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, line.encode())
            finally:
                os.close(descriptor)

    def audit(event, args):
        if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0]))
            resolved = path.resolve()
            if (path.name == protected_name or resolved.name == protected_name) and not resolved.is_relative_to(temporary):
                record('blocked_outcome_open', path=str(resolved))
                raise PermissionError('P08 conformance forbids protected outcome access outside its fresh TMPDIR')
        elif event in ('os.link', 'os.rename'):
            for value in args[:2]:
                if isinstance(value, (str, bytes, os.PathLike)):
                    path = Path(os.fsdecode(value)); resolved = path.resolve()
                    if (path.name == protected_name or resolved.name == protected_name) and not resolved.is_relative_to(temporary):
                        record('blocked_outcome_relocation', path=str(resolved), operation=event)
                        raise PermissionError('P08 conformance forbids relocating a protected outcome into fixtures')
        elif event == 'subprocess.Popen':
            executable, argv, _cwd, environment = args
            environment = os.environ if environment is None else environment
            pythonpath = environment.get('PYTHONPATH', '').split(os.pathsep)
            okay = (isinstance(executable, (str, bytes, os.PathLike))
                    and Path(os.fsdecode(executable)).resolve() == interpreter
                    and isinstance(argv, (list, tuple))
                    and not any(arg in ('-I', '-E', '-S') or
                                (isinstance(arg, str) and arg.startswith('-') and not arg.startswith('--')
                                 and any(flag in arg[1:] for flag in ('I', 'E', 'S')))
                                for arg in argv[1:argv.index('-c') if '-c' in argv else
                                                argv.index('-m') if '-m' in argv else len(argv)])
                    and pythonpath and Path(pythonpath[0]).resolve() == guard_dir
                    and (not config.get('pythonpath') or environment.get('PYTHONPATH') == config['pythonpath'])
                    and (not startup_digest or digest_file(guard_dir / 'sitecustomize.py') == startup_digest)
                    and environment.get(GUARD_ENV) == token
                    and Path(environment.get('TMPDIR', '')).resolve() == temporary)
            if not okay:
                record('blocked_child_process', executable=str(executable))
                raise PermissionError('P08 conformance requires guarded child Python; native or bypass execution denied')
        elif event in ('os.system', 'os.exec', 'os.posix_spawn', 'os.spawn'):
            record('blocked_native_execution', operation=event)
            raise PermissionError('P08 conformance forbids unguarded native execution')
    sys.addaudithook(audit)
    record('guard_installed', temporary_root=str(temporary))
    sys._ds_m01_p08_guard_active = True


def _guard_environment(root, destination, temporary):
    guard_directory = destination / 'python_startup_guard'
    guard_directory.mkdir()
    # Startup runs before unittest imports/discovery. Reused by permitted children.
    startup = ("import json, os\n"
               "try:\n"
               "    from ds_m01.final_gate import GUARD_ENV, install_access_guard\n"
               "    install_access_guard(json.loads(os.environ[GUARD_ENV]))\n"
               "except BaseException:\n"
               "    os.write(2, b'P08 startup access guard failed; tests are not run.\\n')\n"
               "    os._exit(86)\n")
    (guard_directory / 'sitecustomize.py').write_text(startup)
    config = {'temporary_root': str(temporary.resolve()),
              'guard_directory': str(guard_directory.resolve()), 'python': sys.executable,
              'journal': str((destination / 'guard_events.jsonl').resolve()),
              'startup_sha256': digest_file(guard_directory / 'sitecustomize.py'),
              'pythonpath': os.pathsep.join((str(guard_directory), str(root / 'src')))}
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(guard_directory), str(root / 'src'))),
               TMPDIR=str(temporary), TEMP=str(temporary), TMP=str(temporary),
               PYTHONDONTWRITEBYTECODE='1',
               **{GUARD_ENV: json.dumps(config, sort_keys=True, separators=(',', ':'))})
    return env


def conformance(root, destination):
    """Run the full suite behind a recorded Python outcome-access boundary."""
    root, destination = Path(root).resolve(), Path(destination).absolute()
    require(not destination.exists() and not destination.is_symlink(),
            'Conformance evidence exists; choose a fresh path')
    verify_pins(root)
    codes = source_inventory(root)
    require('tests/test_final_independent.py' in codes, 'Independent P08 fixtures are missing')
    destination.mkdir(parents=True)
    started = utc_now()
    # A fresh child temporary root makes permitted fixtures distinguishable from
    # every pre-existing protected dataset, including symlinked input roots.
    with tempfile.TemporaryDirectory(prefix='p08-conformance-') as directory:
        temporary = Path(directory).resolve()
        env = _guard_environment(root, destination, temporary)
        bootstrap = ("import sys; "
                     "getattr(sys, '_ds_m01_p08_guard_active', False) or sys.exit('Access guard missing'); "
                     "import unittest; unittest.main(module=None, "
                     "argv=['unittest','discover','-s','tests','-v'])")
        command = [sys.executable, '-c', bootstrap]
        with (destination / 'unittest_results.txt').open('x') as stream:
            result = subprocess.run(command, cwd=root, stdout=stream, stderr=subprocess.STDOUT,
                                    env=env, check=False)
    log = (destination / 'unittest_results.txt').read_text()
    require(result.returncode == 0, 'Conformance failed; inspect ' + str(destination / 'unittest_results.txt'))
    count = _check_test_log(log)
    events_file = destination / 'guard_events.jsonl'
    events = [json.loads(line) for line in events_file.read_text().splitlines()]
    guarded = {event['pid'] for event in events if event['event'] == 'guard_installed'}
    require(guarded, 'The conformance access guard did not run')
    require(codes == source_inventory(root), 'Code changed during conformance')
    receipt = {'status': 'passed_pre_target_conformance', 'started_at_utc': started,
               'completed_at_utc': utc_now(), 'unit_tests': count, 'failures': 0, 'skips': 0,
               'command': command, 'source_sha256': codes,
               'log_sha256': digest_file(destination / 'unittest_results.txt'),
               'final_outcomes_accessed': False,
               'access_guard': {'version': GUARD_VERSION, 'scope': GUARD_SCOPE,
                   'guarded_processes': len(guarded), 'protected_outcome_opens_allowed': 0,
                   'blocked_attempts': sum(event['event'] != 'guard_installed' for event in events),
                   'events_sha256': digest_file(events_file)}}
    _validate_conformance(receipt)
    write_json(destination / 'conformance_receipt.json', receipt, exclusive=True)
    return receipt


def _correction_evidence(prior_attempt, prior_freeze, settings):
    """Read preserved execution metadata, never an outcome payload."""
    attempt, freeze = Path(prior_attempt).resolve(), Path(prior_freeze).resolve()
    required = ('implementation_freeze.json', 'pretarget_receipt.json', 'exposure_record.json')
    require(all((attempt / name).is_file() for name in required), 'Prior exposed attempt is incomplete')
    require(freeze.is_file() and digest_file(freeze) == digest_file(attempt / 'implementation_freeze.json'),
            'Prior freeze does not match the preserved attempt')
    old = json.loads(freeze.read_text())
    require(old.get('protocol_sha256') == PROTOCOL_SHA256
            and old.get('input_sha256') == settings['input_sha256'],
            'A correction cannot change the scientific protocol or source inputs')
    exposure = json.loads((attempt / 'exposure_record.json').read_text())
    require(exposure.get('status') == 'final_outcome_access_started'
            and exposure.get('pretarget_receipt_sha256') == digest_file(attempt / 'pretarget_receipt.json')
            and exposure.get('expected_source_sha256') == settings['input_sha256'][OUTCOME_PATH],
            'Prior exposure record is not bound to the retained pre-target evidence')
    ended = 'failure_record.json' if (attempt / 'failure_record.json').is_file() else 'run_receipt.json'
    require((attempt / ended).is_file(), 'Prior attempt lacks a retained failure or run receipt')
    result = json.loads((attempt / ended).read_text())
    if ended == 'failure_record.json':
        require(result.get('final_outcomes_exposed') is True, 'Prior failure does not disclose target exposure')
    return {'kind': 'engineering_correction_after_final_target_exposure',
            'prior_implementation_freeze_sha256': digest_file(freeze),
            'prior_exposure_started_at_utc': exposure['started_at_utc'],
            'prior_evidence_sha256': {name: digest_file(attempt / name) for name in (*required, ended)},
            'scientific_protocol_or_inputs_changed': False,
            'untouched_final_test_claim': False,
            'scope': 'Preserves the original exposed attempt and freeze. New code/tests correct engineering only; this result is not a new untouched final test.'}


def verify_correction_history(record, prior_attempt):
    require(record.get('status') == 'registered_correction_after_final_target_exposure',
            'This record is not a disclosed correction')
    require(prior_attempt is not None, 'A correction requires its preserved prior exposed attempt')
    attempt = Path(prior_attempt).resolve()
    for name, expected in record['correction']['prior_evidence_sha256'].items():
        require(digest_file(attempt / name) == expected, 'Preserved correction predecessor changed: ' + name)
    return True


def register(root, input_root, conformance_directory, output, *, prior_attempt=None, prior_freeze=None):
    """Persist source/test and exact metadata inventory pins before target joins."""
    from .final_inputs import prepare_inputs
    root, output = Path(root).resolve(), Path(output).absolute()
    require(not output.exists() and not output.is_symlink(), 'Implementation freeze exists; never overwrite it')
    settings = verify_pins(root)
    qa_dir = Path(conformance_directory)
    qa = _validate_conformance(json.loads((qa_dir / 'conformance_receipt.json').read_text()), before=utc_now())
    log_path = qa_dir / 'unittest_results.txt'
    require(qa['log_sha256'] == digest_file(log_path), 'Test log changed')
    _check_test_log(log_path.read_text(), qa['unit_tests'])
    require(qa['access_guard']['events_sha256'] == digest_file(qa_dir / 'guard_events.jsonl'),
            'Conformance access audit changed')
    require(qa['source_sha256'] == source_inventory(root), 'Implementation changed after tests')
    require((prior_attempt is None) == (prior_freeze is None), 'Supply both prior attempt and prior freeze for a correction')
    correction = _correction_evidence(prior_attempt, prior_freeze, settings) if prior_attempt is not None else None
    inputs = prepare_inputs(root, input_root, settings)
    record = {'status': 'registered_correction_after_final_target_exposure' if correction else 'frozen_before_final_target_access', 'recorded_at_utc': utc_now(),
              'base_public_commit': BASE_PUBLIC_COMMIT, 'protocol_sha256': PROTOCOL_SHA256,
              'source_sha256': qa['source_sha256'], 'historical_report_sha256': historical_inventory(root),
              'input_sha256': settings['input_sha256'], 'metadata_inventory': inputs['inventory'],
              'metadata_inventory_sha256': metadata_digest(inputs['inventory']),
              'conformance': qa, 'final_outcomes_accessed': correction is not None,
              'july_model_calibration_policy_and_delivery_results_previously_observed': True,
              'model_or_calibrator_refitted': False, 'policy_reselected': False,
              'quality_accepted': False, 'operational_approval': False,
              'derived_reporting_exclusions': sorted(DERIVED_EVIDENCE),
              'scope': 'Registers P08 implementation and tested conformance before its first final-target access. Derived current evidence CSVs and prose are reporting, not score-affecting inputs; immutable contracts remain pinned. This does not backdate implementation to the P07 specification freeze.'}
    if correction is not None:
        record['correction'] = correction
        record['scope'] = correction['scope']
    verify_registered(root, record)
    write_json(output, record, exclusive=True)
    return record


def verify_registered(root, record, *, run_started_at=None):
    settings = verify_pins(root)
    require(isinstance(record, dict) and record.get('status') in
            ('frozen_before_final_target_access', 'registered_correction_after_final_target_exposure'),
            'Invalid implementation freeze')
    corrected = record['status'] == 'registered_correction_after_final_target_exposure'
    require(record.get('final_outcomes_accessed') is corrected, 'Invalid prior target exposure disclosure')
    require(corrected == ('correction' in record), 'Correction evidence and status disagree')
    require(record.get('base_public_commit') == BASE_PUBLIC_COMMIT, 'Implementation base differs')
    require(record.get('protocol_sha256') == PROTOCOL_SHA256, 'Final protocol freeze differs')
    for field in ('model_or_calibrator_refitted', 'policy_reselected',
                  'quality_accepted', 'operational_approval'):
        require(record.get(field) is False, 'Invalid pre-target scope: ' + field)
    require(record.get('july_model_calibration_policy_and_delivery_results_previously_observed') is True,
            'Prior July observation disclosure is required')
    require(record.get('derived_reporting_exclusions') == sorted(DERIVED_EVIDENCE), 'Freeze exclusions differ')
    frozen_at = _timestamp(record['recorded_at_utc'])
    if corrected:
        correction = record['correction']
        require(isinstance(correction, dict)
                and correction.get('kind') == 'engineering_correction_after_final_target_exposure'
                and correction.get('scientific_protocol_or_inputs_changed') is False
                and correction.get('untouched_final_test_claim') is False,
                'Invalid correction scope')
        _digest_mapping(correction.get('prior_evidence_sha256'), 'preserved correction predecessor')
        prior = correction['prior_evidence_sha256']
        require(set(prior) in ({'implementation_freeze.json', 'pretarget_receipt.json', 'exposure_record.json', 'failure_record.json'},
                              {'implementation_freeze.json', 'pretarget_receipt.json', 'exposure_record.json', 'run_receipt.json'})
                and correction.get('prior_implementation_freeze_sha256') == prior['implementation_freeze.json'],
                'Incomplete correction predecessor evidence')
        require(_timestamp(correction['prior_exposure_started_at_utc']) <= frozen_at,
                'Correction registration precedes the disclosed exposure')
    require(frozen_at <= _timestamp(utc_now()), 'Implementation freeze lies in the future')
    if run_started_at is not None:
        require(frozen_at <= _timestamp(run_started_at), 'Implementation freeze must precede execution')
    qa = _validate_conformance(record.get('conformance'), before=record['recorded_at_utc'])
    _digest_mapping(record.get('source_sha256'), 'registered source inventory')
    _digest_mapping(record.get('historical_report_sha256'), 'historical report inventory')
    require(record['source_sha256'] == source_inventory(root), 'Registered implementation changed')
    require(record['historical_report_sha256'] == historical_inventory(root), 'Historical evidence changed')
    require(qa['source_sha256'] == record['source_sha256'], 'Tested code differs')
    require(record.get('input_sha256') == settings['input_sha256'], 'Registered source input pins differ')
    require(record.get('metadata_inventory_sha256') == metadata_digest(record.get('metadata_inventory')),
            'Registered metadata inventory digest differs')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    tests = subs.add_parser('conformance')
    tests.add_argument('--root', type=Path, required=True)
    tests.add_argument('--output', type=Path, required=True)
    freeze = subs.add_parser('register')
    freeze.add_argument('--root', type=Path, required=True)
    freeze.add_argument('--input-root', type=Path, required=True)
    freeze.add_argument('--conformance', type=Path, required=True)
    freeze.add_argument('--output', type=Path, required=True)
    freeze.add_argument('--prior-attempt', type=Path)
    freeze.add_argument('--prior-freeze', type=Path)
    args = parser.parse_args()
    if args.command == 'conformance':
        result = conformance(args.root, args.output)
    else:
        result = register(args.root, args.input_root, args.conformance, args.output,
                          prior_attempt=args.prior_attempt, prior_freeze=args.prior_freeze)
    print(json.dumps({k: v for k, v in result.items() if k in ('status', 'unit_tests', 'recorded_at_utc')}, indent=2))


if __name__ == '__main__':
    main()
