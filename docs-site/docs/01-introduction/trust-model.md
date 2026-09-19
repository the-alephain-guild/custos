---
title: "Trust model"
sidebar_position: 2
---

Custos runs on operator-owned infrastructure. Exchange credentials are encrypted locally and used by local venue clients. ARX does not receive exchange secrets or private signing keys through runner telemetry.

## Signed operation

ARX authenticates users, authorizes intent and owns canonical deployment decisions. Custos verifies the issued desired state, resolves local credentials, applies it and signs observations. The runner does not approve its own canonical deployment or promotion.

Command verification covers exact bytes and subject. Fact verification covers the enrolled runner key, scope and sequence. Custos exposes no HTTP administration endpoint; broker-delivered input still requires authentication and validation.

## Offline operation

The operator may explicitly select an independent sandbox/testnet lane, generate an unattested identity and publish local desired state. This path trusts operator-controlled infrastructure and mounted strategy code. Its status is unsigned, it cannot run live, and it produces no promotion evidence. It is not a fallback for failed signed verification.

## Local safety and availability

Temporary upstream loss does not itself stop an already applied deployment. Local safety continues independently. Credential expiry, revocation, engine failure and invalid authority are separate conditions that can stop execution. Offline and signed policies have different scopes; see [safety during disconnects](/trust-model/safety-survives-disconnect).

## Auditing the boundary

The Apache-2.0 source, contract assets and tests support independent inspection. A source review does not establish deployed behavior; verify the exact artifact and configuration you use. Host access control, exchange permissions and strategy risk remain operator responsibilities.

Read the [four guarantees](/trust-model/red-lines), [architecture](/introduction/architecture-at-a-glance) and [audit checklist](/trust-model/audit-checklist) for implementation and verification scope.
