# Native integration evidence

P10 retains service checks for the published P09 base
`8ae479f5391e6b2f0e3b6ad1131e411629f90966`. The scientific implementation and
the retained P08/P09 evidence remain unchanged. This directory records the
preparation environment; a different machine must run the verifier and retain
its own receipts.

Preparation passed all 926 project tests and the full native integration run
on Linux, Python 3.12.14 and PostgreSQL 17.11. All 634 database rejection probes
passed, committed rows matched after reconnect, and owned-schema/server cleanup
was verified. macOS execution and P10 publication remain pending.

| File | Evidence scope |
| --- | --- |
| [summary.json](summary.json) | Bounded integration result and remaining work. |
| [native_integration.json](native_integration.json) | Actual native PostgreSQL/HTTP execution, three input paths, projection counts and owned-cluster lifecycle. |
| [postgres.json](postgres.json) | Typed readback for 11 operational source and 35 P09 output/audit tables, catalog inspection, negative probes, commit/reconnect and cleanup. |
| [http_snapshot.json](http_snapshot.json) | Full GPS payload comparison, cursor paging/retry and downstream snapshot/label/split/membership comparisons. |
| [historical_oracle.json](historical_oracle.json) | Comparison with the unchanged historical projection evidence, separate from agreement between new input paths. |
| [parent_integrity.json](parent_integrity.json) | Frozen parent inventory and registered P10 implementation identity. |
| [runtime_provenance.json](runtime_provenance.json) | Observed PostgreSQL package versions, executable/shared-library identity and platform scope. |
| [p09_publication.json](p09_publication.json) | Six published P09 commits, exact delivery trees and valid GitHub signatures. This does not supply native macOS or browser-review evidence. |
| [p09_replay.json](p09_replay.json) | Replay of the unchanged parent workflow. |
| [verification.json](verification.json) | Required checks and their results, with execution scope stated separately from quality acceptance. |
| [run_receipt.json](run_receipt.json) | Actual execution clocks and hashes binding retained native outputs. |

The 11 operational tables contain 13,706 rows. The separate 35-table P09 store
contains 32,128 rows, including its 11 canonical output contracts and auxiliary
audit records. These are two schemas with distinct populations, not a unified
28-table warehouse. Full-source projection checks cover 197,303 GPS rows,
14,349 snapshots and labels, and 1,800 split/membership records.

The 13 regenerated raw files and the four direct/HTTP serialized projections
match the historical SHA256 records. PostgreSQL-plus-HTTP projections match
their fields and logical values against that historical-byte-verified direct
baseline. Equivalent UTC timestamp spellings are not claimed to be identical
bytes.

The small preprocessing fixture compares all seven processed JSONL files,
including history, provenance and lineage. It does not invoke the DuckDB/Parquet
warehouse build. Full-scale history/provenance reconstruction, multi-revision
canonical telemetry (G11), database/Parquet failure recovery (G13) and actual CI
remain P11/M5.2 work. Rejecting unsupported revisions does not implement their
missing end-to-end semantics.

The runtime uses a fresh PostgreSQL cluster and real HTTP requests on loopback.
It does not contact a fleet or messaging service. Retained clocks describe this
execution; generated synthetic event clocks retain their original meaning.
Existing source/model/policy/target/result files are not rewritten. The P08
scientific quality limits, G14/G15/G16, and quality/operational approval remain
unchanged.

See [the integration specification](../../../docs/p10_integration.md) for setup,
reproduction commands and the distinction between source parity, typed service
readback and full warehouse reconstruction.
