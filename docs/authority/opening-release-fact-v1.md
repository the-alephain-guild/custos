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

The guarded execution kernel now has an acknowledgement transaction. It binds
the current registered process, signed desired command and host-owned observation
while one SQLite writer allocates the sequence, signs the outbox and appends the
execution permission. The host observation guard remains held through commit.
The guard is a native-free retirement certificate. Capture validates the runtime,
context, safety boundary and held lease on the host thread; worker arguments and
exception tracebacks retain neither the host nor its native runtime. Installing
or removing bindings retires prior certificates before changing those bindings.
The hosted run retires its certificate in `finally`, before its task can finish.
Failures roll everything back. A later delivery retries the original signed
bytes. Every outbound operation still checks the current held process lease and
its exact generation-scoped permission.

An immutable acknowledgement is history, not an authorization for another
process. Restart and pause/resume require a fresh complete account-continuity
observation and an independently observed risk value. Account incarnation and
configuration must remain identical; existing positions are preserved rather
than being reclassified as an initial cash account. A stopped or archived
instance cannot inherit permission, and an unanswered attempt never transfers.
The native capture and risk observation must follow the current signed command;
the same node's capture sequence must advance beyond prior permissions. A later
risk timestamp cannot make an old native capture eligible for a new permission.

The registered pre-opening V1 SQLite layout has one controlled in-place upgrade.
Its full table, index and trigger catalog must match the committed predecessor
before one `BEGIN IMMEDIATE` installs the execution tables and guards. The store
identity, mutation counter, stream, signed outbox and process-registry bindings
are retained. Unknown or partial layouts are rejected; no database is deleted or
silently adopted. Installation uses individual DDL statements so a failed table
or guard rolls back the entire extension. Reopening is idempotent.

Synchronous native callbacks reuse only the exact current control writer. Each
reservation operation uses a savepoint and leaves commit ownership outside the
callback; reads see the same pending reservation. Reserve, replace, release,
fills, FIFO reductions, order identity, breaker evidence, batch facts and signal
facts do not acquire a second SQLite writer. Executed overfills retain their
accounting and risk latch, while an opening decision remains all-or-nothing.

Current default capabilities still declare neither opening decisions nor a
complete native model. No installed native qualification, successful producer to
receiver runtime acceptance, issuer, or transport permission is enabled here.
The positive kernel fixtures explicitly register a future semantic model only
inside isolated tests. They are not an artifact receipt. Runtime and consumer
readiness remain false; account restoration and a qualified native host must
provide the continuity evidence before actual restart can trade. Historical
acceptance receipts retain their original revision and are not regenerated.
