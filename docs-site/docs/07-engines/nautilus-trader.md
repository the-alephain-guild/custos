---
title: "NautilusTrader Engine"
sidebar_position: 1
---

# NautilusTrader Engine

[NautilusTrader](https://github.com/nautechsystems/nautilus_trader) is an
event-driven, Python-native algorithmic trading platform with a Rust core. It is
the execution engine Custos ships with, and the one the runner's own contracts
were designed against.

Everything below lives under `src/custos/engines/nautilus/`. It is the only part
of the runner that knows NautilusTrader exists — the reconciler, the breaker and
the fact producer speak to it exclusively through
`ExecutionEngineProtocol`.
<!-- disclosure-ok: auditable source location, custos is open for exactly this -->

## The two hosts

`host.py` provides both implementations of the engine protocol:
<!-- disclosure-ok: auditable source location, custos is open for exactly this -->

**`NtTradingNodeHost`** supervises a real NautilusTrader `TradingNode`. It
declares `sandbox`, `testnet` and `live`, and it is the only host that can reach
a venue.

**`SandboxSimulationHost`** runs the entire local lifecycle — artifact
activation, credential resolution, durability, readiness, fact publication —
without connecting to anything. It declares `sandbox` and nothing else, so a
testnet or live deployment is refused at admission rather than quietly
simulated.

Select one for the lifetime of the process:

```bash
arx-runner start --enabled-mode sandbox --engine nautilus     # real execution
arx-runner start --enabled-mode sandbox --engine sandbox-sim  # simulation only
```

If the NautilusTrader runtime is not installed, `--engine nautilus` fails at
startup. It does not fall back to simulation — a runner that silently
substituted a non-trading host would report healthy while placing no orders.

## What the host owns, and what it does not

The host owns engine process construction, venue client configuration, readiness
observation, stop and reconfigure behaviour, and engine telemetry.

It does **not** own deployment authorization, strategy release state, artifact
verification, credential scope policy, or command acknowledgement. Those belong
to layers above it, which is why swapping the engine does not move any trust
boundary.

The engine entry point reflects that split:

```python
async def deploy(
    spec: dict,
    credential: dict,
    artifact: ActivatedEngineArtifactV1,
) -> str: ...
```

The `artifact` argument is an already-verified, already-activated strategy
object. The host adds it to the node; it never imports strategy code itself.
That is what keeps "which code ran" answerable from outside the engine.

## The protocol it satisfies

`ExecutionEngineProtocol` has two tiers, and both hosts implement the full
surface.

**Tier-1 — lifecycle and capability.** `deploy`, `reconfigure`, `stop`,
`supports_trading_mode`, `supports_venue`. These drive the command coordinator
and the lifecycle supervisor; the last two are what the
[live execution gate](/concepts/live-execution-gate) reads.

**Tier-2 — risk and connectivity state.** `get_open_notional`,
`check_engine_connected`, `flatten_positions`, `get_positions`, `get_orders`,
`get_engine_status`. These exist so the engine-agnostic guards — the notional
cap, the fallback breaker, the zombie watchdog — can enforce the
disconnect-resilient guarantee without knowing which engine is underneath.

Tier-2 is the more interesting half. It is why
[safety survives a disconnect](/trust-model/safety-survives-disconnect) is a
property of the runner rather than a property of NautilusTrader.

## Supporting modules

| File | Role |
|---|---|
| `venue_binance.py` | Binance data and execution client configuration |
| `binance_ledger.py` | Independent venue-ledger evidence for reconciliation |
| `portfolio_snapshot.py` | The single valuation boundary for equity and marked positions |
| `runner_safety.py` | Keeps the venue client behind the order reservation gate |
| `risk.py` | Pre-trade rule configuration |
| `runtime_loader.py` | Proves a strategy module came from the immutable activation root |
| `sandbox_runner_fact_host.py` | Fact publication for the simulation host |
<!-- disclosure-ok: auditable source location, custos is open for exactly this -->

Two of these are load-bearing for guarantees stated elsewhere on this site.

`portfolio_snapshot.py` is the reason open notional and actual equity come from
one valuation boundary rather than two that could disagree. The breaker reads
exactly one engine status per instance per tick, derived from it.

`runner_safety.py` wraps the venue execution client so that orders pass the
reservation boundary before reaching the venue. The guard is a facade the engine
cannot route around, rather than a check the strategy is asked to call.

## Venues

`binance` and `binance_perpetual`, compared case-insensitively. A signed
connector the host does not declare is refused at admission.

Spot and perpetual differ in more than a name — instrument identifiers, account
type and leverage configuration are all derived per connector, which is why the
connector is part of the signed command rather than a local setting.

## Installing the runtime

NautilusTrader is an optional dependency, declared under
`[project.optional-dependencies].nautilus` in `pyproject.toml`. An audit install
does not pull it: you can read and test the entire trust boundary without
installing a trading engine.

The published container image includes it. See
[installation](/getting-started/installation).
