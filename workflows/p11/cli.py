"""Check the frozen parent or verify P11 in a new external output directory."""
import argparse
import json
from pathlib import Path
import sys

CHECKOUT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CHECKOUT))
sys.path.insert(0, str(CHECKOUT / 'src'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check-parent', 'verify'))
    parser.add_argument('--root', type=Path, default=CHECKOUT)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.root.resolve() != CHECKOUT:
        parser.error('The CLI must execute from the requested checkout')
    if args.command == 'check-parent':
        if args.output is not None:
            parser.error('check-parent accepts no --output')
        from workflows.p11.registry import verify_registration
        result = verify_registration(CHECKOUT)
    else:
        if args.output is None:
            parser.error('verify requires a new external --output directory')
        from workflows.p11.run import verify
        result = verify(CHECKOUT, args.output)
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
