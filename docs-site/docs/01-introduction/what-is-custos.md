---
title: "What is Custos?"
sidebar_position: 1
---

Custos is the non-custodial execution runner. It runs on your infrastructure, stores exchange credentials locally, and supervises strategy execution through NautilusTrader.

## Choose a delivery lane

| Lane | Input | Identity | Output | Modes |
|---|---|---|---|---|
| Signed | ARX-issued, signed desired state | Enrolled machine identity | Signed RunnerFacts and strategy signals | Sandbox and testnet execution; live admission remains disabled |
| Offline | Operator-published `OfflineDeploymentSpec` | Standalone or enrolled local identity | Unsigned local deployment status | Sandbox and testnet only |

The offline lane supports local strategy development without an ARX backend. It still uses NATS and may connect to market data or a testnet. It is opt-in, cannot run live, and produces no promotion evidence. It is never selected automatically after a signed-lane failure.

Start with [standalone sandbox](/getting-started/standalone-sandbox) for a local lifecycle check, or [enrollment](/getting-started/enrollment) when connecting to ARX.

## Responsibilities

Custos stores machine and exchange credentials, verifies signed input, applies desired state, supervises engines and enforces local safety. On the signed lane, ARX owns authorization, deployment approval, artifact selection and canonical business records. A runner's signed observation is input to that process, not an approval.

## Terms

| Term | Meaning |
|---|---|
| `DeploymentSpec` | Immutable, signed-lane configuration; its identifier and digest record configuration provenance |
| `DeploymentInstance` | Runtime identity addressed by `deployment_instance_id`; multiple instances may refer to one spec |
| Generation | Monotonic desired-state revision; redelivery of a generation does not create a new instance |
| Engine handle | Local resource associated with a deployment instance |
| `RunnerFact` | Signed observation from the enrolled runner |
| `OfflineDeploymentSpec` | Separate unsigned contract for operator-owned sandbox/testnet work |

The signed runtime verifies the exact command bytes and subject before interpreting the payload. Tenant, mode, runner and instance must agree throughout. See [reconciliation](/concepts/reconcile-loop).

## Current support

Host capability declarations, package publication and production acceptance are separate. Check [release status](/release-governance/release-status) before choosing an artifact. Live execution is disabled in the current daemon composition.
