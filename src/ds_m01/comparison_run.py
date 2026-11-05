"""Publish the M3b.3 frozen-training comparison into a new directory."""

import argparse
import csv
import hashlib
import json
from pathlib import Path
import tempfile

from .common import digest_file
from .comparison_metrics import compare

PROTOCOL_SHA256 = 'ce7dc122d8ed79cede3c3b7f605e0ba40b24c871ed3b1b47436235084b389966'
CORE_FILES = ('metrics.json', 'selection.json', 'slices.csv')
SOURCE_FILES = ('comparison_run.py', 'comparison_inputs.py', 'comparison_metrics.py',
                'model_splits.py', 'baseline_metrics.py', 'common.py')


def _implementation_hashes(root):
    installed = Path(__file__).resolve().parent
    hashes = {}
    for name in SOURCE_FILES:
        relative = 'src/ds_m01/' + name
        current = digest_file(installed / name)
        if current != digest_file(root / relative):
            raise ValueError(f'Installed comparison code differs from project source: {relative}')
        hashes[relative] = current
    return hashes


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')


def run(root, output, *, outcome_path=None):
    from .comparison_inputs import load_comparison_inputs

    root = Path(root).resolve()
    output = Path(output)
    if not output.is_absolute():
        output = root / output
    if output.exists() or output.is_symlink():
        raise FileExistsError(f'Choose a new comparison output directory: {output}')
    protocol_bytes = (root / 'config/m3b3.json').read_bytes()
    if hashlib.sha256(protocol_bytes).hexdigest() != PROTOCOL_SHA256:
        raise ValueError('Comparison protocol differs from the pre-scoring decision rule')
    protocol = json.loads(protocol_bytes)
    implementation = _implementation_hashes(root)
    rows, evidence = load_comparison_inputs(root, protocol, outcome_path=outcome_path)
    if len(rows) != protocol['expected_opportunities_per_method'] or {r['fold_id'] for r in rows} != set(protocol['folds']):
        raise ValueError('Comparison must contain all frozen opportunities and folds')
    metrics, slices, selection = compare(rows, protocol)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.m3b3-', dir=output.parent) as temporary:
        staged = Path(temporary) / 'results'
        staged.mkdir()
        _write_json(staged / 'metrics.json', metrics)
        _write_json(staged / 'selection.json', selection)
        with (staged / 'slices.csv').open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(slices[0]), lineterminator='\n')
            writer.writeheader()
            writer.writerows(slices)
        if _implementation_hashes(root) != implementation or digest_file(root / 'config/m3b3.json') != PROTOCOL_SHA256:
            raise ValueError('Comparison code or protocol changed during scoring')
        receipt = {'protocol_id': protocol['protocol_id'], 'protocol_sha256': PROTOCOL_SHA256,
                   'implementation_sha256': implementation,
                   'inputs': evidence, 'core_output_sha256': {p: digest_file(staged / p) for p in CORE_FILES},
                   'execution': 'Frozen forecast comparison; no model fitting or calibration/test scoring.'}
        _write_json(staged / 'run_receipt.json', receipt)
        if output.exists() or output.is_symlink():
            raise FileExistsError(f'Comparison output appeared during scoring: {output}')
        staged.rename(output)
    return selection


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'), help='Project root containing frozen M3 reports')
    parser.add_argument('--output', type=Path, required=True, help='New output directory, relative to root or absolute')
    parser.add_argument('--outcomes', type=Path, help='Frozen raw outcome JSONL; defaults to data/raw/eta_trip_outcome.jsonl')
    args = parser.parse_args(argv)
    result = run(args.root, args.output, outcome_path=args.outcomes)
    print(f"Carry forward: {result['carry_forward_method']} (training-period synthetic validation)")


if __name__ == '__main__':
    main()
