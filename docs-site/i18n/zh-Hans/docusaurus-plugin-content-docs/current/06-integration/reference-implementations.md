---
title: "Reference Implementations"
sidebar_position: 5
---


# Reference Implementations

:::warning 中文翻译尚未完成
本章暂时显示英文原文。
:::

## Inbound desired state

ARX directly publishes signed domain-event envelopes for both creation and
later desired-state changes:

```
ARX.domain.<tenant>.<mode>.deployment.
  DeploymentSpecReadyForRunner.<runner_id>.<deployment_instance_id>

ARX.domain.<tenant>.<mode>.deployment.
  DeploymentInstanceDesiredStateChanged.<runner_id>.<deployment_instance_id>
```

Custos uses a durable, runner-scoped JetStream consumer and manual ACK/NAK. The
verifier binds the exact subject and exact event bytes to the provisioned
ARX's Ed25519 key. Tenant, mode, runner, instance, canonical spec id and
canonical digest must agree across subject, event and payload.

Both event types carry a complete canonical DeploymentSpec plus explicit
generation and lifecycle_state. Missing values are invalid; Custos never
defaults a signed desired-state command.

## Canonical digest

`sha256-canonical-json-v1` hashes only DeploymentSpecCanonicalPayloadV1. The
command envelope and digest field are excluded. The field set is exact, object
keys are recursively sorted, arrays retain order and compact UTF-8 JSON bytes
are hashed. Cross-language golden fixtures must accompany any algorithm change.

## Outbound facts

The NATS command client has no outbound business publication API. Custos writes
typed facts to RunnerFactOutbox; the separate RunnerFact publisher signs and
publishes batches directly for ARX.

ARX does not publish or relay deployment commands and is not a destination for
Custos business facts. Its availability is irrelevant to command delivery and
fact publication after machine authorization has been provisioned.
