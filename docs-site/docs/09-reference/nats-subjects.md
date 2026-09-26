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
| Runner telemetry snapshot | `arx.<tenant>.telemetry.<runner-label>.<spec-id>.snapshot` |
| Runner telemetry fill | `arx.<tenant>.telemetry.<runner-label>.<spec-id>.fill` |
| Runner telemetry closed position | `arx.<tenant>.telemetry.<runner-label>.<spec-id>.position_closed` |

`nats bootstrap --profile standalone` creates owned deployment/observed streams. Desired state retains the latest message per subject; observed state retains at most 10,000 messages per subject. The observed stream also reserves heartbeat subjects, which the offline daemon does not emit.

Telemetry uses the same envelope as observed status (`envelope_version`, `event_id`, `tenant_id`, `occurred_at`, `payload_schema_version`, `payload`), with `payload.kind` set to `snapshot`, `fill` or `position_closed`. A snapshot is published every 10 seconds for each running deployment and carries its engine status, open positions and open orders; money is a decimal string. When the status is not reliable its reason is included and positions are left out. Telemetry is unsigned and best effort, it is not RunnerFact evidence, and the runner never reads it back.

When the offline daemon receives SIGTERM or SIGINT it stops each running deployment through the engine, publishes its status with phase `stopped`, and exits non-zero if a deployment could not be confirmed stopped within 75 seconds.

Offline input and status are unsigned. Restrict the broker to operator-owned infrastructure; the loopback-only demo broker is not suitable for shared deployment. Offline status publication is best effort and does not share the signed fact outbox.
