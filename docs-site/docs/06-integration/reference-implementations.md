---
title: "Verification reference"
sidebar_position: 5
---

This page describes signed command checks an auditor or integrator can verify against the source. Only ARX issues canonical deployment commands. Operator-owned offline input uses a different contract.

## Command binding

The signed command subject is `<provisioned-prefix>.{tenant_id}.{runner_id}.{mode}`. Event type and deployment instance belong in the signed event material. Do not append them to the broker subject.

The two desired-state event types are `DeploymentSpecReadyForRunner` and `DeploymentInstanceDesiredStateChanged`. Each includes a complete canonical payload, explicit generation and lifecycle state. Custos verifies exact event bytes and subject before interpreting that payload, then checks consistency with transport mode, tenant, runner, instance and digest.

The canonical spec digest uses `sha256-canonical-json-v1` over its defined payload fields. It excludes the command envelope and digest field itself. Use matching producer/consumer golden vectors; generic JSON serialization is not a substitute for the contract.

## Durable handling

| Outcome | Delivery action |
|---|---|
| Invalid signature/subject/contract | Persist rejection, then TERM |
| Exact redelivery | Replay the durable outcome |
| Conflicting or stale generation | Persist terminal outcome, then TERM |
| Successful engine application | Commit applied state and lifecycle fact, then ACK |
| Recoverable engine/dependency failure | NAK for retry |

The signature verifier, command consumer and lifecycle supervisor have distinct responsibilities. Tests must cover rejection before parsing and engine action, plus replay after the durable commit boundary.

## Observation consumers

Use [consumer reference](/integration/consuming-runner-fact) for RunnerFact batches and strategy signals. Consumers validate signature, authority, scope and sequence before changing their own state. Local offline status is useful for operator diagnostics but has no signed authority.
