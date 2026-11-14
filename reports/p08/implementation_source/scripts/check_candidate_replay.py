"""Verify retained JSON inference or replay fixed candidate training."""

import argparse
import json
from pathlib import Path

from ds_m01.candidate_replay import check, check_inference


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, default=Path("reports/m3b2_local"))
    parser.add_argument("--inference-only", action="store_true",
                        help="Use retained model JSON without training or opening outcome targets.")
    args = parser.parse_args()
    answer = check_inference(args.root) if args.inference_only else check(args.root, args.output)
    print(json.dumps(answer, indent=2, allow_nan=False))
