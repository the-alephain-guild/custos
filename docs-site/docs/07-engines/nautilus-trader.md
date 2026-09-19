---
title: "NautilusTrader engine"
sidebar_position: 1
---

Custos integrates NautilusTrader through the optional `nautilus` extra. Install it with `make install-nt` on Python 3.12.

<!-- generated:nautilus-version -->

Engine API: `NautilusTrader 2`.

<!-- /generated:nautilus-version -->

Use the installation commands in this guide to keep the engine and runner compatible. Supported platforms and Python requirements are listed in [installation](/getting-started/installation).

## Host selection

| CLI value | Host | Behavior |
|---|---|---|
| `--engine nautilus` | `NtTradingNodeHost` | Real data clients; locally simulated fills in sandbox, venue orders in testnet |
| `--engine sandbox-sim` | `SandboxSimulationHost` | Local lifecycle simulation without importing a trading strategy or connecting to a venue |

The signed simulation composition adds fact publication around the simulator. The offline composition reports unsigned local status. Neither simulator output proves a real venue round trip.

## Connector declarations

<!-- generated:venues -->

| Connector | sandbox | testnet | live |
|---|---|---|---|
| `binance` | declared | declared | declared |
| `binance_perpetual` | declared | declared | declared |
| `sodex` | declared | declared | unsupported |
| `sodex_perpetual` | declared | declared | unsupported |

<!-- /generated:venues -->

These are host declarations, not production acceptance. Live execution is disabled by the current daemon. SoDEX testnet also has an input-contract limitation; read [SoDEX](/engines/sodex) before configuring it.

## Concurrency and readiness

Custos rejects a second active Nautilus node on the same event loop. Run concurrent nodes in separate processes with separate identity and state roots.

Engine readiness checks the node task, data/execution connectivity, portfolio initialization and reliable valuation, reconciliation, strategy lifecycle acceptance and mandatory capabilities. A successful node construction or daemon health probe does not establish all of these conditions.

## Engine interface

`ExecutionEngineProtocol` defines deployment, reconfiguration, stop, capability queries, typed ready/terminal events, connectivity, order/position snapshots, valuation and containment. Signed deployment passes an already-activated artifact:

```python
async def deploy(
    spec: dict,
    credential: dict,
    artifact: ActivatedEngineArtifactV1,
) -> str: ...
```

Offline deployment supplies an operator-mounted artifact with a directory-derived identity. It has no signed-release assurance.

## Configuration and stop behavior

The host reads `nautilus_config` startup timeouts and reconciliation lookback. Connector-specific code determines instrument identifiers, account type, leverage and client configs. Do not reuse one venue's symbols or account fields for another.

Stop behavior follows the deployment's shutdown policy. The default preserves positions; stopping the runner is not proof that positions are flat. See [emergency recovery](/operator-guide/emergency-playbook).
