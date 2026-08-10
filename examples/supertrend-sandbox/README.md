# SuperTrend sandbox with Custos 0.3.0

This example runs NautilusTrader with live market data and locally simulated
fills. Crucible remains the deployment owner: it enrolls the runner, owns the
JetStream command topology, and sends signed desired-state events. Custos does
not create streams or publish its own DeploymentSpec.

Install the development and NautilusTrader extras on Python 3.12+:

```bash
uv sync --extra dev --extra nautilus
```

## 1. Enroll the runner machine principal

Generate an age identity, obtain a one-time enrollment token from the
Crucible control plane, then run the nonce-bound enrollment command:

```bash
mkdir -p ~/.arx/vault ~/.arx/state
chmod 700 ~/.arx ~/.arx/vault ~/.arx/state
age-keygen -o ~/.arx/age.key
chmod 600 ~/.arx/age.key
export SOPS_AGE_KEY_FILE="$HOME/.arx/age.key"
export SOPS_AGE_RECIPIENT="$(age-keygen -y "$SOPS_AGE_KEY_FILE")"
install -m 600 /dev/null "$HOME/.arx/enrollment-token"
printf '%s' '<one-time-token>' > "$HOME/.arx/enrollment-token"

uv run arx-runner enroll \
  --token-file "$HOME/.arx/enrollment-token" \
  --backend https://crucible.example \
  --tenant-id acme \
  --runner-id 22222222-2222-4222-8222-222222222222
rm -f "$HOME/.arx/enrollment-token"
```

Enrollment writes public binding metadata to `~/.arx/runner.toml` and keeps
the opaque credential plus Ed25519 private key in the encrypted machine vault.
Manual runner records are not supported.

## 2. Provision the venue credential

```bash
printf '%s\n' '<sandbox-api-secret>' | uv run arx-runner vault put \
  --key-id binance-sandbox-key \
  --tenant-id acme \
  --api-key '<sandbox-api-key>' \
  --api-secret-stdin \
  --scope-digest '<lowercase-sha256-bound-by-the-deployment-spec>' \
  --age-recipient "$SOPS_AGE_RECIPIENT" \
  --permission-scope trade_no_withdraw
```

The `--scope-digest` value is the exact lowercase SHA-256 that the DeploymentSpec
binds as this credential's scope. It is required: the vault record must prove it
holds the credential the signed command actually asked for, not merely one with a
matching key id.


## 3. Start the signed-command consumer

```bash
uv run arx-runner start \
  --enabled-mode sandbox \
  --nats-sim-url nats://crucible-nats.internal:4222 \
  --crucible-domain-public-key "$HOME/.arx/crucible-domain-event.pub" \
  --crucible-domain-key-id crucible-domain-v1 \
  --engine nautilus
```

Create and approve the DeploymentSpec in Crucible. The runner becomes ready only
after machine authority verification, exact signed-command consumption and
authenticated StrategyRelease resolution. Lifecycle observations return through
the signed RunnerFact outbox.
