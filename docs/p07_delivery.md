# Synthetic notification delivery and the final-evaluation freeze

P07 implements the delivery part of M4.2: turn the fixed P06 candidates into simulated alert attempts, confirmed service deliveries and bounded failure states. It also freezes the [P08 evaluation specification](p08_evaluation_protocol.md) before final-test outcomes are used. Adoption, dispatcher actions, follow-up and the viewer remain P09 work; this phase does not complete all of M4.2.

The configuration is [config/p07.json](../config/p07.json), based on P06 commit `75a9a24bc4d3b02a667db64b4696b2b9bc4856da`. P05/P06 July outcomes are already known. The transport assumptions below were fixed on November 11, 2026, before the P07 delivery simulation; they were not estimated from July delivery observations or final outcomes. They do not describe a real messaging service.

## Fixed inputs and transport assumptions

Keep the P06 0.55 policy, 30-minute minimum candidate notice, 30-minute candidate suppression and two-candidate tenant/day cap. Reuse the retained 341 July opportunities, 42 trips, selected-rule candidates and risk episodes. Do not rerun threshold selection to compensate for transport loss or change the P05 models and calibrators.

| Transport parameter | Frozen research setting |
|---|---|
| Seed / version | `DS-M01-P07-20261111` / `p07-delivery-v1` |
| Maximum attempts per candidate | 3 |
| Backoff after confirmed transient failure | 30 seconds, then 120 seconds |
| Candidate lifetime | 600 seconds from simulated creation |
| Unknown-attempt timeout | 120 seconds; unresolved and terminal, with no retry |
| Per-attempt outcome probabilities | 80% delivered; 15% transient failure; 3% permanent failure; 2% unknown |
| Service latency | Uniform integer 5–60 seconds, inclusive |
| Receipt knowledge lag | Uniform integer 0–15 seconds, inclusive |
| Successful-delivery cooldown | 1,800 seconds per tenant/trip, across episode and promise changes |
| July receipt accounting cutoff | August 1, 2026, `07:00:00Z`, inclusive for receipt knowledge |

For each attempt and dimension, hash the compact UTF-8 JSON array
`[seed, version, tenant_id, candidate_id, attempt_index, dimension]` with SHA256.
Interpret the first eight bytes as an unsigned big-endian integer and reduce
modulo the dimension's domain size. Outcome, latency and receipt lag use
separate dimension keys. Outcome bins follow delivered, transient failure,
permanent failure, unknown, in that order. Draws depend on identities and the
fixed settings, never on targets, arrival time, model error or whether a
delivery would be useful.

## Time, retries and idempotency

Use one chronological event stream. Equal-time priority is known episode
closure, receipt, candidate, dispatch, then service; tenant, trip, candidate
and attempt identities break remaining ties. This makes a same-clock closure
or known successful receipt visible before a dispatch decision.
Priority applies to events already queued at that clock; a zero-lag receipt
created by a service event still occurs after its cause.

At dispatch and service, require at least 1,800 seconds before the candidate's
original frozen deadline, an episode that is not known closed, time no later
than creation plus the 600-second lifetime, and at least 1,800 seconds since
the previous successful service delivery for that tenant/trip. Equality passes.
A new promise or episode never resets cooldown. A missing feed censors risk;
it does not establish that the trip arrived or risk resolved.

Only a confirmed transient failure permits another attempt, scheduled after
its receipt and the corresponding backoff. Permanent failure stops. Unknown
transport stops unresolved after timeout; absence of confirmation is not proof
of nondelivery. Stop after a successful service delivery. The attempt limit,
lifetime, known closure, notice gate and cooldown all remain in force during
retries. There is no retry-until-success loop.

Each candidate maps to one alert identity; each attempt has a separate delivery
identity. Reingesting an identical candidate, attempt or receipt is idempotent.
Conflicting content under the same identity is an error. A failed retry is not
a duplicate disturbance; only additional confirmed successful deliveries count
toward delivered repetition.

Keep simulated candidate, dispatch, service and receipt-known clocks separate
from actual offline computation and materialization timestamps. The inherited
P06 `eta_output_release` records retain their original actual November execution
times and `offline_replay` mode. Simulated July alert/transport clocks occupy a
different explicitly labelled time domain. Their apparent order relative to an
offline release timestamp is not evidence that messages were sent before the
software existed. Cross-record validation must use the correct clock domain.

## What delivery metrics mean

The first successful delivery for a tenant/trip is the earliest confirmed
service delivery, selected before looking at targets. A failed first candidate
does not force a later successful alert to inherit its promise. Score each
successful alert against its own original candidate snapshot and promise.

A useful delivery requires a mature eligible positive target, delivery before
actual arrival, and at least 1,800 seconds from service delivery to that frozen
promise deadline. Its receipt must be known at or before the accounting cutoff.
Candidate lead time is not substituted for delivered lead time.

Report useful-first-delivery precision as useful first successful deliveries
divided by all known-assessable first successful deliveries. This denominator
includes mature eligible targets and verified exact `already_arrived_at_snapshot`
targets, even though those post-arrival rows are excluded from binary modeling.
Known false, too-late and post-arrival first deliveries remain non-useful in that
denominator. A delivery with independently known insufficient prospective lead
is also known non-useful even when its delay label is unavailable. Only rows
that could still be useful but lack the required outcome evidence remain
unresolved; a non-useful timing classification does not fabricate a negative
binary target. This differs
from P06's positive-first-candidate precision. Show
unknown targets and target coverage alongside it; usefulness bounds across all
first deliveries include unresolved outcomes only in the upper bound. These
bounds are not confidence intervals.

Delivered actionable-trip recall uses the same label-defined actionable
positive-trip denominator as P06, including trips with unavailable forecasts,
blocked candidates or no successful delivery. A candidate with sufficient
initial notice but an unsuccessful or late transport is a miss. Every confirmed
successful delivery counts toward burden per 100 monitored trips, including
repeats and unknown-target deliveries. Attempt volume, failures, queued/unknown
receipts and successful deliveries are separate measures.

The P06 daily cap constrains candidate admissions. It is not automatically a
cap on network attempts or receipt-day delivery counts. No service-delivery
acknowledgment establishes recipient opening, adoption or action.

## Typed outputs and phase boundaries

P07 adds canonical alert episodes, alerts and notification-delivery records to
the five existing formal output contracts. Auxiliary event ledgers must retain
attempt timing, reason, receipt knowledge, synthetic source and cutoff state.
The adoption, action and action-follow-up tables remain reserved. Numeric counts
and successfully exercised behavior belong to the eventual run and independent
verification receipts, not this prospective specification.

Publish only fresh, validated outputs. Preserve source/model/policy hashes,
actual run clocks, typed key/relationship checks, replay and independent boundary
fixtures. Unknown or not-yet-known receipt states stay explicit; do not invent
opening records to fill nullable fields or claim retries prove adoption.

## P08 is a separate evaluation stage

[config/p08_evaluation.json](../config/p08_evaluation.json) fixes the final
population, targets, forecast and policy versions, delivery scenarios, metrics,
denominators, uncertainty and stress transformations. The final inference and
evaluation code is not implemented merely by writing that specification.
P08 must register its implementation and passing conformance tests before
opening final outcomes, then retain predictions and transport ledgers before
joining those outcomes.

Two dated records separate specification from validated readiness. The protocol
freeze records the configurations, existing model states and source pins before
any P07 simulation. The later evaluation-readiness record binds the validated
delivery implementation and P07 run evidence to that same specification, while
explicitly acknowledging that July delivery results have then been observed.
Neither record substitutes for the future P08 evaluator's pre-target conformance
checks.

P05 undercoverage and worse probability losses, plus P06's low actionable recall,
remain open quality limitations. A reproducible transport simulation does not
resolve them. No real notification, fleet adoption, labor saving, causal delay
reduction, operational approval or complete M4.2 workflow is claimed.
