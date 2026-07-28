---
title: "Enrollment"
sidebar_position: 2
---

# Enrollment

Enrollment is how a runner acquires an identity it can prove. `arx-runner
enroll` is the only supported path. There is no NATS enrollment, no local
unsigned bootstrap token, no hand-written `runner.toml`, no default tenant and
no plaintext signing-key fallback.

That list is deliberately closed. Every entry on it would be a way to obtain a
runner identity without the authority having issued one.

## Who does what

**ARX** issues and owns the enrollment token, consumes it exactly once, and
owns the resulting machine credential — its expiry, version, rotation,
revocation and the immutable public-key evidence. It also applies identity,
tenant and access policy at the endpoint.

**Custos** generates the Ed25519 keypair, proves possession of the private key,
stores the returned opaque credential encrypted, and fails closed when that
authority is unusable.

The private key is generated locally and never sent. ARX never sees it, which
is what makes the proof meaningful.

## The exchange

1. You obtain a one-time enrollment token from ARX.
2. Custos generates an Ed25519 keypair in memory and a fresh challenge nonce.
3. Custos signs a canonical proof binding the token digest, claimed tenant,
   runner UUID, nonce, machine key id and public-key digest.
4. Custos sends the token, public key, nonce, key id and signature to
   `POST /api/v1/runner-enrollments`. The private key stays on your machine.
5. ARX verifies the token and the proof, consumes the token once, persists the
   public evidence, and issues a tenant-bearing opaque credential with an id,
   version and expiry.
6. Custos encrypts that credential together with the private key using
   sops+age. Only non-secret binding metadata reaches `runner.toml`.

The proof is newline-delimited UTF-8 in exactly this order — field order is
part of the contract, because a canonical form that both sides do not compute
identically produces a signature neither can verify.
<!-- disclosure-ok: exact signing preimage; an auditor cannot verify a signature without the literal domain string -->

```text
crucible.runner.enrollment.pop.v1
tenant_id=<tenant>
runner_id=<uuid>
challenge_nonce=<uuid>
machine_key_id=<ed25519-key-id>
public_key_sha256=<lowercase-sha256>
enrollment_token_sha256=<lowercase-sha256>
```

## What lands on disk

`~/.arx/vault/runner-machine.enc` is a sops+age document holding the opaque
machine credential and the Ed25519 private key together. Mode `0600`; the
parent and the age identity directory `0700`. Decryption at runtime needs
`SOPS_AGE_KEY_FILE`.

`~/.arx/runner.toml` contains no credential and no key. It records only
`tenant_id`, `runner_id`, `backend_url`, `credential_id`,
`credential_version`, `credential_valid_until`, `machine_key_id`,
`machine_vault_path` and `enrolled_at`.

Any mismatch between those fields and the decrypted vault is a startup error,
not a warning. See [configuration](/reference/configuration) for the field
reference.

## Running it

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
```

Plain HTTP is accepted only for loopback development. Redirects are never
followed — redirecting an enrollment token or a machine credential would move
it across the trust boundary the token exists to establish.

## Rotation and revocation

```bash
arx-runner credential rotate
arx-runner credential revoke
```

**Rotate** generates a new keypair and sends the new public key with a
nonce-bound proof signed by the *old* key — continuity of identity is proven
rather than asserted. Custos replaces the encrypted vault and public metadata
atomically, and only after an accepted response. A failed rotation leaves the
previous credential intact and usable.

**Revoke** sends a nonce-bound proof signed by the current key. Once the
authority confirms the revoked state, Custos immediately deletes the encrypted
vault and `runner.toml`. The execution loop cannot start from a revoked
principal, and there is no path to resurrect one locally.

## Startup and readiness

Before connecting transport or constructing an execution host, startup
requires:

- the encrypted machine vault and the age identity;
- an unexpired credential;
- exact tenant, runner, credential id, version, expiry and key-id binding;
- server verification that the credential is still active;
- a validated capability receipt bound to the same public key.

Readiness output repeats only public credential metadata and its expiry.
`arx-runner health` returns non-zero for missing, expired, revoked or
mismatched authority.

One asymmetry is worth stating plainly: an outage does not stop an
already-running engine, but a new process will not start from authority it
cannot verify. Continuity is preserved for what is already trusted; it is never
extended to something unproven.
