# Local runtime contract V1

Status: accepted contract-only foundation for implementation after independent Astra review; not a live-admission grant. The platform command contract and the sandbox/testnet offline lane retain their existing trust rules until this contract's implementation is separately accepted.

## Authority and identity

Control source (`local` or `platform`) is orthogonal to trading mode (`sandbox`, `testnet`, `live`). A local run has a local UUID and never invents a platform tenant, deployment specification, enrolled identity or capability receipt. A platform adapter retains the actual platform provenance when it supplies the common execution kernel.

The first local live connector is Papertrade. Account identity is the venue account, not a credential identifier: connector, trading mode, chain ID, Exchange proxy and wallet form one scope. Account identity is derived from the loaded adapter deployment, not caller declarations. The first Papertrade live release accepts only chain 999 and Exchange proxy 0x6cd5661646289fb6e65ea5c032310fded797d0a2, matching the actual signing and journal domains. Unsupported mode, chain, Exchange or overridden endpoint deployment is refused before locking or creating storage. Addresses are validated 20-byte EVM addresses normalized to lowercase. The manifest must equal this derived identity; it cannot select a second scope for the same actual adapter target. The configured native engine AccountId is presentation/routing identity and cannot distinguish two controllers of the same wallet. Session keys, API keys and local file names are excluded from account identity.

## Local run manifest and permit

The strict versioned manifest binds:

- local run UUID, monotonically increasing configuration revision and explicit control source;
- strategy artifact and configuration digests, entry point and declared instruments;
- normalized account identity and a reference to the encrypted trading credential;
- engine version, toolkit identity, requested leverage and settlement currency;
- maximum order and total notional, maximum loss, stale-data and stop policies;
- stop position policy and confirmation deadline, never an implicit success on timeout.

A separate operator authorization command signs an immutable local permit using a local management signing identity. It binds the exact manifest digest, operator identity, permit UUID, issue and expiry times and the expected authorization epoch. The daemon verifies the signature against a provisioned local trust root; it cannot authorize itself. The management signing material is not the venue trading session key. Production requires a separately controlled offline signer or an isolated operator identity: the daemon and loaded strategy cannot read or decrypt the management private material, invoke its signer, or replace the provisioned public trust root. In a container, do not mount signing material or its decryption identities; mount the trusted public root read-only, drop mount/admin capabilities, and run as a non-root identity. Bare-host production needs an equivalent independent service identity and file ownership boundary. Sharing an operator UID, writable trust root or management decryption identity is development-only and must not pass production preflight. Test attempted signing, material access and root replacement from the real daemon identity, not a mocked permission check. Untrusted manifests never select the trusted root or signing identity.

Permits use explicit UTC instants. Expired, future-invalid, wrong-account, wrong-authorization-epoch, reused-after-supersession or digest-conflicting permits refuse new activation before importing strategy code. Renewal and configuration changes require explicit operator authorization. Expiry blocks new risk; a valid, independently checked reduction/stop path remains available to the extent the venue still accepts the trading credential. No promise of executable exits survives an expired or revoked venue session.

Mutable source directories are not local live artifacts. Material is prepared and verified before activation; the existing toolkit/engine identity judge is reused after its normal review and integration. Local verification is not platform release approval or promotion evidence.

The internal `custos.local` authorization adapter now defines immutable version-1
run manifests and Ed25519 permits. Manifests bind the existing exact
StrategyArtifactRefV1, release BOM and toolkit identity digests, effective config
digest, entry point, native instruments, vault alias, account, data/stop policy
and explicit USD limits. Amounts are canonical positive decimal strings; leverage
is a strict integer. Order notional equals margin times leverage, and account
exposure must fit the declared capital and leverage. Validation uses an isolated
Decimal context. The loss amount is a risk freeze/exit trigger, not a bound on
the final execution loss. Manifest validation does not establish venue minimums,
funding, material integrity or installed-engine capability.

A permit binds the manifest digest, run, account scope, canonical control registry
UUID, target authorization epoch, operator and management-key fingerprint.
Issued/not-before/expiry and the required `exit_until_utc` instants are explicit
canonical UTC text with six fractional digits. The exit instant must be no
earlier than expiry. Both bounds must project to strictly positive UTC seconds
in the native AtomicTime nanosecond range. The Ed25519 signing frame is
`alephain.custos.local-run-permit.v1` followed by a NUL byte and the canonical
permit JSON. `permit_digest` is the SHA-256 of the complete canonical signed
envelope, including the signature; preparation, verification and recovery use
that same identity. Both documents reject duplicate keys, unknown fields and
noncanonical bytes. The signing function only consumes an explicitly supplied
key object and does not load or generate management credentials.

`VerifiedLocalRunAuthorization.execution_window` projects the signed expiry and
exit instant into an immutable pair of exclusive native bounds. Integer
timedelta division floors each bound independently, without float conversion or
adding the manifest's stop confirmation timeout. Different microseconds in one
second may produce equal bounds. Native intent admission still requires the
actual signed deadline strictly before the selected bound and enough remaining
venue-session lifetime; signature validity alone proves neither. Activation
verification still requires the current instant strictly before expiry. This
projection does not implement an expired-permit activation path or a separate
observation/exit service. Such a service must independently verify the same
signature and durable owner, then authorize only checked reductions; it must
not use an `allow_expired` flag to reactivate strategy decisions.

Verification consumes kernel-provisioned public keys and operator mappings, the
actual adapter account, current registry UUID and the complete prepared claim.
It compares the checkpoint's run, local source, account scope, registry UUID,
target epoch and permit digest, then retains that exact checkpoint for the same
guard's claim. It never grants ownership, imports strategy code or starts a
client. The signed target epoch is distinct from the expected current epoch
used by registry CAS; process generation is not signed. The same valid permit
may therefore authorize recovery with a new process generation. Real daemon
key/root isolation, material binding, lifecycle readiness and the native final
send gate remain separate acceptance requirements.

## Account control store

One durable account registry exists in the canonical guard directory. It records control source, run UUID, authorization_epoch, process_generation, opaque owner instance UUID, phase, permit digest and transfer identity. authorization_epoch changes only on operator-authorized run/configuration replacement or control-source transfer. process_generation increments whenever a new process acquires execution ownership, including crash recovery. Both counters survive stop and restart; deleting a run never resets them. PID is diagnostic only. Every outgoing action and callback checks the tuple (authorization_epoch, process_generation, owner UUID). A valid permit binds authorization_epoch, not process_generation; recovery never changes or re-signs its authorization.

A canonical host-local guard directory is shared by both entry points. It is derived from account identity and is not selectable per run or credential. A kernel-owned OS lock prevents two local processes from holding the account while SQLite transactions persist state. A different run-state or journal directory must not select another account guard. The first release assumes one operator-controlled host; it does not claim to fence unrelated devices, users or external applications holding valid venue access.

The registry binds the lock file's device and inode as well as the account. A registered account never recreates a missing lock file during ordinary recovery; replacement or loss requires explicit repair, and a held descriptor must still match the named and registered file. Fork children close inherited lock descriptors without unlocking the parent's shared lock. A process fence inherited across fork is never usable by the child.

The account registry also binds authority/state store UUIDs, risk state identity, the original adapter journal UUID and canonical location, stable local-command-to-client-order-ID mapping, and the latest verified event/adapter coverage checkpoints. These identities cannot be selected by a new run directory. Registered storage must already exist and match before opening it for recovery. Missing storage, a substituted empty file, mismatched UUID or unproved coverage enters manual recovery and blocks new exposure; never initialize a replacement empty SQLite/redb store as successful recovery. First-time initialization is distinct, explicitly authorized and requires a verified fresh account boundary with no inherited unresolved intents. Existing journals without a UUID need an explicit owner-checked migration preserving all entries and unknown statuses before binding. Initial acquisition and replacement are compare-and-update transactions. The first acquisition creates authorization epoch 1 and process generation 1. Operator-authorized replacement of a stopped run increments authorization_epoch and requires a fresh permit. Repeating the identical current owner request is idempotent; another owner or conflicting content is rejected. A fencing-tuple mismatch rejects every callback and outgoing command before venue submission. An old process cannot release a newer owner's row.

A process crash releases the OS lock but does not erase durable authority. A replacement obtains the canonical lock and opens the registered stores, keeps the same authorization_epoch and exact manifest/permit identity, and enters recovery with a new owner UUID and incremented process_generation. An expired or otherwise invalid permit cannot activate strategy decisions; a separately defined local observation/exit service may recover evidence and perform only authorized, independently checked reductions without treating that as strategy activation. Ownership does not establish strategy readiness: no new risk is allowed before account evidence and the relevant decision state are verified.

The internal registry's expected authorization epoch is a compare-and-update precondition on the current row (zero before first registration). It is distinct from the target epoch signed by the operator: first/new authorization creates the next target epoch, whereas same-run recovery retains it. The upper authorization adapter must validate the permit against the resulting target epoch. Registry acquisition alone never verifies a signature, proves flatness, performs a cross-source handoff or grants readiness. Retiring a registry run only prevents its reuse; it is not a venue stop receipt.

The internal `account_guard` context now holds the canonical OS lock throughout
synchronous preparation. `prepare_claim` applies the same admission rules as the
final claim, checks the registered state store and returns the journal UUID and
revision floor without opening the journal. The caller can then construct an
unstarted, disconnected native client with strict registered recovery, capture
its live probe, and claim with that probe before any execution starts. Final
claim compares the entire registration with the prepared checkpoint and rejects
watermark regression, including state progress observed during preparation.
The checkpoint belongs to one guard and cannot authorize another account.

Claim transfers the same lock descriptor once to the lease; closing the old
guard cannot release that lease. A concurrent close waits for the complete
preparation interval, and a fork copy is rejected before taking inherited
thread locks or touching descriptors. Each lease retains its own identity probe.
The compatibility `claim` call uses this same preparation path. This foundation
does not yet wire the native factory, verify a permit, establish a fresh account
boundary or grant live readiness. Native client cleanup before releasing a
failed preparation guard remains a caller obligation to verify during wiring.
If claim has committed but subsequent setup fails, retain recovery-required
authority; do not erase registration or mark the account retired.

`PapertradeRuntimeStorageProbe` bridges the live native journal probe into those
registered-store checks. The kernel supplies the actual factory account and
journal configuration; the bridge verifies the native snapshot's exact owner,
UUID4, positive bounded revision and canonical path rather than attesting an
arbitrary caller path using another journal's probe. Duplicate or unknown JSON
fields, missing canonical-path evidence, a changed UUID or regressing revision
are refused. Each journal read calls the original probe, whose held storage
checks its original and canonical path against the open file identity. No redb
reopen or cached successful snapshot replaces a released owner. SQLite identity
uses the existing registered ExecutionEventStore and cannot initialize missing
state. Only the two bound paths are readable, and fork children refuse before
acquiring an inherited thread lock or calling native code. The native probe also
retains the actual journal owner creation PID and refuses a cross-process read
before upgrading its weak owner reference or touching redb. Constructing a new
Python bridge or capturing another probe from a fork copy cannot refresh that
native owner process identity.

This bridge is a storage-evidence capability, not a permit verifier or execution
host. Its callback must be passed to the same guard's claim and retained by that
lease. Native clients must still be constructed under that guard, disconnected,
then bound to the verified permit, risk blocker, material and readiness checks
before running a strategy. Older probes without canonical-path evidence cannot
meet this bridge's identity requirements.

## Venue authorization and observation

Read-only market/account/history/chain observation is a separate capability and does not require an unexpired trading session. It cannot sign or submit intents, renew keys or silently fall back to an execution connection that requires trading authorization. This path is required after key revocation to verify flat-transfer evidence.

Opening exposure requires sufficient venue authorization lifetime for the actual intent deadline and the admission safety budget. Reduction and cancellation have their own checked authorization/deadline rules. The current Papertrade adapter applies a one-hour remaining-life rule to connect, prepare_order and cancel_order; it does not yet meet this contract. T4/T5 must first prove the protocol acceptance domain, then separate these operations. If the protocol permits a shorter reduction deadline, bind it no later than session expiry and validate actual receipt semantics; if not, drain positions with a verified full-lifetime reserve before that cutoff and refuse an operating policy that cannot do so. Expired/revoked keys never grant trading rights. Never advertise guaranteed emergency closure after venue authorization is gone.

## Execution records and safety

The common kernel owns artifact activation, engine lifecycle, risk enforcement, reliable portfolio observations and stop completion. Local authorization and platform authorization are adapters into this kernel; there must not be a second order engine or weaker local risk branch.

Record immutable local command/event identity, configuration revision, authorization_epoch, source time and processing result. Record process_generation and owner UUID in the separate process-attempt context, not in an immutable business payload. Do not record plaintext secrets, signing material, raw signed intent payloads or unfiltered exceptions in public events. The adapter's protected journal remains the authority for its signed-intent bytes.

Local event and reservation changes use their local transaction boundary. The Papertrade redb journal and the runner SQLite database are not one atomic transaction: stable command IDs, idempotent replay and reconciliation must cover crashes between their commits. A missing record must remain unresolved, not interpreted as an absent order or free risk capacity.

The shared reservation boundary prioritizes the venue `trade_id` for fills,
including the native object's identity when its serialized event omits that
field. Rebuilding an engine event envelope must not create a second accounting
mutation for one venue fill. Fill source tokens carry the explicit
`fill-source-v1` frame, separate venue and engine domains, and a fixed-length
digest so delimiters in trade and order IDs cannot create the same reservation
event identity for different pairs. Non-fill events retain their engine event
identity; fills without a venue identity retain engine-identity fallback semantics
inside that explicit frame.
This key selection does not prove venue-history coverage, implement local risk
transactions or persist in-flight reductions. Those remain separate acceptance
requirements before independent live execution.
Historical engine-only event keys cannot be guessed into venue fill identities;
recovery must establish coverage before new exposure, rather than treating this
key change as a seamless historical ledger migration.
The real reservation store checks the instance's existing boundary fill history
inside the same SQLite transaction before replay or mutation. An older boundary
fill frame refuses reserve, replace, fill and both reduction paths, preserving
committed values instead of silently applying the same execution twice. Changing
policy or the new request's event ID does not clear this instance isolation.
Observations remain readable, and unrelated instances are not reclassified.
This is a fail-closed refusal boundary, not a migration or history-repair tool;
the affected runtime requires an explicit reconciliation decision before it can
continue, while unaccounted venue evidence remains unresolved.

Persistence failure, unreliable equity, stale account stream, incomplete fill history, ambiguous intent, changed identity or failed risk state blocks new exposure. Risk reduction requires confirmed current quantity and in-flight reduction accounting, or the venue's independently verified reduction guarantee; unavailable proof is not a reason to submit an unchecked reverse trade. Venue-accepted protective action and its unconfirmed outcome must be reported truthfully.

Platform RunnerFact production is a sink with real platform authority, not a prerequisite for local event recording. Local records never become synthetic platform facts, historical eligibility, NAV or settlement inputs.

The internal `ExecutionEventStore` persists refusal observations and local risk
mutations in the same account-registered state database. Immutable records bind
local run identity, configuration revision and authorization epoch; process
attempts are separate. The refusal `events` view and `risk_events` view share one
sequence and watermark. A complete audit export must include both views rather
than interpreting gaps in either filtered view as missing committed events.
`LocalExecutionRecorder` implements the synchronous refusal interface used by
the platform order gate. A refused record freezes further risk increases.

`LocalReservationStore` implements the existing reservation port and reuses the
common reservation and FIFO cost calculations. It accepts a genuinely verified
local authorization only when its complete signed permit digest equals the
actual claimed lease's durable permit digest. Prepared but unclaimed material
cannot supply different limits for the same run or epoch. Each operation checks
the current lease and registered state identity. Registry and journal probes
complete before acquiring the risk database write transaction; the risk lock
must not be held while calling back into those identity probes. Failure of the
post-commit lease check freezes risk but cannot reverse a committed venue fact.

The first local-risk binding atomically binds one account scope in metadata and
creates its risk latch in this registered database. This first-release database
serves one account. After binding, a missing latch row, missing table, conflicting
account or unavailable metadata is storage failure, never permission to insert an
empty replacement. There is no compatibility parser or automatic migration for
an earlier local-risk schema. The account-wide latch is one-way and survives
owner replacement, run changes and permit renewal. Unreconciled reservations or
cost exposure from an earlier run freeze a new owner rather than authorizing it
to continue an inherited strategy.

Reservation and cost exposure mutations, their immutable audit records and any
account latch commit in one SQLite transaction. Reserve and replace require a
current permit and an unfrozen blocker; executed fill and complete-reduction
facts remain recordable after permit expiry. Post-trade limit breaches preserve
the executed amount and latch before reporting refusal of further risk. A nested
savepoint protects each attempted economic mutation: if semantic or supported
numeric validation fails while applying a venue fact, its partial changes roll
back before the outer transaction records unapplied evidence and freezes the
account. This is unresolved execution evidence, not an applied fill or free
capacity. Storage or audit failure freezes the in-process blocker and propagates;
reads use the same failure handling rather than returning an empty portfolio.

Venue fill identities are unique across the account, independently of run,
permit and process generation. Replays retain the original economic result and
append process-attempt evidence. A conflicting identity or a fact owned by an
earlier run is retained as a refusal and freezes the account. Unusable input is
represented by a bounded digest and structured reason; a stable venue identity,
when available, remains pending so corrected input cannot silently reapply it.
Unknown original ownership remains null. Fills without stable venue identity
cannot be silently treated as complete local-risk history.

External monetary and quantity inputs require finite exact Decimal values in
the supported domain. Calculations use an explicit isolated context, independent
of ambient and default Decimal settings. Proportional residual reservations with
more than 120 fractional places round upward to 1e-120; this never releases extra
capacity. A confirmed complete close releases its recorded entry cost even when
exit notional differs. Partial closes remain unapplied and freeze risk in this
first release; the backend never invents a reverse trade or a position quantity.

This component accounts for reserved notional and entry-cost exposure. It does
not yet implement marked exposure, reliable equity, fees or the maximum-loss
trigger, durable in-flight reductions, native blocker wiring, runtime entry or
live admission. Its unfrozen state is not readiness. These remain necessary
before independent production execution can be enabled.

The common submit gate now retains the Native position_id, client_id and params
arguments before judging an order, including their positional calling form. It
rejects duplicate or unsupported argument names before reserving, detaches the
parameter dictionary from caller-owned mutable values and forwards that same
captured content to Native. This preserves target evidence without changing an
ordinary call that supplied no optional arguments.
Parameters must contain only plain JSON builtin values, finite floats and
string dictionary keys. Container subclasses and custom scalar values are
refused before reservation because user-defined copying hooks could preserve
mutable aliases. Tuples are captured as JSON arrays; nesting at 128 levels and
cycles are refused before Native serialization.

The Papertrade reduction-target parser requires an explicit canonical uint256
venue position, matching any simultaneous Native position ID and params target.
An explicitly null or malformed params target cannot silently fall back to a
second target. It reads one current hedged cache position and verifies the
configured Native account/client, instrument, opposite direction, reduce_only
and full exact base quantity. It does not infer flatness or close quantity from
a netted pair of positions. The returned ReductionTarget is immutable evidence
and includes a deterministic params digest. Configured routing identities come
from the kernel's actual factory binding; they do not select another wallet.

This parsing capability does not grant authorization, establish original-run
ownership, create a durable close claim or submit an order. Those checks must be
combined with the signed permit and the existing entry-cost rows in the later
persistent reduction transaction. Native direct-close, list and modify handling
remain separate integration work before any local live entry can be enabled.

## Readiness, restart and stop

Readiness requires an alive node, actual data and execution connectivity, initialized reliable account/portfolio state, completed account reconciliation, active risk enforcement and durable current authority. The current blanket live refusal may be replaced only by proved checks, not a mode-based true value.

Adapter recovery proves order/position identity and execution evidence; it does not prove the strategy's algorithm state. After unclean exit, recover in a no-new-risk state. If decision state cannot be reconstructed, expose recovery/exit controls, reconcile and flatten as authorized, then require a new manifest/permit for a new run. Do not restart the algorithm from an empty state against inherited exposure.

Stop rejects new risk first, handles pending risk-increasing intents and requests the declared position policy. A successful response requires observed engine termination and a confirmed account/intent result. Timeout returns stop-unconfirmed or recovery-required, with residual exposure and uncertain requests explicit. A health probe, request acknowledgement or empty engine cache is insufficient.

## Required falsification cases

Concurrent owners for one wallet, false chain/Exchange declarations against the same adapter, different session keys on one wallet, different run directories including missing original journals with unknown sent intents, stale epochs, PID reuse, owner release after replacement, crash before/after commit, record failure, unknown intents, account-stream silence, incomplete history, permit expiry during exposure, remaining trading-key lifetime below one hour, read-only verification after revocation, valid permit across a changed process generation, management-signer/root access from the daemon identity, late callbacks after transfer and partial stop must have behavioral tests. No fixture or local node test counts as real-funds acceptance.
