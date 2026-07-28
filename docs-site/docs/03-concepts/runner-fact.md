---
title: "RunnerFact"
sidebar_position: 5
---

# RunnerFact

A RunnerFact is a signed statement about something that happened locally. It is
the only way execution reality leaves your machine.

The direction matters. Custos receives instructions and emits observations;
those are separate paths and neither can be used as the other. What the runner
says happened is signed by the runner, so it can be checked rather than
trusted.

```text
engine / watchdog / breaker
  -> typed local fact adapter
  -> fact outbox (durable, local)
  -> signed batch
  -> ARX
```

## Who owns what

Custos observes the local engine and signs what it observed. ARX owns the
canonical business and lifecycle record built from those observations.

Custos does not decide what a fact *means* for the business — it reports what
the engine did. That separation is why a compromised runner cannot rewrite
history: it can only produce signed statements, and a statement that conflicts
with its own prior sequence is detectable.

There is no generic unsigned telemetry path. An engine observation must map to
an explicitly versioned fact type before it can enter the outbox at all.

## Identity

Every deployment-scoped batch carries tenant, mode, runner,
`deployment_instance_id`, `deployment_spec_id`, `deployment_spec_digest`,
generation, strategy and capability provenance, event time, event id and a
typed payload.

`deployment_instance_id` is the runtime identity. Strategy, spec, generation
and process identifiers cannot substitute for it — they describe *what* was
configured, not *which running thing* did something.

The stream identity is stable across spec and generation changes:

```text
tenant_id + mode + runner_id + deployment_instance_id
```

`deployment_spec_id`, `deployment_spec_digest` and `generation` are signed
fences within a batch. They never split the stream and never reset its source
sequence. A configuration change is a fence, not a new stream — otherwise a
consumer would see gaps that look like lost facts.

## Sequence

Sequence numbers are allocated exclusively by the outbox, in the same
transaction that persists the signed batch.

Typed fact builders must not pre-populate a sequence; the outbox rejects such
input. A caller-supplied sequence would be a second allocator, and two
allocators eventually collide.

## The closed union

Thirteen fact kinds exist. Unknown kinds are terminal contract violations —
they cannot fall back to an unsigned log, because a fact that cannot be
expressed in the union is a fact the consumer cannot verify.

| Consumer | Accepted `facts[].kind` |
|---|---|
| settlement | `fill`, `position_closed`, `fee`, `period_closed` |
| risk | `equity_snapshot`, `position_snapshot` |
| health | `heartbeat`, `RunnerRuntimeLogFact.v1` |
| reconciliation | `execution_fill`, `venue_ledger_snapshot_manifest`, `venue_ledger_snapshot_chunk`, `reconciliation_period_closed` |
| deployment lifecycle | `RunnerDeploymentLifecycleFact.v1` |

`period_closed` is a calendar settlement fact whose `period` is exactly
`YYYY-MM`, emitted only by the durable settlement lifecycle. The reconciliation
loop may emit venue-ledger evidence and `reconciliation_period_closed`, but it
must never translate an arbitrary retry or snapshot interval into a settlement
close.

If an independent venue ledger is unavailable, that loop emits **no** close
fact and records the unavailable capability locally. A settlement close that
was not independently corroborated would be an assertion dressed as evidence.

## Numbers

Payload numbers are JSON integers or canonical decimal strings. Python binary
floats are rejected recursively before persistence, so a signature never
depends on how one language happens to render a float.

See [money arithmetic is exact](/trust-model/exact-money-arithmetic).

## Lifecycle facts

`RunnerDeploymentLifecycleFact.v1` records an applied desired generation:
tenant and mode, runner, deployment instance, spec id and digest, generation
and lifecycle state, command fingerprint and terminal outcome, `observed_at`,
and the outbox-allocated `seq`.

Emission requires an exact `deployment_lifecycle` capability binding for the
same mode, instance, spec digest and strategy. A health-only authority cannot
emit lifecycle facts.

The lifecycle event id **excludes** `observed_at`. Its UUIDv5 preimage contains
the stream identity, spec id and digest, generation, lifecycle state, the
stable command fingerprint and the outcome. A retry or restart of the same
apply therefore keeps one event id, while changing any stable component
produces a different one — which is what makes redelivery idempotent for the
consumer rather than merely for the runner.

## Failure semantics

Enqueue success into the outbox is the reporting durability boundary.
Reconciliation keeps separate applied and reported watermarks.

If enqueue fails, the command is not acknowledged. Redelivery retries the
**fact** and does not repeat the successful engine action — the engine work and
the reporting of it are separately recoverable.

Local safety keeps working while ARX is unavailable, and a fact is never
downgraded to an unsigned topic to get it delivered. An undeliverable fact
waits; it does not become a weaker fact.

For subjects, the signing preimage and verification steps, see
[consuming RunnerFact](/integration/consuming-runner-fact).
