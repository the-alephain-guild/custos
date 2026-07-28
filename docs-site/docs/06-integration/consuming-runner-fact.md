---
title: "Consuming RunnerFact"
sidebar_position: 3
---

# Consuming RunnerFact

This page is for building a consumer. It covers the subject, the exact signing
preimage, and what a verifier has to check. For the conceptual model — what a
fact is and why the union is closed — see [RunnerFact](/concepts/runner-fact).

## Subject

```text
crucible.runner_fact.{trading_mode}.{tenant_id}.{runner_id}.{deployment_instance_id}
```

The subject is stable across spec and generation changes. A configuration
change does not move a stream, so a consumer subscribed to one deployment
instance keeps receiving that instance's facts.

## Signing preimage

The signing domain is `CRUCIBLE-RUNNER-FACT-BATCH-V1\0` — note the trailing
NUL, which is part of the domain.

The signed header is a **closed 18-field object**, in this order:

```text
schema_version, batch_id, tenant_id, trading_mode, runner_id,
deployment_instance_id, deployment_spec_id, deployment_spec_digest,
generation, strategy_id, capability_version_id, capability_version,
capability_manifest_digest, key_id, emitted_at, source_seq_start,
source_seq_end, payload_digest
```

`facts` and `signature` are **excluded** from the header. Instead:

```text
payload_digest = sha256(canonical_json(facts))
signed bytes   = DOMAIN || canonical_json(header)
```

A signature therefore covers the payload by digest rather than by value, which
is what lets a verifier check the header without buffering an arbitrarily large
batch.

### Canonical JSON

Getting this wrong is the most likely reason a correct signature fails to
verify, so implement against the rules rather than against an existing
serializer:

- UTF-8, compact (no insignificant whitespace);
- object members sorted by ascending Unicode code point;
- array order preserved;
- ordinary Unicode **not** ASCII-escaped;
- NaN and binary floats rejected;
- no trailing newline.

The V1 signing-preimage golden fixes the exact bytes, digest, synthetic key and
signature. Implement against the golden, not against your language's default
JSON encoder — most of them differ in at least one of the rules above.

The synthetic key in that golden is contract evidence only. It is never runtime
identity evidence, and a batch signed with it must never be accepted as real.

## What a verifier must check

1. The subject matches the batch's tenant, mode, runner and deployment
   instance.
2. `payload_digest` equals `sha256(canonical_json(facts))` as received.
3. The signature verifies over `DOMAIN || canonical_json(header)` with the
   runner's enrolled public key for `key_id`.
4. `source_seq_start` and `source_seq_end` are contiguous with what you have
   already accepted for that stream.
5. Every `facts[].kind` is in the closed union. An unknown kind is a terminal
   contract violation, not a value to skip.

Check the sequence before acting on payload contents. A batch that verifies
cryptographically but skips sequence numbers means facts were lost, and acting
on the later ones would silently accept an incomplete history.

## Accepted kinds

| Consumer | `facts[].kind` |
|---|---|
| settlement | `fill`, `position_closed`, `fee`, `period_closed` |
| risk | `equity_snapshot`, `position_snapshot` |
| health | `heartbeat`, `RunnerRuntimeLogFact.v1` |
| reconciliation | `execution_fill`, `venue_ledger_snapshot_manifest`, `venue_ledger_snapshot_chunk`, `reconciliation_period_closed` |
| deployment lifecycle | `RunnerDeploymentLifecycleFact.v1` |

## Idempotency

The lifecycle event id excludes `observed_at`; its UUIDv5 preimage is built
from stream identity, spec id and digest, generation, lifecycle state, the
stable command fingerprint and the outcome.

A retry or restart of the same apply therefore produces the **same** event id.
Deduplicate on it — that is what it is for.

## Numbers

Payload numbers arrive as JSON integers or canonical decimal strings, never as
floats. Parse decimal strings into an exact decimal type. Parsing them into a
double reintroduces exactly the error the string representation exists to
avoid.

## Availability

Facts accumulate in the runner's durable outbox when a consumer is unreachable,
and publish when it returns — with identity and sequence unchanged. There is no
lossy mode and no unsigned fallback topic.

A consumer that has been down does not need a backfill mechanism; it needs to
resume from its last accepted sequence.
