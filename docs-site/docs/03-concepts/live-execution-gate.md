---
title: "Live Execution Gate"
sidebar_position: 3
---

# Live Execution Gate

The live execution gate is the admission check that stands between a verified
deployment and a running engine. It runs once per deployment, before any engine
process is constructed, and it fails closed: a deployment that cannot satisfy
every applicable condition is refused rather than started in a weaker form.

Admission is not a risk check. It does not ask whether a trade is a good idea —
it asks whether this runner is permitted to execute this deployment, on this
venue, in this mode, with this credential, at all.

## Why it runs before the engine exists

The expensive failure is not a refused deployment. It is a deployment that
starts, reports healthy, and does something other than what was approved.

Every check below therefore runs before the engine is built, not after. A gate
that ran afterwards would have to stop something already connected to a venue,
and "stop it quickly" is a weaker guarantee than "never start it".

## The seven conditions

Admission is a single function. Each failure raises the same typed refusal, with
a distinct reason:

| # | Condition | Applies to |
|---|---|---|
| 1 | The artifact runtime capability is `READY` | every mode |
| 2 | The runtime mode equals the mode in the signed command | every mode |
| 3 | The engine declares support for that mode | every mode |
| 4 | The engine declares support for the signed connector | every mode |
| 5 | The credential is scoped `trade_no_withdraw` | `testnet`, `live` |
| 6 | Live execution is enabled in this build | `live` |
| 7 | The command carries signed promotion evidence | `live` |

Conditions 1–4 apply everywhere, including `sandbox`. There is no mode in which
admission is skipped — sandbox simply has fewer conditions to satisfy, not a
different code path.

### On condition 2

The mode is checked twice deliberately: once as the signed value that was
authorized, once as the value the local runtime is about to act on. Comparing
them catches the case where the runner is about to execute against a different
mode than the one that was approved, which no single-sided check can detect.

### On condition 6

Live execution is disabled by default. The flag becomes true only in the
composition root that consumes the final image receipt — it is not an operator
switch, an environment variable, or a configuration file setting.

That is why "enable live" is not something you can do to a running deployment.
The capability is a property of the build you are running, and a build that was
not produced through the release chain does not have it.

### On condition 7

Promotion evidence is issued by ARX and travels inside the signed command.
Custos verifies that it is present and bound to this deployment; it does not
evaluate who approved it or whether the approval was reasonable.

That division is deliberate. Custos refuses to act without evidence that the
decision happened, and has no opinion about the decision itself.

## Hosts

Two execution hosts ship, and their declared capabilities are what conditions 3
and 4 read:

| Host | Modes | Purpose |
|---|---|---|
| `NtTradingNodeHost` | `sandbox`, `testnet`, `live` | Real execution against a venue |
| `SandboxSimulationHost` | `sandbox` only | Exercises the full lifecycle without connecting to a venue |

The simulation host is useful because it runs artifact activation, credential
resolution, lifecycle durability, readiness and fact publication for real — only
the venue connection is absent. That makes it a rehearsal of everything except
the trade.

It declares `sandbox` and nothing else. A `testnet` or `live` deployment
therefore cannot reach it: condition 3 refuses before anything else is
attempted. The refusal comes from the host's own declaration rather than from a
list of forbidden combinations maintained somewhere else.

Host selection is bound for the lifetime of the process:

```bash
arx-runner start --engine nautilus     # default
arx-runner start --engine sandbox-sim
```

If the NautilusTrader runtime is not installed, `--engine nautilus` fails at
startup rather than silently falling back to simulation. A runner that quietly
degraded to simulation would report healthy while placing no orders.

## Venues

Connector support is declared per host and is currently `binance` and
`binance_perpetual`. The comparison is case-insensitive; a signed connector the
host does not declare is refused at condition 4.

## What refusal looks like

A blocked deployment produces a typed terminal outcome and a lifecycle fact. It
does not retry, because none of the seven conditions become true by waiting — an
unsupported venue is still unsupported on the second attempt.

This is the opposite of the transient path: an engine that fails to start for a
recoverable reason is retried under a durable restart budget. Admission failures
and runtime failures are deliberately not the same kind of event. See
[reconcile loop](/concepts/reconcile-loop) for the full disposition table.

## Verifying it yourself

The interesting question is not whether the checks exist but whether they can
fire. A condition that can never fail looks identical, in a passing suite, to
one that always passes.

Read `_require_authorized_runtime` in `src/custos/core/engine_lifecycle.py` —
that single function is the whole gate. There is no second admission path, and
no caller reaches engine construction without passing through it.
<!-- disclosure-ok: auditable source location, custos is open for exactly this -->

Then confirm no venue client is constructed anywhere outside the engine host:

```bash
grep -rn 'CEXOMS\|BinanceClient\|OKXClient' src/ \
  --exclude=host.py --exclude=venue_binance.py
```

Returns nothing on a clean tree. A venue client built elsewhere would be a path
around the gate entirely, and no amount of correctness inside the gate would
compensate.

Coverage:

| What | Test |
|---|---|
| Per-host mode and connector declarations | `tests/test_nautilus_host_capability.py` |
| Which host a given selection binds | `tests/test_main_host_selection.py` |
| Blocked capability and live mode refuse before any engine action | `tests/test_engine_lifecycle.py` |
| Venue adapter and credential wiring | `tests/test_nt_binance_venue.py` |
<!-- disclosure-ok: auditable source location, custos is open for exactly this -->

## What the gate does not do

It governs admission, not conduct. Exposure limits, drawdown breakers and the
notional cap are enforced continuously while a deployment runs, by modules that
know nothing about which engine is underneath — see
[safety survives a disconnect](/trust-model/safety-survives-disconnect).

Credentials handed to a host are used to construct the venue client and are
never retained in host state, written to logs, or published upstream. The runner
holds keys in memory to sign venue requests, which is unavoidable and in scope;
the guarantee is about the I/O boundary, not about memory. See
[credential vault](/operator-guide/credential-vault).
