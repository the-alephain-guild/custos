---
title: "Runtime Log & Observability"
sidebar_position: 4
---


# Runtime Log & Observability

:::warning 中文翻译尚未完成
本章暂时显示英文原文。
:::

Custos emits runtime observability as a signed RunnerFact variant. It does not
ship stdout, arbitrary exception text, or a second logging stream.

## Canonical wire

The fact is stored inside the existing `RunnerFactBatchV1` envelope and uses
the existing subject:

```text
crucible.runner_fact.{mode}.{tenant_id}.{runner_id}.{deployment_instance_id}  <!-- disclosure-ok: published subject an integrator must subscribe to -->
```

The fact shape before the durable local queue allocates `seq` is:

```json
{
  "kind": "RunnerRuntimeLogFact.v1",
  "event_id": "<deterministic uuidv5>",
  "occurred_at": "<RFC3339 UTC>",
  "level": "INFO",
  "component": "deployment_reconciler",
  "message": "Deployment status observed",
  "structured_fields": {},
  "correlation_id": "<uuid>",
  "causation_id": null
}
```

Allowed levels are `DEBUG`, `INFO`, `WARN`, and `ERROR`. The surrounding batch
adds tenant, mode, Runner, exact DeploymentInstance/spec/digest/generation,
strategy, capability, key ID, a contiguous sequence range, payload digest, and
signature. Spec/digest/generation are signed fences only; they do not alter the
subject or reset sequence.

The deterministic event UUIDv5 preimage contains tenant, mode, runner,
deployment instance, correlation ID and the digest of the sanitized structured
content. Identical content within one stream is idempotent, while the same
content in another tenant/mode/runner/instance stream cannot collide in the
global durable local queue event-deduplication table.

## Shared authority and delivery

- `RunnerRuntimeLogFact.v1` uses the same Ed25519 identity, capability receipt,
  `RunnerFactAuthority`, source sequence, event deduplication, SQLite durable local queue,
  subject, and `CRUCIBLE-RUNNER-FACT-BATCH-V1\0` signing domain as settlement, <!-- disclosure-ok: published signing domain required to verify batches -->
  reconciliation, risk, and health facts.
- A deployment does not enter Vault/G6/host when its runtime-log health binding
  cannot be resolved exactly from the validated capability receipt.
- The durable local queue commits the fact before publish. A JetStream PubAck is required
  before the batch is deleted, which is the producer delivery checkpoint. A
  crash between PubAck and deletion replays the same `batch_id`; consumer dedup
  makes this safe.
- A failed stream blocks later batches from the same exact-binding stream for
  that drain pass, preserving contiguous sequence delivery.

## Secret boundary

Only explicit structured events are accepted. The producer never tails stdout
and never falls back to sending raw exceptions.

Before enqueue, `RuntimeLogRedactor` recursively processes message and fields:

- sensitive keys such as API keys/secrets, passwords, tokens, credentials,
  authorization, private keys, age keys, and KEKs are anonymized;
- registered exact secrets and recognizable Bearer, `rkc1`, age secret key,
  private-key PEM, assignment, and high-entropy shapes are replaced;
- unsupported objects, non-finite floats, excessive nesting/size, and any
  residual recognizable secret material reject the entire fact before SQLite.
- binary floats are rejected recursively; numeric structured values use JSON
  integers or canonical decimal strings.

Publisher failure logs contain only structured event identity and exception
type. Plaintext log content is never used as a fallback diagnostic.
