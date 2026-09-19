---
title: "Gateway contract V1"
sidebar_position: 1
---

Custos publishes schemas for its own observation and execution boundaries. The current inventory is in [JSON Schema reference](/reference/json-schema).

## Signed deployment input

ARX owns the canonical DeploymentSpec and command issuance. Custos has a strict consumer that verifies exact bytes/subject, field sets, identity, generation and digest before deriving a local execution view. Custos does not publish a competing canonical DeploymentSpec schema or a command-signing CLI.

The signed command client binds an existing authorized durable. See [NATS subjects](/reference/nats-subjects) and [verification reference](/integration/reference-implementations).

## Offline input

`offline_deployment_spec.schema.json` describes a separate, operator-owned contract. `deployment validate/publish` checks and publishes that input in sandbox/testnet only. It cannot produce canonical signed commands or promotion evidence.

## Observation output

RunnerFact batches have a closed 13-kind union with signed identity, digest and sequence fields. Strategy signals use a separate signed envelope. Offline status is unsigned and must not be accepted by a signed-fact consumer.

## Validation and compatibility

JSON Schema validates shape. Signature, authority, cross-field binding and sequence invariants require the contract verifier and tests. Use matching revisions on both sides and run:

```bash
make check-authority
```

See [contract versioning](/integration/contract-versioning) for changes to strict V1 field sets and the treatment of historical evidence.
