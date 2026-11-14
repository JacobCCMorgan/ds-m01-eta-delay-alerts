# P08 numerical replay and retained-store integrity

P08's final results remain fixed. This revision changes how reproduction is checked
across environments; it does not repeat the experiment, refit a model, select a policy,
change a tolerance or replace a retained artifact.

## Observed failure

Native Python 3.12.2 replay on macOS passed its earlier forecast comparisons, then
stopped with `Final JSONL differs from reconstructed rows: p08_opportunity_audit`.
A subsequent nine-scenario diagnostic showed:

- Every scenario passed the original forecast comparison rules.
- Rebuilding the stored tables from their retained inputs reproduced all JSONL table
  digests exactly.
- Fresh native inference changed raw probability values by at most approximately
  `1.11e-16` in the displayed opportunity-audit differences. No displayed opportunity
  difference exceeded the frozen tolerance.
- These raw values also appear in `p08_metadata_audit` and feed `p08_run_audit` hashes.

This observation is transcribed from a terminal capture supplied by the reporting
machine. The exported diagnostic JSON was not supplied for this revision. It does not
establish that the original strict replay or this revised verifier passed on that
machine; a successful run of the revised verifier is still required.

The mismatch arose because the original numerical comparator permits small floating-point
differences, while the typed-store verifier requires an exact JSON digest. Hashes of
raw floats can differ even when the values satisfy the declared comparison rule.

## Two checks with different purposes

| Check | Reconstructed evidence | Required agreement |
|---|---|---|
| Numerical replay | Saved model states applied to the frozen inputs; rebuilt forecasts, targets, policy/delivery decisions and scores | Original structures, identifiers, order, counts and discrete values are exact. Floating-point values retain absolute `1e-10` and relative `1e-9` tolerances. |
| Retained-store integrity | Tables rebuilt from the retained opportunities, registry and policy/delivery bundle after their comparison with the rebuilt evidence | Exact JSONL/file hashes, DuckDB and Parquet logical contents, schema, constraints and SQL relationships. |

The second check uses the original `final_store.verify_store()` unchanged. It does not
accept approximate digest strings, rewrite table files, relax database constraints or
replace saved values with native values. In particular, canonical projected ETA values,
probability strings, identifiers and policy decisions remain subject to their original
exact comparisons.

## Registration and scope

The portability verifier and its tests are separately versioned under
`reproducibility/p08/`. The [verification registration](../reproducibility/p08/portability_v1/registration.json)
was created after final outcomes were exposed. Its actual registration timestamp is UTC;
it binds the new verifier and tests to the original implementation freeze and retained run. They are not part of the original scientific source inventory or the original
727-test pre-target conformance claim. The original source inventory check remains in
force, rather than being edited to absorb later code.

The original `src/ds_m01/final_replay.py` remains available, including its strict store
comparison behavior. The new receipt identifies version `p08-portable-replay-v1`, using mode
`source_assisted_portable_final_replay` or `retained_portable_final_replay`. It must not
be presented as an original strict-replay success. It records tolerated numerical
differences separately from failed checks, rather than claiming that unequal raw values
were bitwise equal. A source-assisted run also checks the frozen input and outcome-source
bindings. A retained-input run does not make that source-assisted claim.

The original final outcomes, estimates, metrics, result hashes and quality limitations
remain unchanged. G14/G15/G16 remain open, and quality and operational approval remain
false. A change to the scientific implementation, source data, model, policy, scoring or
retained result would need its own disclosed experiment-correction process. This
verification revision does not authorize such a change.

## Run the checks

Use the configured environment from the repository root, with the exact recorded P08
scientific source set and the registered portability-verifier files present:

```bash
python -m unittest discover -s tests -v
python -m unittest discover -s reproducibility/p08/tests -v
python reproducibility/p08/portable_replay.py --root . --report reports/p08
```

The first command runs the original suite. The second runs the supplemental verifier
regressions; its results must be reported separately from the original 727-test record.
The third reruns verification against retained final evidence, without fitting models
or selecting thresholds. It checks all nine scenarios and all 27 tables per scenario.

To additionally bind the supplied frozen raw sources, use:

```bash
python reproducibility/p08/portable_replay.py --root . --report reports/p08 --input-root /path/to/frozen_input_root
```

Run the independent reference with a fresh output path:

```bash
python scripts/check_final_evaluation.py --root . --report reports/p08 --input-root /path/to/frozen_input_root --output /tmp/p08_independent_checks.json
```

A successful portability run establishes agreement under these two declared checks.
It does not establish bitwise-identical native inference, fresh final-test independence,
CI success, GitHub publication or operational acceptance. Optional `--output` saves the
verification receipt to a fresh path outside the checkout; do not overwrite the
original report or failed native replay log.
