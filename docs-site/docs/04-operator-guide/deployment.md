---
title: "Deployment"
sidebar_position: 1
---

# Deployment

Custos runs on your infrastructure. ARX authorizes intent, owns deployment
business state, signs commands and ingests execution facts; Custos verifies
those commands, reconciles the local runtime, and signs the facts it reports
back.

This page covers provisioning a runner from nothing to a running deployment.

## Runtime artifact

The current downstream-development artifact is the verified local image:

```text
custos-runner:v0.3.0
```

Build and gate it with `make verify-local-v030`. Remote release is still
deferred. Consume this image directly — do not maintain a derived Dockerfile,
because a derived image is not the artifact the gate verified.

## What you need first

- An ARX enrollment endpoint and a one-time enrollment token.
- The exact ARX Ed25519 domain-event public key and its key ID.
- Network reach to the signed command stream.
- One sops+age encrypted file per venue credential, each scoped
  `trade_no_withdraw`.

Custos never creates streams and never publishes deployment commands. Its
`deployment` CLI has one offline action, `validate`.

## Enrollment and venue credentials

```bash
mkdir -p "$HOME/.arx/vault" "$HOME/.arx/state"
chmod 700 "$HOME/.arx" "$HOME/.arx/vault" "$HOME/.arx/state"
age-keygen -o "$HOME/.arx/age.key"
chmod 600 "$HOME/.arx/age.key"
export SOPS_AGE_KEY_FILE="$HOME/.arx/age.key"
export SOPS_AGE_RECIPIENT="$(age-keygen -y "$SOPS_AGE_KEY_FILE")"

arx-runner enroll \
  --token '<one-time-enrollment-token>' \
  --backend https://arx.internal \
  --tenant-id acme \
  --runner-id 22222222-2222-4222-8222-222222222222

printf '%s\n' '<venue-api-secret>' | arx-runner vault put \
  --key-id binance-testnet \
  --tenant-id acme \
  --api-key '<venue-api-key>' \
  --scope-digest '<credential_scope.scope_digest from the deployment>' \
  --api-secret-stdin \
  --age-recipient "$SOPS_AGE_RECIPIENT" \
  --permission-scope trade_no_withdraw
```

`runner.toml` holds only public binding metadata. The opaque machine credential
and the Ed25519 private key stay encrypted together in `runner-machine.enc`.
Hand-written runner records are unsupported in every mode — a runner that
cannot prove its own enrollment does not start.

## Starting the runner

```bash
arx-runner start \
  --enabled-mode sandbox \
  --nats-sim-url tls://arx-nats.internal:4222 \
  --nats-sim-ca "$HOME/.arx/certs/arx-nats-ca.pem" \
  --nats-sim-server-name arx-nats.internal \
  --nats-sim-issuer-public-key "$ARX_NATS_SIM_ISSUER_PUBLIC_KEY" \
  --crucible-domain-public-key "$HOME/.arx/crucible-domain-event.pub" \
  --crucible-domain-key-id arx-domain-v1 \
  --engine nautilus
```

Readiness is fail-closed. `arx-runner health` succeeds only after machine
authority verification and after the exact runner subscription is established.

`deployment_instance_id` is the runtime primary key for reconciler state,
engine handles, watchdogs, breakers and facts. `spec_id` identifies immutable
configuration provenance and is not a runtime handle.

### Workstation demonstration

For a non-promotable local demonstration there is one plaintext exception, an
explicit loopback sandbox session:

```bash
arx-runner start --enabled-mode sandbox --reconcile \
  --development-local-nats-url nats://127.0.0.1:24222 \
  --crucible-domain-public-key /tmp/demo/crucible-domain-event.pub \
  --crucible-domain-key-id arx-domain-v1 \
  --engine sandbox-sim
```

The development flag rejects non-loopback hosts, `testnet`, `live`,
credentials embedded in the URL, and any simultaneous production endpoint. It
is never a fallback when TLS or key authority fails — a failed authority check
stays failed.

## Deployment lifecycle

Deployments and every desired-state change originate upstream. Custos has no
local creation path. It verifies the signed event, canonical digest, tenant,
runner, deployment instance and generation, then resolves strategy release
material through the authenticated owner boundary.

Production strategy execution additionally requires this trust configuration,
complete or not at all:

```bash
export CUSTOS_ARTIFACT_CACHE_DIR=/var/lib/custos/artifacts
export CUSTOS_ARTIFACT_RELEASE_POLICY_ENVELOPE=/etc/custos/artifact-release-policy.json
export CUSTOS_ARTIFACT_RELEASE_POLICY_PUBLIC_KEY=/etc/custos/artifact-release-policy.pub
export CUSTOS_ARTIFACT_SIGSTORE_TRUSTED_ROOT=/etc/custos/sigstore-trusted-root.json
export CUSTOS_ARTIFACT_RELEASE_POLICY_KEY_ID=custos-artifact-release-policy-v1
export CUSTOS_ARTIFACT_REGISTRY=ghcr.io
```

For a private registry, also set `CUSTOS_ARTIFACT_REGISTRY_USERNAME` and
`CUSTOS_ARTIFACT_REGISTRY_TOKEN` together. The token deliberately has no CLI
flag, so it cannot end up in process arguments.

Custos accepts only signed detached material coordinates on the configured
HTTPS registry, verifies the complete snapshot and evidence chain, and stores
immutable blobs under `$CUSTOS_ARTIFACT_CACHE_DIR/sha256/<digest>`. Missing or
partial trust configuration fails startup; an unavailable resolver never falls
back to development material.

Live execution requires an issued `promotion_id` and
`promotion_evidence_digest`. Custos validates that they are present and
correctly bound — it does not count approvers or implement the
separation-of-duties policy itself.

Applied lifecycle generations are reported as
`RunnerDeploymentLifecycleFact.v1` through the signed fact outbox, which owns
sequence allocation. If a fact cannot be durably enqueued, the command is not
acknowledged. Redelivery resumes the same instance and activation identity
without repeating a committed engine action.

## Container example

The runnable `examples/supertrend-testnet` Compose file starts the runner
only; the signed command stream is an external dependency.

```bash
make verify-local-v030
cd examples/supertrend-testnet
test -f .env || cp .env.example .env
docker compose up
```

Persist `/home/custos/.arx`. An ephemeral mount loses machine authority and
venue credentials, and the runner will not start without them.

When something goes wrong, see [troubleshooting](./troubleshooting); for
outages and recovery, see the [emergency playbook](./emergency-playbook).
