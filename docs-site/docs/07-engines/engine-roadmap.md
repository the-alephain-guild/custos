---
title: "Engine Roadmap"
sidebar_position: 3
---

# Engine Roadmap

Custos does not hard-code a trading engine. Everything above the engine —
the reconcile loop, the live execution gate, the safety breaker and signed
RunnerFacts — talks to a single Python interface, `ExecutionEngineProtocol`.
Any engine that satisfies that interface can be supervised by the runner
without changing the safety machinery around it.

This page describes what ships today, what an engine adapter has to provide,
and which engines are plausible candidates next.

## What ships today

| Host | Module | Modes |
|---|---|---|
| `NtTradingNodeHost` | `custos.engines.nautilus` | `sandbox`, `testnet`, `live` |
| `SandboxSimulationHost` | `custos.engines.nautilus` | `sandbox` only |

Both ship in the same module; they are two implementations of one protocol, not
two engines. See [NautilusTrader](/engines/nautilus-trader) and
[the sandbox simulation host](/engines/sandbox-simulation-host).

The simulation host exists so an operator can exercise enrollment, command
delivery, reconciliation and fact publication without placing an order. It
declares `sandbox` and nothing else, so admission refuses it for `testnet` and
`live` — the refusal comes from the host's own declaration rather than from a
list of forbidden combinations maintained elsewhere. See
[the live execution gate](/concepts/live-execution-gate).

## What an engine adapter must provide

An adapter implements the Tier-1 surface of `ExecutionEngineProtocol`:

| Method | Responsibility |
|---|---|
| `deploy` | Start a strategy for one deployment instance |
| `reconfigure` | Apply a new desired state to a running instance |
| `stop` | Stop an instance and release its resources |
| `supports_trading_mode` | Declare which trading modes this engine may run |
| `supports_venue` | Declare which venues this engine can reach |

The last two are what the live execution gate reads. An engine that does not
declare a mode can never be admitted for it, regardless of what the desired
state asks for — the gate fails closed rather than degrading.

Because the reconciler and the gate only ever see the protocol, adding an
engine does not require changes to either.

## Candidate engines

These are open-source engines whose programming models are compatible enough
with the runner to be worth evaluating. None of them is scheduled; each entry
records what integration would actually involve.

### Hummingbot

[Hummingbot](https://github.com/hummingbot/hummingbot) is a Python framework
for market-making and liquidity provision across centralized and
decentralized venues.

- **Fits well**: Python-native and async, so an adapter runs inside the
  existing daemon with no process bridge.
- **Fits poorly**: a Hummingbot deployment is conventionally a standalone bot
  instance with its own config and strategy conventions, rather than a
  library embedded in a host process. Supervision would look closer to
  process management than to the in-process model used for NautilusTrader.
- **Not portable**: Hummingbot strategies cannot be moved across from
  NautilusTrader strategies without a rewrite.

### Freqtrade

[Freqtrade](https://github.com/freqtrade/freqtrade) is a Python crypto
trading bot with a DataFrame-based strategy interface and its own
backtesting engine.

- **Fits well**: Python-native, and its declarative strategy shape maps
  cleanly onto the existing desired-state and telemetry pipeline.
- **Fits poorly**: Freqtrade normally runs behind a REST API for its own UI.
  Custos exposes no inbound network surface by design, so an adapter would
  have to run it without that surface rather than proxy it.
- **Not portable**: the indicator-driven strategy interface is a different
  programming model from event-driven `on_bar` / `on_trade` handlers.

### A native Rust binding

NautilusTrader's execution core is progressively moving to Rust. A second,
Rust-native binding to that core — distinct from today's adapter, which
consumes the Python SDK — is conceivable.

Two bridge shapes are possible: an in-process extension module built with
`pyo3`, which keeps the runner's single-process async model intact; or a
supervised child process communicating over a local socket.

This one is performance-motivated rather than feature-motivated, and is only
worth scoping if profiling shows the current Python SDK path is a real
bottleneck under production load.

## Red lines apply to every engine

Whatever runs underneath, the four non-custodial guarantees hold:

- Credentials are decrypted inside the runner process and are never written
  to logs, published upstream, or passed to a child process as environment
  variables or command-line arguments where a process listing would expose
  them.
- Live execution requires an engine that declares live support; the gate
  fails closed otherwise.
- Local safety enforcement keeps working while ARX is
  unreachable.
- Money values use decimal arithmetic end to end and cross the wire as
  strings.

An adapter that cannot honour these is not a candidate.
