# P11 reconstruction and recovery evidence

This directory retains execution receipts from preparation of the registered P11
implementation. The parent is published P10 commit
`151ad00095fa84a3c9c975a8a74546a798a4a135`; the earlier scientific inventories and
results are preserved. These files are preparation evidence, not a claim that
someone else executed the same checkout or that GitHub Actions accepted it.

The preparation environment passed 1,021 tests across the original (727),
portability (17), P09 (115), P10 (67) and P11 (95) suites, with no skips. P09
retained replay had zero differences. The P10 native rerun used PostgreSQL 17.11
and actual loopback HTTP; its private cluster closed normally.

| P11 check | Observed result |
| --- | --- |
| Fresh full M2 rebuilds | 2 complete, independent workers |
| Historical JSONL files per run | 13 raw + 7 processed, byte-exact SHA256 |
| Typed warehouse per run | 28 schemas: 17 populated, 11 reserved empty |
| Original warehouse invariants per run | 27 passed |
| Complete DB/Parquet row comparison per run | 5,553,304 rows, all typed columns |
| Repeat comparison | All 28 schemas, counts and complete logical-row digests equal |
| Revision witness | 4 versions of 2 identities; 10 snapshots; 4 real constraint rejections |
| Abrupt process exits | 5 boundaries, each preserving the prior published generation |
| GitHub CI for the user publication | Pending publication and observed exact-commit run |

| Execution | Wall time | Peak worker RSS | Retained source bytes | Generation bytes |
| --- | ---: | ---: | ---: | ---: |
| Fresh run 1 | 571.17 s | 2.460 GiB | 434,381,652 | 978,623,686 |
| Fresh run 2 | 576.00 s | 2.400 GiB | 434,381,652 | 977,837,286 |

P11 integration elapsed time was 1165.39 seconds on Linux
x86_64 with Python 3.12.14. RSS is the fresh worker's OS high-water mark, not an
engine budget or whole-machine measurement. Disk columns are retained file sizes,
not maximum temporary spill usage. Each successful run retained 21 lossless gzip
streams: the 20 historical raw/processed comparisons plus the freshly generated
synthetic truth file, whose hash is checked against that run's scenario registry.
Every archive was fully decoded and byte-verified before removing only its owned
plain working copy. These are observations on the preparation host, not performance
guarantees for another computer.


| File | Evidence and boundary |
|---|---|
| [summary.json](summary.json) | Completed P11 rebuild, revision and recovery phases; quality and operational acceptance remain false. |
| [receipt.json](receipt.json) | Actual runtime, clocks, execution checkout and SHA256 bindings for the P11 phase reports. |
| [rebuild.json](rebuild.json) | Two complete fixed M2 runs, historical-file comparisons, all 28 typed schemas, row digests and measured resources. |
| [revision.json](revision.json) | Actual HTTP correction witness, knowledge-time snapshots, versioned lineage, replay and typed store checks. |
| [recovery.json](recovery.json) | Five real abrupt-process exits on a small typed-store fixture and explicit recovery/clean-build comparisons on one filesystem. |
| [parent_integrity.json](parent_integrity.json) | The immutable published parent and separately registered P11 code bindings. |
| [p10_publication.json](p10_publication.json) | Six actual P10 commits, exact trees and GitHub Verified status. Verification time is not an exact push timestamp. |

- [Preparation binding and five suite logs](./verification.json)
- [Native PostgreSQL/HTTP rerun receipt](./p10_native/run_receipt.json)
- [P09 retained replay](./p09_replay.json)
- [Observed resource failures and bounded resolution](./development_resource_findings.json)
- [Published P10 commits and signature checks](./p10_publication.json)


The five executed suite logs are [original](tests/original_unit_tests.log),
[portability](tests/portable_unit_tests.log), [P09](tests/p09_unit_tests.log),
[P10](tests/p10_unit_tests.log) and [P11](tests/p11_unit_tests.log).

The complete publication gate runs the original 727 tests, 17 portability tests,
115 P09 tests, 67 P10 tests, the new P11 suite, P09 retained/independent replay,
P10 PostgreSQL/HTTP integration and the complete P11 run. See the
[reproduction commands](../../../docs/p11_rebuild_recovery.md).

The execution JSON files are retained byte-for-byte. One temporary-directory
path in the P11 unit-test log is explicitly redacted in the public copy;
`verification.json` records the source hash, retained hash and redaction count.
The other test logs retain their executed bytes. This log-only redaction does
not change a runtime receipt or a source/warehouse comparison.

The full-size raw, processed and warehouse outputs are generated into fresh
external directories and described by the receipts; the compact public evidence
retains their comparison results rather than duplicating the large working set.
All current indexes keep their original historical counts and classifications.
The eleven empty downstream M2 schemas and the separate populated P09 output
store must not be combined into a claim of 28 populated native PostgreSQL tables.

P11 is a later engineering extension, not a new untouched evaluation. G14, G15
and G16 remain open. Local success and signed commits do not prove a successful
GitHub Actions run. CI acceptance requires the required job and retained artifact
for the exact published commit; it remains pending until observed.
