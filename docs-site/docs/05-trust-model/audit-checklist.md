---
title: "Audit Checklist"
sidebar_position: 7
---

# Audit Checklist

Custos is open so that the people trusting it with exchange credentials can
check that trust themselves. This page is the concrete version of that offer:
what to run, what to read, and what a passing result actually proves.

Nothing here requires our cooperation. Clone the repository and work through
it.

```bash
git clone https://github.com/the-alephain-guild/custos.git
cd custos
uv sync --extra dev
```

## Step 1 — Reproduce the baseline

```bash
make verify
```

This runs formatting, lint and the standalone test baseline. It has to pass on
a clean clone with no credentials and no network access to our infrastructure.
If it does not, stop — everything below assumes a green baseline.

`make test-baseline` alone runs the suite without the style gates.

## Step 2 — Keys never leave the host

The claim: credentials are decrypted in-process and never written to a log,
published upstream, or passed anywhere they could be observed.

```bash
# no credential material in log calls
grep -rnE 'log\.(info|debug|warning).*api[_-]?key' src/ tests/

# no credential material in outbound calls
grep -rnE 'publish.*password|send.*secret' src/
```

Both should return nothing.

Then read the vault itself — `src/custos/core/per_key_vault.py` and
`machine_credential_vault.py`. What to check: the secret reaches `sops` on
stdin rather than argv; the decrypt result is used to build a client and not
retained; every decrypt emits an audit event carrying only an identifier.

The relevant tests are `tests/test_per_key_vault.py` and
`tests/test_credential_lifecycle.py`. The second one is the interesting one: it
walks the constructed engine object graph and asserts no credential is
reachable from it.

## Step 3 — Live execution is always gated

The claim: reaching a live venue requires four independent checks, and the gate
fails closed.

```bash
# no venue client constructed outside the engine host
grep -rn 'CEXOMS\|BinanceClient\|OKXClient' src/ --exclude=host.py --exclude=venue_binance.py
```

Should return nothing — a venue client built anywhere else would be a path
around the gate.

Read the gate in `src/custos/engines/nautilus/host.py` — `supports_live` and
`supports_venue` are the capability surface admission queries — then check that
each layer has a test proving it is live rather than dead code. The distinction
matters: a check that can never fire looks identical to a check that passes.
See [live execution gate](/concepts/live-execution-gate) for the four layers.

`tests/test_nautilus_host_capability.py` covers the capability declarations and
`tests/test_main_host_selection.py` covers which host a given mode may bind.

## Step 4 — Safety survives a disconnect

The claim: local enforcement keeps working when the platform is unreachable,
and the runner neither stops nor runs unguarded.

```bash
# no blanket shutdown anywhere in the runtime
grep -rn 'stop_all_strategies\|force_shutdown' src/custos/
```

Should return nothing.

The three guards each have their own module and their own tests:

| Guard | Module | Test |
|---|---|---|
| Aggregate cap | `src/custos/core/local_cap.py` | `tests/core/test_local_cap.py` |
| Fallback breaker | `src/custos/core/fallback_breaker.py` | `tests/core/test_fallback_breaker.py` |
| Zombie watchdog | `src/custos/core/zombie_watchdog.py` | `tests/core/test_zombie_watchdog.py` |

Confirm each is evaluated on a local tick rather than in response to an
upstream message — a guard that needs the platform to tell it to run is not a
guard against the platform being gone.

## Step 5 — Money arithmetic is exact

The claim: decimal end to end, strings on the wire, no float in a money path.

```bash
grep -rnE 'float\(.*price|float\(.*amount|float\(.*notional' src/
```

Should return nothing.

`tests/test_nt_risk_engine.py` and `tests/test_runner_fact_store.py` exercise
the decimal paths and their wire representation. The thing to check while
reading is that values are constructed as `Decimal(str(x))` rather than
`Decimal(x)` — the latter silently inherits binary float error, and the two
look identical at a glance.

## Step 6 — Verify the artifact you will actually run

A clean source tree proves nothing about the binary you deploy.

```bash
make verify-local-v030
```

This builds the image, records the revision label, and runs the full Docker
runtime contract plus a standalone acceptance against a real broker.

For release artifacts, see [signed release chain](./signed-release-chain) —
the wheel is signed, the image digest is recorded, and the runtime gate runs
against that exact digest before any stable tag points at it.

## Step 7 — Check the boundary claims

Read [what is custos](/introduction/what-is-custos) and
[the trust model](/introduction/trust-model), then verify against the code that
the runner:

- exposes no inbound network surface other than its outbound subscriptions;
- has no local path to create or approve a deployment;
- refuses a live deployment lacking promotion evidence, rather than proceeding;
- cannot be instructed to decrypt a credential and return it.

The last one is worth checking directly. `decrypt` is called only by the local
reconciler. If you find any path from a network message to a decrypt result
leaving the process, that is a critical finding —
[report it](https://github.com/the-alephain-guild/custos/blob/main/SECURITY.md).

## What a pass means

Passing every step means the code in the tree you cloned honours the four
guarantees, and that the artifact built from it behaves the same way.

It does not mean your deployment is safe. Custos cannot protect you from a
credential with withdraw permission, an exchange account without IP
restrictions, a host other people can read, or a strategy that loses money
correctly. Those remain yours.
