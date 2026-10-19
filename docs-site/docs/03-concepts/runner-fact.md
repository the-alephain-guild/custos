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

The batch kinds are listed with exact signing rules in [consumer reference](/integration/consuming-runner-fact). Unknown kinds are terminal contract violations. Wire values are integers or canonical decimal strings; binary floats are rejected before persistence.

## Settlement and reconciliation

`period_closed` is a calendar settlement observation whose period is `YYYY-MM`. The runtime emits it once per stream and calendar month, as the last fact of its own batch, when a reconciliation period crosses a month boundary. A reconciliation interval inside one month does not create a settlement close: venue-ledger evidence uses `reconciliation_period_closed`. If independent ledger evidence is unavailable, that path does not emit a close fact.

## Orders and fill corrections

Two kinds are optional, and each has its own capability flag. A deployment declares `reconciliation.order_lineage = "v1"` to receive `RunnerOrderEventFact.v1`, and `settlement.fill_correction = "v1"` to receive `RunnerFillCorrectionFact.v1`. The two flags are independent and are offered only together with the terminal valuation and valuation checkpoint flags. The default capability template declares neither, so an operator turns them on per deployment.

`RunnerOrderEventFact.v1` records one order event: `initialized`, `submitted`, `updated`, `rejected`, `denied`, `refused`, `canceled`, `expired`, `modify_refused`, `modify_rejected`, `cancel_rejected` or `fill_voided`. The schema fixes which fields each event carries. An order's state is a coarse state that signed events can establish (`initialized`, `working`, `partially_filled`, `filled`, `denied`, `rejected`, `refused`, `canceled`, `expired`, `voided`), not every state the engine tracks.

Quantities are signed in two units. A native quantity is in the venue's own units; a base quantity is in base-asset units and is exactly the native quantity times `quantity_multiplier`, signed with it.

| Fact | Native | Base |
|---|---|---|
| `execution_fill` | none | `quantity` |
| `RunnerOrderEventFact.v1` | `quantity`, `requested_quantity`, `voided_quantity`, `leaves_quantity_after` | `base_quantity`, `requested_base_quantity`, `voided_base_quantity`, `leaves_base_quantity_after` |
| `RunnerFillCorrectionFact.v1` | `voided_quantity` | `voided_base_quantity` |

A refused amendment or cancellation (`modify_refused`, `modify_rejected`, `cancel_rejected`) never changes the order's confirmed quantity or prices. `requested_values_known` says whether the refused values are known; when it is true, a requested value left out was not part of the request.

A fill void is cumulative for the fill it names. Two voids of one fill are the same version only when the voided quantity, the commission reversal and the reopen flag are all equal; a fact delivered again is recognised by its `event_id`. A version with a smaller voided quantity or a smaller absolute commission reversal is not applied. When the voided fill carried a commission, a version without a reversal is not applied once an earlier version gave one. A version whose voided quantity exceeds the fill's quantity is not applied either. `order_status_after` is the order's state once the engine applied the void, and it decides the order's state.

| Flags declared | Fill signed by this instance | No fill signed by this instance |
|---|---|---|
| neither | nothing | nothing |
| `order_lineage` only | `fill_voided` (settlement is not corrected) | `fill_voided` |
| `fill_correction` only | the fill correction | nothing |
| both | both, in one batch, with one `source_event_id` | `fill_voided` |

A void without a local fill, such as one the venue states during reconciliation, has a synthetic trade id and only an order event. A void observed after the instance stream was sealed by its terminal valuation, or of a fill an earlier runner signed, is not corrected; the venue ledger shows the difference.

## Other observation channels

Strategy signals are separately signed envelopes with independent sequencing and publication tracking. They are not extra batch kinds. Local offline status is unsigned and cannot be interpreted as RunnerFact evidence.

## Delivery failures

Signed batches remain in the durable outbox until PubAck handling completes. A failed stream blocks later batches from that stream for that drain pass. Monitor queue age and storage capacity. Upstream delivery failure does not itself disable local risk enforcement.

Runtime logs use explicitly constructed, redacted `RunnerRuntimeLogFact.v1` events. Raw stdout is not forwarded. See [observability](/operator-guide/runtime-log-observability).
