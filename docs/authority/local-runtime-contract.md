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

## Account control store

One durable account registry exists in the canonical guard directory. It records control source, run UUID, authorization_epoch, process_generation, opaque owner instance UUID, phase, permit digest and transfer identity. authorization_epoch changes only on operator-authorized run/configuration replacement or control-source transfer. process_generation increments whenever a new process acquires execution ownership, including crash recovery. Both counters survive stop and restart; deleting a run never resets them. PID is diagnostic only. Every outgoing action and callback checks the tuple (authorization_epoch, process_generation, owner UUID). A valid permit binds authorization_epoch, not process_generation; recovery never changes or re-signs its authorization.

A canonical host-local guard directory is shared by both entry points. It is derived from account identity and is not selectable per run or credential. A kernel-owned OS lock prevents two local processes from holding the account while SQLite transactions persist state. A different run-state or journal directory must not select another account guard. The first release assumes one operator-controlled host; it does not claim to fence unrelated devices, users or external applications holding valid venue access.

The registry binds the lock file's device and inode as well as the account. A registered account never recreates a missing lock file during ordinary recovery; replacement or loss requires explicit repair, and a held descriptor must still match the named and registered file. Fork children close inherited lock descriptors without unlocking the parent's shared lock. A process fence inherited across fork is never usable by the child.

The account registry also binds authority/state store UUIDs, risk state identity, the original adapter journal UUID and canonical location, stable local-command-to-client-order-ID mapping, and the latest verified event/adapter coverage checkpoints. These identities cannot be selected by a new run directory. Registered storage must already exist and match before opening it for recovery. Missing storage, a substituted empty file, mismatched UUID or unproved coverage enters manual recovery and blocks new exposure; never initialize a replacement empty SQLite/redb store as successful recovery. First-time initialization is distinct, explicitly authorized and requires a verified fresh account boundary with no inherited unresolved intents. Existing journals without a UUID need an explicit owner-checked migration preserving all entries and unknown statuses before binding. Initial acquisition and replacement are compare-and-update transactions. The first acquisition creates authorization epoch 1 and process generation 1. Operator-authorized replacement of a stopped run increments authorization_epoch and requires a fresh permit. Repeating the identical current owner request is idempotent; another owner or conflicting content is rejected. A fencing-tuple mismatch rejects every callback and outgoing command before venue submission. An old process cannot release a newer owner's row.

A process crash releases the OS lock but does not erase durable authority. A replacement obtains the canonical lock and opens the registered stores, keeps the same authorization_epoch and exact manifest/permit identity, and enters recovery with a new owner UUID and incremented process_generation. An expired or otherwise invalid permit cannot activate strategy decisions; a separately defined local observation/exit service may recover evidence and perform only authorized, independently checked reductions without treating that as strategy activation. Ownership does not establish strategy readiness: no new risk is allowed before account evidence and the relevant decision state are verified.

The internal registry's expected authorization epoch is a compare-and-update precondition on the current row (zero before first registration). It is distinct from the target epoch signed by the operator: first/new authorization creates the next target epoch, whereas same-run recovery retains it. The upper authorization adapter must validate the permit against the resulting target epoch. Registry acquisition alone never verifies a signature, proves flatness, performs a cross-source handoff or grants readiness. Retiring a registry run only prevents its reuse; it is not a venue stop receipt.

## Venue authorization and observation

Read-only market/account/history/chain observation is a separate capability and does not require an unexpired trading session. It cannot sign or submit intents, renew keys or silently fall back to an execution connection that requires trading authorization. This path is required after key revocation to verify flat-transfer evidence.

Opening exposure requires sufficient venue authorization lifetime for the actual intent deadline and the admission safety budget. Reduction and cancellation have their own checked authorization/deadline rules. The current Papertrade adapter applies a one-hour remaining-life rule to connect, prepare_order and cancel_order; it does not yet meet this contract. T4/T5 must first prove the protocol acceptance domain, then separate these operations. If the protocol permits a shorter reduction deadline, bind it no later than session expiry and validate actual receipt semantics; if not, drain positions with a verified full-lifetime reserve before that cutoff and refuse an operating policy that cannot do so. Expired/revoked keys never grant trading rights. Never advertise guaranteed emergency closure after venue authorization is gone.

## Execution records and safety

The common kernel owns artifact activation, engine lifecycle, risk enforcement, reliable portfolio observations and stop completion. Local authorization and platform authorization are adapters into this kernel; there must not be a second order engine or weaker local risk branch.

Record immutable local command/event identity, configuration revision, authorization_epoch, source time and processing result. Record process_generation and owner UUID in the separate process-attempt context, not in an immutable business payload. Do not record plaintext secrets, signing material, raw signed intent payloads or unfiltered exceptions in public events. The adapter's protected journal remains the authority for its signed-intent bytes.

Local event and reservation changes use their local transaction boundary. The Papertrade redb journal and the runner SQLite database are not one atomic transaction: stable command IDs, idempotent replay and reconciliation must cover crashes between their commits. A missing record must remain unresolved, not interpreted as an absent order or free risk capacity.

Persistence failure, unreliable equity, stale account stream, incomplete fill history, ambiguous intent, changed identity or failed risk state blocks new exposure. Risk reduction requires confirmed current quantity and in-flight reduction accounting, or the venue's independently verified reduction guarantee; unavailable proof is not a reason to submit an unchecked reverse trade. Venue-accepted protective action and its unconfirmed outcome must be reported truthfully.

Platform RunnerFact production is a sink with real platform authority, not a prerequisite for local event recording. Local records never become synthetic platform facts, historical eligibility, NAV or settlement inputs.

The internal `ExecutionEventStore` currently persists refusal observations in the
account-registered state database. Immutable records bind local run identity,
configuration revision and authorization epoch; process attempts are separate.
`LocalExecutionRecorder` implements the same synchronous refusal interface used
by the platform order gate. A refused record freezes further risk increases.
This foundation does not yet provide the local reservation transaction, runtime
entry point or live admission described above.

## Readiness, restart and stop

Readiness requires an alive node, actual data and execution connectivity, initialized reliable account/portfolio state, completed account reconciliation, active risk enforcement and durable current authority. The current blanket live refusal may be replaced only by proved checks, not a mode-based true value.

Adapter recovery proves order/position identity and execution evidence; it does not prove the strategy's algorithm state. After unclean exit, recover in a no-new-risk state. If decision state cannot be reconstructed, expose recovery/exit controls, reconcile and flatten as authorized, then require a new manifest/permit for a new run. Do not restart the algorithm from an empty state against inherited exposure.

Stop rejects new risk first, handles pending risk-increasing intents and requests the declared position policy. A successful response requires observed engine termination and a confirmed account/intent result. Timeout returns stop-unconfirmed or recovery-required, with residual exposure and uncertain requests explicit. A health probe, request acknowledgement or empty engine cache is insufficient.

## Required falsification cases

Concurrent owners for one wallet, false chain/Exchange declarations against the same adapter, different session keys on one wallet, different run directories including missing original journals with unknown sent intents, stale epochs, PID reuse, owner release after replacement, crash before/after commit, record failure, unknown intents, account-stream silence, incomplete history, permit expiry during exposure, remaining trading-key lifetime below one hour, read-only verification after revocation, valid permit across a changed process generation, management-signer/root access from the daemon identity, late callbacks after transfer and partial stop must have behavioral tests. No fixture or local node test counts as real-funds acceptance.
