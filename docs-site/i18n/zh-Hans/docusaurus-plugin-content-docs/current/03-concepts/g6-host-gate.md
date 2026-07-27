---
title: "G6 Host Gate"
sidebar_position: 3
---

# G6 Host Gate

:::warning 中文翻译尚未完成
本章暂时显示英文原文。
:::

The G6 host gate is the check that stands between a deployment and a real
venue. Before Custos will start a strategy in `live` mode it verifies, in
four independent layers, that the engine underneath is actually capable of
live execution and that the credential it was handed is scoped correctly.

If any layer fails, the deployment is refused. The gate never degrades to a
weaker mode and never silently accepts.

## Why the gate exists

Custos ships more than one execution host. The no-op host is useful for
rehearsing enrollment, spec delivery and reconciliation without touching a
venue — it accepts a deployment and reports healthy without placing orders.

That behaviour is exactly what makes it dangerous in `live` mode: a live
order routed to a host that quietly does nothing would be indistinguishable
from a live order that succeeded. The gate makes that failure impossible
rather than unlikely.

## The four layers

Every layer runs on each `live` deployment. Each emits a distinct structured
event so an operator can tell from the log which one refused.

| Layer | Check | Refusal event |
|---|---|---|
| 1 | The host declares live support (`supports_live()`) | `g6_gate_live_capability_denied` |
| 2 | The host supports the venue the spec names (`supports_venue()`) | `g6_gate_venue_unsupported` |
| 3 | The strategy code hash in the spec matches the local source tree | `g6_gate_code_hash_mismatch` |
| 4 | The credential's permission scope is `trade_no_withdraw` | `g6_gate_credential_scope_violation` |

Layer 3 is what stops a signed deployment from running code other than the
code it was approved for. Layer 4 is a backstop — the credential vault
already refuses to store a withdraw-capable key — because a single enforcement
point is one mistake away from being bypassed.

The trading mode comparison is case-insensitive. The control plane and the
runner serialize the mode differently, and a case-sensitive comparison here
would produce a gate that silently never fires.

## Separation of duties

Live deployments additionally require two distinct approvers. Custos verifies
that the signed spec carries at least two distinct entries in `approved_by`
before it builds a live execution config; a spec that does not is refused with
`sod_approval_missing`.

Approval itself is a control-plane decision. Custos does not grant it — it
only refuses to act without evidence of it.

## Host and mode matrix

Host selection (`--engine`) and the spec's `trading_mode` form a six-cell
space. The two `live` cells are the load-bearing ones.

| `trading_mode` | Engine | Behaviour |
|---|---|---|
| `sandbox` | `noop` | Accepted as a no-op; reports `phase=running`, `health=healthy` |
| `sandbox` | `nautilus` | Real sandbox session against live market data with local simulated matching |
| `testnet` | `noop` | Gate does not apply below live; accepted as a no-op |
| `testnet` | `nautilus` | Real venue testnet endpoints with test funds |
| `live` | `noop` | **Refused at layer 1** (`g6_gate_live_capability_denied`); the deployment reports `phase=degraded` |
| `live` | `nautilus` | Runs only after all four layers and the two-approver check pass |

`phase` reports lifecycle state and `health` reports condition. They are not
interchangeable: a deployment can be `running` and unhealthy, and a refused
live deployment is `degraded` rather than absent.

## Choosing an engine

`arx-runner start` binds one host for the lifetime of the process:

- `--engine nautilus` (default) enables real sandbox, testnet and live
  execution. If the Nautilus runtime is not installed, deployment fails fast
  rather than falling back.
- `--engine noop` selects the no-op host for sandbox and contract rehearsal.
  A live spec is still refused at layer 1.

## What the gate does not do

The gate protects the boundary between a deployment and a venue. It is not a
risk engine and does not evaluate whether a trade is a good idea. Position
and exposure limits are enforced separately and continuously — see
[reconcile loop](./reconcile-loop) and
[disconnect behaviour](/trust-model/rl3-reconcile-disconnect).

Credentials handed to a host are used to construct the venue client and are
never retained in host state, written to logs, or published upstream. The
runner holds keys in memory to sign venue requests, which is unavoidable and
in scope; the guarantee is about the I/O boundary, not about memory. See
[credential vault](/operator-guide/credential-vault).
