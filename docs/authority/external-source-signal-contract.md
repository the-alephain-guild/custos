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
source-IP/TLS edge. An independently authorized broker credential, the shared
stream/ACL provisioning, and runner daemon subscription remain uncomposed.

`external_source_transport.py` provides an adapter for
isolated JetStream lane, exact source subjects, a stable `Nats-Msg-Id`, and one
pre-provisioned durable per runner and deployment. Its worker marks the outbox
published only after the expected stream returns a positive PubAck. Broker
failure or an unexpected PubAck leaves the lease retryable. A consumer bind
checks the existing durable's filter and delivery subject instead of creating
or widening it. Publisher and subscriber permission profiles are generated for
these exact subjects. `tests/integration/test_external_source_fanout.py` can
verify that two distinct durables receive the same sanitized event against an
isolated local NATS server. Broker JWT issuance, shared-environment provisioning,
daemon composition and venue execution have not occurred. Live mode and real
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
JWT. Daemon lifecycle composition, exact broker ACL and venue execution remain
open.

A normal entry must be durably claimed
before the host queue is touched; a persisted handoff means only that the
in-memory queue accepted it. The inbox also persists the source timestamp and
`maxLag`, so a restarted consumer can check expiry without recovering the
bearer token. Restart moves claimed, queued or requested-flat results to
`uncertain` and blocks new entries for that deployment. A `FLAT` or `FLATTEN`
event has its own per-deployment fence, supersedes older unqueued entries, and
does not free reentry until a caller independently observes flat positions and
no risk-increasing orders and calls `confirm_flat`. A different deployment's
confirmation cannot release this fence. Broker message receipt and target
inbox acceptance are therefore still distinct from strategy handoff and venue
fill. `ExternalSourceSubscriber` validates broker subject and trusted tenant,
source, symbol and active deployment scope, then acknowledges a message only
after this target inbox durably accepts it. Malformed broker messages terminate
without an execution receipt; storage or runtime unavailability leaves the
message unacknowledged for recovery. An isolated local NATS integration test
exercises two independent target inboxes for OKX and Binance scopes. The
subscriber is not yet composed into the signed-lane daemon and does not call
the host's trading methods. `ExternalTargetDispatcher` now reads an active
host scope, claims one pending target, and hands a bounded entry to that
host's strategy queue or requests containment for `FLAT`/`FLATTEN`. It then
records only `queued` or `flat_requested`; failures and cancellation quarantine
the claimed result so new entries cannot pass an unknown outcome. Startup must
invoke its recovery method before any new dispatch. The dispatcher is not yet
composed into the daemon, and no queue receipt attests an order, fill or flat
account.
