---
title: "Standalone sandbox"
sidebar_position: 5
---

This exercise uses local NATS, an unattested identity and `sandbox-sim`. It verifies encrypted credential loading, desired-state delivery, local apply and status reporting. It does not import a trading strategy, connect to a venue or produce signed RunnerFacts.

Run from the Custos checkout after `make install`. Install `sops`, `age` and Docker. The fixture directory is temporary and separate from your normal `~/.arx` state.

## 1. Start an isolated broker

In a separate terminal:

```bash
docker run --rm --name custos-docs-nats \
  -p 127.0.0.1:14222:4222 nats:2 -js
```

This broker has no authentication and binds only to loopback. It is for this disposable exercise. Its data disappears when the container is removed; use authenticated, durable operator-owned infrastructure for ongoing work.

## 2. Create a local identity and demo vault entry

```bash
umask 077
export DEMO_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/custos-docs.XXXXXX")"
uv run python docs-site/examples/standalone/prepare.py --root "$DEMO_ROOT"
age-keygen -o "$DEMO_ROOT/age.key"
export SOPS_AGE_KEY_FILE="$DEMO_ROOT/age.key"
export SOPS_AGE_RECIPIENT="$(age-keygen -y "$SOPS_AGE_KEY_FILE")"
uv run arx-runner identity standalone \
  --tenant-id docs-demo \
  --runner-toml "$DEMO_ROOT/runner.toml" \
  --machine-vault "$DEMO_ROOT/vault/runner-machine.enc"
printf '%s\n' 'sandbox-demo-secret' | uv run arx-runner vault put \
  --key-id docs-demo --tenant-id docs-demo --api-key sandbox-demo-key \
  --api-secret-stdin --scope-digest "$(printf '0%.0s' {1..64})" \
  --vault-dir "$DEMO_ROOT/vault"
uv run arx-runner vault verify \
  --key-id docs-demo --tenant-id docs-demo --vault-dir "$DEMO_ROOT/vault"
```

Keep the printed `DEMO_ROOT` path. The zero scope digest and demo keys are non-trading fixture values for this offline simulation, not credentials or scope evidence for a signed deployment.

`identity standalone` writes an encrypted machine identity with `backend_url=http://standalone.invalid`. It cannot enroll transport, publish signed capabilities or start the signed lane.

## 3. Bootstrap, validate and publish

```bash
uv run arx-runner nats bootstrap --profile standalone \
  --tenant-id docs-demo --nats-url nats://127.0.0.1:14222
uv run arx-runner deployment validate \
  --spec-file "$DEMO_ROOT/spec.json" --strategy-dir "$DEMO_ROOT/strategy" \
  --mode sandbox
uv run arx-runner deployment publish \
  --spec-file "$DEMO_ROOT/spec.json" --strategy-dir "$DEMO_ROOT/strategy" \
  --mode sandbox --tenant-id docs-demo --strategy-id docs-sandbox \
  --nats-url nats://127.0.0.1:14222
```

Validation exits zero and reports a directory digest. Publishing waits for a JetStream acknowledgement. `--strategy-dir` binds/checks the in-memory spec's code hash; it does not rewrite the JSON file. The desired-state stream keeps the latest message per subject.

## 4. Run the simulator

```bash
uv run arx-runner start \
  --runner-toml "$DEMO_ROOT/runner.toml" \
  --vault-dir "$DEMO_ROOT/vault" \
  --ready-file "$DEMO_ROOT/ready.json" \
  --offline-state "$DEMO_ROOT/offline.db" \
  --reconcile-strategy-id docs-sandbox --runner-label docs-runner \
  --engine sandbox-sim --nats-url nats://127.0.0.1:14222
```

Leave the process running. In another terminal, return to the same checkout and export `DEMO_ROOT` to the path printed in step 2, then run:

```bash
uv run arx-runner health --ready-file "$DEMO_ROOT/ready.json" --json
uv run python docs-site/examples/standalone/status.py \
  --subject arx.docs-demo.deployment_status.docs-runner.docs-sandbox \
  --generation 1 --phase running
```

Expect daemon `ready: true`, plus local status `observed_generation: 1`, `phase: running`, `health: healthy`. That status means the simulator applied the request. It does not prove a real engine or portfolio is ready.

## 5. Stop the deployment

```bash
uv run arx-runner deployment publish \
  --spec-file "$DEMO_ROOT/stop.json" --strategy-dir "$DEMO_ROOT/strategy" \
  --mode sandbox --tenant-id docs-demo --strategy-id docs-sandbox \
  --nats-url nats://127.0.0.1:14222
uv run python docs-site/examples/standalone/status.py \
  --subject arx.docs-demo.deployment_status.docs-runner.docs-sandbox \
  --generation 2 --phase stopped
```

After observing generation 2, stop the runner with Ctrl-C in its terminal, then stop the demo broker with Ctrl-C. Retain the temporary directory while diagnosing failures. Do not reuse this dummy strategy directory with `--engine nautilus`.

## Run an actual strategy

For Nautilus, install `make install-nt` and supply a compatible strategy directory with `config.yaml`, a registered strategy name, matching instruments, and suitable risk ceilings. The offline loader discovers that directory once per process. A second directory or node requires another process and isolated state.

To use Binance testnet, supply testnet-only credentials, set the spec to `testnet`, remove its sandbox balances, and select `--engine nautilus`; see [offline testnet](/operator-guide/offline-testnet). SoDEX has additional input limitations documented in [SoDEX](/engines/sodex).
