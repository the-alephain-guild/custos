# Platform process ownership foundation

This module provides persistent, machine-local account exclusion while the
platform opening gate remains closed. The signed Nautilus host can acquire
ownership before validating venue connectivity and retain it until the node
has ended and disposed successfully. The lease carries no order permission.
Opening acknowledgement, economic recovery, and enforcement at the native
send boundary remain separate. The independent Papertrade local-runtime
identity and its live-only restriction are unchanged.

## Trusted registry and supported storage

`PlatformAuthorityBinding` identifies one absolute authority root, a non-nil
registry UUID, a non-nil host-scope UUID, and schema version 1. All conforming
platform runners on the same supported host must receive the same trusted
binding, even when their tenants, runner configurations, or outbox paths differ.
The signed Nautilus daemon reads `.arx/platform-process-authority.toml` from
the operating-system account's registered home directory. The private regular
file contains exactly `schema_version = 1`, `authority_root`, `registry_id`,
and `host_scope_id`. The root is an absolute canonical path and the two IDs
are canonical non-nil UUID strings. The host uses an existing registered
store and registry. When the file is absent, the daemon retains the existing
closed opening behavior. Invalid files fail startup.


`PlatformProcessRegistry.initialize` exclusively creates a new or empty private
root. `open_registered` never initializes or repairs missing state. The marker
binds the root, lock directory, registry database and UUIDs; the registry also
retains the marker inode. Held descriptors, named-path device/inode checks,
ordinary-file checks, ownership, permissions, and symlink rejection protect
these bindings. Registered alias lock files are never silently recreated.

Storage must provide local filesystem semantics, `flock`, `O_NOFOLLOW`,
`O_CLOEXEC`, and SQLite WAL with FULL synchronization. Darwin APFS/HFS and the
explicit Linux local-filesystem allowlist are recognized by the implementation;
Linux deployment behavior requires its own real-platform verification. Unknown
and network filesystems fail with `unsupported_storage`. This is not a
cross-host arbitration service and cannot protect against a host administrator
changing every trusted configuration or cloning the complete machine state.

## Account identity

The concrete Nautilus resolver binds an immutable signed desired context to the
actual native execution configuration. Sandbox uses the stable alias
`(sandbox, platform_instance, deployment_instance_uuid)`. Tenant, runner,
generation, spec revision, and random node incarnation do not split this lock.
Different instances can own different simulated accounts despite equal default
native account IDs. Actual node/capture identity binding remains a later step.

The returned `PlatformAccountAliases` is read-only and cannot be constructed
directly from a caller dictionary. Its private construction path is used only
after the concrete resolver checks native config type and fields. The internal
binding is closed and includes the signed connector; SoDEX network, market,
wallet and numeric account must also match the signed context. This Python API
construction boundary is not cryptographic provenance against code with
arbitrary access to process internals. Subsequent integration must still bind
and recheck the actual node, capture source and runtime before any execution.

SoDEX testnet/live uses all three independent aliases within the logical mode:
credential-scope UUID, canonical 20-byte wallet address, and canonical
`<resolved_venue>:<positive_u64_account_id>`. Any shared alias conflicts across
tenants and outboxes. The resolver checks actual native network, market, wallet
and numeric account ID; it never treats an ordinal native account label as the
physical account. A native ABI without the public account identity getters is
explicitly unsupported. The released runtime and the original U2 candidate7
artifact lack these getters. A separate local validation artifact can expose
them under the same distribution version; the version string alone therefore
does not prove API support or artifact identity. Actual getter and process tests
in such an isolated environment do not qualify that uncommitted validation
artifact as a release or an installed production runtime. Final artifact
verification remains separate. No credential is read or sent by this resolver.

The actual-native test matrix checks Testnet spot and perpetual account mapping,
each independently shared account alias across different PIDs and outboxes, and
fully disjoint aliases. A partial-acquisition test keeps the refused process
alive: another PID can acquire its formerly held unique alias while the original
owner's shared wallet remains occupied. Nine wrong native-config domains are
rejected. The Live fixtures deliberately lack promotion evidence and must fail
the existing signed Live admission guard; they do not establish successful Live
account binding or a complete promoted platform lifecycle.

Each alias lock name is SHA-256 of compact, sorted-key UTF-8 JSON containing
`domain="custos.platform-process-alias.v1"`, `trading_mode`, `kind`, and `value`.
There is no trailing newline or extra Unicode normalization. All aliases are
obtained in deterministic order with nonblocking exclusive OS locks. Failure
closes the complete partial descriptor set. A lease from another PID, including
a fork child, cannot be used. Fork children close inherited descriptors without
unlocking the parent's open-file description; exec does not inherit them.

## Registered runner storage

A genuinely new `RunnerFactOutbox` database receives one immutable store UUID.
Every persistent business-table insert, update and delete advances a durable
mutation counter in the same SQLite transaction. Rollback also rolls back the
counter; integer overflow rejects the business write. The canonical persistent
table and trigger definitions are checked by platform reads. UUID replacement,
including SQLite REPLACE with recursive triggers disabled, is rejected.

An existing nonempty database without an identity is not automatically adopted.
Its historical outbox remains readable, but platform registration and claim fail.
An explicit, separately reviewed maintenance procedure is needed to enroll such
a database. Missing paths are opened with SQLite `mode=rw`; platform readers do
not create replacement databases.

`RunnerStateStore.platform_process_snapshot` reads the storage identity, mutation
counter, current signed desired, and owner mirrors in one read transaction. It
checks the stored verification receipt and exact event digest, reconstructs the
typed command from the exact event payload, compares the durable command, and
recomputes its subject-bound fingerprint. A stopped, paused, replaced or corrupt
desired cannot supply the former context. This read relies on the durable
authenticated intake record; it is not a new public-key verification API.

## Claim, checkpoint, and cleanup

`register_store` accepts the concrete `RunnerStateStore`; caller-provided
sequence numbers and snapshot proxies are not supported. The registry binds its
store UUID to the canonical path and inode. `claim_closed` then performs:

1. Acquire every account alias OS lock and read actual runner state.
2. In a registry writer, validate existing store/mirror anchors and append a new
   `claim_pending` owner and process epoch. Preserve any abandoned prior owner as
   `recovery_required` with reason `abandoned_claim`.
3. After the registry writer closes, recheck the current desired in an outbox
   writer and append the exact registry/owner/epoch/alias/context binding.
4. After the outbox writer closes, revalidate the committed snapshot against the
   registry high-water in another writer and finalize `claimed_closed`.
5. Recheck the resulting current identity before returning the held lease.

Registry and outbox writers are never held together. Historical mirrors,
claims, alias heads, registered identities and checkpoints are protected against
ordinary destructive DML. `assert_current` validates current desired, binding,
owner, epoch, held descriptors and already-observed progress. `checkpoint`
finishes its outbox read before obtaining the registry writer, then rechecks that
same snapshot against the writer's current high-water and bindings. A concurrent
newer checkpoint cannot be overwritten by an older read. Finalization applies
the same rule.

`retire_closed` persists retirement before descriptors are closed. `close` is
idempotent for the original process after successful cleanup. It does not prove
that a future native engine has exited; later integration must bind lease life
to actual runtime termination. `claim_closed_async` offloads blocking work and
waits for the real claim and cleanup tasks even across repeated cancellation.
Cleanup errors propagate rather than being reported as a successful release.
Typed `PlatformFenceError.reason_code` values form a closed set; no raw config,
credential, or database path is included in their messages.

## Crash and rollback limits

After SIGKILL at pending, mirror, finalize, or returned-lease boundaries, a new
process must actually acquire all OS locks and allocate a new owner UUID. It
advances the epoch relative to visible retained history. The recovered result
is still closed and does not establish any balance, order, position, or pending
transport state.

A store behind a registry checkpoint is rejected. A registry missing an owner
already mirrored in the outbox is rejected. Inode replacement and progress
behind the live process's memory are also rejected. These checks do not prove
all historical freshness: a pending allocation lost before any outbox mirror,
followed by an original-inode registry rewind and process exit, has no independent
anchor. The lost ordinal can be allocated again under a new closed owner UUID.
A consistent rollback of all trusted state after every process exits is likewise
outside this machine-local protocol. These limits are explicit and must not be
described as complete rollback detection or economic recovery.

The existing opening gate remains `awaiting_release` or `revoked`. Native queue,
spawn and rate-limit boundaries, pause/resume account continuity, actual economic
restart recovery, signed release intake and atomic acknowledgment remain required
separate implementation and acceptance work.
