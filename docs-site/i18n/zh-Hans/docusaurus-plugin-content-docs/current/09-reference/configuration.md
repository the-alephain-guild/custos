---
title: "Configuration Reference"
sidebar_position: 2
---

# Configuration Reference

:::warning 中文翻译尚未完成
本章暂时显示英文原文。
:::

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
| `--runner-fact-outbox <path>` | `~/.arx/state/runner-fact-outbox.db` | 支撑持久事实投递的 SQLite 数据库 |
| `--vault-dir <path>` | `~/.arx/vault` | 存放 per-key 加密凭据的目录 |
| `--ready-file <path>` | `~/.arx/state/runner-ready.json` | 供 `arx-runner health` 读取的就绪标记 |
| `--runner-capability <path>` | `~/.arx/runner-capability.json` | 绑定到 runner 密钥的已验证能力回执 |

需要跨重启存活的状态位于 `~/.arx/state/`。它**不是缓存**：删掉 outbox 数据库等于丢弃
runner 已经承诺投递的事实。

Enrollment and vault management are separate subcommands:

```bash
arx-runner enroll --token <enrollment-token>
arx-runner vault put <key-id>
arx-runner vault verify <key-id>
arx-runner vault list
arx-runner start --engine nautilus
```

## Environment

| Variable | Required | Purpose |
|---|---|---|
| `SOPS_AGE_KEY_FILE` | yes | Path to the age identity that decrypts the vault. |

Custos deliberately reads no venue credentials from the environment. Keys
enter the process only by decrypting a vault file, so that a process listing
or an inherited environment cannot leak them.

## Running in Docker

The published image expects the two host paths to be mounted, and the age
identity to be provided at runtime. Mount `~/.arx` read-write — the runner
needs to persist credential rotations. See
[deployment](/operator-guide/deployment) for the full invocation.
