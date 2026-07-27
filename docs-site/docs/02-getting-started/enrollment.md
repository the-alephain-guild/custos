---
title: "Enrollment"
sidebar_position: 2
---


# Enrollment

`arx-runner enroll` is the only supported path for creating a runner machine
principal. There is no NATS enrollment path, local unsigned bootstrap token,
manual `runner.toml`, default tenant, or plaintext RunnerFact key fallback.

## Ownership

- the control plane owns enrollment token state, one-time consumption, Runner
  machine credentials, expiry, version, rotation, revocation, immutable public
  key evidence, and health projections.
- ARX exposes the public typed URL and applies identity/tenant/RBAC policy. It
  does not persist or reconstruct Runner business state.
- Custos generates and retains the Ed25519 private key, performs proof of
  possession, stores the returned opaque machine credential, and fails closed
  when the authority is unusable.

## Enrollment v2

1. An operator obtains a one-time enrollment token from the authorized control
   plane.
2. Custos generates an Ed25519 keypair in memory and a fresh challenge nonce.
3. Custos signs the canonical `arx.runner.enrollment.pop.v1` proof. The proof
   binds the token digest, claimed tenant, Runner UUID, nonce, machine key ID,
   and public-key digest.
4. Custos sends the one-time token, public key, nonce, key ID, and signature to
   ARX `POST /api/v1/enrollments`. The private key is never sent.
5. The control plane verifies the token authority and proof, consumes the token once,
   persists immutable public evidence, and issues a tenant-bearing opaque
   `rkc1` credential with `credential_id`, version, and expiry.
6. Custos encrypts the credential and private key together with sops+age. Only
   non-secret binding metadata is written to `runner.toml`.

The canonical proof is newline-delimited UTF-8 in this exact order:

```text
arx.runner.enrollment.pop.v1
tenant_id=<tenant>
runner_id=<uuid>
challenge_nonce=<uuid>
machine_key_id=<ed25519-key-id>
public_key_sha256=<lowercase-sha256>
enrollment_token_sha256=<lowercase-sha256>
```

## Local authority files

`~/.arx/vault/runner-machine.enc` is a sops+age JSON document containing the
opaque machine credential and Ed25519 private key. It must be mode `0600`; the
parent directory and age identity directory must be `0700`. Runtime decryption
requires `SOPS_AGE_KEY_FILE`.

`~/.arx/runner.toml` contains no credential or private key. It records only:

- `tenant_id`
- `runner_id`
- `backend_url`
- `credential_id`
- `credential_version`
- `credential_valid_until`
- `machine_key_id`
- `machine_vault_path`
- `enrolled_at`

Any mismatch between these fields and the decrypted vault is a startup error.

## Operator flow

```bash
mkdir -p "$HOME/.arx/vault" "$HOME/.arx/state"
chmod 700 "$HOME/.arx" "$HOME/.arx/vault" "$HOME/.arx/state"
age-keygen -o "$HOME/.arx/age.key"
chmod 600 "$HOME/.arx/age.key"

export SOPS_AGE_KEY_FILE="$HOME/.arx/age.key"
export SOPS_AGE_RECIPIENT='age1...'

arx-runner enroll \
  --token '<one-time-token>' \
  --backend https://arx.internal:8000 \
  --tenant-id acme \
  --runner-id 018f8b5f-6f7d-7e23-8c31-bd34ab9d0d41

arx-runner credential verify
arx-runner onboard --manifest runner-capability.json
arx-runner start --nats-url nats://arx.internal:4222
```

HTTP is accepted only for loopback development. Redirects are not followed,
because redirecting an enrollment token or machine credential would cross the
intended trust boundary.

## Rotation and revocation

`arx-runner credential rotate` generates a new keypair and sends the new public
key with a nonce-bound proof signed by the old key. The authority returns a new
opaque credential, incremented version, expiry, and new key binding. Custos
atomically replaces the encrypted vault and public metadata only after an
accepted response.

`arx-runner credential revoke` sends a nonce-bound proof signed by the current
key. After the authority confirms `state=revoked`, Custos immediately deletes
the encrypted machine vault and `runner.toml`; the execution loop cannot be
started with the revoked principal.

## Startup and readiness

Before connecting NATS or constructing the execution host, startup requires:

- the encrypted machine vault and age identity;
- an unexpired `rkc1` credential;
- exact tenant, Runner, credential ID/version/expiry, and key-ID binding;
- server verification that the credential remains active;
- a validated Runner capability receipt bound to the same public key.

The readiness file repeats only public credential metadata and its expiry.
`arx-runner health` returns non-zero for missing, expired, revoked, or mismatched
authority. A cloud outage does not stop an already-running local engine, but a
new process does not start from unverifiable authority.

## Migration order

The control plane control migration `0024` must land and be populated before ARX
migration `0067` removes the source tables. The sequence is: target migration,
semantic lift and retirement permit, then source drop. Never run `0067` first.
