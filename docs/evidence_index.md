# What has been demonstrated?

The warehouse has 17 populated synthetic tables. The ETA experiments produce additional files outside the warehouse: training-period fold assignments, model artifacts, prediction rows and a comparison on a common target. An experiment file does not populate a canonical table. There is no calibrated final-test result, operational delay probability, delivered alert or measured fleet outcome.

| Evidence index | What a row tells you |
|---|---|
| [110 original field requirements](../contracts/current_requirement_evidence.csv) | The original ID, field ID, canonical table/column, original condition and intended output; what actually exists; the source, test or receipt to examine; and the remaining boundary. |
| [28 required and 2 optional tables](../contracts/current_table_evidence.csv) | Whether the canonical table has rows, only a schema, a related experiment file, or was disabled. Row counts are the retained M2 warehouse counts. A blank count means the optional table was never instantiated. |
| [14 validation scenarios](../contracts/current_validation_evidence.csv) | Which behaviors were exercised, where specific tests passed, whether only part of the scenario was tested, and which cases remain unexecuted. |

Of the 110 field mappings, 60 have a synthetic data-stage implementation and two have partial source semantics: a version attribute does not demonstrate multi-revision telemetry, and a configured synthetic receipt clock does not demonstrate an external acquisition. Twelve requirements have related experiment outputs but no populated canonical rows. Twenty have only a required schema; another 16 belong to disabled forecast or routing contracts. These counts describe evidence types, not a completion percentage. For nullable fields, a populated table is not a claim that every row has a value. Two requirements can validly reference the same column, such as origin and destination latitude through different facility joins.

The four fully exercised data-behavior cases cover target states, gate-arrival semantics, split grouping and restricted history. Five cases have partial evidence, one has separately tested components, two are planned and two depend on disabled optional inputs. An individual test is named only when its definition and its recorded passing result can be located. Those historical receipts are not new test runs. See [known issues](known_issues.md) for inputs and failure modes outside the retained case.

For the modeling decision, start with the [comparison receipt](../reports/m3b3/run_receipt.json), [cohort metrics](../reports/m3b3/metrics.json), [slice results](../reports/m3b3/slices.csv) and [selection record](../reports/m3b3/selection.json). All methods are compared on training-period validation opportunities; the common anchor is the set the baseline can score. Availability on all opportunities is reported separately. The selected candidate is an input to later calibration, not a dispatch policy.

The [original field requirements](../contracts/requirement_traceability.csv), [table plan](../contracts/table_delivery_register.csv) and [case specifications](../contracts/validation_cases_reference.json) deliberately retain their planning-time statuses. Read those registers for the specification and the three current indexes for observed implementation and evidence.
