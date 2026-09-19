---
title: "Deployment spec and instance"
sidebar_position: 1
---

A signed `DeploymentSpec` records immutable configuration. A `DeploymentInstance` identifies one runtime execution of that configuration.

| Identifier | Purpose |
|---|---|
| `deployment_spec_id` / `deployment_spec_digest` | Configuration provenance |
| `deployment_instance_id` | Address engine, lifecycle, watchdog, breaker and fact stream |
| `generation` | Order desired-state changes for an instance |

Multiple instances can refer to one spec. Transport retries and replay of the same accepted command preserve its instance identity; a newly issued deployment instance has its own id. This identity model does not imply unlimited host concurrency. Nautilus currently admits one active node per event loop.

Applied and reported progress are tracked separately so reporting can retry without repeating an already committed engine operation. RunnerFacts carry both runtime identity and configuration provenance; ARX validates them before updating canonical business state.

Offline specs use their own `spec_id` and generation. Their bridge derives deterministic local runtime UUIDs from that spec id, allowing restart recognition. Those UUIDs and unsigned local status do not substitute for signed ARX instance authority.
