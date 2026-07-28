---
title: "Configuration Reference"
sidebar_position: 2
---

# Configuration Reference

Custos keeps its configuration in two places under `~/.arx`, split by
sensitivity:

| Path | Contents | Permissions |
|---|---|---|
| `~/.arx/runner.toml` | Non-secret binding metadata written at enrollment | `0600`, directory `0700` |
| `~/.arx/vault/<key-id>.enc` | Encrypted credentials and the runner signing key | `0600`, directory `0700` |

Nothing secret is stored in `runner.toml`. The opaque machine credential and
the Ed25519 private key live together in the encrypted vault that
`machine_vault_path` points at.

Custos refuses to start if either path is group- or world-accessible.

## `runner.toml`

This file is written by `arx-runner enroll` and should not be edited by hand.
It is documented here so operators can audit what the runner persists.

| Field | Type | Meaning |
|---|---|---|
| `tenant_id` | string | Owning tenant. Non-empty, no whitespace. |
| `runner_id` | UUID | This runner's identity. Must not be the nil UUID. |
| `backend_url` | absolute URL | Control-plane endpoint this runner enrolled against. |
| `credential_id` | UUID | Identifier of the issued machine credential. Must not be nil. |
| `credential_version` | integer ≥ 1 | Generation of the machine credential; increases on rotation. |
| `credential_valid_until` | RFC 3339 timestamp | Expiry of the current credential generation. |
| `machine_key_id` | string | Signing key identifier. Must begin with `ed25519-`. |
| `machine_vault_path` | absolute path | Location of the encrypted machine vault. |
| `enrolled_at` | RFC 3339 timestamp | When enrollment completed. |

Every field is validated on load. A malformed value is a startup failure, not
a warning — a runner that cannot prove its own identity must not reach a
venue.

Specifically: `runner_id` and `credential_id` must parse as UUIDs and must not
be nil; `credential_version` must be a positive integer; both timestamps must be
RFC 3339 **with a timezone**; `backend_url` must have a scheme and a host; and
`machine_vault_path` must be absolute.

The field set must match exactly. A missing key and an unexpected key are the
same class of failure, and the error names both — an extra field means the file
was written by something that does not agree with this runner about what a
runner identity is.

The file is written atomically — to a temporary file in the same directory,
fsynced, chmodded to `0600`, then renamed over the target. A crash mid-write
leaves the previous file intact rather than a half-written one, which matters
because this file is read at every startup.

### Example

```toml
tenant_id = "acme"
runner_id = "6f1c8a30-6a5f-4a1e-9f0f-2a1d0f7a55c1"
backend_url = "https://control.example.com"
credential_id = "b0e4a8f2-9a11-4d3e-8f77-1c2b3d4e5f60"
credential_version = 2
credential_valid_until = "2026-12-31T23:59:59Z"
machine_key_id = "ed25519-7f3a1c"
machine_vault_path = "/home/operator/.arx/vault/runner-machine.enc"
enrolled_at = "2026-07-01T09:14:22Z"
```

## Vault layout

Credentials are stored one encrypted file per key, decrypted in-process with
sops and age. See [credential vault](/operator-guide/credential-vault)
for provisioning, rotation and verification.

```
~/.arx/vault/
├── runner-machine.enc       # machine credential + Ed25519 signing key
└── <venue-key-id>.enc       # one file per venue API credential
```

The age identity used to decrypt these files is supplied through
`SOPS_AGE_KEY_FILE`. It never leaves the host and is never transmitted.

## Command-line options

`arx-runner start` accepts:

| Option | Default | Effect |
|---|---|---|
| `--engine nautilus` | default | Real execution. Sandbox, testnet and live are all available, subject to the [live execution gate](/concepts/live-execution-gate). |
| `--engine sandbox-sim` | — | Simulation host: full local lifecycle, no venue connection. Declares `sandbox` only, so testnet and live deployments are refused. |
| `--runner-fact-outbox <path>` | `~/.arx/state/runner-fact-outbox.db` | SQLite database backing durable fact delivery. |
| `--vault-dir <path>` | `~/.arx/vault` | Directory holding the per-key encrypted credentials. |
| `--ready-file <path>` | `~/.arx/state/runner-ready.json` | Readiness marker consumed by `arx-runner health`. |
| `--runner-capability <path>` | `~/.arx/runner-capability.json` | Validated capability receipt bound to the runner key. |

State that must survive a restart lives under `~/.arx/state/`. It is not a cache:
deleting the outbox database discards facts the runner has accepted
responsibility for delivering.

Enrollment and vault management are separate subcommands:

```bash
arx-runner enroll \
  --token <enrollment-token> \
  --backend https://arx.example.com \
  --tenant-id <tenant> \
  --runner-id <uuid>

arx-runner vault put \
  --key-id <key-id> \
  --tenant-id <tenant> \
  --api-key <api-key> \
  --api-secret-stdin \
  --scope-digest <lowercase-sha256>
arx-runner vault verify --key-id <key-id> --tenant-id <tenant>
arx-runner vault list
arx-runner start --enabled-mode sandbox --engine nautilus
```

## Environment

| Variable | Required | Purpose |
|---|---|---|
| `SOPS_AGE_KEY_FILE` | yes | Path to the age identity that decrypts the vault. |

The running daemon reads no venue credentials from the environment. A key
enters the process only by decrypting a vault file, so neither a process listing
nor an inherited environment can leak one.

Provisioning is the one place an environment variable may carry a secret:
`vault put --api-secret-env` reads from a named variable, as an alternative to
`--api-secret-stdin`. Prefer stdin. Both keep the secret off the command line,
but an environment variable is inherited by whatever that shell starts next.

## Running in Docker

The image expects the two host paths to be mounted and the age identity to be
provided at runtime. Mount `~/.arx` read-write — the runner needs to persist
credential rotations. See [deployment](/operator-guide/deployment) for the full
invocation, and [installation](/getting-started/installation) for building the
image, which is currently the only way to obtain one.
