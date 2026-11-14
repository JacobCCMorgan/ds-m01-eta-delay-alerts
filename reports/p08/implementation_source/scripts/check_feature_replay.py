"""Rebuild M3b.1 features and compare the five retained core artifacts."""

import argparse
import json
from pathlib import Path

from ds_m01.common import digest_file
from ds_m01.feature_run import run


def check(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    reference = json.loads((root / 'reports/m3b1/run_receipt.json').read_text())
    if digest_file(root / 'config/m3b1.json') != reference['settings_sha256']:
        raise ValueError('Feature settings changed since the retained run')
    if digest_file(root / 'contracts/m3b1_features.json') != reference['feature_contract_sha256']:
        raise ValueError('Feature contract changed since the retained run')
    for name, expected in reference['code_sha256'].items():
        if digest_file(root / 'src/ds_m01' / name) != expected:
            raise ValueError(f'Feature code changed since the retained run: {name}')
    for name, expected in reference['output_sha256'].items():
        if digest_file(root / 'reports/m3b1' / name) != expected:
            raise ValueError(f'Retained feature evidence was changed: {name}')
    summary = run(root, root / 'config/m3b1.json', output)
    comparisons = {name: digest_file(output / name) == expected
                   for name, expected in reference['output_sha256'].items()}
    if not all(comparisons.values()):
        raise ValueError(f'Feature replay differs from retained evidence: {comparisons}')
    return dict(status='passed', identical_core_artifacts=comparisons,
                validation_matrix_rows=summary['validation_matrix_rows'],
                transformer_count=summary['transformer_count'], output=str(output),
                note='Execution timestamps are run-specific. No model accuracy or heldout scoring is asserted.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--output', type=Path, default=Path('reports/m3b1_local'))
    args = parser.parse_args()
    print(json.dumps(check(args.root, args.output), indent=2))
