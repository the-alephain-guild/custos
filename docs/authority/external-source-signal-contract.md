# Target-neutral external source signal

`src/custos/core/external_source_signal.py` defines `ExternalSourceSignal` as a
direction observation from one configured source. It is not a deployment
command, a venue order, or a subscriber authorization. Its parser receives
`tenant_id`, `expected_source_id` and `allowed_symbols` from trusted local
configuration; the alert body cannot select a tenant or a deployment.

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
`tests/test_external_source_signal.py`. No source publisher, NATS signal stream,
runner consumer or TradingView alert is activated by this file. Live mode and
real TradingView egress remain outside the current acceptance scope.
