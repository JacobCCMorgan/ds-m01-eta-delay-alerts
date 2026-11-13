# Synthetic delivery of the frozen July alert policy

P07 adds the delivery portion of M4.2 to the retained P06 policy-development
experiment. It simulates transport attempts and confirmed service deliveries;
it sends no notification and does not simulate recipient opening, adoption or
operator action. The P05 models/calibrators and P06 threshold remain unchanged.
This is not an independent policy test or operational approval.

The [delivery protocol](../../docs/p07_delivery.md) and
[configuration](../../config/p07.json) were frozen before the first simulation.
July assessment outcomes were already known when this specification was written.
The transport probabilities, delays, retry limits and cooldown are hypothetical
research assumptions, not measurements from a messaging service.

## Observed July scenario

The complete population remains 341 opportunities from 42 monitored trips.
There are 143 mature eligible snapshots from 29 trips and 198 excluded-label
opportunities. Eighteen trips have at least one actionable positive target;
trips without an admitted or delivered alert remain in that denominator.

| Measure | Result |
|---|---:|
| Risk episodes / candidate alerts | 35 / 9 |
| Delivery-cooldown-suppressed alerts | 2 |
| Transport attempts / retry attempts | 8 / 1 |
| Confirmed successful deliveries / delivered trips | 7 / 7 |
| Transient failed attempts | 1 |
| Unknown or queued attempts at the cutoff | 0 / 0 |
| Additional deliveries within an episode | 0 |
| Useful deliveries / actionable positive trips | 4 / 18 (22.2%) |
| Useful / known-assessable first successful deliveries | 4 / 6 (66.7%) |
| Unresolved targets among first successful deliveries | 1 of 7 |
| Successful deliveries per 100 monitored trips | 16.67 |

One transient failure was followed by a successful retry. Failed attempts are
counted as transport work, not delivered duplicates. The two suppressed alerts
have canonical alert rows but no transport attempt. No permanent or unknown
transport outcome occurred in this observed run; boundary fixtures separately
exercise those branches. The unresolved first-delivery target is distinct from
unknown transport: its service delivery is confirmed, but its usefulness cannot
be resolved from the retained target evidence.

A first success is chosen chronologically for each tenant/trip before scoring.
It uses that alert's original snapshot promise, even if an earlier candidate
failed. A useful delivery requires an eligible positive target, service delivery
strictly before actual arrival and at least 1,800 seconds before the original
deadline, with receipt knowledge at or before the accounting cutoff. Targets
must be known strictly before August 1 at `07:00:00Z`; transport receipt knowledge
is inclusive at the same clock. These two boundary conventions are intentional.

Known-assessable precision includes independently known non-useful timing
outcomes as well as mature eligible labels; unresolved first deliveries are
excluded from that conditional denominator. Across all seven first successes,
usefulness is bounded by 4/7 to 5/7 (57.1%–71.4%). Those are identification
bounds, not confidence intervals. This denominator differs from P06's
eligible-target first-candidate precision even though both observed fractions
happen to be 4/6 in this run.

Median candidate-to-success time was 28 seconds (range 6–74), including any
failed attempt and retry wait. Median service latency for the successful attempt
alone was 24 seconds. The minimum prospective deadline lead at service was
2,730.324138 seconds; candidate lead is not substituted for delivery lead.
Retrospective arrival lead is available for only six of the seven successes.
Full timestamps, classifications and denominators are in [metrics.json](metrics.json).

| Tenant | Monitored trips | Successful deliveries | Useful / actionable trips | Useful / assessable first successes | Unresolved first targets |
|---|---:|---:|---:|---:|---:|
| T01 | 17 | 2 | 1 / 7 | 1 / 2 | 0 |
| T02 | 13 | 2 | 0 / 3 | 0 / 1 | 1 |
| T03 | 12 | 3 | 3 / 8 | 3 / 3 | 0 |

All tenant slices have fewer than 20 monitored trips. They are descriptive;
T03's three assessable first deliveries do not establish reliable perfect
precision. Shared tenant/day capacity and trip state also prevent interpreting
these retained policy trajectories as independent-trip performance samples.

## What is materialized?

The separate typed store contains eight populated canonical tables and three
reserved canonical tables. It retains all P06 canonical and auxiliary rows
without rebuilding the historical M2 warehouse.

| Canonical table | Rows | Meaning |
|---|---:|---|
| `eta_model_version` | 3 | Inherited frozen model identities |
| `eta_evaluation_membership` | 341 | Every retained July opportunity |
| `eta_prediction` | 341 | Inherited canonical forecasts |
| `eta_alert_rule_version` | 1 | The fixed P06 rule |
| `eta_output_release` | 341 | Inherited actual offline-release records |
| `eta_alert_episode` | 35 | Risk episodes with synthetic delivered-repeat counts |
| `eta_alert` | 9 | Seven delivered and two suppressed candidate-linked alerts |
| `eta_notification_delivery` | 8 | Seven delivered and one failed attempt |
| `eta_adoption`, `eta_action`, `eta_action_followup` | 0 each | Future workflow evidence |

Fifteen auxiliary tables retain policy inputs and P07 alerts, attempts, episodes,
events, provenance and run metadata. All 26 tables are checked across JSONL,
DuckDB and Parquet. `received_or_opened_at` is null in all eight notification
rows: a synthetic service receipt does not establish recipient opening.

The inherited `eta_output_release` rows preserve P06's actual November execution
clocks and `offline_replay` mode. New alert, dispatch, service and receipt-known
clocks belong to the explicitly labelled synthetic July scenario. Actual P07
materialization clocks remain separate. These are different provenance domains,
not a claim that real messages preceded the software run.

## Verification

The reference environment passed [576 unit tests](unittest_results.txt) with no
skips. [Retained replay](retained_replay_verification.json) and
[source-assisted replay](source_replay_verification.json) each found zero
differences, including 9,093 logical row comparisons across the 26 JSONL,
DuckDB and Parquet tables. The [independent reference](independent_checks.json)
performed 9,226 comparisons with zero failures and zero numeric difference;
it uses no production imports and does not reopen raw outcome sources.

[Two fresh executions](reference_repeatability.json) reproduced eight core files
byte-for-byte. All 3,031 rows across 26 tables matched after normalizing only
actual P07 materialization clocks and provenance recording clocks. Scenario
clocks, inherited P06 clocks, identities, values and source digests remained
exact. This does not assert database-file or Parquet-file byte identity.

The [engineering receipt](verification.json) and [store checks](store/verification.json)
retain these scopes. They establish implementation behavior, not policy quality,
user-machine execution, a GitHub Actions run or P07 publication.

## Reproduce the retained experiment

Use the environment installed by the [repository quick start](../../README.md#check-a-saved-forecast-run).
These commands use retained July labels and perform no fitting or notification:

```bash
python -m ds_m01.delivery_replay --root . --report reports/p07
python -m ds_m01.delivery_run --root . --output reports/p07_local
python scripts/check_delivery_simulation.py --root . --report reports/p07 --output /tmp/p07_independent_checks.json
```

Choose fresh output paths; existing reports and audit receipts are not replaced.
The replay should report `status: passed`, `differences: 0`, eight canonical
tables, three reserved tables, 341 opportunities, nine candidate alerts,
35 risk episodes and eight attempts. `july_source_targets_verified` is false
for a retained-only replay; saved target evidence is checked, but the raw source
is not independently reopened by that mode.

For source-assisted reconstruction, point `--input-root` at the frozen model-input
root used by the [P06 source replay](../p06/README.md). That root must contain the
exact files pinned in [config/p07.json](../../config/p07.json).

```bash
python -m ds_m01.delivery_replay --root . --report reports/p07 --input-root /path/to/frozen_input_root
python -m unittest discover -s tests -v
```

The source-assisted check reconstructs July source context/targets and then
replays the retained policy and transport. It does not independently reaudit
all original training labels, refit models or calibrators, select another rule,
or score August–September targets.

## What remains open?

The P05 whole-trip coverage target was not met, probability losses worsened,
and P06/P07 actionable recall is only 4/18. These quality limitations remain
open. Reproducible transport does not repair the forecasts, establish independent
policy quality or demonstrate saved labor, avoided delay or fleet adoption.

The [P08 specification](../../docs/p08_evaluation_protocol.md) fixes final cohorts,
models, calibrators, policy, transport, metrics and nine stress cases. The
[pre-simulation freeze](../../reproducibility/p07/protocol_freeze.json) pins that
specification. The later [evaluation-readiness record](../../reproducibility/p07/evaluation_readiness.json)
binds the validated delivery implementation and this July run while acknowledging
that July delivery results have been observed. P08's new inference/evaluation
code still requires its own passing conformance tests and code-hash registration
before accessing final outcomes. P07 does not complete P08 or all of M4.2;
adoption, actions, follow-up and the viewer remain later work.
