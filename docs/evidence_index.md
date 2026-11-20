# What has been demonstrated?

P12 reconciles the planned scope against the retained implementation and results. The [field ledger](../workflows/p12/contracts/p12_requirement_reconciliation.csv) addresses 110 requirements, the [table ledger](../workflows/p12/contracts/p12_table_reconciliation.csv) distinguishes 28 mandatory contracts from optional inputs, and the [scenario ledger](../workflows/p12/contracts/p12_validation_reconciliation.csv) covers 14 validation cases. Each entry identifies evidence for its own scope. For the dispatch question, cohort definitions and measured outcomes, read the [methods and results](p12_methods_results.md).

The historical M2 warehouse has 17 populated synthetic tables. P06 added a
separate offline store with five canonical output tables; P07 adds canonical
alert episodes, alerts and notification-delivery attempts. P08 preserves those
eight contracts in each final scenario store. P09 then adds the three downstream
adoption, action and follow-up contracts in its own primary-S00 workflow store.
This does not rebuild the combined M2 warehouse. Earlier experimental artifacts
and dated receipts remain available. Synthetic service delivery and recipient
behavior do not establish operational approval or a measured fleet outcome.
P08's fixed evaluation and quality limitations remain unchanged.

P10 adds native PostgreSQL readback and actual HTTP/snapshot input comparisons.
It uses separate schemas for 11 operational source tables and the 35 retained
P09 output/audit tables. Full-source checks compare 197,303 GPS payloads and
14,349 snapshot/label projections, plus split/membership projections. A small
fixture compares all seven processed JSONL files, without building the
DuckDB/Parquet warehouse. This does not establish a full M2 clean rebuild or a unified native
warehouse for all 28 required contracts. See the [integration
scope](p10_integration.md) and [service evidence](../workflows/p10/evidence/README.md).

P11 subsequently reconstructs the complete frozen M2 warehouse twice, including
all 28 required schemas: 17 populated tables and 11 typed empty downstream tables.
It compares the 13 raw and seven processed files against the historical SHA256
records, then compares all columns in each DuckDB/Parquet relation and both clean
runs. The P09 output store remains separate. See the [P11 scope](p11_rebuild_recovery.md)
and [retained reconstruction evidence](../workflows/p11/evidence/README.md).

P11 also exercises an additive revision-aware HTTP/snapshot/provenance store and
five abrupt process exits during complete-generation publication. G11 and G13
have bounded evidence for those new entry points; the original frozen entry
points retain their earlier limitations. The publication protocol covers
cooperating readers/writers on one local filesystem, not hardware power loss
or a PostgreSQL/filesystem distributed transaction. The subsequent [P11 GitHub Actions run](https://github.com/JacobCCMorgan/ds-m01-eta-delay-alerts/actions/runs/44578265478) passed on published commit `2757d817`. The dated preparation receipts describe the earlier local run.

The following indexes retain the evidence classifications through P11. The P12 ledgers above give the later item-by-item dispositions.

| Historical evidence index | What a row tells you |
|---|---|
| [110 original field requirements](../contracts/current_requirement_evidence.csv) | The original ID, field ID, canonical table/column, condition and intended output; observed evidence; and the remaining boundary. |
| [28 required and 2 optional tables](../contracts/current_table_evidence.csv) | Data-stage rows, formal offline rows, required schemas or disabled inputs. M2, P06, P07, primary-S00 P08 and P09 counts describe separate retained stores. A blank phase count means the table is outside that store; zero means an empty reserved table. |
| [14 validation scenarios](../contracts/current_validation_evidence.csv) | Specifically exercised behaviors, test definitions and passing receipts, with partial or unexecuted scope stated separately. |

The two P10 integration columns identify later service evidence without changing
historical row counts or scenario classifications. Native readback of a retained
table is not a newly observed business event. In particular, a source GPS
comparison is not a native roundtrip of the canonical telemetry table, and
projection parity is not a complete history/provenance warehouse rebuild.
The additional P11 reconstruction columns identify later full-rebuild and
revision/recovery evidence without changing historical rows, field categories
or scenario classifications. The P10 guard continues to reject unsupported
revisions; supported revision behavior belongs to the P11 path.

In the historical field index, 60 of 110 mappings have a synthetic data-stage implementation and two
have partial source semantics. A version attribute does not demonstrate
multi-revision telemetry; a configured synthetic receipt clock does not prove
external acquisition. Thirty-one mappings have materialized canonical offline
rows. One retains an unobserved nullable canonical field; another
16 belong to disabled forecast or routing contracts. These evidence categories
are not a completion percentage. Populating a table does not prove every field
was observed: all P07/P08 and imported P09 `received_or_opened_at` values remain
null. P09 retains opening in a separate synthetic audit, without rewriting the
parent notification table. Two
requirements can validly map to the same column through different facility joins.

The historical scenario index records five exercised cases, six partially tested cases, one case checked through separate components and two cases dependent on disabled optional inputs. ETA-V10 has synthetic retry/repetition and partial-adoption component
evidence, while the original combined operational case remains partial. Named
tests are linked only when both the definition and passing result are retained.

Historical suites remain tied to their original runs: [P04](../reports/m3c/unittest_results.txt)
records 356 passing tests, [P05](../reports/m3d/unittest_results.txt) 416,
[P06](../reports/p06/unittest_results.txt) 489 and [P07](../reports/p07/unittest_results.txt)
576. The earlier receipts are not rewritten. The [P06 independent reference](../reports/p06/independent_checks.json)
retains 163,968 passing comparisons; the [P07 reference](../reports/p07/independent_checks.json)
adds 9,226 with no failures. P07 [engineering verification](../reports/p07/verification.json)
records zero differences in both replay modes, while [typed-store checks](../reports/p07/store/verification.json)
and [repeatability](../reports/p07/reference_repeatability.json) state format and
clock-normalization scope separately from quality acceptance. See [known issues](known_issues.md) for inputs
and failure modes outside the retained case.

For synthetic delivery, start with the [P07 results](../reports/p07/README.md),
[fixed transport protocol](p07_delivery.md), [delivery metrics](../reports/p07/metrics.json)
and [typed-store checks](../reports/p07/store/verification.json). Nine candidate
alerts led to eight attempts and seven confirmed synthetic deliveries across
42 monitored trips. Two alerts were suppressed by delivery cooldown; one
transient failure led to a successful retry. Four of 18 actionable delayed trips
received a useful delivery. Conditional useful-first precision is 4/6; one of
seven first deliveries has an unresolved target. Across all first deliveries,
usefulness is bounded by 4/7 to 5/7. These are identification bounds, not confidence
intervals, and selection still reuses the known July development cohort.

P07 adds 35 canonical episodes, nine alerts and eight delivery-attempt rows to
the inherited P06 canonical records. Its successful delivered-repeat counts are
zero; P06's two repeated candidates remain distinct historical evidence. The
simulated July transport clocks and actual November output-release clocks have
separate provenance domains. Recipient opening, adoption, operator actions and
follow-up remain absent from that historical P07 store. The [P08 protocol](p08_evaluation_protocol.md) is frozen,
and P08 has now executed it under a separate registered implementation, with results below. Complete M4.2 acceptance is not claimed.

For policy development and formal outputs, start with the [P06 results](../reports/p06/README.md), [frozen protocol](p06_alert_policy.md), [all threshold trials](../reports/p06/policy_trials.json), [selection record](../reports/p06/selection_manifest.json) and [typed-store checks](../reports/p06/store/verification.json). The 0.55 threshold produces nine candidates across 42 monitored trips and captures 4/18 actionable delayed trips. First-candidate precision is 4/6 conditional on eligible targets; one of seven first candidates is unresolved. The 50-per-100-trip budget and two-per-tenant-day cap are hypothetical research settings, not measured capacity. Selection and reporting reuse the known July assessment outcomes, so they are not independent final-policy performance.

The historical P06 store contains three model versions, 341 memberships, 341 predictions, one rule version and 341 offline releases. Its candidate/episode audits remain auxiliary and its six workflow schemas remain empty in that retained store. P07 subsequently promotes the three delivery-related contracts in a new store, as described above. Historical M2 and P06 count columns and execution receipts remain unchanged; the P07 column identifies the later store separately.

For the modeling decision, start with the [comparison receipt](../reports/m3b3/run_receipt.json), [cohort metrics](../reports/m3b3/metrics.json), [slice results](../reports/m3b3/slices.csv) and [selection record](../reports/m3b3/selection.json). All methods are compared on training-period validation opportunities; the common anchor is the set the baseline can score. Availability on all opportunities is reported separately. The selected candidate is an input to later calibration, not a dispatch policy.

For delay classification, the [P04 results](../reports/m3c/README.md), [raw-probability metrics](../reports/m3c/metrics.json), [reliability bins](../reports/m3c/reliability.csv) and [independent checks](../reports/m3c/independent_checks.json) cover the same 5,163 training-period opportunities per method. The independent logistic classifier and fold/tenant prevalence baseline are scored on 2,358 mature snapshots from 511 trips; 2,805 excluded-label opportunities remain visible. Labels use the promise known at the snapshot, with a strict gate-arrival-after-deadline rule and no grace. The deadline-advance slices measure time from snapshot to that promise deadline, not time from an actually delivered alert. These historical files establish raw classification behavior and probability diagnostics. P05 subsequently adds calibration evidence; P06 adds the experimental threshold and separate formal outputs described above.

For calibration, read the [P05 results](../reports/m3d/README.md), [frozen protocol](m3d_calibration.md), [fit states](../reports/m3d/calibrators.json), [assessment metrics](../reports/m3d/metrics.json) and [independent checks](../reports/m3d/independent_checks.json). The base models use original training trips and a July 1 knowledge cutoff; early July trips fit calibrators, a buffer separates roles, and later July trips assess them. Assessment retains 341 opportunities and scores 143 snapshots from 29 trips. Whole-trip interval coverage is 19/29 (65.5%), below the nominal 80%; probability calibration worsens Brier score and log loss. Supported fitting does not establish quality improvement. Unsupported calibrators retain explicitly uncalibrated raw outputs. This assessment cohort is reused for P06 policy development, not independent final-policy testing. August–September values remained reserved at P05; P08 later evaluated them without fitting or reselection.

The [original field requirements](../contracts/requirement_traceability.csv), [table plan](../contracts/table_delivery_register.csv) and [case specifications](../contracts/validation_cases_reference.json) deliberately retain their planning-time statuses. Read those registers for the specification and the three current indexes for observed implementation and evidence.

## Synthetic recipient and operator workflow

P09 extends only the fixed primary S00 population: 400 trips, 3,200 opportunities,
63 alerts, 59 transport attempts and 42 confirmed successful deliveries. It does
not pool the stress scenarios or modify forecasts, final targets, model states,
the 0.55 policy or the original scientific registration.

The [workflow specification](p09_workflow.md) explains the separate delivery,
opening, adoption, action and follow-up clocks. [Saved results and
denominators](../workflows/p09/evidence/README.md) distinguish canonical rows from
known outcomes. Only mature, confirmed, source-linked arrivals enter the original
versus revised promise comparison. A future verified source revision cannot
replace a pending version that was known at the follow-up endpoint. Synthetic
handling time is input effort, not labor saved; changing a deadline is not an
arrival improvement.

The [typed store](../workflows/p09/evidence/store_verification.json), [independent
reference](../workflows/p09/evidence/independent_checks.json), [engineering
verification](../workflows/p09/evidence/verification.json), [tests](../workflows/p09/evidence/unittest_results.txt)
and [repeatability receipt](../workflows/p09/evidence/repeatability.json) retain the
implemented checks separately from acceptance claims. P09 has its own source
registration under `workflows/p09/`; all earlier scientific source and report
inventories remain unchanged.

The self-contained [trip viewer](../workflows/p09/evidence/viewer.html) includes
trips with no alert and alerts with no successful delivery. Download or open the
file locally to use it. It exposes retained lineage and labels retrospective
outcomes separately from contemporaneous dispatch evidence. It is not a live
service, human adoption study or deployment approval. G14/G15/G16 remain open.

## Fixed final evaluation

This is an engineering and method demonstration on fixed synthetic trajectories, not
customer telemetry or a fleet trial. G14 (July interval undercoverage/width), G15 (July
probability-calibration deterioration) and G16 (low July policy recall and selection
optimism) remain open. A final measurement does not itself resolve their causes or
establish acceptable costs, precision, recall or latency. `quality_accepted` and
`operational_approval` remain false.

P08 completes the registered final evaluation on 400 original test trips and 3,200
opportunities per scenario. [All nine results](../reports/p08/README.md) remain visible;
S00 is primary. Its whole-trip interval coverage is 243/328 (74.1%), delivered
actionable-trip recall 26/137 (19.0%), and conditional useful-first precision 26/38
(68.4%). 4 of 42 first successful deliveries retain unresolved targets. Unknown outcomes
remain outside eligible-loss denominators and inside opportunity/burden denominators.

The [unit receipt](../reports/p08/unittest_results.txt) records 727 passing tests;
[independent checks](../reports/p08/independent_checks.json) record 10,349,858
comparisons without failure. [Retained replay](../reports/p08/retained_replay.json)
checks all nine scenarios and their 27-table stores. The new P08 count columns refer
only to the primary store and preserve historical counts. At P08, twenty-two field mappings
were offline-materialized and ten schema-only; final scoring does not invent opening,
adoption or action observations. All 110 requirement IDs, 30 table rows and 14
validation IDs are retained. Partially tested operational scenarios remain partial.

Original holdout support is 325/37/34/4 trips. Descriptive trip-bootstrap intervals do
not guarantee future-fleet performance; sparse/undefined results retain null reasons.
The [findings](p08_final_evaluation.md) distinguish engineering completion, remaining
quality limits and P09 workflow work. The frozen [P07/P08
specification](p08_evaluation_protocol.md) is preserved as historical preregistration
evidence.
