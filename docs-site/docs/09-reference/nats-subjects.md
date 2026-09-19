---
title: "NATS subjects"
sidebar_position: 4
---

Signed and offline traffic use separate contracts and provisioning paths. Select the lane explicitly.

## Signed inbound control

ARX provisions a runner-control durable with exact command and safety-policy filters. Custos binds that existing durable and checks its configuration; it does not create signed-lane topology.

The command subject uses this shape:

```text
<provisioned-command-prefix>.{tenant_id}.{runner_id}.{mode}
```

The deployment instance and event type are in signed event material, not appended to this subject. Both `DeploymentSpecReadyForRunner` and `DeploymentInstanceDesiredStateChanged` carry complete desired state with an explicit generation and lifecycle state. The safety-policy filter is also supplied by transport authority.

Signature verification binds exact subject and event bytes before payload interpretation. The transport session's mode must match the signed command or policy. Custos acknowledges through durable handling outcomes: ACK for completed work, NAK for recoverable failure, TERM for terminal rejection.

ARX is the upstream product that issues signed intent and consumes signed observations. Identity enrollment is a separate interface; command/fact delivery uses the provisioned transport, whose availability must be checked independently.

## Signed outbound observations

RunnerFact batches:

```text
crucible.runner_fact.{trading_mode}.{tenant_id}.{runner_id}.{deployment_instance_id}
```

Strategy signals use a separate envelope and subject:

`crucible.runner.strategy-signal.v1.{tenant_id}.{runner_id}.{trading_mode}` <!-- disclosure-ok: exact public strategy-signal subject required by consumers -->

Both use durable local publication and PubAck handling. The signal sequence is independent of the batch sequence. See [consumer verification](/integration/consuming-runner-fact).

## Offline traffic

| Direction | Subject |
|---|---|
| Operator desired state | `arx.<tenant>.deployment_spec.<strategy-id>` |
| Runner observed status | `arx.<tenant>.deployment_status.<runner-label>.<spec-id>` |

`nats bootstrap --profile standalone` creates owned deployment/observed streams. Desired state retains the latest message per subject. The observed stream also reserves heartbeat/telemetry subjects; reservation is not a guarantee that the offline daemon emits those messages.

Offline input and status are unsigned. Restrict the broker to operator-owned infrastructure; the loopback-only demo broker is not suitable for shared deployment. Offline status publication is best effort and does not share the signed fact outbox.
