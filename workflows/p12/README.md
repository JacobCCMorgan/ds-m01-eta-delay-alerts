# Reproduce the M2 column counts

From the repository root, use the project's Python 3.12 environment with the pinned DuckDB and PyArrow dependencies. The P11 worker creates an independent copy of the synthetic M2 generation in a fresh external directory. Use a location with at least 20 GiB free.

```bash
(
  set -eu
  repo="$(pwd -P)"
  py="$repo/.venv/bin/python"
  build="$(mktemp -d "${TMPDIR:-/tmp}/ds-m01-p12-m2.XXXXXX")"
  "$py" -I -B workflows/p11/rebuild.py worker --repo "$repo" \
    --run "$build/worker" --store "$build/store"
  "$py" -I -B workflows/p12/field_profile.py --store "$build/store" \
    --worker-result "$build/worker/result.json" --output "$build/profile.json"
  "$py" -I -B - "$build/profile.json" <<'PY'
import json
import sys
from pathlib import Path

fresh = json.loads(Path(sys.argv[1]).read_bytes())
retained = json.loads(Path('workflows/p12/evidence/m2_field_profile.json').read_bytes())
if fresh['fields'] != retained['fields']:
    raise SystemExit('Field counts or logical table hashes differ')
print(f"Matched {len(fresh['fields'])} field records")
PY
  printf 'Rebuild and profile evidence: %s\n' "$build"
)
```

Each build receives a new generation ID. The generation, worker receipt, manifest, and physical file hashes identify the specific run. The field records and logical table hashes are comparable across builds. SQL counts describe the entire named column. For a shared column, separate checks establish the requested entity, role, and timing semantics.
