# Independent point-in-time cases

`point_in_time_case.json` is a manually specified timeline, not simulator output.
The baseline has a 09:00 UTC departure, a 09:55 position available at 09:56,
a 10:00 snapshot, an 11:00 gate arrival, 12:00 service and 13:00 completion.
The expected elapsed and remaining arrival times are independently 3,600
seconds; the 300-second position age follows directly from the written clock
times. An initial promise ends at 10:50, so the gate arrival is late.

Tests add isolated adversarial records: future-known promises, late positions,
future event clocks, retrospective assignments, cross-tenant ID collisions,
invalid positions, late outcome corrections, unknown/cancelled outcomes and
the repeated DST hour. They also check later-known status corrections and
unknown/withdrawn assignments rather than treating them as known mappings.
No generator, fitted model, or hidden simulator truth
is imported. The split dates are copied from the accepted execution contract;
the expected role for each boundary date is explicitly written in the test.

`test_build_invariants.py` extends the same written case into two trips and
runs the complete feature build in a temporary directory. The historical trip
was planned for 09:00, actually departed at 09:30 and arrived at 11:00, so its
history duration must be 5,400 seconds, not 7,200. A focal trip departs at noon.
Distinct provenance IDs make missing parents and availability earlier than a
parent observable. Date-only, interval and cutoff-equality outcomes must not
enter that focal snapshot's history.

Two additional integration cases retain a short trip cancelled 25 minutes
after departure when departure first becomes known 20 seconds late, and
verify that the selected known status update appears in snapshot lineage
while a future-available update does not. The short-trip snapshot starts at
the 20-second knowledge delay and retains a cancelled label with null targets.

These fixtures test the public point-in-time functions. They are not a claim
that the complete M1 or later project acceptance criteria have passed.
