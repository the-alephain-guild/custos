---
title: "Four safety guarantees"
sidebar_position: 1
---

These requirements apply to runner implementation and review. Their operating scope is explained in the linked chapters.

| Requirement | Enforcement scope |
|---|---|
| [Keys remain local](./keys-never-leave-the-host) | Local encryption; no secret keys in telemetry, logs or upstream messages |
| [Live execution is gated](./live-execution-is-gated) | Signed admission; live defaults to disabled and requires verified runtime approval; offline input rejects live |
| [Safety continues during disconnects](./safety-survives-disconnect) | Local exposure/drawdown checks independent of transport |
| [Money uses decimal arithmetic](./exact-money-arithmetic) | Typed money boundaries and canonical wire representation |

The signed lane verifies upstream authority and reports signed observations. The explicitly selected offline lane uses local unsigned input for sandbox/testnet. It retains the local credential and safety requirements without claiming signed-release or promotion authority.

These requirements do not eliminate host compromise, misconfigured venue permissions or strategy losses. Verify the tested scope and actual artifact using the [audit checklist](./audit-checklist).
