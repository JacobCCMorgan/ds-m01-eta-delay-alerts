"""Validate the P10 registration or run native integration in a fresh directory."""
import argparse
import json
import os
from pathlib import Path
import sys

CHECKOUT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CHECKOUT))
sys.path.insert(0, str(CHECKOUT / 'src'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check-parent', 'verify-native'))
    parser.add_argument('--root', type=Path, default=CHECKOUT)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if root != CHECKOUT:
        parser.error('The CLI must execute from the requested checkout')
    if args.command == 'check-parent':
        if args.output is not None:
            parser.error('check-parent emits JSON to stdout; no --output is accepted')
        from workflows.p10.registry import verify_registration
        result = verify_registration(root)
    else:
        if args.output is None:
            parser.error('verify-native requires a fresh external --output directory')
        for name in tuple(os.environ):
            if name.startswith(('PG', 'POSTGRES')) or name == 'DATABASE_URL':
                os.environ.pop(name, None)
        from workflows.p10.run import verify_native
        result = verify_native(root, args.output)
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
