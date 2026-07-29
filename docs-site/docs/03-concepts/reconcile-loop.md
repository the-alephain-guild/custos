---
title: "Reconcile Loop"
sidebar_position: 4
---

# Reconcile Loop

Custos does not take orders imperatively. It receives a signed statement of
what *should* be running, compares it with what *is* running, and closes the
gap. That difference matters when things go wrong: a lost message or a crashed
process leaves the desired state intact, so the runner converges on restart
instead of needing the instruction replayed.

## Input

Desired state arrives as a signed domain event. The verifier authenticates the
exact subject and the exact serialized bytes **before** the payload is parsed —
nothing inside an unverified message is trusted, including the fields that
would tell you whether to trust it.

The payload carries the immutable deployment spec, the exact deployment
instance, the desired lifecycle state and the generation. See
[NATS subjects](/reference/nats-subjects) for the subject shape.

## State

Reconciliation authority is durable, in the same SQLite database that holds the
fact outbox:

```text
desired_deployments[deployment_instance_id]
applied_deployments[deployment_instance_id]
command_in_progress_lease[deployment_instance_id]
command_outcomes[outcome_id]
runner_fact_outbox[batch_id]
```

Everything is keyed by `deployment_instance_id`, never by spec id. That allows
several instances of one immutable spec, and it stops a retry from acting on
the wrong process — two consequences of the same choice.

There is no in-memory reconciler, no spec-keyed watermark and no compatibility
fallback. A command that cannot enter this authority is rejected fail-closed;
the runtime never reconstructs authority from a local file or an older payload
shape.

## The algorithm

1. Verify the signed envelope and exact subject binding, before parsing payload
   fields.
2. Validate tenant, mode, runner, instance, spec digest, release binding and
   generation.
3. Persist the exact command and compare it with the accepted generation for
   that instance.
4. Load the exact durable desired record.
5. Resolve strategy release material through the authenticated resolver.
6. Verify and activate the exact artifact under the immutable activation root.
   An exact redelivery reloads the durable activation; it never imports a
   mutable source path.
7. Resolve the signed credential scope locally and apply through the engine
   lifecycle supervisor, passing the verified artifact as a required input.
8. Wait for the typed seven-check ready receipt. Task creation alone is not
   readiness — a task that started and immediately failed looks the same as one
   that started and works.
9. Atomically commit applied state and the lifecycle fact in one transaction.
10. Acknowledge only after that transaction. A matching restart or redelivery
    probes ready state rather than deploying again.

The lifecycle event id is derived from stream authority, spec id and digest,
generation, lifecycle state, the stable command fingerprint and the outcome.
Observation time stays in the payload and is never part of identity — no
timestamp, local file or reconstructed payload can substitute for the signed
fingerprint.

## Delivery disposition

Three of these are the transport's own acknowledgements, and the difference
between them is the whole design:

| | Meaning |
|---|---|
| `ACK` | handled — do not send it again |
| `NAK` | not handled yet — send it again |
| `TERM` | will never be handled — stop sending it |

Every message reaches exactly one of these, and the choice is durable:

| Outcome | Disposition |
|---|---|
| Bad signature, subject mismatch, invalid contract | Durable untrusted rejection, then TERM |
| Same generation, exact same bytes | Replay the prior durable disposition |
| Same generation with different bytes; stale; retries exhausted | Atomic terminal outcome and fact, then TERM |
| Successful application | Atomic applied state and fact, then ACK |
| Transient engine or local dependency failure | NAK for redelivery |

Two properties fall out of this table, and both are deliberate. A poison
command cannot create an infinite redelivery loop — it terminates. A transient
failure is never acknowledged as success — it is retried.

## Supervision

Zombie detection, breaker state, peak-equity tracking and engine task
completion are all keyed by `deployment_instance_id`. Credentials are indexed
by signed credential scope, and each use is bound to the exact instance. Facts
retain `deployment_spec_id` only to record which immutable configuration ran.

Ready timeout, retryable terminal events and zombie disconnect share **one**
durable restart budget with exponential backoff. A non-retryable terminal event
or an exhausted budget atomically quarantines the deployment and enqueues the
terminal lifecycle fact.

If any long-running task exits unexpectedly, the daemon treats it as fatal: it
cancels sibling tasks, stops deployments, flushes the fact outbox, then closes
transports. In that order — the facts are flushed before the transport that
would carry them is torn down.

## The breaker's valuation boundary

The fallback breaker reads exactly one engine status per instance per tick,
derived from the portfolio snapshot provider. Open notional and actual equity
therefore come from a single valuation boundary rather than two that could
disagree.

A probe exception or a typed unreliable status immediately freezes the breaker
and requests flattening. A missing mark or equity can never skip a tick or be
treated as zero risk — unknown exposure is handled as exposure, not as none.

This is local execution evidence. It does not replace the signed runner policy
that supplies the aggregate cap, and a deployment's own `risk_config` cannot
define or override that cap. See
[safety survives a disconnect](/trust-model/safety-survives-disconnect).
