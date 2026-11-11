# Signed opening decisions

`RunnerOpeningReleaseFact.v1` uses the existing RunnerFact stream, sequence,
signing header and outbox. RunnerFact contract revision 8 adds the kind to the
closed union. The capability must explicitly declare the kind's `risk` projector
and `runner_fact_contracts.risk.opening_release="v1"`; older capabilities keep
their existing shape. This flag requires the current terminal and valuation
checkpoint contracts and is independent of order lineage and fill correction.

Each decision binds a non-nil release ID, the signed release command event ID
and the frozen basis digest. Its exact nonnegative decimal, currency and UTC
observation timestamp must match any coherent inventory equity binding. The
inventory retains the stream's scope and capability identity. Event identity is
derived from the release ID and outcome, so refusal retries keep their existing
identity. The receiver must still authenticate the command and recheck its
account, basis, generation, lifecycle and ordering against retained state.

`refused` requires one reason: `inventory_unproven`, `account_mismatch` or
`balance_mismatch`. `acknowledged` requires a null reason, complete inventory,
no positions and no ordinary orders. Complete cash inventory validation also
requires the original account enumeration and coherent mapped balances; unknown
assets, liabilities and boundaries cannot become an acknowledgement.

The production refusal transaction now uses the same strict builder as the
published vectors. Generic emitters cannot enqueue this kind: only its exclusive
durable release transaction may allocate its sequence and sign the batch. The
opening golden records a refusal; shared vectors include a signed acknowledgement
parser fixture. Its synthetic native model remains refused at capability
admission because no complete runtime model has been registered. These fixtures
are not observed account evidence or runtime credentials.

This delivery does not add the successful acknowledgement transaction, a released
gate state, an issuer or a receiver projector. Current runtime readiness and
consumer readiness remain false. Successful release must atomically bind the
current registered process, desired command, observation, sequence, exact signed
outbox bytes and gate state; restart, pause/resume and first-order acceptance
remain required before enabling runtime release. Historical acceptance receipts
retain their original revision and are not regenerated.
