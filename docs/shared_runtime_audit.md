# Shared runtime and entity identifier audit

The M0 audit established a narrow reuse boundary: stable entity identifiers, named seed behavior and selected utilities were compatible with DS-M01. The earlier 2025 fuel-data generator, domain contracts and analytical pipeline were not reusable as ETA observations. Ten audit checks passed, and the reference source files were unchanged.

This is a historical source compatibility audit. It did not run the complete DS-M01 pipeline or validate any model. Later M1 and M2 execution receipts provide their own evidence.

## Reference source and audit record

| Item | Historical value |
|---|---|
| Source archive | `DA-E01_v1_source.zip` |
| Archive SHA256 | `c6f890c1bd0724bcf3f133dde50957a259502280c05f9f4eadd2f75daacc1e0a` |
| Extracted source files | 165, all with identical pre/post-audit hashes |
| Source-tree digest | `5eb5c3cda0d3b5ef1f4273a31fd2121b205b292fadc2936fc2517566d9ab04fa` |
| Runtime | Python 3.12.14, bytecode writing disabled |
| Audit timestamp | `2026-10-27T00:51:53.392378+00:00` |

The digest method, selected file hashes, exact entity IDs and check details are retained in [shared_runtime_audit.json](shared_runtime_audit.json). The audit did not verify every version in the dependency lock file.

At the audit timestamp, DS-M01 `common.py` had SHA256 `1aa015f6f39c1ca0a7f0a4496bec6ad185f0521644c2bf7b2125a17d60b64b94`. Its source prefix before the new `write_source_manifest` and `verify_source_manifest` helpers had SHA256 `2b24ce3bedab5ef6dc70d2896d92f6b303802d232d58d1b35e6282efaf140a6c`, matching the previously audited complete helper file. This established that the existing seed, digest, time and serialization helpers had not changed at that point. These are historical digests, not assertions about later file versions.

The new manifest functions received source review only in M0. They check published counts and file hashes and guard pipeline/warehouse entry points when enabled. Their integration checks were recorded later by the main pipeline. M0 itself did not read or write generated DS-M01 data, and the helpers were not interpreted as a transaction spanning the whole project.

## Reuse decisions

| Component | Compatibility boundary |
|---|---|
| Entity IDs | Retain identifier rules for three tenants, 30 vehicles, 36 devices, 45 drivers, 12 sites and 12 routes. Regenerate 2026 validity intervals, assignments and route versions. |
| Named seeds | Three test vectors matched between the original `sub_seed` and DS-M01 `stable_seed`. New observations use the `ds-m01-2026-v1` namespace. |
| SHA256 and JSON | File digests are compatible. DS-M01 explicitly uses UTF-8, sorted fields and `allow_nan=False`. JSONL writing was source-reviewed but not executed in this audit. |
| Time | DS-M01 rejects naive timestamps and normalizes to UTC. Timestamp semantics are preserved without requiring identical string precision to the earlier helper. |
| Cursors and source snapshots | Retain design principles. Pure cursor functions were tested; HTTP service, retry and snapshot integration were outside M0. |

The earlier `common.ident` quoted SQL identifiers; DS-M01's function of the same name generates hashed record IDs. They are different APIs. The previous fuel-domain `contract()`, table allowlist, PostgreSQL `FUEL_*` settings and fuel-unit conversions were not carried over.

## Entity and seed contract

| Entity | Identifier range |
|---|---|
| Tenant | `T01`–`T03` |
| Vehicle | `Txx-V01`–`Txx-V10` per tenant |
| Device | `Txx-D01`–`Txx-D12` per tenant |
| Driver | `Txx-R01`–`Txx-R15` per tenant |
| Site / route | `SITE-01`–`SITE-12` / `ROUTE-01`–`ROUTE-12` |

The source archive contained no generated raw entity registry. These IDs were reconstructed from the actual `simulation.py` loops and an in-memory `build_plan()` call. The audit JSON retains the complete lists. Reusing a registry identifier does not imply that every corresponding entity is active in the DS-M01 data contracts; for example, driver records are not among the 17 populated data-stage tables.

For seed derivation, convert the base seed and tokens to strings, join them with `\x1f`, encode as UTF-8, take the first 16 bytes of SHA256 and interpret them as an unsigned big-endian integer. The base seed is `20261021`:

```python
stable_seed(20261021, "ds-m01-2026-v1", purpose, entity_id)
```

For `purpose="trip"` and `entity_id="T01-V01-TR001"`, the result is `22982877921785618888433484590010977744`. JSON audit records store the complete 128-bit value as a decimal string.

## Executed checks

| Check | Actual scope and result |
|---|---|
| M0-01 | Existing stable-ID/time-interval test method passed when called directly; `setUpClass` and `generate()` were not invoked. |
| M0-02 | Three independently calculated SHA256 seed vectors passed. |
| M0-03 | Explicit-offset normalization across DST and rejection of naive timestamps passed. |
| M0-04 | Cursor roundtrip and wrong-snapshot rejection passed. |
| M0-05 | Digest, sorted JSON and SQL identifier quoting checks passed. |
| M0-06 | Legacy truth-prefix guard and its limits were confirmed: `arrival_time` was not rejected by that prefix rule. |
| M0-07 | In-memory source-plan reconstruction returned 1,800 trips, three tenants, 30 vehicles and 45 drivers. |
| M0-08 | DS-M01 helper seed/digest compatibility and naive-timestamp rejection passed. |
| M0-09 | All reference source files remained unchanged after the audit. |
| M0-10 | Existing helper bytes before the manifest additions matched the earlier audit; new helpers received source review only. |

M0 did not run the earlier project's full test suite or data generation, PostgreSQL, HTTP fault injection, the complete DuckDB pipeline, the complete DS-M01 pipeline, model fitting, calibration or evaluation. Existing reports from other executions were not counted as new M0 results.

## Implications for ETA data

1. The earlier routes were abstract site loops; distance and 90–180-minute duration were generated independently. Old coordinates and durations cannot serve as real road-network ETA labels.
2. Stable vehicle IDs may span datasets, but 2026 trips are different business events. Cross-dataset joins need an explicit project/dataset identity to avoid collisions among similarly formatted trip IDs.
3. Spreading 1,800 trips across nine months yields lower monthly density than a six-month design. Equal monthly statistical precision cannot be assumed.
4. ETA features require an explicit allowlist excluding actual arrivals, delay labels and simulator truth. The legacy prefix guard is insufficient.
5. Validity intervals, assignment histories and availability timestamps must be regenerated and checked under DS-M01 contracts. This audit does not replace the later temporal and leakage checks.
