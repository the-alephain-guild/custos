# External signal input contract

This contract defines parsing only. It does not authorize a sender, activate a
deployment, place an order, or attest a venue fill. The owner is Custos. The
implementation is `src/custos/core/external_signal_contract.py`; tests are in
`tests/test_external_signal_contract.py`.

## Two closed message shapes

`OKX_B` models [OKX Signal Bot alert specification 2.0, Section B](https://www.okx.com/zh-hans-sg/help/signal-bot-alert-message-specifications#section-b-universally-compatible).
The required JSON keys are `action`, `instrument`, `signalToken`, and `timestamp`.
The accepted actions are `ENTER_LONG`, `ENTER_SHORT`, `EXIT_LONG`, and
`EXIT_SHORT`. The optional keys are `maxLag`, `orderType`, `orderPriceOffset`,
`investmentType`, and `amount`. No other key is accepted in this shape.

An instrument must match the caller's exact allowlist. In particular,
`ETHUSDT.P` and `ETH-USDT-SWAP` are different strings and neither is inferred
from the other. The caller must bind an allowed instrument to its actual venue
contract before executing. `timestamp` is an ISO 8601 UTC emission time, not the
bar-close time. `maxLag` is an integer from 1 through 3600 seconds; when omitted,
the parser uses 60 seconds for Section B compatibility. Senders in this project
must include it explicitly because the OKX specification contains conflicting
prose about the default. A future or expired timestamp is refused.

`orderType` is `market` or `limit`; `limit` requires `orderPriceOffset` from 0
through 100 percent. The offset is refused for a non-limit order. Entry
`investmentType` may be `margin`, `contract`, `percentage_balance`, or
`percentage_investment`; exit may use `percentage_position`. An amount is
optional only because the OKX bot can be preconfigured. When present, it is a
positive exact Decimal value; contract count must be integral, and percentages
must be between 0.01 and 100. This parser does not infer missing bot settings.
There is no leverage or risk command in Section B. `signalToken` is preserved
only in memory and omitted from object representations. Parsing its presence
does not authenticate a request to Custos.

`CUSTOS_V1` contains the Section B trade fields and exactly these required
extensions: `schemaVersion: 1`, `sourceId`, `strategyRevision`,
`deploymentInstanceId`, `timeframe: "30"`, `barCloseTime`,
`requestedLeverage`, and `eventId`. `riskAction: "FLATTEN"` is optional and
valid only with an exit action. The source and strategy identifiers are short
ASCII labels. The deployment UUID must equal the authorized instance given by
the caller. `barCloseTime` is an aligned 30-minute close in UTC, at or before
the emission timestamp. `requestedLeverage` is an integer from 1 through 125;
an entry must match the deployment's authorized leverage exactly. A mismatch
does not prevent an otherwise authenticated risk-reducing exit. No field in
this message changes the venue's leverage dynamically.

`eventId` is deterministic and Pine-reproducible:

```text
sourceId:strategyRevision:instrument:timeframe:barCloseEpochMs:action:TRADE|FLATTEN
```

The final component is `FLATTEN` only when `riskAction` is present. A changed
action or risk reason therefore has a different identity. The receiver must
compare the supplied ID with the value recomputed from the other fields.

## Implementation state

The Section B and runner-extension parser, a token-free SQLite inbox, and an
HMAC-authenticated loopback HTTP handler have local tests. The HTTP handler is
deliberately not registered as a runnable `arx-runner` subcommand yet: its
scope must be supplied by a verified active deployment, not by operator flags
that merely claim an instance, instrument, and leverage. The inbox acceptance
receipt is not a strategy-delivery or venue-fill receipt. No external signal
path is active in the daemon at this stage.

The handler requires an `X-Custos-Signature` HMAC header. TradingView's
[webhook documentation](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/)
documents a POST body and its `Content-Type`, but does not offer a configurable
HMAC header. Consequently, this handler is not a direct TradingView webhook
endpoint. A future TradingView path needs a separately verified TLS edge which
authenticates the sender, strips any inbound assertion headers, computes the
HMAC over the exact forwarded body, and forwards only to the loopback listener.
TradingView separately documents a client-certificate authentication option;
its verification and edge configuration have not been implemented or tested.
The `CUSTOS_V1` `signalToken` field is compatibility syntax, never ingress
authentication and must carry no exchange secret.

## Boundaries still to implement

The HTTP ingress in plan 42 T3 must authenticate the source independently of
`signalToken`, enforce size and rate limits, bind the instance and instrument
allowlist, and persist a sanitized event before acknowledging it. It must not
persist the raw token or let the alert choose credentials. Its durable inbox
will decide duplicate, conflicting, out-of-order, and restart behavior. The
Nautilus bridge in T4 must distinguish delivery and intent from a confirmed
position change. For `FLATTEN`, a later entry is allowed only after the old
entry orders are contained and the actual position is confirmed flat; the
external event does not trip the runner's permanent breaker.

The inbox persists the latest risk-event fence and blocks newer entries until
`confirm_flat` is called for that exact delivered risk event. The method
requires an independently verified venue-flat observation from its future
runtime caller; marking delivery alone does not release the fence. An older
database fence without a risk-event identity migrates conservatively and
continues blocking entries. This is a durable safety primitive, not proof that
the Nautilus bridge or venue confirmation exists yet.

Acceptance now requires an `EngineLifecycleAuthority` supplied by the runner,
which fixes the exact deployment instance, signed spec digest, generation, and
non-live mode at the time of receipt. Reading pending events, marking delivery,
and confirming flat require that same instance, spec ID, digest, and generation. A new
generation cannot consume old events. Rows from an earlier database schema
migrate with an empty digest and generation zero, so they cannot be delivered
into any valid current generation. Constructing an authority object is not
itself proof that the caller verified the signed command; the daemon must
derive it from its active verified deployment before enabling this path.

`NtTradingNodeHost.external_signal_runtime_scope()` now exposes only the
non-live OKX perpetual instruments and leverage of a currently running
strategy that implements both signal receipt and pending-entry removal. The
host has already checked that strategy's trading declaration against its
deployment at `deploy()`. This read-only scope does not authenticate a sender,
prove that the daemon used the signed command lane, or turn on the HTTP
listener. The daemon must recheck the active signed generation before each
delivery; a scope captured before a stop or replacement is stale.

The ingress wire's `investmentType: "margin"` amount is quote-currency margin.
The toolkit's `SignalExecutionCoordinator` instead treats `signal.amount` as
quote-currency order notional, overriding its calculated size before the order
factory converts notional into contract quantity. Passing the wire amount
unchanged would lose the requested leverage's sizing effect. The future bridge
must use an exact Decimal conversion under the signed leverage and local
maximum-exposure policy, and must define full-position exits separately.
`external_signal.to_toolkit_signal()` now provides a pure, non-delivering
conversion for the narrow `CUSTOS_V1` market-order shape: an entry's exact
quote margin is multiplied by the active scope's leverage and refused above a
caller-supplied maximum order notional; a 100% position exit has no toolkit
`amount`, so the toolkit closes the actual position rather than 100 quote
units. The caller must obtain that maximum from the active signed runner
policy, not from the alert, and pass its explicit settlement currency. A
different currency is refused; any required conversion needs an independent
trusted price before calling this helper. The helper rejects risk `FLATTEN`, different
deployment generations, unsupported order forms and inexact money input.
Event-to-order lineage and the actual runtime policy source are still untested;
neither this helper nor the host scope method queues a signal or confirms a fill.

The source Nautilus `SignalProcessorStrategy` still consumes queued signals
on its next configured bar. It now drops an event when that bar's event time is
past `timestamp + maxLag`, waits if the bar precedes the event, and can remove
queued entries when runtime containment begins. This prevents a 30-minute
execution bar from silently trading a 60-second alert on the next bar, but
does not make execution immediate. A future deployment must use a tested
execution-bar cadence and latency budget, and its runtime must call the queue
removal plus independently cancel orders and confirm venue flat. This source
change is not part of the authorized rc10 wheel. No daemon delivery is wired yet.

Neither message shape creates a signed desired-state command. The signed
deployment and local risk policy remain the authority for venue, account,
instrument, leverage, and maximum exposure. This contract permits local
fixtures and independently authorized sources. TradingView-sourced machine
delivery remains blocked by the source-rights gate in the coordinating plan.
