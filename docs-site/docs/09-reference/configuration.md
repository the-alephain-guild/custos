---
title: "Configuration reference"
sidebar_position: 2
---

Each runner needs a separate identity and state directory. The default root is `~/.arx`; explicit CLI flags can change individual paths.

## Identity metadata

`runner.toml` is written by `enroll` or `identity standalone`. Its exact field set is:

| Field | Meaning / validation |
|---|---|
| `tenant_id` | Non-empty tenant without whitespace |
| `runner_id` | Non-nil UUID |
| `backend_url` | Absolute backend URL; standalone uses `http://standalone.invalid` |
| `credential_id` | Non-nil credential UUID |
| `credential_version` | Positive integer |
| `credential_valid_until` | Timestamp with timezone |
| `machine_key_id` | Ed25519 identifier beginning `ed25519-` |
| `machine_vault_path` | Absolute encrypted machine-vault path |
| `enrolled_at` | Timestamp with timezone |

Missing/unknown fields fail loading. The record is public metadata; the opaque credential and private key are encrypted in the referenced vault. Do not hand-edit bindings to switch identities. Signed commands reject a standalone `.invalid` backend identity.

## State and artifact paths

| Option | Default / purpose |
|---|---|
| `--runner-toml` | `~/.arx/runner.toml` |
| `--machine-vault` | Optional override; must match the metadata binding |
| `--vault-dir` | `~/.arx/vault` |
| `--ready-file` | `~/.arx/state/runner-ready.json` |
| `--runner-capability` | `~/.arx/runner-capability.json`; signed lane |
| `--runner-fact-outbox` | `~/.arx/state/runner-fact-outbox.db`; signed durable state/facts |
| `--offline-state` | `~/.arx/state/offline-lane.db`; offline applied generations |
| `--production-state-root` | Optional persistent root for mutable signed-lane paths; cannot select offline |

Artifact cache, quarantine, activation, development-source and transport-vault paths have separate flags in the generated [CLI reference](/reference/cli). A custom production root must be a real directory without group/world write access; bound state paths must remain inside it.

Files holding identity or credentials use `0600`; private directories use `0700`. Keep database and vault storage persistent in containers. `runner-ready.json` is a derived health document, not a replacement for the databases.

## Environment inputs

| Variable | Purpose |
|---|---|
| `SOPS_AGE_KEY_FILE` | Age identity used to decrypt local vaults |
| `SOPS_AGE_RECIPIENT` | Default public recipient during provisioning |
| `CUSTOS_ARTIFACT_RELEASE_POLICY_ENVELOPE` | Signed release policy file |
| `CUSTOS_ARTIFACT_RELEASE_POLICY_PUBLIC_KEY` / `CUSTOS_ARTIFACT_RELEASE_POLICY_KEY_ID` | Policy authority binding |
| `CUSTOS_ARTIFACT_SIGSTORE_TRUSTED_ROOT` | Trusted Sigstore root |
| `CUSTOS_ARTIFACT_REGISTRY` / `CUSTOS_ARTIFACT_CACHE_DIR` | Registry and cache defaults |
| `CUSTOS_ARTIFACT_REGISTRY_USERNAME` / `CUSTOS_ARTIFACT_REGISTRY_TOKEN` | Private registry authentication, provided together |
| `CUSTOS_DEVELOPMENT_ARTIFACT_ROOT` | Explicit sandbox development-source location |
| `CUSTOS_DEVELOPMENT_LOCAL_NATS_URL` | Explicit loopback sandbox transport exception for the signed development path |
| `CUSTOS_VENUE_PROXY_URL` | Forward proxy for all venue traffic; see below |

### Venue proxy

Where a venue cannot be reached directly, set `CUSTOS_VENUE_PROXY_URL` to an `http://` or `https://` forward proxy, for example `http://proxy.internal:3128`. Market data, order execution and the independent account ledger then all connect through it. SOCKS proxies are refused at startup.

- The address may include credentials. Pass it in the environment, not on the command line. Logs show only its scheme, host and port.
- General variables such as `HTTPS_PROXY` do not affect venue traffic.
- Binance routes through the proxy. OKX and SoDEX do not yet; while a proxy is configured, a deployment on them is refused instead of connecting directly.

Venue secrets are decrypted from the vault rather than inherited as runtime environment credentials. `vault put --api-secret-env` is a provisioning input; prefer stdin. See [deployment](/operator-guide/deployment) for policy provisioning and [trading modes](/concepts/trading-modes) for lane selection.
