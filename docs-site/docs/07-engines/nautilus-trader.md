---
title: "NautilusTrader Engine"
sidebar_position: 1
---


# NautilusTrader Engine

> Status: **implemented** (`src/custos/engines/nautilus/`). This page is an
> overview and index — the full design (process lifecycle, G6 gate, venue
> adapters) lives in [`docs/design/nautilus_host.md`](/engines/nautilus-trader)
> and is not duplicated here.

## What it is

[NautilusTrader](https://github.com/nautechsystems/nautilus_trader) is an
event-driven, Python-native algorithmic trading platform with a Rust core.
It is Custos's reference execution engine: the six core modules (reconcile,
telemetry_actor, credential_vault, etc.) were designed and validated against
it first, and every other engine in this directory is a future integration
that follows the same `ExecutionEngineProtocol` contract NautilusTrader
already satisfies.

## Relationship to `ExecutionEngineProtocol`

`src/custos/engines/nautilus/host.py` provides two implementations:

- `NoopHost` — a stub that never touches a real venue; used for paper / dev
  runs and as the fail-safe target when the G6 gate denies a live deploy.
- `NtTradingNodeHost` — the real implementation, supervising a NautilusTrader
  `TradingNode` process across `sandbox` / `testnet` / `live` trading modes.

Both satisfy the Tier-1 contract in
[`docs/design/engine_protocol.md`](/engines/engine-roadmap); the mapping
from each Tier-1 method to the underlying NT SDK call is documented in
[`docs/design/nautilus_host.md`](/engines/nautilus-trader).

## Where to look next

| Topic | Doc |
|-------|-----|
| Process lifecycle, G6 gate, venue adapters | [`docs/design/nautilus_host.md`](/engines/nautilus-trader) |
| `ExecutionEngineProtocol` Tier-1/Tier-2 contract | [`docs/design/engine_protocol.md`](/engines/engine-roadmap) |
| Strategy loading (vendored toolkit) | `src/custos/engines/nautilus/strategy_loader.py` |
| Binance venue adapter | `src/custos/engines/nautilus/venue_binance.py` |
| Optional dependency | `pyproject.toml` → `[project.optional-dependencies].nautilus` |

## Follow-up plans

Strategy migrations onto this engine (e.g. the Supertrend strategy) and the
vendored indicator toolkit under `src/custos/engines/nautilus/toolkit/` are
tracked by their own plans, not this stub.
