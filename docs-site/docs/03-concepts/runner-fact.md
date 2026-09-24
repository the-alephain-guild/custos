---
title: "RunnerFact"
sidebar_position: 5
---

A RunnerFact is a signed observation from an enrolled runner. ARX validates observations before using them in canonical business records.

```text
engine / watchdog / breaker -> typed fact -> SQLite outbox -> signed batch -> ARX
```

## Identity and sequence

A deployment stream is identified by `tenant_id + trading_mode + runner_id + deployment_instance_id`. Spec id, spec digest and generation are signed provenance/fences; they do not split the stream or reset its sequence.

The outbox allocates sequence numbers in the same transaction that persists the batch. Lifecycle apply and lifecycle fact are committed atomically; a reporting retry does not repeat an already committed engine action.

The batch has thirteen kinds, listed with exact signing rules in [consumer reference](/integration/consuming-runner-fact). Unknown kinds are terminal contract violations. Wire values are integers or canonical decimal strings; binary floats are rejected before persistence.

## Settlement and reconciliation

`period_closed` is a calendar settlement observation whose period is `YYYY-MM`. The runtime emits it once per stream and calendar month, as the last fact of its own batch, when a reconciliation period crosses a month boundary. A reconciliation interval inside one month does not create a settlement close: venue-ledger evidence uses `reconciliation_period_closed`. If independent ledger evidence is unavailable, that path does not emit a close fact.

## Other observation channels

Strategy signals are separately signed envelopes with independent sequencing and publication tracking. They are not extra batch kinds. Local offline status is unsigned and cannot be interpreted as RunnerFact evidence.

## Delivery failures

Signed batches remain in the durable outbox until PubAck handling completes. A failed stream blocks later batches from that stream for that drain pass. Monitor queue age and storage capacity. Upstream delivery failure does not itself disable local risk enforcement.

Runtime logs use explicitly constructed, redacted `RunnerRuntimeLogFact.v1` events. Raw stdout is not forwarded. See [observability](/operator-guide/runtime-log-observability).
