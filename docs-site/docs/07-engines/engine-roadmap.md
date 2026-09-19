---
title: "Engine roadmap"
sidebar_position: 3
---

Custos currently ships a NautilusTrader host and a sandbox simulation host. The `hummingbot`, `freqtrade` and other reserved extras do not install working engine integrations.

## Current interface

An engine adapter must implement the lifecycle, capability, readiness, terminal-event, risk and connectivity contracts in `ExecutionEngineProtocol`. It must preserve local credential handling, execution admission, containment and exact money representations.

The current Nautilus host declares sandbox/testnet/live capability by connector, but the daemon keeps live disabled. See [NautilusTrader](/engines/nautilus-trader) for actual limits and [release status](/release-governance/release-status) for acceptance.

## Candidates

| Candidate | Main integration work |
|---|---|
| Hummingbot | Adapt standalone bot configuration and lifecycle to runner supervision; strategies need their own integration |
| Freqtrade | Map its strategy/configuration model and lifecycle without exposing a runner administration proxy |
| Native engine binding | Evaluate an in-process extension or supervised process boundary only where measured performance warrants it |

These are design candidates, not scheduled releases. A new adapter needs demonstrated ready/stop/failure behavior and complete safety/observation integration; implementing a few lifecycle methods is insufficient.
