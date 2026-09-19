---
title: "First signed sandbox run"
sidebar_position: 3
---

This guide connects an enrolled runner to ARX and enables deployment reconciliation. For a local exercise without ARX, use [standalone sandbox](/getting-started/standalone-sandbox).

## Before starting

Complete [enrollment](/getting-started/enrollment). Obtain these additional inputs from your deployment administrator:

- A sandbox NATS transport authority, TLS CA, server name and pinned issuer key.
- The domain-event public key and key id used to verify deployment commands.
- A valid runner capability receipt at `~/.arx/runner-capability.json`.
- A venue credential and the exact scope digest bound by the signed deployment.
- Accessible strategy release material and runner-local release trust configuration, or an explicit sandbox development artifact supplied through the supported signed path.

Obtain the transport authority URL and approved authorization intent UUID as `TRANSPORT_AUTHORITY_URL` and `TRANSPORT_INTENT_ID`. Set `NATS_SIM_URL`, `NATS_SIM_SERVER_NAME`, `NATS_SIM_ISSUER_PUBLIC_KEY`, `NATS_CA_FILE`, `DOMAIN_PUBLIC_KEY_FILE` and `DOMAIN_KEY_ID` to those issued values. `SOPS_AGE_KEY_FILE` must point to the age identity used at enrollment.

## Provision transport and the venue key

```bash
uv run arx-runner nats-transport enroll \
  --trading-mode sandbox \
  --authorization-intent-id "$TRANSPORT_INTENT_ID" \
  --nats-url "$NATS_SIM_URL" \
  --nats-server-name "$NATS_SIM_SERVER_NAME" \
  --nats-ca "$NATS_CA_FILE" \
  --crucible-url "$TRANSPORT_AUTHORITY_URL" # disclosure-ok: exact CLI flag accepted by the parser
```

Provision the key using [credential vault operations](/operator-guide/credential-vault). Even `sandbox-sim` exercises local vault resolution. Use demo key material only when the approved sandbox deployment is explicitly configured for it; do not invent a signed scope digest.

For immutable releases, configure the release policy and trust root as described in [deployment](/operator-guide/deployment) before starting.

## Start reconciliation

```bash
uv run arx-runner start \
  --enabled-mode sandbox \
  --engine sandbox-sim \
  --reconcile \
  --nats-sim-url "$NATS_SIM_URL" \
  --nats-sim-ca "$NATS_CA_FILE" \
  --nats-sim-server-name "$NATS_SIM_SERVER_NAME" \
  --nats-sim-issuer-public-key "$NATS_SIM_ISSUER_PUBLIC_KEY" \
  --crucible-domain-public-key "$DOMAIN_PUBLIC_KEY_FILE" \
  --crucible-domain-key-id "$DOMAIN_KEY_ID"
```
<!-- disclosure-ok: exact CLI flags accepted by the runner -->

`--reconcile` is required to construct the engine and subscribe to deployment commands. Without it, the process can pass its health check while no deployment consumer is running.

`sandbox-sim` makes no venue connection. To run a compatible strategy against live data with local fills, install the Nautilus extra and select `--engine nautilus`.

## Verify the result

In another terminal:

```bash
uv run arx-runner health --json
```

Check `ready: true` and `deployment_subscription: true`. Then create/approve the sandbox deployment in ARX and confirm the lifecycle fact for its exact instance and generation. Engine readiness also requires reliable portfolio valuation; a healthy daemon alone does not prove strategy readiness.

If startup fails, follow the named authority, transport or artifact check in [troubleshooting](/operator-guide/troubleshooting). Do not switch to the offline lane as a recovery step for a failed signed deployment.
