# Custos 0.3.0 deployment

Custos is the local execution boundary. ARX authenticates actors and authorizes
intent; Crucible owns deployment business state, promotion, command signing,
and execution-fact ingestion; Custos verifies commands, reconciles the local
runtime, and signs RunnerFacts. ARX is not a DeploymentSpec or RunnerFact relay.

## Runtime artifact

The current downstream-development artifact is the verified local image:

```text
custos-runner:v0.3.0
```

Build and gate it with `make verify-local-v030`. Remote release remains
deferred. Downstream projects consume this image directly and must not maintain
a derived Custos Dockerfile.

## Required external services

- Crucible HTTP enrollment authority.
- Crucible-owned JetStream streams containing signed runner commands.
- The exact Crucible Ed25519 domain-event public key and key ID.
- A nonce-bound runner machine credential created by `arx-runner enroll`.
- One sops+age encrypted file per venue credential, limited to
  `trade_no_withdraw`.

Custos never creates business streams and never publishes deployment commands.
The `deployment` CLI has only an offline `validate` action.

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
  --backend https://crucible.internal \
  --tenant-id acme \
  --runner-id 22222222-2222-4222-8222-222222222222

printf '%s\n' '<venue-api-secret>' | arx-runner vault put \
  --key-id binance-testnet \
  --tenant-id acme \
  --api-key '<venue-api-key>' \
  --scope-digest '<DeploymentSpec credential_scope.scope_digest>' \
  --api-secret-stdin \
  --age-recipient "$SOPS_AGE_RECIPIENT" \
  --permission-scope trade_no_withdraw
```

`runner.toml` contains only public binding metadata. The opaque machine
credential and Ed25519 private key remain encrypted together in
`runner-machine.enc`. Manual runner records are unsupported in every mode.

## Start

```bash
arx-runner start \
  --enabled-mode sandbox \
  --nats-sim-url tls://crucible-nats.internal:4222 \
  --nats-sim-ca "$HOME/.arx/certs/crucible-nats-ca.pem" \
  --nats-sim-server-name crucible-nats.internal \
  --nats-sim-issuer-public-key "$CRUCIBLE_NATS_SIM_ISSUER_PUBLIC_KEY" \
  --crucible-domain-public-key "$HOME/.arx/crucible-domain-event.pub" \
  --crucible-domain-key-id crucible-domain-v1 \
  --engine nautilus
```

For a non-promotable workstation demonstration, the sole plaintext exception is
an explicit loopback sandbox session:

```bash
arx-runner start --enabled-mode sandbox --reconcile \
  --development-local-nats-url nats://127.0.0.1:24222 \
  --crucible-domain-public-key /tmp/v1-team-demo/crucible-domain-event.pub \
  --crucible-domain-key-id crucible-domain-v1 \
  --engine sandbox-sim
```

The development flag rejects non-loopback hosts, `testnet`, `live`, credentials
in the URL, and any simultaneous production NATS endpoint. It is never a
fallback from failed TLS/NKey authority.

`deployment_instance_id` is the runtime primary key for reconciler state,
engine handles, watchdogs, fallback breakers, and facts. `spec_id` identifies
immutable configuration provenance and must not be used as a runtime handle.

Readiness is fail-closed. `arx-runner health` succeeds only after machine
authority verification and establishment of the exact runner subscription.

## Deployment lifecycle

Initial deployment and every desired-state change originate in Crucible.
Custos has no local DeploymentSpec creation or validation CLI. It verifies the
signed event, canonical digest, tenant, runner, deployment instance and
generation, then resolves StrategyRelease material through the authenticated
owner boundary.

Production StrategyRelease execution additionally requires the following
environment variables as one complete trust configuration:

```bash
export CUSTOS_ARTIFACT_CACHE_DIR=/var/lib/custos/artifacts
export CUSTOS_ARTIFACT_RELEASE_POLICY_ENVELOPE=/etc/custos/artifact-release-policy.json
export CUSTOS_ARTIFACT_RELEASE_POLICY_PUBLIC_KEY=/etc/custos/artifact-release-policy.pub
export CUSTOS_ARTIFACT_SIGSTORE_TRUSTED_ROOT=/etc/custos/sigstore-trusted-root.json
export CUSTOS_ARTIFACT_RELEASE_POLICY_KEY_ID=custos-artifact-release-policy-v1
export CUSTOS_ARTIFACT_REGISTRY=ghcr.io
```

For a private registry, also set
`CUSTOS_ARTIFACT_REGISTRY_USERNAME` and
`CUSTOS_ARTIFACT_REGISTRY_TOKEN` together. The token intentionally has no CLI
flag so it cannot enter process arguments. Custos accepts only signed detached
material coordinates on the configured HTTPS registry, verifies the complete
Crucible snapshot and evidence chain, and stores immutable blobs under
`$CUSTOS_ARTIFACT_CACHE_DIR/sha256/<digest>`. Missing or partial production
trust configuration fails startup; an unavailable production resolver never
falls back to development material.

Live execution requires a Crucible-issued `promotion_id` and
`promotion_evidence_digest`. Custos validates their presence but does not count
human approvers or implement separation-of-duties policy.

Applied lifecycle generations are reported as
`RunnerDeploymentLifecycleFact.v1` through the signed RunnerFact outbox. The
outbox owns sequence allocation. Failures to durably enqueue a fact prevent the
command acknowledgement. Redelivery resumes the same instance and activation
identity without repeating a committed engine action.

## Container example

The runnable [`examples/supertrend-testnet`](../../examples/supertrend-testnet/)
Compose file starts only the runner. Crucible and its JetStream topology are
external dependencies:

```bash
make verify-local-v030
cd examples/supertrend-testnet
test -f .env || cp .env.example .env
docker compose up
```

Persist `/home/custos/.arx`; an ephemeral mount loses machine authority and
venue credentials. See [`runbook.md`](runbook.md) for failure handling.
