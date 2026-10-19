---
title: "Consuming signed observations"
sidebar_position: 3
---

The signed lane publishes RunnerFact batches and separate strategy signal envelopes. Use the enrolled runner public key and the exact contract for each surface. Offline deployment status is not accepted input to either verifier.

## RunnerFact subject

```text
crucible.runner.fact.v1.{tenant_id}.{runner_id}.{trading_mode}
```

The subject names the tenant, runner and mode; the deployment instance is in the signed batch header, not the subject. The sequence stream is tenant + mode + runner + deployment instance, so several instances of one runner share the subject with independent sequences. A spec or generation change does not reset a sequence.

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
| Settlement | `fill`, `position_closed`, `fee`, `period_closed`, `RunnerInstanceTerminalValuationFact.v1`, `RunnerFillCorrectionFact.v1` |
| Risk | `equity_snapshot`, `position_snapshot` |
| Health | `heartbeat`, `RunnerRuntimeLogFact.v1` |
| Reconciliation | `execution_fill`, `venue_ledger_snapshot_manifest`, `venue_ledger_snapshot_chunk`, `reconciliation_period_closed`, `RunnerValuationCheckpointFact.v1`, `RunnerOrderEventFact.v1` |
| Deployment lifecycle | `RunnerDeploymentLifecycleFact.v1` |

Four kinds appear only when the runner's capability declares their flag: `RunnerInstanceTerminalValuationFact.v1` with `settlement.terminal_valuation`, `RunnerValuationCheckpointFact.v1` with `reconciliation.valuation_checkpoint`, `RunnerOrderEventFact.v1` with `reconciliation.order_lineage`, and `RunnerFillCorrectionFact.v1` with `settlement.fill_correction`. Refuse a batch that carries a kind its capability does not declare. The conformance vectors list every combination of the four flags with its verdict, and the conformance sequences give the state a fold must reach after each step of an order's history.

A `heartbeat` reports `online` only while the deployment's engine is ready and its state is reliable. A deployment that is still starting, including one retrying because its venue cannot be reached, reports `degraded`.

A lifecycle event id includes stable command/apply identity and excludes observation time. Redelivery of the same apply therefore remains idempotent.

## Venue ledger snapshots

A venue ledger snapshot is one manifest followed by its chunks. Each chunk holds five sections: `balances`, `positions`, `fills`, `fees` and `cash_flows`. A chunk holds at most 512 items across all five sections and at most 262144 canonical bytes.

Every balance names the wallet it belongs to. `wallet_type` is the venue's own wallet name as a lowercase token, such as `spot`. `sub_account` is null for the venue's main account and otherwise a non-empty name. The manifest carries the snapshot's own `sub_account` and one count per section, including `cash_flows_count`.

A cash flow records money moving into, out of or within the account:

| `kind` | `from` | `to` | `counterparty_uid` | `destination` |
|---|---|---|---|---|
| `internal_transfer` | wallet | wallet in the same sub-account | null | null |
| `sub_account_transfer` | wallet | wallet in another sub-account | null | null |
| `uid_transfer_out` | wallet | null | required | null |
| `uid_transfer_in` | null | wallet | required | null |
| `deposit` | null | wallet | null | null |
| `withdrawal` | wallet | null | null | object or null |

`amount` is greater than zero and `fee` is not negative; the direction comes from `kind`, never from a sign. `destination` holds the receiving `address`, and its `network` and `memo` when the venue reports them. `external_reference` is the transaction hash or external reference when the venue reports one. `cash_flow_id` is the venue's own record id and is unique within a snapshot.

`completeness` reports each section separately. `cash_flows_complete` does not affect the other four flags. A snapshot with no cash flows and `cash_flows_complete: true` states that the covered window had no money movement. The same empty list with `cash_flows_complete: false` states only that cash flows were not collected. A consumer that needs every transfer and withdrawal must require `cash_flows_complete: true`.

Balances sort by wallet, sub-account, asset and currency, where a null sub-account sorts first. Cash flows sort by `occurred_at`, then `cash_flow_id`. Verify each `chunk_digest` over the canonical chunk and the manifest `content_digest` over the snapshot identity, completeness, counts and chunk digests. The contract golden contains a reference snapshot for both digests.

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

`schema_version` is 1; `direction` is `long`, `short` or `flat`; `client_order_id` is always present and may be null. Validate exact subject and full instance/capability scope, verify the enrolled signature, deduplicate `fact_id` and enforce `source_sequence` per tenant/mode/runner/instance. Multiple instances share the signal subject but have separate sequence streams.

A signal may also carry a decision. These fields are optional and, when absent, are left out of the payload, never written as null, so the signature covers whichever of them are present:

```text
signal_id, signal_event, closed_reason, signal_action,
target_quantity, target_quantity_native, target_quantity_multiplier,
target_notional, target_notional_currency,
reversal_close_quantity, reversal_close_quantity_native,
reversal_close_quantity_multiplier, reversal_closes_signal_id,
arrival_price, arrival_price_source, arrival_observed_at,
reason_code, reason_fields, valid_until, supersedes_signal_id,
residue_min_quantity, residue_min_quantity_native,
residue_min_quantity_multiplier, residue_min_notional, residue_min_notional_currency
```

A decision field needs `signal_id`. `signal_event` is `decided`, `withdrawn` or `closed`: a decided signal names its `signal_action` and exactly one target, a quantity or a notional; a withdrawn signal carries nothing else; a closed signal adds `closed_reason`. Fields that belong together appear together, and each base quantity is exactly its native quantity times its multiplier. Refuse a decision field written as null. The schema is `strategy_signal_fact_v1.schema.json`.

Signals have their own durable sequence allocation and PubAck tracking. They are not a RunnerFactBatch kind, and their sequence must not be compared with batch sequence numbers. A strategy signal does not establish that an order filled.

## Delivery

The outbox persists before publishing and records PubAck before completing local delivery. Retry preserves identity. Consumers must retain their own durable cursor and deduplication state; broker retention and consumer provisioning determine replay availability. Local outbox durability is not unlimited historical retention for downstream consumers.
