# Synthetic fleet source generator

This is an original, fictional engineering dataset. It is not Rollingtrans
customer telemetry, a real fleet operating record, or evidence of real-world
forecast performance. All business source rows have `origin_type=synthetic`
and `availability_basis=simulated` in their linked provenance record.

## Reproducible design and subset

`generate(out_dir, config)` requires `generated_at`, the actual UTC execution
timestamp supplied by the caller. `seed` defaults to 20261021 and `namespace`
defaults to `ds-m01-2026-v1`. The substream seed is the first 16 bytes, interpreted
as an unsigned big-endian integer, of SHA256 over UTF-8
`'\x1f'.join(map(str, (seed, namespace, *tokens)))`.

The complete design contains 200 trips in each month January–September 2026,
1,800 total. The 120-trip trial selects zero-based global ordinals
`0, 15, ..., 1785`. Per-trip named streams and a complete design built before
selection preserve trial trip identities and source values when expanding to
1,800. `sample_size=120` selects the trial; `1800` or `0` selects the full design.
Changing the actual `generated_at` changes provenance acquisition metadata,
not the synthetic business events.

Entities are 3 tenants, 30 vehicles and 36 devices. Vehicles use `Txx-V01..10`,
devices `Txx-D01..12`. A deterministic vehicle permutation in every monthly
30-trip block avoids the stride-15 sampling artefact that would otherwise
select only two vehicles. Each vehicle has at most seven scheduled trips in a
month and at most one trip on a given day. Physical trip non-overlap is checked
against the entire 1,800-trip design before any rows are written.

There are 12 fictional California sites and 12 directed route segments,
four per tenant. IDs are `SITE-01..12` and `ROUTE-01..12`, with stable `-V1`
version IDs. Each tenant's four segments form a directed loop.

## Explicit route-geometry execution assumption

The source contract describes `planned_distance_km` as distance along a route,
not an arbitrary displacement. For the M1 and M2 dataset versions, each fictional route
consists of one directed straight geographical segment; distance is the
haversine length of that synthetic segment. Thus geometry length and planned
distance agree. These segments are **not real roads**, truck-approved paths,
commercial routing responses, or evidence of road travel times. This is a
documented simulation assumption, not a claim to have obtained road-network
data. A later routed-network experiment must use a new dataset/route version.

Motion duration uses planned segment distance, a 58 km/h nominal scenario
speed, a fixed handling component, per-trip variation and explicit delay
fixtures. This is not a fitted traffic model. GNSS positions follow noisy
interpolation of the fictional segment with a small smooth progress variation;
position noise and transport/availability delays use a separate random stream.
GNSS speed is a noisy scenario observation, not an ECU speed measurement.
ECU distance, runtime, RPM and counter epoch remain NULL/not_applicable.

## Independent gate source and outcome versions

The simulated gate process has its own named random stream. Gate arrival is
physical motion completion plus an independently sampled 3–24 minute gate
queue. It is not inferred from the GNSS points, a geofence, a model prediction,
or an ETA feature. Service starts 6–35 minutes after gate arrival and delivery
completes a further 20–65 minutes later. These three targets remain distinct.

Every trip first has a pending outcome known five minutes after departure;
there is no arrival label in that row. Normal verified outcomes become
available 48 hours after arrival. The gate status message is available 30
seconds after arrival. The `arrival_report_late` fixture withholds both gate
status and verified label until 20 days after arrival, exercising pending
labels at earlier fitting/calibration cutoffs. This deliberate stress-case
latency is not a claim about the usual reporting behaviour of a fleet.

For `outcome_revision`, the first verified record has a gate time five minutes
too early. Seven days after arrival a new immutable outcome version corrects
it to the independent simulated gate truth. The pending, initial verified and
corrected rows remain separate with explicit supersession links.

`unfinished` means source follow-up is incomplete: position reporting ends
within 75 minutes and no verified arrival evidence is released. The physical
vehicle may complete its journey and operate later trips, but that hidden
truth is not an available target. This avoids depicting a vehicle as still
driving an old trip while simultaneously starting later ones. Cancellation is
reported after departure; cancelled trips have no arrival target.

The outcome extraction cutoff is exclusive `2026-10-15T07:00:00Z` by default.
Consumers must apply availability and cutoff rules, rather than selecting the
latest record without a time condition. The real `generated_at/retrieved_at`
timestamp is never substituted for simulated historical availability, nor is
simulated availability presented as a real past receipt log.

## Device-history consistency

Device swap fixtures change to tenant spare `D11` halfway through the trip and
return to the original device 15 minutes after arrival. Same-day swap
appointments are staggered, and spare-device non-overlap is checked against the
full design. `D12` remains an unused spare; its existence does not imply a
deployment or a telemetry stream.

Assignments maintain both valid and known intervals. An initially open
assignment is superseded when the switch is reported two minutes later; a
closed historical version and the new assignment are then available. Before
that report, the new relationship is unavailable. It must not be backfilled
into an earlier snapshot. Full shared assignment history is emitted in both
trial and full builds, ensuring identical histories across builds.

## Eleven fixture families

The zero-based global ordinal modulo 11 chooses one family: normal,
late_arrival, cancelled, gps_dropout, late_gps, promise_revision, device_swap,
arrival_report_late, outcome_revision, unfinished, invalid_position.

- Late arrival adds 60–120 minutes of physical travel delay.
- GPS dropout removes observations during minute 30–89, at most 60 minutes.
- Late GPS delays minute 15–74 observations by an additional 40 minutes.
- Promise revision creates a new, later promise known halfway through motion
  or 35 minutes after departure, whichever is earlier; the original promise
  remains available as its own version.
- Invalid position supplies numerically plausible but explicitly invalid GNSS
  fixes during minute 25–54. Range checks alone must not accept these points.

The fixture schedule is deliberately controlled for coverage. It is not an
estimate of the prevalence of these events in a real fleet. Routes and vehicle
holdouts are generated normally; generation does not exclude their data.

## Provenance and validation isolation

Each emitted raw row has a provenance record with a stable canonical key and
relative raw-file line reference. Core tables without a provenance column are
linked through `provenance_record.canonical_record_key`; no noncanonical fields
are added to those rows. NULL source values have explicit state/reason entries.
The uniformly synthetic raw records use a row-level origin declaration;
`field_origins` is an empty object because there are no mixed-origin fields.
The `retrieved_at` field records the caller's actual execution timestamp.

`validation/latent_truth.jsonl` and `validation/scenario_registry.json` are
validation-only artifacts. The raw provenance scenario identifier supports
auditing but is also forbidden as a model feature. The generator does not
produce snapshots, training labels, split assignments, predictions, fitted
models, evaluation scores, or downstream-derived provenance. Those must come
from the separately executed pipeline. Simulated verification does not mean
external expert review or real customer adoption.
