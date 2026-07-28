---
title: "Trading Modes"
sidebar_position: 2
---

# Trading Modes

There are exactly three modes, and the set is closed:

```text
sandbox    testnet    live
```

No fourth value exists — not "paper", not "production", not an empty default.
A mode that could be omitted would be a mode nobody chose, and the choice here
decides whether real money is at risk.

## What each one means

| Mode | Market data | Fills | Money |
|---|---|---|---|
| `sandbox` | live or none, depending on engine | locally simulated | none |
| `testnet` | real venue testnet | real venue testnet | test funds |
| `live` | real venue | real venue | **real** |

`sandbox` covers two useful arrangements. With `--engine sandbox-sim` nothing
connects to a venue at all; with `--engine nautilus` you get real market data
with locally simulated matching. Both are safe; they differ in whether the
prices are real.

## One mode per process

`--enabled-mode` is required on `arx-runner start` and takes exactly one value.
A runner does not switch modes at runtime and does not serve two modes at once.

That is why mode belongs to the process rather than to a deployment: a single
runner handling both testnet and live would have exactly one bug between test
funds and real ones.

## Mode is signed, and checked twice

Mode appears in the subject, the envelope and the payload, and all three must
agree. Admission then compares the mode in the signed command against the mode
the local runtime is about to act on.

Comparing them catches the case where a runner is about to execute against a
different mode than the one that was authorized — which no single-sided check
can detect. See [the live execution gate](/concepts/live-execution-gate).

## What each mode requires

Requirements accumulate; nothing is relaxed as you move down the table.

| | sandbox | testnet | live |
|---|---|---|---|
| Signed command | ✅ | ✅ | ✅ |
| Engine declares the mode | ✅ | ✅ | ✅ |
| Engine declares the connector | ✅ | ✅ | ✅ |
| Credential scoped `trade_no_withdraw` | | ✅ | ✅ |
| Live execution enabled in the build | | | ✅ |
| Signed promotion evidence | | | ✅ |

The last two are what make `live` different in kind rather than in degree.
Live execution is off unless the build was produced through the release chain,
and it is not an environment variable or a configuration setting — so it is not
something an operator can switch on under pressure, including pressure applied
by someone else.

## Transport follows mode

Only `live` uses the live transport. Both `sandbox` and `testnet` use the
simulation transport, so a testnet runner is configured with the `--nats-sim-*`
flags, not the `--nats-live-*` ones.

This surprises people, so it is worth stating plainly: **testnet is not on the
live transport.** Test funds are still not real funds, and the transport
separation follows the money, not the venue.

## The boundary that cannot be crossed silently

A DeploymentSpec cannot move between simulated and real-money execution without
that being visible: the mode is part of the signed material, so changing it
changes what was signed.

There is no promotion path inside the runner. A deployment does not "graduate"
from testnet to live locally — a live deployment is a different signed command
carrying evidence that a decision was made upstream.

## Choosing one

| You want to | Use |
|---|---|
| Prove enrollment, credentials and reconciliation work | `sandbox` with `--engine sandbox-sim` |
| Exercise a strategy against real market data, risk-free | `sandbox` with `--engine nautilus` |
| Exercise the full venue round trip with test funds | `testnet` |
| Trade | `live` |

Start at the top. Each row exercises everything the rows above it do, which
means a failure at any level has already been ruled out by the level below.
