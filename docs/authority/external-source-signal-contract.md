# Target-neutral external source signal

`src/custos/core/external_source_signal.py` defines `ExternalSourceSignal` as a
direction observation from one configured source. It is not a deployment
command, a venue order, or a subscriber authorization. Its parser receives
`tenant_id`, `expected_source_id` and `allowed_symbols` from trusted local
configuration; the alert body cannot select a tenant or a deployment. The
publisher's sanitized broker envelope adds `tenantId`, which the subscriber
accepts only when it equals that trusted tenant scope.

The closed `schemaVersion: 1` wire shape contains `sourceId`,
`strategyRevision`, `symbol`, `timeframe: "30"`, `barCloseTime`, `timestamp`,
`maxLag`, `target`, and `eventId`. `target` is `LONG`, `SHORT`, or `FLAT`.
Optional `riskAction: "FLATTEN"` requires `target: "FLAT"`. Optional
`signalToken` is accepted only as ingress syntax and is absent from the parsed
object; presence alone is not authentication. The publisher must authenticate
the source before parsing and must not persist the raw token.

The source event ID is reproducible without Pine hashing:

```text
sourceId:strategyRevision:symbol:30:barCloseEpochMs:target:TRADE|FLATTEN
```

It deliberately excludes `deploymentInstanceId`, connector, instrument ID,
leverage and amount. A future subscriber derives a separate per-deployment
delivery ID and checks its own signed authority, venue mapping, notional cap,
expiry and risk fence before making an execution intent. Distinct subscribers
must retain distinct acceptance, intent, venue-fill and flat-confirmation
states. The source parser alone does not publish, subscribe or trade.

This contract is currently exercised with synthetic source events in
`tests/test_external_source_signal.py`. The local
`src/custos/core/external_source_outbox.py` stores sanitized events in a private
SQLite outbox. A claim returns the immutable canonical JSON bytes, a bounded
lease and a stable publication ID; a crash
may cause another publish attempt after the lease expires. Only a caller that
has received a positive broker PubAck may record its sequence using
`mark_published`. An older claim cannot acknowledge a newer claim. This gives
at-least-once publication attempts, not exactly-once downstream execution.
Expired source events are not published, and expiration frees outbox capacity.

`src/custos/cli/subcommands/source_webhook.py` provides a separate loopback
`POST /v1/external-source-signals` receiver for this contract. Its configured
tenant, source and symbol allowlist are trusted local scope, never taken from
the alert. The receiver requires either an exact HMAC request signature or a
preconfigured digest of a dedicated 32–512 character ASCII `signalToken`;
an invalid supplied signature cannot fall back to the token. It rejects
duplicate JSON fields, nonfinite values and oversized bodies before durable
acceptance. A 202 response means the redacted event reached the private source
outbox; a duplicate returns 200. Neither response says a broker published it
or a subscriber traded. The HTTP server binds to IPv4 loopback only and is not
composed into the signed runner daemon. `SourcePublisherService` runs that
receiver alongside the outbox publication worker with an injected JetStream
client; a broker publication failure ends the service and closes HTTP intake.
The internal `python -m custos.cli.subcommands.source_publisher_sandbox` tool runs this service
only against unauthenticated IPv4 loopback NATS, with a pre-existing signal
stream, a private source-token digest file, and a private outbox. It is a
non-promotable synthetic harness; it does not issue a broker JWT or install a
source-IP/TLS edge. The shared stream/ACL provisioning and independent broker
credential issuance remain operator work. The signed runner can opt into a
separate subscriber.

The internal `python -m custos.cli.subcommands.source_publisher_tls` process
uses a separate publisher JWT and verified broker TLS connection. Its
owner-only authorization JSON has exactly `schemaVersion`, `tenantId`,
`sourceId`, `allowedSymbols`, `signalTokenSha256` and
`natsCredentialsSha256`; the JWT seed stays in a different owner-only file.
The process checks the credential digest before connecting and rechecks both
files before each outbox publication. HTTP still binds only to `127.0.0.1`;
the public TLS endpoint and source-IP allowlist belong to an operator edge,
not this HTTP server. A test with an isolated real TLS NATS broker sends an
authenticated loopback HTTP source event through the private outbox to two
distinct JWT subscribers and verifies their independent inbox handoffs. It
does not provision a shared broker or exercise a real TradingView alert or
venue account.
For local acceptance, `python -m custos.cli.subcommands.source_signal_sender`
reads a 32–512 character ASCII token from a separate owner-only file,
constructs the reproducible `CUSTOS_SOURCE_V1` event ID and posts one synthetic
`LONG`, `SHORT` or `FLAT` event to the loopback receiver. Its URL is restricted
to `http://127.0.0.1`; stdout contains only the event ID and HTTP receipt,
never the token. This tool simulates the source contract and does not claim a
TradingView alert or a venue fill.

`external_source_transport.py` provides an adapter for
isolated JetStream lane, exact source subjects, a stable `Nats-Msg-Id`, and one
pre-provisioned durable per runner and deployment. Its worker marks the outbox
published only after the expected stream returns a positive PubAck. Broker
failure or an unexpected PubAck leaves the lease retryable. A consumer bind
checks the existing durable's filter and delivery subject instead of creating
or widening it. Publisher and subscriber permission profiles are generated for
these exact subjects. `tests/integration/test_external_source_fanout.py` can
verify that two distinct durables receive the same sanitized event against an
isolated local NATS server. Broker JWT issuance in the shared environment,
provisioning and venue execution have not occurred. Live mode and real
TradingView egress remain outside the current acceptance scope.

`NtTradingNodeHost.external_source_runtime_scope()` is a separate read-only
scope from the older OKX-only `external_signal_runtime_scope()`. It requires a
running sandbox/testnet deployment with one signal-capable strategy and currently
admits OKX and Binance perpetual connectors. Its pair list comes from the
strategy declaration already checked against the deployment during `deploy()`.
`queue_external_source_signal()` rechecks the exact active authority and pair,
and rejects any source-supplied amount, investment type, token or non-market
order override before calling the strategy queue. A successful return proves
only in-memory acceptance. Neither method binds a broker subscriber to the
verified signed command lane or reports an order or venue fill.

`external_source_delivery.plan_target_delivery()` derives a distinct target
delivery ID from the source event and exact subscriber authority, connector and
pair. `LONG` and `SHORT` become toolkit entry signals with no amount override;
the strategy's own signed configuration and runner safety gate still determine
size. A normal `FLAT` or risk `FLATTEN` becomes a containment directive without
an entry signal. The caller must observe each target's position and orders,
persist the target delivery state, and prove final flatness separately. This
pure conversion does not touch a runner queue or the venue.

`ExternalTargetInbox` stores each derived delivery under its own deployment
instance, spec ID/digest and generation. A deployment's inbox is bound to one
tenant and one source; a second source cannot share its account or flat fence.

The first sandbox/testnet subscriber is selected by a private operator file,
not by a field in the source alert. `external_source_authorization.py` accepts
only this closed `schemaVersion: 1` JSON shape: `tenantId`, `runnerId`,
`deploymentInstanceId`, `deploymentSpecId`, `deploymentSpecDigest`, `generation`,
`tradingMode`, `sourceId`, `sourceSymbol`, `targetPair`, and
`natsCredentialsSha256`. The file and its separate NATS `.creds` file must be
regular files owned by the current user with no group or world access; symlinks
and oversized files are refused. The credential digest binds the exact local
broker identity without putting its JWT or seed into the authorization JSON.
Trusted tenant and runner IDs come from verified machine metadata. The active
host supplies the signed deployment authority and admitted strategy pair; the
operator file must match them exactly, including generation and spec digest.
Only sandbox/testnet and the admitted OKX/Binance perpetual scopes can bind.
This file grants a subscription choice, not deployment authority, size or a
venue credential. The broker must separately enforce an exact pre-provisioned
durable and least-privilege JWT ACL; a file/digest comparison alone cannot prove
those broker permissions. `ExternalSourceRunnerSession` reopens the private
authorization and credential files against the active host before broker intake
and again before handoff. It also pins the active connector, pair set and
leverage observed at bind time. A changed generation, venue scope, source mapping or credential
digest leaves the broker message unacknowledged and prevents host handoff. It
binds only an existing consumer whose delivery, filter, ack and replay policy
match the computed subscription. Its bounded run loop rechecks scope, drains
persisted pending deliveries and unsubscribes on stop or failure; a source
message is acknowledged after inbox persistence, before host handoff. The
session is tested with an injected broker and host. `ExternalSourceNatsProfile` separately reads the
exact owner-only credential bytes, rechecks their digest, and connects with
TLS certificate validation and a dedicated NATS user credential; it never
reuses the runner-control transport. A mocked connection test proves its
arguments and changed-file refusal, not issuance or broker acceptance of a real
JWT. Shared-environment broker ACL and venue execution remain open.

`ExternalSourceRunnerRuntime` is the optional lifecycle wrapper for this
session. It waits for an active signed deployment, reports only state changes,
and uses a separate connection. Invalid local authorization or broker loss
closes source intake and leaves messages unacknowledged; it retries after a
bounded delay. It never requests flatten on a transport or authorization
fault, so the existing position remains under the deployment's local safety
policy and venue protection. `arx-runner start` now has an explicit three-flag
opt-in for an exact instance, private authorization file and separate NATS
credential. It reuses the signed SIM broker endpoint and TLS trust but not the
runner-control connection or JWT, and rejects offline, live, unauthenticated
development NATS and non-Nautilus hosts. The daemon logs state changes without
including credentials or raw messages. An opt-in isolated NATS integration test
generates short-lived publisher and two distinct subscriber JWTs with exact
subject permissions, provisions two separate durables, publishes one sanitized
source event and verifies two independent inbox/host handoffs. A subscriber's
attempt to publish on the source subject is rejected by the real broker. This
proves the synthetic broker ACL path, not issuance or revocation in the shared
environment; two real runner processes, account fills and venue execution still
require separate receipts.

A normal entry must be durably claimed
before the host queue is touched; a persisted handoff means only that the
in-memory queue accepted it. The inbox also persists the source timestamp and
`maxLag`, so a restarted consumer can check expiry without recovering the
bearer token. Restart moves claimed, queued or requested-flat results to
`uncertain` and blocks new entries for that deployment. A `FLAT` or `FLATTEN`
event has its own per-deployment fence, supersedes older unqueued entries, and
does not free reentry until the exact deployment independently observes zero
positions and zero open orders and calls `confirm_flat`. A different deployment's
confirmation cannot release this fence. Broker message receipt and target
inbox acceptance are therefore still distinct from strategy handoff and venue
fill. `ExternalSourceSubscriber` validates broker subject and trusted tenant,
source, symbol and active deployment scope, then acknowledges a message only
after this target inbox durably accepts it. Malformed broker messages terminate
without an execution receipt; storage or runtime unavailability leaves the
message unacknowledged for recovery. An isolated local NATS integration test
exercises two independent target inboxes for OKX and Binance scopes.
`ExternalTargetDispatcher` reads an active
host scope, claims one pending target, and hands a bounded entry to that
host's strategy queue or requests containment for `FLAT`/`FLATTEN`. It then
records only `queued` or `flat_requested`; failures and cancellation quarantine
the claimed result so new entries cannot pass an unknown outcome. Startup must
invoke its recovery method before any new dispatch. The optional signed runner
source session composes this dispatcher; no queue receipt attests an order or fill.

For testnet, `ExternalSourceRunnerSession` obtains the independent REST reader
already attached to the active signed deployment. Its loop finds unresolved
flat fences for the bound source after startup recovery. The
`ExternalTargetFlatVerifier` first vetoes a claim if the local node still has
positions, open orders or reserved/open exposure. It then reads the venue and
requires the exact connector and pair, complete position and order views,
zero positions and zero orders, and a fresh venue timestamp after the persisted
flatten handoff. It checks the local node and operator authorization again
before persisting `flat_confirmed`. OKX reads positions, ordinary pending
orders and all four documented pending algo types. Binance reads the account
and position-risk views plus ordinary and algo open orders; inconsistent views
fail closed. Both adapters read positions again after the pending-order reads
and refuse a changed position view; Binance also rejects a position-risk
response for another symbol. Sandbox has no independent venue REST reader;
its flat fence may
instead clear only after two guarded reads of the simulated engine's local
positions, orders and reservation exposure. That is simulation state, not an
independent venue receipt. Unit tests use injected
responses and a fake host; no real testnet account has yet supplied a flat
receipt. A venue API snapshot is also not an atomic order/fill history, so
full order lineage and a two-account venue acceptance run remain outstanding.
The flat fence persists the claimed observation source and timestamp. The
local JSON export calls this `flatConfirmationSourceClaim` and retains the
`local_callbacks_only` grade; a string saying `venue_api` in that file is not
itself a signed or independently verified venue receipt.

For entry targets, the strategy adds the canonical target delivery digest to
the entry order's local Nautilus tags without changing the signal-generated
order size. The optional source inbox path is passed to the host before node
startup, so its order-event sink can persist a tagged `OrderInitialized` and
subsequent `OrderFilled` events even before the source subscriber reconnects.
The private inbox binds each client order ID and fill event ID to the exact
deployment generation and target delivery, and refuses conflicting replays or
a different instrument. Unrelated, untagged orders remain unattributed. A
source lineage storage failure suspends new source intake and reports a source
fault; it does not stop the signed host or remove local protection. The
current mapping covers tagged entry orders and their fills. Flat-containment
orders, protective orders, fees and final target outcome are not yet linked
to the source delivery, and no real venue execution receipt has been captured.
`ExternalTargetInbox.target_execution()` returns a deployment-scoped local
snapshot with the target handoff status and linked client order, fill event,
venue order and venue trade IDs. `NtTradingNodeHost` can collect its own signed
testnet deployment's independent ledger for a caller-specified interval and
compare all fills on the known venue order IDs. The comparison returns
`matched_known_order_fills`, `disagreement` or `unknown`; an unfilled order,
incomplete interval or missing venue account view remains `unknown`. A match
does not attest final order state, position flatness or quantity
equivalence: OKX reports contract quantities on native fills while the ledger
converts them to base units. No real account has yet produced a comparison.
When the independent ledger declares fee coverage complete, the comparison
also checks each known trade's commission against the separate venue fee row
and reports a per-currency total. Its fee status remains `unknown` without
complete coverage and `disagreement` on mismatched rows. This is commission
attribution for known fills, not funding, realized PnL or final settlement.
The internal `python -m custos.cli.subcommands.source_execution_report`
command opens an existing owner-only target inbox in SQLite read-only mode and
exports one exact deployment generation's local target/order/fill rows as JSON.
Its identity arguments are caller supplied and only matched to the local
inbox; the output labels itself `local_callbacks_only` and is not a signed
RunnerFact or independent venue receipt. A missing inbox is not created.
