---
title: "Reference Implementations"
sidebar_position: 5
---

# Reference Implementations

What a producer has to get exactly right for a Custos runner to accept a
command, and what it can expect back.

## Inbound: the command subject

The subject is a fixed prefix followed by tenant, runner and mode, in that
order:

```text
<fixed prefix>.{tenant_id}.{runner_id}.{mode}
```

Note what is **not** in it: the deployment instance and the event type. Both
live in the payload, and a command that encodes them into the subject instead
fails verification.

:::note This is not a third-party integration surface
Only ARX publishes commands to a runner, and the subject prefix is part of that
closed contract. If a third party could publish one, the trust model would
already be broken.

What is documented here is the **verification** a runner performs before acting,
which is what an auditor needs. If you are building against Custos, the surface
you consume is the fact stream — see
[consuming RunnerFact](/integration/consuming-runner-fact).
:::

Custos subscribes with a durable, runner-scoped JetStream consumer and manual
ACK/NAK.

## The two event types

```text
DeploymentSpecReadyForRunner
DeploymentInstanceDesiredStateChanged
```

The event type is a payload field, formed as
`{type}.{runner_id}.{deployment_instance_id}`. Both carry a complete canonical
DeploymentSpec plus an explicit `generation` and `lifecycle_state`.

Missing values are invalid. Custos never defaults any field of a signed
desired-state command — a default would mean acting on a value nobody signed.

## What must agree

Verification binds the exact subject and the exact event bytes to the
provisioned Ed25519 key, and then requires agreement across three places:

| Field | Subject | Envelope | Payload |
|---|---|---|---|
| tenant | ✅ | ✅ | ✅ |
| runner | ✅ | ✅ | ✅ |
| mode | ✅ | | ✅ |
| deployment instance | | ✅ | ✅ |
| generation | | ✅ | ✅ |

Any disagreement is a terminal rejection, not a retry. The signature is checked
**before** any payload field is parsed — nothing inside an unverified message is
trusted, including the fields that would tell you whether to trust it.

## Canonical digest

`sha256-canonical-json-v1` hashes only the canonical spec payload. The command
envelope and the digest field itself are excluded.

The field set is exact, object keys are recursively sorted, arrays keep their
order, and compact UTF-8 JSON bytes are hashed. Any change to the algorithm must
ship with cross-language golden fixtures — two implementations that agree on the
description but not on the bytes will each be certain the other is correct.

## Outbound: facts

Custos writes typed facts to a durable local outbox; a separate publisher signs
and publishes batches. See
[consuming RunnerFact](/integration/consuming-runner-fact) for the subject, the
signing preimage and the verifier checklist.

The command client has **no** outbound business publication API. A runner cannot
publish a command, including to itself.

## What a producer cannot rely on

- **No unsigned path.** There is no compatibility topic and no fallback schema.
- **No defaulting.** Omitted required fields are rejected, not filled in.
- **No local publication.** Custos will not relay, re-emit or synthesise a
  command.
- **No ordering assumption beyond generation.** Generation is the ordering
  input; delivery order is not.

## Rejection versus retry

| Producer error | Runner behaviour |
|---|---|
| Bad signature, wrong subject, invalid contract | Durable rejection, then TERM |
| Same generation, byte-identical | Prior disposition replayed |
| Same generation, different bytes | Terminal outcome, then TERM |
| Stale generation | Terminal outcome, then TERM |
| Transient local failure | NAK for redelivery |

A producer that re-sends byte-identical material is safe: the runner replays its
earlier decision rather than acting twice. A producer that re-sends *different*
bytes under the same generation is making a contradictory claim, and that is
terminal.
