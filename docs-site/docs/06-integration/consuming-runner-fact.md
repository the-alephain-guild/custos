---
title: "Consuming signed observations"
sidebar_position: 3
---

The signed lane publishes RunnerFact batches and separate strategy signal envelopes. Use the enrolled runner public key and the exact contract for each surface. Offline deployment status is not accepted input to either verifier.

## RunnerFact subject

```text
crucible.runner_fact.{trading_mode}.{tenant_id}.{runner_id}.{deployment_instance_id}
```

The stream identity is tenant + mode + runner + deployment instance. A spec or generation change does not reset its sequence.

## Batch signing

The domain is `CRUCIBLE-RUNNER-FACT-BATCH-V1\0`, including the trailing NUL. The closed header contains:

```text
schema_version, batch_id, tenant_id, trading_mode, runner_id,
deployment_instance_id, deployment_spec_id, deployment_spec_digest,
generation, strategy_id, capability_version_id, capability_version,
capability_manifest_digest, key_id, emitted_at, source_seq_start,
source_seq_end, payload_digest
```

```text
payload_digest = sha256(canonical_json(facts))
signed bytes = DOMAIN || canonical_json(header)
```

`facts` and `signature` are outside the header. Canonical JSON uses compact UTF-8, sorted object keys, original array order, non-ASCII-escaped Unicode and no trailing newline. Binary floats and non-finite numbers are rejected. Parse decimal strings exactly.

## Verify before applying

1. Match subject, tenant, mode, runner and deployment instance.
2. Recompute the facts digest.
3. Verify the signature with the enrolled key for `key_id`, and verify its authority/capability binding.
4. Enforce the expected sequence for that instance stream and deduplicate stable event/batch identities.
5. Reject unknown fact kinds and invalid contract fields.

Contract golden keys are synthetic test evidence and must not be trusted as runtime identities.

## Batch kinds

| Purpose | `facts[].kind` |
|---|---|
| Settlement | `fill`, `position_closed`, `fee`, `period_closed` |
| Risk | `equity_snapshot`, `position_snapshot` |
| Health | `heartbeat`, `RunnerRuntimeLogFact.v1` |
| Reconciliation | `execution_fill`, `venue_ledger_snapshot_manifest`, `venue_ledger_snapshot_chunk`, `reconciliation_period_closed` |
| Deployment lifecycle | `RunnerDeploymentLifecycleFact.v1` |

A `heartbeat` reports `online` only while the deployment's engine is ready and its state is reliable. A deployment that is still starting, including one retrying because its venue cannot be reached, reports `degraded`.

A lifecycle event id includes stable command/apply identity and excludes observation time. Redelivery of the same apply therefore remains idempotent.

## Strategy signals

Subject: `crucible.runner.strategy-signal.v1.{tenant_id}.{runner_id}.{trading_mode}`. <!-- disclosure-ok: exact public strategy-signal subject -->

Domain: `CRUCIBLE-RUNNER-STRATEGY-SIGNAL-V1\0`. <!-- disclosure-ok: exact strategy-signal signing domain -->

The signature covers domain bytes followed by the canonical JSON of the following closed payload, excluding only `signature`:

```text
schema_version, fact_id, subject, tenant_id, trading_mode, runner_id,
deployment_instance_id, deployment_spec_id, deployment_spec_digest,
generation, strategy_id, capability_version_id, capability_version,
capability_manifest_digest, strategy_version, instrument, client_order_id,
timeframe, direction, occurred_at, source_sequence, input_digest, trace_id, key_id
```

`schema_version` is 1; `direction` is `long`, `short` or `flat`; `client_order_id` may be null. Validate exact subject and full instance/capability scope, verify the enrolled signature, deduplicate `fact_id` and enforce `source_sequence` per tenant/mode/runner/instance. Multiple instances share the signal subject but have separate sequence streams.

Signals have their own durable sequence allocation and PubAck tracking. They are not a fourteenth RunnerFactBatch kind, and their sequence must not be compared with batch sequence numbers. A strategy signal does not establish that an order filled.

## Delivery

The outbox persists before publishing and records PubAck before completing local delivery. Retry preserves identity. Consumers must retain their own durable cursor and deduplication state; broker retention and consumer provisioning determine replay availability. Local outbox durability is not unlimited historical retention for downstream consumers.
