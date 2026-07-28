---
title: "Credential Vault Operations"
sidebar_position: 2
---

# Credential Vault Operations

Custos decrypts venue credentials inside its own process, on your machine. The
encryption key never leaves the host, and no part of ARX ever holds your
plaintext API keys. This page covers how to put credentials in, verify them,
rotate them, and what the runner guarantees while it holds them.

## Layout

```
~/.arx/vault/
├── runner-machine.enc       # runner signing key + machine credential
└── <key-id>.enc             # one file per venue credential
```

One credential per file. Each file is encrypted with sops and age; the age
identity that decrypts them is located through `SOPS_AGE_KEY_FILE` and stays on
the host. Directory mode is `0700`, files are `0600`, and the runner warns on
startup if it finds anything looser.

## Adding a credential

```bash
arx-runner vault put <key-id> --permission-scope trade_no_withdraw
```

The secret is passed to sops on stdin, never as a command-line argument, so it
does not appear in your shell history or in a process listing.

`--permission-scope` accepts exactly one value today, `trade_no_withdraw`, and
that is also the default. The runner refuses any credential declaring a wider
scope — see [permission scope](#permission-scope) below.

## Verifying a credential

```bash
arx-runner vault verify <key-id>
```

This runs the real decrypt path end to end: it decrypts through sops, parses
the payload, checks the file mode, and re-checks the permission scope. It is
the acceptance surface an operator should use before a deployment — invoking
`sops` by hand tests a different code path and does not prove the runner can
read the credential.

```bash
arx-runner vault list
```

Lists the key IDs present and warns on stderr about any file whose mode is
group- or world-readable.

## Permission scope

`trade_no_withdraw` is the only scope Custos accepts.

The check runs twice: once when you write the credential, and again on every
decrypt at runtime. Two enforcement points rather than one, because a single
point is one mistake away from being bypassed. A credential that can withdraw
is refused outright — the guarantee is enforced at the key's own permissions,
not by trusting the runner to never call a withdraw endpoint.

Widening this set would change a public CLI contract and a cross-system
permission boundary. It requires a minor version and a coordinated update on
the ARX side; it cannot be done by editing an encrypted payload.

## What the runner guarantees

- **The decryption key never leaves the host.** The age identity is read from
  local disk. It is never transmitted, never logged, and never included in any
  message the runner publishes.
- **Plaintext never reaches a log, a message, or an HTTP body.** Credentials
  exist in process memory long enough to construct a venue client and sign
  requests, which is unavoidable. The guarantee is about the I/O boundary.
- **Every decrypt is audited.** A `CredentialDecrypted` event records that a
  credential was used, by ID only. The plaintext is never part of the record.
- **Startup fails closed.** A missing, expired or revoked machine credential
  stops the runner rather than degrading it. There is no unsigned bootstrap
  path, and no sandbox exception.
- **One identity, one place.** Enrollment proof, transport authentication and
  fact signing all use the same signing key from the same encrypted file.
  There is no second plaintext key file anywhere.

## Deployment references

A deployment names the credential it needs by ID. That ID resolves to a file
name under the vault directory, so it must match `^[a-zA-Z0-9_-]{1,64}$`. The
restriction is what stops a deployment received over the network from escaping
the vault directory through path separators, dots or control characters.

## Rotation and upgrade

Rotation writes a new credential generation, persists it before the network is
touched, and only then promotes it. If the promotion does not complete, the
previous generation stays authoritative — there is no window where the runner
holds no usable credential.

Upgrading from 0.1.x, where several credentials shared one encrypted JSON file,
is manual and deliberate: decrypt the old file, then `vault put` each
credential individually. There is no automatic migration and no fallback read
path for the old layout. The single-file model had a write race between
concurrent updates; per-key files remove it.

## Roadmap

A Hashicorp Vault provider is planned for team deployments — the Vault token
would still live only on the runner. Hardware-backed signing is the longer-term
direction, moving the encryption key behind a boundary it cannot be exported
from at all.
