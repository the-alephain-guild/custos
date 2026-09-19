---
title: "Trading modes"
sidebar_position: 2
---

Custos accepts three trading modes.

| Mode | Market data | Execution | Funds |
|---|---|---|---|
| `sandbox` | Live feed with Nautilus, none with `sandbox-sim` | Local simulation | Simulated |
| `testnet` | Venue testnet | Venue testnet orders | Test funds |
| `live` | Production venue | Production orders | Real funds; currently blocked by daemon admission |

## Lane and process selection

The signed lane requires one or more `--enabled-mode` arguments. Repeating the flag creates mode-scoped transport sessions; the mode in each signed command must belong to that configured set.

```bash
arx-runner start --enabled-mode sandbox --enabled-mode testnet --reconcile
```

This fragment shows mode selection only. Identity, transport, signing-key and artifact prerequisites are in [deployment](/operator-guide/deployment).

The offline lane is selected by `--reconcile-strategy-id`. It reads the mode from each `OfflineDeploymentSpec`, accepts only sandbox/testnet, and does not require `--enabled-mode`. `deployment validate/publish --mode` can additionally assert the expected spec mode; it cannot override it.

The Nautilus 2 host admits one active node per event loop. Multiple enabled modes are not a promise of concurrent deployments in one process.

## Admission

Signed commands are checked for exact mode binding, host/connector support, artifact capability and credential scope. Live additionally requires enabled execution capability and signed promotion evidence. The current daemon sets live execution to disabled.

Offline input uses a separate contract and admission path. It does not require signed deployment approval, but still rejects live and keeps local credentials and safety checks. Neither offline results nor sandbox development artifacts can be promoted locally to production.

## Transport

Signed sandbox and testnet sessions use `--nats-sim-*`; signed live transport uses `--nats-live-*`. Transport connectivity does not enable live execution. Offline traffic uses the operator-owned broker selected by `--nats-url`.

See [connector support](/engines/nautilus-trader) before choosing a venue and mode.
