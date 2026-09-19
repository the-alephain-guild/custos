---
title: "Reconcile loop"
sidebar_position: 4
---

The signed reconciler compares durable desired state with local engine state. The offline reconciler uses its own input and applied-state store; see [offline operation](/operator-guide/offline-testnet).

## Signed input and state

The runner authenticates exact event bytes and subject before interpreting a command. It validates tenant, mode, runner, instance, spec digest, release binding and generation, then records the accepted input.

Desired/applied deployments, command leases and outcomes, artifact activation, policy state and the fact outbox share the local SQLite authority. Runtime operations use deployment instance identity. A spec identifier is configuration provenance, not an engine address.

## Apply sequence

1. Verify and durably accept the signed command.
2. Load the durable desired record and resolve its release material.
3. Verify, quarantine and atomically activate the artifact before import.
4. Resolve the bound local credential and invoke the engine lifecycle supervisor.
5. Wait for typed engine readiness, including reliable portfolio valuation.
6. Commit applied state and the lifecycle fact in one transaction.
7. Acknowledge the delivery only after that durable boundary.

Exact redelivery reuses the prior disposition. Restart recovery probes a matching ready engine before deciding to deploy again. The lifecycle event id includes stable stream, command and outcome identity, excluding observation time.

## Delivery outcomes

| Result | Action |
|---|---|
| Invalid signature, subject or contract | Durable rejection, then TERM |
| Exact same accepted generation and bytes | Replay prior durable disposition |
| Conflicting bytes, stale generation or exhausted retry budget | Atomic terminal outcome/fact, then TERM |
| Successful application | Atomic applied state/fact, then ACK |
| Recoverable local engine or dependency failure | NAK for redelivery |

## Supervision

Ready timeout, retryable terminal events and zombie disconnection use a durable restart budget with bounded backoff. Non-retryable failures and exhausted budgets quarantine the instance and record the outcome. An unexpected long-running task exit is fatal to the daemon; shutdown stops intake/deployments, flushes pending facts and closes transport in order.

The breaker reads one coherent engine status per instance per tick. Missing equity, missing marks or probe failure are unreliable valuation, not zero exposure. Signed runner policy supplies the aggregate cap; the deployment's `risk_config` cannot override it.

Temporary transport loss alone does not erase desired state or local safety. Credential expiry, revocation and invalid transport authority are separate conditions that can terminate the running daemon. See [readiness](/operator-guide/readiness-health) and [recovery](/operator-guide/emergency-playbook).
