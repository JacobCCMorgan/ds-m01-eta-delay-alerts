# Synthetic adoption, action and follow-up workflow

P09 continues the fixed P08 primary scenario through recipient review, operator
actions and follow-up. It is an offline engineering demonstration using synthetic
trips and synthetic recipient behavior. It does not send notifications, observe
real dispatchers, retrain a model or estimate a causal fleet benefit.

## Parent and population

The parent is published commit `12b904696b87d90369d85dabeee01eadadc854cd`.
P09 uses `reports/p08/scenarios/S00_unmodified`: 400 trips, 3,200 prediction
opportunities, 63 alerts and 59 transport attempts. The transport result includes
42 confirmed successful deliveries. P09 does not pool the nine stress scenarios
or count repeated predictions as independent trips.

The original P08 reports, models, settings, scientific registration, targets and
metrics remain intact. The extension lives under `workflows/p09/`, including its
own settings, tests and evidence. That location keeps it outside the historical
source and report inventories sealed for P08. P09 binds its own complete source
and output inventories separately; the P08 freeze is not rewritten to admit new
work after final outcomes were exposed.

## Delivery is not adoption

The workflow starts from confirmed service deliveries whose receipts are known
by the observation cutoff. A failed attempt does not establish non-adoption,
and a successful retry does not create a second recipient decision for the same
alert. Recipient opening is retained as a separate synthetic audit observation;
the imported P08 notification rows remain unchanged.

An opening is not automatically an adoption decision. Accepted, partially
accepted, rejected, overridden and not-reviewed states remain distinct. Partial
adoption, rejection and override retain reasons. A not-reviewed record has no
invented decision time. Review and action behavior are determined without reading
the final arrival outcome; those rules are scenario assumptions, not estimated
human-response probabilities.

An action is a separate record linked to an adoption and alert. Contacting a
driver or customer, rescheduling, reassignment and an explicit no-action decision
are distinguishable. Handling time is simulated labor input, not measured labor
savings. Event clocks belong to the historical synthetic scenario; run and file
materialization clocks record the actual execution time.

## Follow-up preserves the original promise

Each observed follow-up retains the alert's original promise version separately
from any synthetic revised promise. The same retained arrival is compared with
both deadlines. Extending a deadline can reduce the delay measured against that
new promise while leaving the original-promise delay unchanged; it does not
demonstrate a faster arrival or an effective intervention.

Outcome versions must belong to the same tenant and trip and be known at the
follow-up cutoff. A missing eligible source outcome remains in the auxiliary
pending-observation audit. It cannot be replaced with an invented outcome ID to
populate a required canonical field. Mature known, disputed, unreported, open and
lost observations remain distinct, and unknown observations do not become zero
delay or negative outcomes.

Some retained source records have a pending version available during the 24-hour
follow-up window, while the verified arrival version becomes known roughly 20 days
later. P09 selects the version known at the observation endpoint. A verified label
visible to a later analyst cannot be moved back into an earlier follow-up record.
The resulting `not_reported` states are part of the evidence, not a reason to widen
the window after seeing the results.

## Reading the trip viewer

The viewer is a self-contained, read-only HTML file. It includes the full primary
trip population, including trips with no alert and alerts without a successful
delivery. Tenant and trip filters lead to a timeline of predictions, original
promise versions, transport attempts, recipient observations, review, actions and
follow-up. Model and evidence identifiers remain available for tracing a case.

Retrospective outcome information is labeled separately from what dispatch could
know at prediction time. The viewer is a way to inspect retained evidence, not a
live dispatch application, production integration or human adoption record.

Automated tests check the viewer's retained data and JavaScript interactions.
Actual browser rendering and visual review remain pending; open the saved HTML
and inspect a normal trip, a failed delivery and an unresolved follow-up before
accepting the viewer for use.

## Reproduce and inspect the saved workflow

Use the repository's Python environment with the locked model dependencies already
installed. The extension does not rerun training or alter the saved P08 reports.
From the repository root, check the registered parent, run the dedicated tests,
then replay and independently audit the retained workflow:

```bash
python -I -B workflows/p09/cli.py check-parent --root .
python -I -B workflows/p09/test_suite.py
python -I -B workflows/p09/cli.py replay --root . --report workflows/p09/evidence
python -I -B workflows/p09/cli.py audit --root . --report workflows/p09/evidence
```

To reproduce the workflow into a fresh directory outside the checkout:

```bash
P09_CHECK_PARENT="$(mktemp -d "${TMPDIR:-/tmp}/ds-m01-p09.XXXXXX")"
python -I -B workflows/p09/cli.py run --root . --output "$P09_CHECK_PARENT/run"
```

Open `workflows/p09/evidence/viewer.html`, or the `viewer.html` in the new output
directory, in a browser. The HTML embeds its own data and needs no server. A
GitHub file page displays its source; download the file or open the checked-out
copy to use the filters. The [saved evidence](../workflows/p09/evidence/README.md)
links the metrics, independent audit, format checks and execution receipts.

The workflow's event clocks are synthetic 2026 trip times. Registration, execution
and file-generation timestamps record the actual later run. Reproduction must use
a fresh output directory; it does not overwrite the retained evidence.

## Scope of acceptance

P09 can demonstrate that the workflow and its typed outputs reproduce, preserve
lineage and distinguish missing observations. It cannot accept the ETA model or
authorize operations. P08 primary whole-trip interval coverage remains 243/328
(74.1%), with zero covered trips out of 33 on the unseen route. G14, G15 and G16
remain open; `quality_accepted` and `operational_approval` remain false.

Native PostgreSQL and service integration, continuous integration, and operational
acceptance remain separate later tasks. Historical receipts retain their original
test counts and execution dates.
