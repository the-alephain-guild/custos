---
title: "Your first deployment"
sidebar_position: 4
---

Choose the contract that matches your delivery lane.

| Lane | Who creates desired state | How it arrives | Evidence to inspect |
|---|---|---|---|
| Signed | ARX | Signed command through the provisioned subscription | Applied generation and lifecycle RunnerFact for the exact instance |
| Offline | Operator | `arx-runner deployment publish` to local NATS | Local status subject with the expected `observed_generation` |

## Signed deployment

Complete [enrollment](/getting-started/enrollment) and start the daemon with `--reconcile` and the required trust configuration from [signed sandbox](/getting-started/first-sandbox-run). Create and approve the deployment in ARX.

The runner verifies the signed bytes and subject, persists desired state, resolves and activates the artifact, loads the bound credential, then waits for engine readiness. Applied state and the lifecycle fact are committed together before acknowledgement.

Check the expected `deployment_instance_id`, spec digest and generation in the lifecycle observation. A successful `arx-runner health` only checks daemon readiness; it does not prove a particular deployment has applied.

Invalid signatures, conflicting generations or admission failures are terminally rejected. Recoverable local failures use bounded retry. See [reconciliation](/concepts/reconcile-loop).

## Offline deployment

Follow [standalone sandbox](/getting-started/standalone-sandbox) to validate and publish an `OfflineDeploymentSpec`. Reuse the strategy id and `spec_id`, increasing `generation` for a new desired state. Use a terminal lifecycle state to stop the deployment, then inspect its status and any venue positions.

Offline status is unsigned and is not a RunnerFact, approval or promotion receipt. A status reporting an applied generation is also separate from Nautilus portfolio readiness.
