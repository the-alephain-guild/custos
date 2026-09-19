---
title: "CLI reference"
sidebar_position: 1
---

`arx-runner` is the runner's console interface. Use `uv run arx-runner` from the source checkout or `arx-runner` in an activated environment. `python -m custos` is retired. There is no HTTP administration API.

## Command map

| Command | Purpose |
|---|---|
| `enroll` | Obtain an ARX-attested machine identity |
| `credential verify/rotate/revoke` | Manage that enrolled credential |
| `identity standalone` | Create a local unattested identity for offline work |
| `vault put/verify/list` | Store and inspect local exchange credentials |
| `nats bootstrap` | Provision owned offline streams with `--profile standalone` |
| `deployment validate/publish` | Validate or publish `OfflineDeploymentSpec`; sandbox/testnet only |
| `nats-transport enroll/rotate/revoke/resume/verify` | Manage signed-lane transport authority |
| `publish-capability` | Publish a signed capability revision using an enrolled identity |
| `release-policy generate-development-authority/issue` | Create local trust-policy material; development authorities are not production approval |
| `start` | Start the selected lane |
| `health` | Read the local readiness document |

## Starting a runner

Signed deployment consumption requires `--reconcile`, enrolled identity, capability and trust inputs, and at least one `--enabled-mode`. Repeat the latter for additional signed transport sessions. The Nautilus host still allows one active node per event loop.

Offline operation requires `--reconcile-strategy-id`, an existing local identity and `--nats-url`. It reads mode from the spec, uses `--offline-state` for its own database and reports under `--runner-label` (runner UUID by default). It rejects live. `--production-state-root` is for the signed lane and cannot select offline composition.

See [signed sandbox](/getting-started/first-sandbox-run), [standalone sandbox](/getting-started/standalone-sandbox) and [configuration](/reference/configuration).

## Provisioning notes

- `vault put` requires exactly one secret input method. Prefer `--api-secret-stdin`; command-line secrets can appear in process listings/history.
- `--scope-digest` binds signed-lane credential scope. Offline specs reference a local key id and derive their host scope separately.
- `deployment --mode` asserts the spec mode; it does not convert it. `--strategy-dir` binds/checks a directory digest in memory without rewriting the spec file.
- `credential rotate/revoke` require `--reason`. Standalone identities cannot use these remote lifecycle operations.
- Release trust-policy issuance requires the expected authority key and Sigstore identity/root. See [deployment](/operator-guide/deployment).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Command succeeded; `health` predicate passed |
| `1` | Operation failed or health is not ready |
| `2` | Parser usage error or retired entry point |

## Parser reference

The following tables are generated from the actual parser. Defaults are shown with optional environment overrides cleared. Paths use `~` for the current user's home. Runtime checks can impose additional requirements, such as signed-lane `--enabled-mode`; inspect the command-specific help and the guides above.

<!-- generated:cli -->

### credential

### credential verify

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--runner-toml` | optional | `~/.arx/runner.toml` | — |

### credential rotate

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--runner-toml` | optional | `~/.arx/runner.toml` | — |
| `--reason` | required | — | — |
| `--age-recipient` | optional | — | — |

### credential revoke

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--runner-toml` | optional | `~/.arx/runner.toml` | — |
| `--reason` | required | — | — |
| `--authority-path` | optional | `~/.arx/runner-capability.json` | — |
| `--ready-file` | optional | `~/.arx/state/runner-ready.json` | — |

### deployment

### deployment validate

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--spec-file` | required | — | — |
| `--strategy-dir` | optional | — | — |
| `--mode` | optional | — | — |

### deployment publish

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--spec-file` | required | — | — |
| `--tenant-id` | required | — | — |
| `--strategy-id` | required | — | — |
| `--nats-url` | optional | `nats://localhost:4222` | — |
| `--strategy-dir` | optional | — | — |
| `--mode` | optional | — | — |

### enroll

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--token-file` | required | — | — |
| `--backend` | required | — | — |
| `--tenant-id` | required | — | — |
| `--runner-id` | required | — | — |
| `--agent-version` | optional | — | — |
| `--runner-toml` | optional | `~/.arx/runner.toml` | — |
| `--machine-vault` | optional | `~/.arx/vault/runner-machine.enc` | — |
| `--age-recipient` | optional | — | — |

### publish-capability

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--manifest` | required | — | — |
| `--runner-toml` | optional | `~/.arx/runner.toml` | — |
| `--authority-path` | optional | `~/.arx/runner-capability.json` | — |
| `--idempotency-key` | optional | — | — |
| `--capability-version-id` | optional | — | — |
| `--capability-version` | optional | — | — |

### release-policy

### release-policy generate-development-authority

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--private-key-output` | required | — | — |
| `--public-key-output` | required | — | — |
| `--receipt-output` | required | — | — |

### release-policy issue

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--authority-private-key` | required | — | — |
| `--authority-public-key` | required | — | — |
| `--sigstore-trusted-root` | required | — | — |
| `--policy-id` | required | — | — |
| `--version` | required | — | — |
| `--not-before` | required | — | — |
| `--expires-at` | required | — | — |
| `--issuer` | required | — | — |
| `--workflow-identity` | required | — | — |
| `--source-repository` | required | — | — |
| `--envelope-output` | required | — | — |
| `--receipt-output` | required | — | — |
| `--environment-output` | required | — | — |

### health

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--ready-file` | optional | `~/.arx/state/runner-ready.json` | — |
| `--json` | optional | `False` | — |

### identity

### identity standalone

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--tenant-id` | required | — | — |
| `--runner-toml` | optional | `~/.arx/runner.toml` | — |
| `--machine-vault` | optional | `~/.arx/vault/runner-machine.enc` | — |
| `--age-recipient` | optional | — | — |
| `--valid-days` | optional | `365` | — |

### nats

### nats bootstrap

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--profile` | required | — | `standalone` |
| `--nats-url` | optional | `nats://localhost:4222` | — |
| `--tenant-id` | required | — | — |
| `--timeout-secs` | optional | `30.0` | — |

### nats-transport

### nats-transport enroll

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--runner-toml` | optional | `~/.arx/runner.toml` | — |
| `--machine-vault` | optional | — | — |
| `--transport-vault-dir` | optional | `~/.arx/vault/runner-nats-transport` | — |
| `--trading-mode` | required | — | `sandbox`, `testnet`, `live` |
| `--nats-url` | required | — | — |
| `--nats-ca` | optional | `~/.arx/certs/crucible-nats-ca.pem` | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--nats-server-name` | required | — | — |
| `--verification-timeout-secs` | optional | `30.0` | — |
| `--issuer-public-key` | optional | — | — |
| `--crucible-url` | required | — | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--authorization-intent-id` | required | — | — |
| `--operation-timeout-secs` | optional | `300.0` | — |
| `--age-recipient` | optional | — | — |

### nats-transport rotate

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--runner-toml` | optional | `~/.arx/runner.toml` | — |
| `--machine-vault` | optional | — | — |
| `--transport-vault-dir` | optional | `~/.arx/vault/runner-nats-transport` | — |
| `--trading-mode` | required | — | `sandbox`, `testnet`, `live` |
| `--nats-url` | required | — | — |
| `--nats-ca` | optional | `~/.arx/certs/crucible-nats-ca.pem` | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--nats-server-name` | required | — | — |
| `--verification-timeout-secs` | optional | `30.0` | — |
| `--issuer-public-key` | optional | — | — |
| `--crucible-url` | required | — | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--authorization-intent-id` | required | — | — |
| `--operation-timeout-secs` | optional | `300.0` | — |
| `--age-recipient` | optional | — | — |

### nats-transport revoke

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--runner-toml` | optional | `~/.arx/runner.toml` | — |
| `--machine-vault` | optional | — | — |
| `--transport-vault-dir` | optional | `~/.arx/vault/runner-nats-transport` | — |
| `--trading-mode` | required | — | `sandbox`, `testnet`, `live` |
| `--nats-url` | required | — | — |
| `--nats-ca` | optional | `~/.arx/certs/crucible-nats-ca.pem` | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--nats-server-name` | required | — | — |
| `--verification-timeout-secs` | optional | `30.0` | — |
| `--issuer-public-key` | optional | — | — |
| `--crucible-url` | required | — | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--authorization-intent-id` | required | — | — |
| `--operation-timeout-secs` | optional | `300.0` | — |
| `--age-recipient` | optional | — | — |

### nats-transport resume

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--runner-toml` | optional | `~/.arx/runner.toml` | — |
| `--machine-vault` | optional | — | — |
| `--transport-vault-dir` | optional | `~/.arx/vault/runner-nats-transport` | — |
| `--trading-mode` | required | — | `sandbox`, `testnet`, `live` |
| `--nats-url` | required | — | — |
| `--nats-ca` | optional | `~/.arx/certs/crucible-nats-ca.pem` | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--nats-server-name` | required | — | — |
| `--verification-timeout-secs` | optional | `30.0` | — |
| `--issuer-public-key` | optional | — | — |
| `--crucible-url` | required | — | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--operation-timeout-secs` | optional | `300.0` | — |
| `--age-recipient` | optional | — | — |

### nats-transport verify

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--runner-toml` | optional | `~/.arx/runner.toml` | — |
| `--machine-vault` | optional | — | — |
| `--transport-vault-dir` | optional | `~/.arx/vault/runner-nats-transport` | — |
| `--trading-mode` | required | — | `sandbox`, `testnet`, `live` |
| `--nats-url` | required | — | — |
| `--nats-ca` | optional | `~/.arx/certs/crucible-nats-ca.pem` | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--nats-server-name` | required | — | — |
| `--verification-timeout-secs` | optional | `30.0` | — |
| `--issuer-public-key` | optional | — | — |

### start

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--runner-toml` | optional | `~/.arx/runner.toml` | — |
| `--machine-vault` | optional | — | — |
| `--nats-transport-vault-dir` | optional | `~/.arx/vault/runner-nats-transport` | — |
| `--enabled-mode` | optional | — | `sandbox`, `testnet`, `live`; repeatable |
| `--development-local-nats-url` | optional | — | — |
| `--nats-sim-url` | optional | — | — |
| `--nats-sim-ca` | optional | `~/.arx/certs/crucible-nats-ca.pem` | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--nats-sim-server-name` | optional | — | — |
| `--nats-sim-issuer-public-key` | optional | — | — |
| `--nats-live-url` | optional | — | — |
| `--nats-live-ca` | optional | `~/.arx/certs/crucible-nats-ca.pem` | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--nats-live-server-name` | optional | — | — |
| `--nats-live-issuer-public-key` | optional | — | — |
| `--vault-dir` | optional | `~/.arx/vault` | — |
| `--reconcile` | optional | `False` | — |
| `--reconcile-strategy-id` | optional | — | — |
| `--runner-label` | optional | — | — |
| `--nats-url` | optional | `nats://localhost:4222` | — |
| `--offline-state` | optional | `~/.arx/state/offline-lane.db` | — |
| `--crucible-domain-public-key` | optional | `~/.arx/crucible-domain-event.pub` | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--crucible-domain-key-id` | optional | — | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--engine` | optional | `nautilus` | `nautilus`, `sandbox-sim` |
| `--ready-file` | optional | `~/.arx/state/runner-ready.json` | — |
| `--runner-capability` | optional | `~/.arx/runner-capability.json` | — |
| `--runner-fact-outbox` | optional | `~/.arx/state/runner-fact-outbox.db` | — |
| `--development-artifact-root` | optional | `~/.alephain/v1-team/strategy-artifacts` | — |
| `--artifact-quarantine-dir` | optional | `~/.arx/state/artifact-quarantine` | — |
| `--artifact-activation-dir` | optional | `~/.arx/state/artifact-activations` | — |
| `--artifact-cache-dir` | optional | `~/.arx/state/artifact-cache` | — |
| `--artifact-registry` | optional | `ghcr.io` | — |
| `--artifact-registry-username` | optional | — | — |
| `--artifact-release-policy-envelope` | optional | — | — |
| `--artifact-release-policy-key-id` | optional | — | — |
| `--artifact-release-policy-public-key` | optional | — | — |
| `--artifact-sigstore-trusted-root` | optional | — | — |
| `--runner-fact-snapshot-interval-secs` | optional | `10.0` | — |
| `--runner-fact-period-secs` | optional | `86400` | — |
| `--runner-fact-period-retry-secs` | optional | `30.0` | — |
| `--production-state-root` | optional | — | — |

### vault

### vault put

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--key-id` | required | — | — |
| `--tenant-id` | required | — | — |
| `--api-key` | required | — | — |
| `--scope-digest` | required | — | — |
| `--api-secret-stdin` | one in group | `False` | — |
| `--api-secret-env` | one in group | — | — |
| `--api-secret` | one in group | — | — |
| `--age-recipient` | optional | — | — |
| `--permission-scope` | optional | `trade_no_withdraw` | `trade_no_withdraw` |
| `--vault-dir` | optional | `~/.arx/vault` | — |

### vault verify

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--key-id` | required | — | — |
| `--tenant-id` | required | — | — |
| `--vault-dir` | optional | `~/.arx/vault` | — |
| `--age-key-file` | optional | — | — |

### vault list

| Option | Requirement | Default | Choices / repetition |
|---|---|---|---|
| `--vault-dir` | optional | `~/.arx/vault` | — |

<!-- /generated:cli -->
