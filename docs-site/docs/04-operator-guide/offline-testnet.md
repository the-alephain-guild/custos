---
title: "Offline testnet operation"
sidebar_position: 7
---

Use this path to run compatible strategy code against a venue's testnet without ARX. It uses real network orders with test funds. The standalone simulator exercise does not test this path.

## Prepare

1. Install the Nautilus extra on Python 3.12.
2. Create a standalone identity and broker topology using [standalone sandbox](/getting-started/standalone-sandbox), with a separate durable state directory.
3. Supply a real compatible strategy directory and its registry name. Its `config.yaml` must set `trading.connector`, `trading.pairs` and `trading.leverage` explicitly. The simulator fixture is not suitable.
4. Provision testnet-only exchange credentials in the local vault. Match tenant and `provenance_ref.credential_id`.
5. Use a Binance connector supported by your testnet account. For SoDEX, first read its [input limitation](/engines/sodex).

## Configure and validate

Start from your strategy's offline spec, which must declare `"spec_version": 2`; `arx-runner deployment schema` prints the version your runner accepts. Set `trading_mode` to `testnet`, remove the `sandbox` object and choose explicit risk limits. The spec does not carry the connector, pairs or leverage: the runner reads them from the `trading` section of `config.yaml` under `strategy_path`, the same values the strategy reads, and refuses a spec that still sets them. Confirm those three suit your testnet account before publishing. `risk_config` accepts only `max_total_notional` and `max_drawdown_pct`; values must be positive decimal strings or integers. Limits apply per deployment.

Set `SPEC_FILE`, `STRATEGY_DIR`, `STATE_ROOT`, `TENANT_ID`, `STRATEGY_ID`, `RUNNER_LABEL` and `NATS_URL` to your local configuration. Export the age identity path through `SOPS_AGE_KEY_FILE`.

```bash
uv run arx-runner deployment validate --mode testnet \
  --spec-file "$SPEC_FILE" --strategy-dir "$STRATEGY_DIR"
uv run arx-runner deployment publish --mode testnet \
  --spec-file "$SPEC_FILE" --strategy-dir "$STRATEGY_DIR" \
  --tenant-id "$TENANT_ID" --strategy-id "$STRATEGY_ID" --nats-url "$NATS_URL"
uv run arx-runner start --engine nautilus \
  --runner-toml "$STATE_ROOT/runner.toml" --vault-dir "$STATE_ROOT/vault" \
  --ready-file "$STATE_ROOT/ready.json" --offline-state "$STATE_ROOT/offline.db" \
  --reconcile-strategy-id "$STRATEGY_ID" --runner-label "$RUNNER_LABEL" \
  --nats-url "$NATS_URL"
```

`--strategy-dir` checks/binds the source digest; `strategy_path` in the spec must identify the same readable directory inside the runner environment. Avoid changing mounted code while the deployment runs.

The offline bridge derives a credential scope from the credential id. The host uses it to prevent conflicting active deployments; the offline schema has no `credential_scope` field to edit.

## Observe and stop

Inspect local status at `arx.<tenant>.deployment_status.<runner-label>.<spec-id>`, then confirm engine readiness, orders and positions against the testnet. Local `health: healthy` reports application of desired state, not a signed acceptance receipt.

To stop, publish the same spec id with a higher generation and `lifecycle_state: stopped`. The default engine stop policy preserves positions. Verify outstanding orders and positions at the venue before ending supervision. A breaker trip has separate containment behavior; see [emergency recovery](/operator-guide/emergency-playbook).

## When the venue refuses a cancel

Containment and stop withdraw orders one at a time and read each refusal before asking again:

| Refusal | What the runner does |
|---|---|
| The order is already waiting for its cancel | Sends nothing more; asks the venue for the order if no answer comes within 15 seconds |
| The venue does not know the order | Stops cancelling it and asks the venue for it three times: at once, then 10 and 20 seconds later. If it is still open locally 40 seconds after the last answer, it is left for reconciliation and no further requests are sent for it |
| Rate limited | Pauses every safety request on that account (cancels, closes and queries) for 60 seconds, doubling up to 600; never falls back to cancelling everything |
| Connection failure | Pauses the account for 5 seconds, doubling up to 120; gives up on the order after 10 retries |
| Any other refusal | Retries that order after 2 seconds, doubling up to 60; gives up after 5 retries |

Only SoDEX refusal texts are classified today; on other venues every refusal is handled as "any other refusal". On SoDEX the safety path also keeps to ten requests at once and then one a second per account, leaving the rest of the account's order allowance to whatever else trades on it.

An order the runner gave up on is not cancelled: the stop or containment still reports it as unconfirmed. The stop's error log and `stop_summary.residual_orders` count what is left by reason, such as `in_flight`, `not_found`, `venue_state_contradicted`, `cancel_abandoned`, `paused` or `backing_off`. Check those orders at the venue before ending supervision.

## Dust in status and stop reports

Dust is a holding below the smallest order the pair accepts: under its minimum order quantity, or, when a price is known, worth less than its minimum notional. No order can sell it or close it, so containment and a flatten stop treat it as flat, send no close for it and do not wait for it. A spot fee taken in the base currency commonly leaves such a remainder.

Dust appears in the `dust` list of the engine status and the telemetry snapshot, and in `stop_summary.dust` and `offline_shutdown_summary` when the lane stops. Each entry names the instrument, the asset, the quantity and whether it was read from the position or the account balance, the minimum it is below and why (`quantity` or `notional`), and the price if one was known. `position_count` includes dust positions.

Without a price, dust is left out of the equity and exposure the guard evaluates, so it cannot trip the breaker. A holding at or above the minimum quantity is never treated this way: without a price the status is unreliable and the guard fails closed. On OKX, where one minimum order can be worth tens of dollars, dust is valued like any other holding and fails closed when unpriced.

## Keep container logs bounded

Run the runner container with log rotation, for example `max-size` and `max-file` options on the Docker `json-file` logging driver, so that a long or noisy run cannot fill the disk. Read the stop's exit status before exporting its logs: a large export can outlast the time you allow for the stop.

Testnet results and offline status cannot authorize live execution or become production promotion evidence.
