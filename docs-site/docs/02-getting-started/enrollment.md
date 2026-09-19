---
title: "Enrollment"
sidebar_position: 2
---

The signed lane requires an identity enrolled with ARX. For local sandbox/testnet work without ARX, use [standalone identity](/getting-started/standalone-sandbox); that identity cannot start the signed lane.

## Prerequisites

Obtain a one-time enrollment token, tenant id, runner UUID and backend URL from ARX. Install `sops` and `age`. Use a separate state directory for each runner.

```bash
umask 077
mkdir -p "$HOME/.arx/vault" "$HOME/.arx/state"
chmod 700 "$HOME/.arx" "$HOME/.arx/vault" "$HOME/.arx/state"
# Generate only if this runner has no age identity yet.
age-keygen -o "$HOME/.arx/age.key"
export SOPS_AGE_KEY_FILE="$HOME/.arx/age.key"
export SOPS_AGE_RECIPIENT="$(age-keygen -y "$SOPS_AGE_KEY_FILE")"
```

Save the token in a mode-`0600` file and set `ENROLLMENT_TOKEN_FILE`, `ARX_BACKEND_URL`, `TENANT_ID` and `RUNNER_ID` to the issued values. Do not put the token in a command argument.

```bash
uv run arx-runner enroll \
  --token-file "$ENROLLMENT_TOKEN_FILE" \
  --backend "$ARX_BACKEND_URL" \
  --tenant-id "$TENANT_ID" \
  --runner-id "$RUNNER_ID"
uv run arx-runner credential verify
```

Remove the consumed token file after enrollment succeeds. Plain HTTP is accepted only for loopback development; redirects are not followed.

## Proof and local files

Custos generates an Ed25519 keypair locally. It signs a proof binding the token digest, tenant, runner UUID, nonce, key id and public-key digest, then sends only public material and the proof to `POST /api/v1/runner-enrollments`.

The signing preimage is newline-delimited UTF-8 in this order:

```text
crucible.runner.enrollment.pop.v1
tenant_id=<tenant>
runner_id=<uuid>
challenge_nonce=<uuid>
machine_key_id=<ed25519-key-id>
public_key_sha256=<lowercase-sha256>
enrollment_token_sha256=<lowercase-sha256>
```
<!-- disclosure-ok: exact enrollment signing domain required for verification -->

| File | Content |
|---|---|
| `~/.arx/runner.toml` | Public identity, backend, credential expiry/version and vault path |
| `~/.arx/vault/runner-machine.enc` | Encrypted machine credential and private signing key |
| `~/.arx/age.key` | Local age identity needed to decrypt the vault |

Files use mode `0600` and private directories `0700`. Startup compares metadata and decrypted identity, checks expiry, and on the signed lane verifies authority with ARX. It also requires a capability receipt bound to the same public key. See [configuration](/reference/configuration).

## Rotate or revoke

```bash
uv run arx-runner credential rotate --reason "scheduled rotation"
uv run arx-runner credential revoke --reason "host decommissioned"
```

Rotation proves continuity with the old key and writes the replacement only after the authority accepts it. Revocation removes the local machine vault and metadata after confirmation. These operations require an enrolled identity; a standalone identity has no remote credential lifecycle.

Enrollment alone does not provision transport or authorize a deployment. Continue with [signed sandbox](/getting-started/first-sandbox-run).
