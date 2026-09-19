---
title: "Offline testnet operation"
sidebar_position: 7
---

Use this path to run compatible strategy code against a venue's testnet without ARX. It uses real network orders with test funds. The standalone simulator exercise does not test this path.

## Prepare

1. Install the Nautilus extra on Python 3.12.
2. Create a standalone identity and broker topology using [standalone sandbox](/getting-started/standalone-sandbox), with a separate durable state directory.
3. Supply a real compatible strategy directory with `config.yaml` and its registry name. The simulator fixture is not suitable.
4. Provision testnet-only exchange credentials in the local vault. Match tenant and `provenance_ref.credential_id`.
5. Use a Binance connector supported by your testnet account. For SoDEX, first read its [input limitation](/engines/sodex).

## Configure and validate

Start from your strategy's offline spec. Set `trading_mode` to `testnet`, remove the `sandbox` object, verify connector/pairs/leverage and choose explicit risk limits. `risk_config` accepts only `max_total_notional` and `max_drawdown_pct`; values must be positive decimal strings or integers. Limits apply per deployment.

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

Testnet results and offline status cannot authorize live execution or become production promotion evidence.
