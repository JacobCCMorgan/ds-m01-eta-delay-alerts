"""Package a validated milestone with explicit source/data boundaries; no Git writes."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from datetime import datetime, timezone
import argparse
import hashlib
import json


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def included(relative):
    parts = relative.parts
    return not any(p in {'.git', '.venv', '__pycache__', '.DS_Store'} or
                   p.startswith(('.generator-stage-', '.pipeline-stage-')) or
                   p.endswith('.egg-info') for p in parts) and relative.suffix not in {'.pyc', '.wal'}


def source_file(relative):
    return included(relative) and (relative.parts[0] not in {'data', 'validation'} or
                                  relative.as_posix() == 'data/LICENSE.md') and relative.name not in {'MANIFEST.json', 'SHA256SUMS.txt'}


def files(root, predicate=included):
    return sorted(p for p in root.rglob('*') if p.is_file() and predicate(p.relative_to(root)))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, required=True)
    ap.add_argument('--baseline-source-zip', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    if output == root or root in output.parents:
        raise ValueError('Archive output must be outside the project tree')
    output.mkdir(parents=True, exist_ok=True)
    read = lambda name: json.loads((root / 'reports' / name).read_text())
    assert read('quality_report.json')['passed']
    assert read('m2_readiness.json')['technical_checks_passed']
    assert read('rebuild_report.json')['status'] == 'passed'
    assert read('last_http-check.json')['status'] == 'passed'
    # An unfinished publication should be investigated rather than shipped.
    if any(root.glob('.generator-stage-*')) or any(root.glob('.pipeline-stage-*')):
        raise ValueError('Unfinished data staging directory remains')
    with ZipFile(args.baseline_source_zip) as baseline:
        old = {n.removeprefix('ds_m01/'): hashlib.sha256(baseline.read(n)).hexdigest()
               for n in baseline.namelist() if not n.endswith('/')}
    integrity = {'SOURCE_SHA256SUMS.txt', 'SOURCE_DELTA.json', 'MANIFEST.json', 'SHA256SUMS.txt'}
    current = {p.relative_to(root).as_posix(): digest(p) for p in files(root, source_file)
               if p.relative_to(root).as_posix() not in integrity}
    old_compare = {n: h for n, h in old.items() if n not in integrity}
    delta = {
        'baseline_repository': 'JacobCCMorgan/ds-m01-eta-delay-alerts',
        'baseline_commit': 'ff9ad674434064366c789b5d7de944bfbdb5b0d4',
        'baseline_verified': True,
        'baseline_source_archive_sha256': digest(args.baseline_source_zip),
        'added': sorted(set(current) - set(old_compare)),
        'changed': sorted(n for n in current.keys() & old_compare.keys() if current[n] != old_compare[n]),
        'removed': sorted(set(old_compare) - set(current)),
        'unchanged_count': sum(current[n] == old_compare[n] for n in current.keys() & old_compare.keys()),
        'integrity_metadata_recomputed': sorted(integrity),
        'git_commit_or_push_performed': False,
    }
    write_json(root / 'SOURCE_DELTA.json', delta)
    source = [p for p in files(root, source_file) if p.name != 'SOURCE_SHA256SUMS.txt']
    (root / 'SOURCE_SHA256SUMS.txt').write_text(''.join(f'{digest(p)}  {p.relative_to(root).as_posix()}\n' for p in source))
    inventory = [p for p in files(root) if p.relative_to(root).as_posix() not in {'MANIFEST.json', 'SHA256SUMS.txt'}]
    records = [{'path': p.relative_to(root).as_posix(), 'bytes': p.stat().st_size, 'sha256': digest(p)} for p in inventory]
    write_json(root / 'MANIFEST.json', {
        'project': 'DS-M01', 'milestone': 'M2', 'version': '0.2.0',
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'Complete source, data and evidence; manifest and SHA256SUMS excluded from this array',
        'files': records,
    })
    checksum_inputs = [p for p in files(root) if p.name != 'SHA256SUMS.txt']
    (root / 'SHA256SUMS.txt').write_text(''.join(f'{digest(p)}  {p.relative_to(root).as_posix()}\n' for p in checksum_inputs))
    results = []
    for kind, predicate in [('Source', source_file), ('Complete', included)]:
        target = output / f'DS-M01_M2_{kind}_20261027_v0_2.zip'
        entries = files(root, predicate)
        with ZipFile(target, 'w', ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            for path in entries:
                archive.write(path, 'ds_m01/' + path.relative_to(root).as_posix())
        with ZipFile(target) as archive:
            assert len(archive.namelist()) == len(entries)
            assert not any('.generator-stage-' in n or '.pipeline-stage-' in n or '/.git/' in n for n in archive.namelist())
            assert archive.testzip() is None
            if kind == 'Source':
                assert not any(n.startswith('ds_m01/validation/') or (n.startswith('ds_m01/data/') and n != 'ds_m01/data/LICENSE.md') for n in archive.namelist())
                sums = archive.read('ds_m01/SOURCE_SHA256SUMS.txt').decode().splitlines()
                checked = set()
                for line in sums:
                    expected, relative = line.split('  ', 1)
                    assert hashlib.sha256(archive.read('ds_m01/' + relative)).hexdigest() == expected
                    checked.add('ds_m01/' + relative)
                assert checked == set(archive.namelist()) - {'ds_m01/SOURCE_SHA256SUMS.txt'}
        results.append({'kind': kind, 'path': str(target), 'bytes': target.stat().st_size,
                        'sha256': digest(target), 'files': len(entries), 'zip_crc_verified': True})
        print(json.dumps(results[-1]), flush=True)
    write_json(output / 'DS-M01_M2_Package_Receipt_20261027_v0_2.json', {'packages': results, 'source_delta': delta})


if __name__ == '__main__':
    main()
