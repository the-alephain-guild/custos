---
title: "NATS Subject Reference"
sidebar_position: 4
---

# NATS Subject Reference

Custos has exactly two transport relationships: it consumes signed desired
state, and it publishes signed facts. There is no third channel, and no
inbound control path other than the one below.

## Inbound — desired state

Two event types carry desired state, one for creation and one for later
changes. Their subjects are scoped to the exact runner and deployment instance:

```text
<domain-prefix>.<tenant>.<mode>.deployment.
  DeploymentSpecReadyForRunner.<runner_id>.<deployment_instance_id>

<domain-prefix>.<tenant>.<mode>.deployment.
  DeploymentInstanceDesiredStateChanged.<runner_id>.<deployment_instance_id>
```

The prefix is provisioned with your ARX enrollment; the runner binds it at
startup and will not accept an event arriving on any other subject.

This is not an integration point. ARX is the only publisher of desired state,
and an event from anywhere else fails signature verification regardless of the
subject it arrives on. It is documented here so you can reason about what the
runner subscribes to, not so you can write to it.

Custos subscribes with a durable, runner-scoped JetStream consumer and manual
ACK/NAK.

Verification binds **the exact subject and the exact event bytes** to the
provisioned Ed25519 key. Tenant, mode, runner, instance, canonical spec id and
canonical digest must agree across subject, event and payload — a mismatch in
any one of them is a rejection, not a warning.

Both event types carry a complete canonical deployment payload plus explicit
`generation` and `lifecycle_state`. Missing values are invalid: Custos never
supplies a default for a signed command, because a defaulted field is a field
nobody signed.

## Canonical digest

The digest algorithm is `sha256-canonical-json-v1`. It hashes **only** the
canonical deployment payload — the command envelope and the digest field itself
are excluded, since a digest cannot cover itself.

Rules:

- the field set is exact;
- object keys are recursively sorted;
- arrays retain their order;
- compact UTF-8 JSON bytes are hashed.

Any change to this algorithm must ship with cross-language golden fixtures.
Two implementations that disagree by one byte produce two different digests,
and the failure surfaces as an unexplained signature rejection.

## Outbound — signed facts

The command client has **no** outbound business publication API. That is a
deliberate asymmetry: the path that receives instructions cannot be used to
send anything.

Custos writes typed facts to its durable local outbox. A separate publisher
signs and publishes batches for upstream ingestion. The outbox owns sequence
allocation, so a fact that cannot be durably enqueued blocks the command
acknowledgement rather than being silently dropped.

See [consuming RunnerFact](/integration/consuming-runner-fact) for the fact
schema and subject.

## What is not a channel

ARX authorization is provisioned once, at enrollment. After that it is not in
the delivery path: it neither publishes nor relays deployment commands, and it
is not a destination for facts. Its availability does not affect command
delivery or fact publication.

This matters operationally — it means an authorization outage cannot stop a
running deployment, and cannot be used to inject one either.
