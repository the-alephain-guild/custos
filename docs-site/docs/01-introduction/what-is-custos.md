---
title: "What is custos?"
sidebar_position: 1
---

# What is custos?

Custos is a daemon you run on your own machine. It receives signed instructions
about what should be running, executes them against a venue using credentials
that never leave that machine, and signs statements about what actually
happened.

It is deliberately small. Everything it does is local execution mechanics; every
decision about *whether* something should run is made somewhere else.

## What it owns

- runner enrollment material and local machine credentials;
- verification of signed commands;
- reconciliation of desired deployment state into a local engine;
- process supervision, watchdogs and local safety circuit breakers;
- signing and publishing observed runner facts.

## What it does not own

Actor authorization, approval workflows, strategy or risk configuration,
promotion decisions, portfolio truth, settlement truth, and the canonical
deployment lifecycle. All of that belongs to ARX.

The division is not a layering preference. It is what makes the runner safe to
hand your keys to: a Custos that could approve its own deployments would be a
system where compromising one machine is enough.

## Vocabulary

These five terms appear throughout the documentation and in every fact the
runner emits.

### DeploymentSpec

An immutable configuration owned upstream. The spec carries strategy artifact
provenance, mode, target runner, credential scope, parameters, and — for live
mode — promotion evidence.

Its identifier and digest are **provenance**: they record what was configured,
not what is running.

### DeploymentInstance

One attempt to run a DeploymentSpec on a runner. `deployment_instance_id` is the
runtime primary key.

Retries, redeployments and parallel instances of the same spec each get a
distinct instance identifier. This is why a retry cannot act on the wrong
process: the identifier names the attempt, not the configuration.

### Generation and watermarks

A generation is a monotonic integer attached to a signed desired-state command.

Custos tracks the applied generation separately from the reported one. That
separation is what lets a failure to report a fact retry the *reporting* without
repeating a successful engine action — the work and the record of the work are
independently recoverable.

### Engine handle

The local engine resource for one deployment instance. Every engine protocol
operation takes `deployment_instance_id`; the spec identifier is retained only
as provenance in facts and diagnostics.

### RunnerFact

A signed observation emitted by Custos, stating what this runner observed or
executed.

A fact is not the canonical business record. Upstream validates it and persists
it before any canonical state changes. The runner reports; it does not decide
what its report means.

## Invariants

Seven properties hold regardless of configuration:

1. A command is processed only after exact-byte and exact-subject signature
   verification.
2. Tenant, mode, runner and instance must agree across the subject, the envelope
   and the payload.
3. Runtime state is keyed only by `deployment_instance_id`.
4. A DeploymentSpec cannot silently cross the boundary between simulated and
   real-money execution.
5. Live execution fails closed without signed promotion evidence.
6. Invalid signed commands are terminally acknowledged and audited; transient
   local apply failures are retried.
7. Custos never fabricates an approval, a promotion, or a business fact.

Invariant 6 is the one that surprises people. A malformed command is not retried
— retrying it forever would be a denial of service against your own runner — but
it is never discarded silently either. It is recorded and acknowledged as
terminal.

## Where to go next

- The guarantees and how they are held: [trust model](/introduction/trust-model)
- How the pieces fit: [architecture at a glance](/introduction/architecture-at-a-glance)
- Running one: [installation](/getting-started/installation)
