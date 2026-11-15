# P09 synthetic workflow results

This retained run extends the fixed P08 primary scenario through recipient review,
adoption or override, operator action and follow-up. All behavior is synthetic and
offline. No notification was sent, no real recipient behavior was observed, and no
causal arrival benefit, model acceptance or operational approval is claimed.

The parent is `12b904696b87d90369d85dabeee01eadadc854cd`. Original P08 source,
models, calibration, the 0.55 policy, predictions, targets, scientific registration
and all nine scenario results remain unchanged. P09 uses only `S00_unmodified`;
the stress scenarios are not pooled into its denominators. This extension was
developed after P08 final outcomes were known, not as a new untouched final test.

## Population and review

| Measure | Count or ratio |
| --- | ---: |
| Primary trips / prediction opportunities | 400 / 3,200 |
| Candidate alerts / transport attempts | 63 / 59 |
| Confirmed delivered alerts / non-delivered alerts | 42 / 21 |
| Synthetic opened / reviewed / not-reviewed alerts | 40 / 33 / 9 |
| Accepted / partially accepted / overridden / rejected | 18 / 7 / 6 / 2 |
| Accepted or partial among all confirmed deliveries | 25/42 (59.5%) |
| Accepted or partial among reviewed alerts | 25/33 (75.8%) |

These are fixed scenario frequencies, not estimated human adoption rates.
Overrides remain separate from accepted or partially accepted decisions. A failed
attempt is not rejection; retries do not duplicate decisions for the same alert.
An opening is not review, adoption or action. Not-reviewed states have no invented
decision timestamp. Review and action choices use delivery identity hashes without
reading arrival outcomes.

## Actions and follow-up

| Action type | Records |
| --- | ---: |
| Contact driver | 6 |
| Contact customer | 5 |
| Reassign | 16 |
| Reschedule | 4 |
| Explicit no action | 2 |
| Total records / executed actions | 33 / 31 |

The simulated handling input totals 12,074 seconds across the 33 records. This is
not measured dispatcher effort or labor saved. Four reschedules create separate
promise versions without altering the original alert promise or retained arrival.

| Follow-up state | Action records |
| --- | ---: |
| Mature confirmed arrival comparison | 1 |
| Not reported | 21 |
| Lost | 5 |
| Disputed | 3 |
| Rejected follow-up observation | 1 |
| No action, auxiliary audit only | 2 |

There are **31 canonical follow-up rows but only one comparable mature arrival**.
The other 30 executed actions have no comparable arrival. They are not zero-delay
observations or successful interventions. Canonical rows reference real retained
outcome-version identities; a missing eligible source version would remain in the
auxiliary audit instead of receiving an invented outcome ID.

Seventeen not-reported cases have only a pending source version known within the
24-hour follow-up window. Their verified revisions become known roughly 20 days
later and cannot be used early. The fixed rules also retain distinct disputed,
lost and rejected observations. No window was widened to manufacture more mature
outcomes after these results appeared.

For the single comparable arrival, original-promise and comparison-promise delay
are both 5,251 seconds. The observed deadline-definition difference is zero.
Neither this point comparison nor the synthetic reschedules estimate an
intervention effect. The same arrival must remain fixed when comparing deadlines.

## Typed evidence and checks

The store has 35 typed tables and 32,128 logical rows. Eleven canonical tables
contain output records, including the new `eta_adoption`, `eta_action` and
`eta_action_followup` contracts. The 24 other inherited P08 tables are preserved;
the original P08 store still retains its three empty downstream contracts. Opening
is retained in `p09_opening_audit`; imported `received_or_opened_at` fields remain
null. The combined historical M2 warehouse is not rebuilt.

| Engineering check | Observed result |
| --- | --- |
| Original / portability / P09 unit suites | 727 / 17 / 115 passed |
| Independent workflow reference | 26,524 comparisons, zero failures |
| JSONL, DuckDB and Parquet reconstruction | 96,384 format-row comparisons, zero differences |
| Two executions | Six core files byte-identical |
| Two-run logical-table comparison | 35 tables, 32,128 rows, zero differences |
| Viewer tests | Nine unit tests within the P09 suite; 18 offline JavaScript checks passed |
| Actual browser rendering and visual review | Pending |
| User's native Mac adoption validation | Pending |

Repeatability normalizes only `p09_run_audit.materialized_at`; all other logical
table values are compared exactly. Identical database binary files are not claimed.
The six byte-identical files are `simulation.json`, `metrics.json`,
`independent_checks.json`, `input_manifest.json`, `summary.json` and `viewer.html`.
Synthetic event clocks remain separate from actual registration and execution
timestamps. The dated P08 receipts retain their original counts and scope.

| Artifact | Purpose |
| --- | --- |
| [Metrics](metrics.json) | Full denominators, tenant counts and interpretation boundaries |
| [Simulation](simulation.json) | Synthetic decisions, actions, follow-up and event records |
| [Input manifest](input_manifest.json) | Bound parent inputs and their digests |
| [Independent checks](independent_checks.json) | Separate source, clock, lineage and arithmetic comparisons |
| [Store verification](store_verification.json) | Table counts, constraints and three-format checks |
| [Engineering verification](verification.json) | Test counts, read-only replay, repeatability and pending validation scope |
| [Repeatability](repeatability.json) | Two-run comparison and exact normalization boundary |
| [P09 unit log](unittest_results.txt) | Passing new-suite test names and count |
| [Run receipt](run_receipt.json) | Actual runtime, execution clocks and artifact hashes |
| [Trip viewer](viewer.html) | Self-contained, read-only inspection of all 400 trips |
| [Registration](../registration.json) and [settings](../settings.json) | Separate P09 source inventory and fixed workflow assumptions |

## Reproduce and inspect

From the repository root, using the environment with the locked dependencies:

```bash
python -I -B workflows/p09/cli.py check-parent --root .
python -I -B workflows/p09/test_suite.py
python -I -B workflows/p09/cli.py replay --root . --report workflows/p09/evidence
python -I -B workflows/p09/cli.py audit --root . --report workflows/p09/evidence
```

Create a fresh run outside the checkout:

```bash
P09_CHECK_PARENT="$(mktemp -d "${TMPDIR:-/tmp}/ds-m01-p09.XXXXXX")"
python -I -B workflows/p09/cli.py run --root . --output "$P09_CHECK_PARENT/run"
```

Download or open `viewer.html` locally. It embeds its data and requires no server.
Use the tenant, trip and state filters, then inspect a normal trip, a failed
delivery and an unresolved follow-up. Knowledge-time filtering separates evidence
available at a selected time from later outcomes. Actual browser rendering and
visual review have not yet been recorded; automated offline checks do not replace
that review.

## Remaining limits

P08 primary whole-trip interval coverage remains 243/328 (74.1%), with zero
covered trips out of 33 on the unseen route. G14/G15/G16 remain open.
`quality_accepted` and `operational_approval` remain false. This preparation receipt
does not establish native service integration, CI success, user publication or
formal acceptance.

See the [workflow specification](../../../docs/p09_workflow.md),
[current evidence index](../../../docs/evidence_index.md) and
[unchanged P08 results](../../../reports/p08/README.md).
