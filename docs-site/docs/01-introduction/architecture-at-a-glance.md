---
title: "Architecture at a Glance"
sidebar_position: 3
---

# Architecture at a Glance

Custos is a daemon you run on your own machine. It receives signed instructions,
executes them against a venue using credentials that never leave that machine,
and reports back what happened in signed statements.

This page is the shape of the whole thing in one read. Each section links to the
chapter that goes deeper.

## The split

```text
  ARX  ──── signed deployment command ────▶  Custos
   ▲                                          │
   │                                          ├─▶ credential vault (local)
   └──── signed runner facts ─────────────────┤
                                              └─▶ venue (Binance, …)
```

**ARX** authenticates who you are, authorizes what you asked for, owns the
deployment record, and decides what should be running. It never holds a venue
credential and never places an order.

**Custos** verifies the instruction, resolves credentials locally, runs the
strategy, enforces local safety, and signs statements about what the engine
actually did. It never decides what *should* run and cannot approve its own
deployment.

Two directions, two signature checks, and no shared secret between them. That is
the entire trust model; everything below is how it is held up.

## Why the boundary is where it is

The runner is open source because that is the only way the credential claim can
be checked. A closed runner asking to hold your exchange keys is asking for
trust it cannot demonstrate.

So the question a reader should be able to answer is not "do I believe this
document" but "does the code do what it says". Every guarantee chapter on this
site names the file and the test, so you can confirm rather than accept — see
the [audit checklist](/trust-model/audit-checklist) for the guided version.

## The four guarantees

These four are structural. They are not features that can be toggled, and each
one has a chapter explaining how it is held.

| Guarantee | Where it is held |
|---|---|
| Credentials never leave the host | [keys never leave the host](/trust-model/keys-never-leave-the-host) |
| Live execution is always gated | [live execution is gated](/trust-model/live-execution-is-gated) |
| Safety survives a disconnect | [safety survives a disconnect](/trust-model/safety-survives-disconnect) |
| Money arithmetic is exact | [money arithmetic is exact](/trust-model/exact-money-arithmetic) |

### Credentials never leave the host

Venue credentials live in `sops`+`age` encrypted files under `~/.arx/vault/`,
one file per key. Decryption happens in-process at the moment a venue client is
constructed; the plaintext is never written to state, logs, commands or facts.

The machine identity works the same way. The Ed25519 private key is generated
locally during enrollment, proves possession without being transmitted, and is
stored in the same encrypted vault.

### Live execution is always gated

Admission runs before any engine is constructed, and checks seven conditions —
artifact readiness, mode agreement, engine mode support, connector support,
credential scope, whether this build has live execution enabled at all, and
whether the command carries signed promotion evidence.

The live capability belongs to the build, not to a configuration file. It cannot
be switched on at runtime.

### Safety survives a disconnect

If ARX becomes unreachable, running deployments keep running from durable
applied state, and the local guards keep enforcing: the aggregate notional cap,
the drawdown breaker, and the zombie watchdog all evaluate locally.

Losing the ability to receive new instructions is not the same as losing the
ability to protect the account. Conflating them would mean an upstream outage
either stops a working strategy or removes its supervision.

### Money arithmetic is exact

Prices, quantities and notionals are `Decimal` end to end, serialized as strings
on the wire. Python binary floats are rejected recursively before anything is
persisted, so a signature never depends on how one language renders a float.

## The modules

Six modules carry the guarantees. Each has its own chapter; this table is the
map.

| Module | Responsibility | Chapter |
|---|---|---|
| Enrollment | Nonce-bound proof of possession, encrypted machine credential, rotation and revocation | [enrollment](/getting-started/enrollment) |
| Command intake and reconcile | Verify signed desired state, converge local runtime, record outcomes durably | [reconcile loop](/concepts/reconcile-loop) |
| Engine host | Supervise the trading engine, configure venue clients, enforce admission | [NautilusTrader engine](/engines/nautilus-trader) |
| Credential vault | Decrypt venue credentials in-process, bound to signed scope | [credential vault](/operator-guide/credential-vault) |
| RunnerFact | Typed signed statements through a durable local queue | [RunnerFact](/concepts/runner-fact) |
| Transport | Subscribe to signed desired state; publish signed facts | [NATS subjects](/reference/nats-subjects) |

The engine host is deliberately the only module that knows which trading engine
is underneath. Everything else speaks to it through a protocol, which is why the
safety guards work identically regardless of engine.

## Runtime identity

One identifier keys everything at runtime: `deployment_instance_id`. The
reconciler, engine, watchdog, breaker, credential resolution and fact streams are
all indexed by it.

The spec identifier is configuration provenance — it records *what* was
configured, not *which running thing* did something. Two instances of the same
immutable spec are two separate things, and a retry that acted on the wrong one
would be a real incident.

See [spec vs instance](/concepts/deployment-spec-vs-instance).

## Modes

Three, and only three: `sandbox`, `testnet`, `live`. There is no implicit
fallback mode and no fourth value that means "production". Mode is part of the
signed command, part of the subject, and part of every fact.

See [trading modes](/concepts/trading-modes).

## What to read next

- Running it for the first time: [installation](/getting-started/installation)
- The guarantees in depth: [trust model](/introduction/trust-model)
- Consuming what it emits: [consuming RunnerFact](/integration/consuming-runner-fact)
- Checking the claims yourself: [audit checklist](/trust-model/audit-checklist)
