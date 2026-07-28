---
title: "Sandbox Simulation Host"
sidebar_position: 2
---

# Sandbox Simulation Host

`SandboxSimulationHost` runs the full local deployment lifecycle without
connecting to a venue. It is selected with `--engine sandbox-sim`.

It is not a stub that does nothing. Artifact activation, credential resolution,
lifecycle durability, readiness receipts and RunnerFact publication all execute
for real — the venue connection is the only part that is absent. That makes it a
rehearsal of everything except the trade itself.

## What it is for

Use it to verify that enrollment, signed command intake, reconciliation and fact
delivery work end to end on your infrastructure, before any credential with
trading permission is involved.

It also backs the contract tests, which is the more important reason it exists:
the lifecycle is exercised on every run of the suite rather than only when
someone has a venue available.

## Why it declares only `sandbox`

`supports_trading_mode` returns true for `sandbox` and nothing else. A `testnet`
or `live` deployment is therefore refused at admission — condition 3 of the
[live execution gate](/concepts/live-execution-gate) — before anything else is
attempted.

That refusal comes from the host's own declaration rather than from a list of
forbidden combinations maintained elsewhere. A host that cannot trade says so,
and admission believes it.

The alternative would be far worse than a refused deployment: a live order
routed to a host that quietly does nothing is indistinguishable, from the
outside, from a live order that succeeded.

## Observed exposure

The simulator holds no positions, so `get_open_notional` returns exactly zero and
`flatten_positions` is a logged no-op. The breaker still runs against it, and its
trips are still observable — which is what makes breaker behaviour testable
without a venue.

## Source

`src/custos/engines/nautilus/host.py`, alongside `NtTradingNodeHost`. Both
satisfy the same `ExecutionEngineProtocol`; see
[NautilusTrader engine](/engines/nautilus-trader) for the protocol surface.
<!-- disclosure-ok: auditable source location, custos is open for exactly this -->
