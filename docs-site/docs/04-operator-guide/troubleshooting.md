---
title: "Troubleshooting"
sidebar_position: 6
---

Start with the selected lane, source/image revision, CLI arguments and local JSON logs. Keep credential material out of diagnostic output.

| Symptom | Check | Action |
|---|---|---|
| Health passes but signed deployments never arrive | `deployment_subscription` and `--reconcile` | Start with reconciliation enabled and the issued command trust inputs |
| `Runner startup authority check failed` | `runner.toml`, vault path, age key, expiry and identity binding | Correct paths or renew the identity through its supported lifecycle |
| Standalone identity refused by signed command | `backend_url` names `.invalid` | Use the offline lane, or enroll a separate identity with ARX |
| No local desired-state delivery | Broker URL, bootstrap, tenant and strategy id | Use matching `nats bootstrap`, publish and start inputs |
| `stream ... is not owned` | Existing broker topology | Choose an isolated broker/tenant; do not overwrite another application's stream |
| Spec rejected before publish | Mode, exact schema fields, source digest | Run `deployment validate`; live cannot use the offline lane |
| Credential decrypt/scope failure | Key id, tenant, vault path, age identity | Run `vault verify`; signed scope must match the deployment |
| `credential scope already has an active ...` | Existing testnet deployment using that scope | Stop and inspect the existing instance before reusing it |
| `already holds this runner's event loop` | Existing Nautilus node | Stop it or use a separate process and state root |
| `strategy discovery is already pointed at ...` | Mounted strategy directory | Use one directory per offline process |
| `strategy config ... does not set trading.<key>` | `config.yaml` under the spec's `strategy_path` | Set `trading.connector`, `trading.pairs` and `trading.leverage` in that file; built-in defaults are not used |
| Signed deployment quarantined with `strategy_trading_scope_mismatch` | The connector, instruments and leverage in the refusal, strategy side against deployment side | Create the deployment with the scope the strategy release declares; the runner does not run a strategy on instruments or leverage its deployment did not authorize |
| Quarantined with `strategy_trading_scope_undeclared` or `signed_strategy_config_overrides_trading` | The strategy's config, and the signed strategy config | The strategy must declare `trading` in its config; the signed strategy config may not carry a `trading` section |
| `portfolio_prices_missing:<instrument>` | Exact missing instrument and its mark/quote asset | Check symbol, network and market-data availability; do not substitute zero |
| `portfolio_equity_missing:<currency>` | Account and settlement currency | Confirm the account balance and required valuation prices |
| Offline generation refused after trip | Guard latch and recent containment logs | Inspect positions/orders, address the cause, then deliberately restart |
| SoDEX testnet account fields unavailable | Offline spec contract | Use the supported sandbox path; see [SoDEX](/engines/sodex) |

## Signed command failures

Check signature/key id, subject identity, exact digest, generation, artifact binding, capability and credential scope. Invalid input is terminal; recoverable local dependency failures are retried. Correct the canonical desired state in ARX rather than injecting unsigned input into the signed subscription.

## Vault check

```bash
uv run arx-runner vault verify --key-id "$KEY_ID" --tenant-id "$TENANT_ID"
```

Add `--vault-dir` if customized. This uses the same explicit sops JSON decrypt path as the runner. Successful manual sops decryption alone does not test tenant/scope checks.

## Evidence to retain

Record instance/spec id, generation, revision, timestamp, typed failure reason and non-secret health fields. Preserve SQLite state and its WAL/SHM files before recovery. The offline status channel is best effort; check local logs if publication fails. See [emergency recovery](/operator-guide/emergency-playbook).
