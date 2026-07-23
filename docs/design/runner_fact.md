# RunnerFact v1 production authority

## Ownership

Custos observes the local engine. Crucible owns canonical business and lifecycle
facts. ARX may consume audit projections but is not in the publication path.

    engine / watchdog / breaker
      -> typed local fact adapter
      -> RunnerFactOutbox
      -> signed RunnerFact batch
      -> Crucible ingestion and projection

There is no generic unsigned NATS telemetry actor. Engine observations must map
to an explicitly versioned fact type before entering the durable outbox.

## Required identity

Every deployment-scoped signed batch carries tenant, mode, runner,
deployment_instance_id, deployment_spec_id, deployment_spec_digest, generation,
strategy/capability provenance, event time, event id and typed payload. Strategy,
spec, generation or process identifiers cannot replace deployment_instance_id as
the runtime identity.

The subject and durable stream identity are stable across spec and generation
changes:

```text
crucible.runner_fact.{mode}.{tenant_id}.{runner_id}.{deployment_instance_id}
tenant_id + mode + runner_id + deployment_instance_id
```

`deployment_spec_id`, `deployment_spec_digest`, and `generation` are signed batch
fences. They never split the stream and never reset its source sequence. The v1
signing domain is `CRUCIBLE-RUNNER-FACT-BATCH-V1\0`.

The signing header is a closed 18-field object in this order:
`schema_version`, `batch_id`, `tenant_id`, `trading_mode`, `runner_id`,
`deployment_instance_id`, `deployment_spec_id`, `deployment_spec_digest`,
`generation`, `strategy_id`, `capability_version_id`, `capability_version`,
`capability_manifest_digest`, `key_id`, `emitted_at`, `source_seq_start`,
`source_seq_end`, `payload_digest`. `facts` and `signature` are excluded from
the header; `payload_digest = sha256(canonical_json(facts))`. The bytes signed
are exactly `DOMAIN || canonical_json(header)`. Canonical JSON is UTF-8,
compact, sorts object members by ascending Unicode code point, preserves array
order, does not ASCII-escape ordinary Unicode, rejects NaN and binary floats,
and has no trailing newline. The V1 signing-preimage golden fixes the
exact bytes, digest, synthetic key and signature for cross-language consumers.

## Closed fact union

The canonical first-production V1 retains the existing 13 wire kind names. Unknown
kinds are terminal contract violations; they cannot fall back to unsigned logs.

| Projector | Accepted `facts[].kind` |
|---|---|
| settlement | `execution_fill`, `fill`, `position_closed`, `fee`, `period_closed` |
| risk | `equity_snapshot`, `position_snapshot` |
| health | `heartbeat`, `RunnerRuntimeLogFact.v1` |
| reconciliation | `venue_ledger_snapshot_manifest`, `venue_ledger_snapshot_chunk`, `reconciliation_period_closed` |
| deployment lifecycle | `RunnerDeploymentLifecycleFact.v1` |

`period_closed` is a calendar settlement fact and its V1 `period` is exactly
`YYYY-MM`. It is emitted only by the durable settlement lifecycle. The
reconciliation production loop may emit venue-ledger evidence and
`reconciliation_period_closed`; it must never translate an arbitrary retry or
snapshot interval into a settlement close. If an independent venue ledger is
unavailable, that loop emits no close fact and records the unavailable
capability locally.

Payload numbers are either JSON integers or canonical decimal strings. Python
binary floats are rejected recursively before SQLite persistence so signatures
do not depend on language-specific number rendering.

`RunnerDeploymentLifecycleFact.v1` records an applied desired generation with:

- tenant_id and mode;
- runner_id;
- deployment_instance_id;
- deployment_spec_id and deployment_spec_digest;
- generation and lifecycle_state;
- command_fingerprint and terminal apply outcome;
- observed_at;
- seq, allocated exclusively by RunnerFactOutbox when the fact enters the
  signed batch `facts[]` array.

Emission requires an exact `deployment_lifecycle` capability projector binding
for the same mode, instance, spec digest and strategy. A health-only authority
cannot emit lifecycle facts.

The lifecycle event ID excludes `observed_at`. Its UUIDv5 preimage contains the
complete stream identity, spec id/digest, generation, lifecycle state, stable
command/apply fingerprint and outcome. Retry or restart of the same apply keeps
one event ID; changing any stable identity component produces a different ID.

Typed fact builders must not pre-populate seq. RunnerFactOutbox rejects such an
input and assigns the stream-monotonic sequence in the same transaction that
persists the signed batch.

## Failure semantics

Outbox enqueue success is the reporting durability boundary. Reconciliation
keeps separate applied_generation and reported_generation watermarks. If enqueue
fails, Custos NAKs the command; redelivery retries only the fact and does not
repeat the successful engine action.

Local safety continues while Crucible is unavailable. Custos never downgrades a
fact to an unsigned compatibility topic.

## V1 readiness ceiling

`custos.runner-fact.v1` is the sole producer contract authority. Its synthetic
Ed25519 key and signature are cross-language golden evidence only; they are not
runtime identity evidence. Pre-production candidate lineage is retained only in
Git history. Until Crucible returns a receipt over the exact V1 bytes,
projector compatibility, runtime RC, real round-trip, live, runtime and
production readiness remain false.
