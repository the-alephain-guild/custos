---
title: "CLI Reference"
sidebar_position: 1
---

# CLI Reference

`arx-runner` is the only interface to the runner. There is no HTTP admin API, no
config-file-driven mode, and no second entry point — `python -m custos` exits
non-zero with a pointer here.

```text
arx-runner {enroll,credential,vault,nats-transport,publish-capability,start,health}
```

Every subcommand accepts `--help`. This page is the map; `--help` is the
authority, because it is generated from the parser you are actually running.

Flags marked **required** have no default. That is deliberate for anything that
selects a trading mode or names an authority: a default would be a choice nobody
made.

## enroll

Obtain a machine identity the runner can prove.

```bash
install -m 600 /dev/null "$HOME/.arx/enrollment-token"
printf '%s' '<one-time-token>' > "$HOME/.arx/enrollment-token"
arx-runner enroll \
  --token-file "$HOME/.arx/enrollment-token" \
  --backend https://arx.example.com \
  --tenant-id acme \
  --runner-id 018f8b5f-6f7d-7e23-8c31-bd34ab9d0d41
rm -f "$HOME/.arx/enrollment-token"
```

| Flag | Required | Meaning |
|---|---|---|
| `--token-file` | ✅ | Mode-`0600` regular file containing the one-time token issued by ARX |
| `--backend` | ✅ | Endpoint to enroll against |
| `--tenant-id` | ✅ | Owning tenant |
| `--runner-id` | ✅ | This runner's UUID |
| `--agent-version` | | Reported agent version |
| `--runner-toml` | | Override the metadata path |
| `--machine-vault` | | Override the encrypted vault path |
| `--age-recipient` | | age public recipient; defaults to `SOPS_AGE_RECIPIENT` |

Writes public binding metadata to `runner.toml` and keeps the credential and
Ed25519 private key in the encrypted machine vault. The private key is generated
locally and never transmitted. See [enrollment](/getting-started/enrollment).

## credential

Manage the machine credential produced by enrollment.

```bash
arx-runner credential verify
arx-runner credential rotate --reason "scheduled rotation"
arx-runner credential revoke --reason "host decommissioned"
```

| Subcommand | Required | Also accepts |
|---|---|---|
| `verify` | — | `--runner-toml` |
| `rotate` | `--reason` | `--runner-toml`, `--age-recipient` |
| `revoke` | `--reason` | `--runner-toml`, `--authority-path`, `--ready-file` |

`--reason` is required on both destructive operations, and it is recorded. An
unexplained rotation is indistinguishable from an attacker rotating a key they
just stole.

Rotation sends the new public key with a proof signed by the **old** key, and
writes locally only after the authority accepts. Revocation erases the local
vault and metadata once the revoked state is confirmed.

## vault

Manage venue credentials. One encrypted file per key.

```bash
printf '%s\n' '<api-secret>' | arx-runner vault put \
  --key-id binance-testnet \
  --tenant-id acme \
  --api-key '<api-key>' \
  --api-secret-stdin \
  --scope-digest '<lowercase-sha256>' \
  --age-recipient "$SOPS_AGE_RECIPIENT" \
  --permission-scope trade_no_withdraw

arx-runner vault verify --key-id binance-testnet --tenant-id acme
arx-runner vault list
```

### `vault put`

| Flag | Required | Meaning |
|---|---|---|
| `--key-id` | ✅ | Vault entry name; also the filename, so `^[a-zA-Z0-9_-]{1,64}$` |
| `--tenant-id` | ✅ | Owning tenant |
| `--api-key` | ✅ | Venue API key (not secret) |
| `--scope-digest` | ✅ | Lowercase SHA-256 the DeploymentSpec binds as this credential's scope |
| `--api-secret-stdin` / `--api-secret-env` / `--api-secret` | ✅ (one of) | How the secret is supplied |
| `--age-recipient` | | age public recipient |
| `--permission-scope` | | Only `trade_no_withdraw`; also the default |
| `--vault-dir` | | Override the vault directory |

Prefer `--api-secret-stdin`. A secret passed as `--api-secret` appears in `ps`
output and in shell history.

### `vault verify`

Requires `--key-id` and `--tenant-id`; accepts `--vault-dir` and
`--age-key-file`. Runs the real decrypt path — sops decrypt, payload parse, file
mode, permission scope. This is the acceptance surface; calling `sops` by hand
tests something else.

### `vault list`

Lists key ids present, warning on stderr about group- or world-readable files.
Accepts `--vault-dir`.

## nats-transport

Issue and manage the runner's transport authority. All five subcommands take the
same flags.

```bash
arx-runner nats-transport verify \
  --trading-mode sandbox \
  --nats-url tls://nats.example.com:4222 \
  --nats-server-name nats.example.com
```

| Flag | Required |
|---|---|
| `--trading-mode` `{sandbox,testnet,live}` | ✅ |
| `--nats-url` | ✅ |
| `--nats-server-name` | ✅ |
| `--nats-ca`, `--runner-toml`, `--machine-vault`, `--transport-vault-dir`, `--verification-timeout-secs` | |

Subcommands: `enroll`, `rotate`, `revoke`, `resume`, `verify`.

## publish-capability

Publish the next capability revision, signed with the enrolled machine key.

| Flag | Required |
|---|---|
| `--manifest` | ✅ |
| `--runner-toml`, `--authority-path`, `--idempotency-key`, `--capability-version-id`, `--capability-version` | |

## start

Run the daemon. It starts only after machine authority passes fail-closed
verification.

```bash
arx-runner start \
  --enabled-mode sandbox \
  --engine sandbox-sim
```

**`--enabled-mode {sandbox,testnet,live}` is required.** One mode per process.

### Selecting the engine

| Flag | Default | Effect |
|---|---|---|
| `--engine nautilus` | default | Real execution across all three modes |
| `--engine sandbox-sim` | | Full local lifecycle, no venue connection; declares `sandbox` only |

### Transport

Sandbox and testnet use the simulation transport; only `live` uses the live one.
So a sandbox runner is configured with the `--nats-sim-*` flags.

| Flag group | Flags |
|---|---|
| Simulation | `--nats-sim-url`, `--nats-sim-ca`, `--nats-sim-server-name`, `--nats-sim-issuer-public-key` |
| Live | `--nats-live-url`, `--nats-live-ca`, `--nats-live-server-name`, `--nats-live-issuer-public-key` |
| Loopback development | `--development-local-nats-url` — sandbox-only, non-promotable |

### Command verification

| Flag | Meaning |
|---|---|
| `--crucible-domain-public-key` | Public key that signed desired-state commands are verified against |
| `--crucible-domain-key-id` | Expected signing key id |
| `--reconcile` | Enable the reconcile loop |

Those two flag names keep a legacy spelling. They are the strings the parser
accepts, so they are reproduced verbatim rather than tidied — a renamed flag in
documentation is a command that does not run.
<!-- disclosure-ok: exact CLI flag an operator types; renaming it here would document a command argparse rejects -->

### Paths

| Flag | Default |
|---|---|
| `--runner-toml` | `~/.arx/runner.toml` |
| `--machine-vault` | override; must equal the `runner.toml` value |
| `--vault-dir` | `~/.arx/vault` |
| `--ready-file` | `~/.arx/state/runner-ready.json` |
| `--runner-capability` | `~/.arx/runner-capability.json` |
| `--runner-fact-outbox` | `~/.arx/state/runner-fact-outbox.db` |
| `--nats-transport-vault-dir` | `~/.arx/vault/runner-nats-transport` |

### Artifact handling

`--artifact-quarantine-dir`, `--artifact-activation-dir`, `--artifact-cache-dir`,
`--artifact-registry`, `--artifact-registry-username`,
`--artifact-release-policy-envelope`, `--artifact-release-policy-key-id`,
`--artifact-release-policy-public-key`, `--artifact-sigstore-trusted-root`,
`--development-artifact-root`.

Verification is fail closed — see
[strategy toolkit](/toolkit/overview).

### Fact cadence

`--runner-fact-snapshot-interval-secs` (default `10.0`),
`--runner-fact-period-secs` (default `86400`),
`--runner-fact-period-retry-secs` (default `30.0`).

## health

```bash
arx-runner health
arx-runner health --json
```

Accepts `--ready-file`. Exits non-zero for missing, expired, revoked or
mismatched authority. Readiness is not "the process is up" — see
[readiness and health](/operator-guide/readiness-health).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Operation failed — the message names the failing check |
| `2` | Usage error, or the retired `python -m custos` entry point |

## What has no command

There is no command to create, approve or publish a DeploymentSpec, and none to
grant a runner its own authority. Those live in ARX by design — see
[the trust model](/introduction/trust-model).
