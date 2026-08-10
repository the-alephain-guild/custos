# SuperTrend on Binance testnet with Custos 0.3.0

This Compose example runs only the Custos execution boundary from the verified
local `custos-runner:v0.3.0` image. A reachable Crucible deployment service and
its provisioned JetStream domain-event stream are prerequisites. No derived
Custos Dockerfile, local business-stream bootstrap, or runner-side command
publisher is used.

## Prerequisites

- Docker with Compose v2.
- A released strategy artifact selected by Crucible.
- Crucible HTTP and NATS endpoints plus its domain-event public key.
- A one-time runner enrollment token.
- Binance testnet credentials with withdrawal disabled.

Build and verify the exact local image:

```bash
make verify-local-v030
cd examples/supertrend-testnet
test -f .env || cp .env.example .env
set -a; . ./.env; set +a
```

## 1. Enroll the machine principal

```bash
mkdir -p runtime/.arx/vault runtime/.arx/state
chmod 700 runtime/.arx runtime/.arx/vault runtime/.arx/state
age-keygen -o runtime/.arx/age.key
chmod 600 runtime/.arx/age.key
export SOPS_AGE_RECIPIENT="$(age-keygen -y runtime/.arx/age.key)"
install -m 600 /dev/null runtime/.arx/enrollment-token
printf '%s' '<one-time-token>' > runtime/.arx/enrollment-token

docker run --rm \
  -v "$PWD/runtime/.arx:/home/custos/.arx" \
  -e SOPS_AGE_RECIPIENT \
  custos-runner:v0.3.0 enroll \
  --token-file /home/custos/.arx/enrollment-token \
  --backend "$CRUCIBLE_HTTP_URL" \
  --tenant-id "$CUSTOS_TENANT_ID" \
  --runner-id "$CUSTOS_RUNNER_ID"
rm -f runtime/.arx/enrollment-token
```

The `arx-runner enroll` flow creates `runner.toml` public metadata and an
encrypted runner machine credential. Do not construct either file manually.
Install the Crucible event verification key at
`runtime/.arx/crucible-domain-event.pub`.

## 2. Provision the venue credential

The container entrypoint below invokes the same `arx-runner vault put` command
used by a source installation.

```bash
printf '%s\n' '<binance-testnet-api-secret>' | docker run --rm -i \
  -v "$PWD/runtime/.arx:/home/custos/.arx" \
  custos-runner:v0.3.0 vault put \
  --key-id binance-testnet \
  --tenant-id "$CUSTOS_TENANT_ID" \
  --api-key '<binance-testnet-api-key>' \
  --api-secret-stdin \
  --scope-digest '<lowercase-sha256-bound-by-the-deployment-spec>' \
  --age-recipient "$SOPS_AGE_RECIPIENT" \
  --permission-scope trade_no_withdraw
```

The `--scope-digest` value is the exact lowercase SHA-256 that the DeploymentSpec
binds as this credential's scope. It is required: the vault record must prove it
holds the credential the signed command actually asked for, not merely one with a
matching key id.


## 3. Run the signed-command consumer

```bash
docker compose up
```

Create, approve, promote, stop, or archive the deployment through Crucible.
Custos consumes the signed command, resolves the authenticated StrategyRelease
and emits signed lifecycle facts; it never becomes the business fact owner.
Observe only the runner with `docker compose logs -f runner`.

Remote release remains deferred; this workflow consumes no GHCR artifact.
