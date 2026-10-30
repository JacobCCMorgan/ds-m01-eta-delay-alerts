# M3b.1 feature preparation results

**Engineering and Method Demonstration — Synthetic Data.** These are feature
preparation results on the frozen 2026 M2 synthetic scenarios, not ETA accuracy
or real-world operational improvements.

| Scope | Result |
|---|---:|
| Original training trips / snapshots | 992 / 7,850 |
| Frozen chronological folds | 4 |
| Tenant/fold transformers | 12 |
| Fit matrix rows across expanding folds | 18,343 |
| Validation opportunities retained | 5,163 |
| Validation rows without tenant fit support | 0 |
| Valid / stale / absent position snapshots | 3,369 / 4,016 / 465 |
| Missing speed and remaining-distance features after masking | 4,481 each |
| Missing position ages | 465 |

Fit row counts include reuse of historical trips across expanding folds; they
are not independent trip counts. Available vectors are not available ETA
predictions. All 5,163 validation opportunities remain, including those with
unknown outcomes; this stage never opens target files or evaluates errors.

The five reproducible artifacts are `features.jsonl`, `memberships.jsonl`,
`matrices.jsonl`, `transformers.json` and `summary.json`. Run-specific provenance
is in `run_receipt.json`. Vectors contain five scaled numeric values, their five
missing indicators and tenant/fold-specific one-hot slots; dimensions range from
62 to 70. Inspect each transformer's `feature_names` and `categorical` mapping
instead of assuming a shared layout.

See [verification.json](verification.json) for actual test and replay evidence
and [the protocol](../../docs/m3b1_features.md) for source boundaries, missingness,
fit populations, modeling handoff and external-validity limitations. No accuracy
claim follows from passing leakage checks. The historical M3a metrics and
receipts remain unchanged.
