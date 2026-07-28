---
title: "Your First Deployment"
sidebar_position: 4
---

# Your First Deployment

A running runner does nothing until a deployment arrives. This chapter is about
what arrives, what the runner does with it, and how to tell it worked.

:::note You do not create this
Custos cannot author or approve a DeploymentSpec, and there is no CLI command to
publish one. Deployments are created and approved in ARX and arrive as signed
events. If that seems inconvenient, it is the same property that stops a
compromised runner from deploying anything to itself.
:::

## What arrives

A signed desired-state command carrying the immutable deployment spec, the exact
deployment instance, the desired lifecycle state, and the generation. For live
mode it also carries promotion evidence.

The runner verifies the signature over the **exact bytes** and the **exact
subject** before parsing any field — nothing inside an unverified message is
trusted, including the fields that would tell you whether to trust it.

## What the runner does

1. Verifies the envelope and subject binding.
2. Validates tenant, mode, runner, instance, spec digest, release binding and
   generation.
3. Persists the exact command and compares it with the accepted generation.
4. Resolves the strategy release through the authenticated resolver.
5. Verifies and activates the artifact under an immutable activation root.
6. Resolves the credential scope locally and applies through the engine.
7. Waits for a typed readiness receipt — task creation alone is not readiness.
8. Commits applied state and the lifecycle fact in one transaction.
9. Acknowledges only after that transaction.

Full detail in [the reconcile loop](/concepts/reconcile-loop).

## Watching it happen

The daemon writes structured JSON to stdout. The three events worth watching
are the command being accepted, the engine reporting ready, and the lifecycle
fact being enqueued.

```bash
arx-runner health --json
```

Readiness flips only after step 8. If health still reports not-ready while the
logs show engine activity, the deployment is mid-apply rather than failed.

## Confirming the right thing ran

Ask the runner what it applied, not what you think you sent:

- `deployment_instance_id` is the runtime identity. Two instances of one spec are
  two separate things.
- the spec id and digest travel with every fact as provenance, so the fact
  stream answers "which configuration ran" without trusting local state.

See [spec vs instance](/concepts/deployment-spec-vs-instance).

## If it is refused

A deployment can be rejected before any engine is built. That is admission, and
it is deliberate — the gate refuses rather than starting something weaker. The
seven conditions are listed in
[the live execution gate](/concepts/live-execution-gate).

A refusal is terminal and audited; it is not retried, because none of the
conditions become true by waiting. A transient failure — the engine failing to
start for a recoverable reason — is retried under a durable restart budget
instead. The two are deliberately different kinds of event.

## Then what

Nothing about a running deployment depends on staying connected. If the upstream
authority becomes unreachable, the deployment keeps running from durable local
state and the local guards keep enforcing — see
[safety survives a disconnect](/trust-model/safety-survives-disconnect).
