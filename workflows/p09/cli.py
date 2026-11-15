"""Run, inspect or replay the separately registered P09 workflow."""
import argparse
import json
from pathlib import Path
import sys

CHECKOUT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CHECKOUT))
sys.path.insert(0, str(CHECKOUT / 'src'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('run', 'replay', 'audit', 'check-parent'))
    parser.add_argument('--root', type=Path, default=CHECKOUT)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if root != CHECKOUT:
        parser.error('The CLI must run from the requested checkout')
    if args.command != 'run' and args.output is not None and args.output.resolve().is_relative_to(root):
        parser.error('Verification JSON must be written outside the checkout')
    if args.command == 'run':
        if args.output is None:
            parser.error('run requires a fresh --output directory')
        from workflows.p09.run import run
        result = run(root, args.output)
    elif args.command == 'check-parent':
        from workflows.p09.registry import verify_registration
        result = verify_registration(root)
    else:
        from workflows.p09.replay import verify, audit_report
        report = args.report or root / 'workflows/p09/evidence'
        result = (verify if args.command == 'replay' else audit_report)(root, report)
    text = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + '\n'
    if args.output is not None and args.command != 'run':
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(text)
    print(text, end='')


if __name__ == '__main__':
    main()
