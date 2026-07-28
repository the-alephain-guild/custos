---
title: "Keys Never Leave the Host"
sidebar_position: 2
---

# Keys Never Leave the Host

Your exchange credentials are encrypted on your machine, decrypted inside the
runner process, and used to sign venue requests. The key that decrypts them is
also on your machine and is never transmitted.

This is why Custos is open source. You are being asked to put API keys on a
daemon; the only honest answer to "why should I trust it" is that you can read
what it does with them.

## The boundary

The guarantee is about **I/O**, not about memory.

A real venue client must hold the key in process memory to sign requests —
that is unavoidable for any client that talks to an exchange, and pretending
otherwise would be theatre. What the guarantee covers is every way the key
could leave: logs, published messages, HTTP bodies, process arguments,
environment variables inherited by a child, and anything written to disk in
plaintext.

## What holds it

### Storage

```
~/.arx/vault/
├── runner-machine.enc       # Ed25519 signing key + opaque machine credential
└── <key-id>.enc             # one file per venue credential
```

One credential per file, each a separate sops+age document. The age identity
that decrypts them is found through `SOPS_AGE_KEY_FILE` and never leaves the
host. Directory mode is `0700`, files `0600`; the runner warns on anything
looser.

`runner.toml` alongside them holds only public binding metadata — credential
id, version, expiry, key id and a vault reference. No plaintext.

### Writing

`arx-runner vault put` passes the secret to `sops` on **stdin**, never as an
argument. A secret in argv would be visible in the shell history and in any
process listing on the machine, including to other users.

Implementation: `src/custos/cli/subcommands/vault.py`. <!-- disclosure-ok: auditable source location -->

### Reading

`PerKeyVault` in `src/custos/core/per_key_vault.py` shells out to sops with
explicit `--input-type json --output-type json`. The decrypt argv is built in
one place, `sops_json_decrypt_command()`, so the CLI and the runtime cannot
drift into different invocations.
<!-- disclosure-ok: auditable source location -->

Both vault classes inherit `_BaseVault`, which enforces two invariants on every
read:

- `_verify_permission_scope` rejects any credential not scoped
  `trade_no_withdraw`;
- `_emit_decrypt_audit` emits a `CredentialDecrypted` event carrying the
  credential id only.

The machine identity lives in `MachineCredentialVault`
(`src/custos/core/machine_credential_vault.py`), which encrypts the Ed25519
private key and the opaque machine credential together in one file. Enrollment
and rotation are the only write paths.
<!-- disclosure-ok: auditable source location -->

## One identity, one place

Enrollment proof, transport authentication and fact signing all use the same
signing key from that same encrypted file.

There is deliberately no second plaintext key file anywhere. A second copy is a
second thing to leak, and a second thing to forget when rotating.

## Verifying it

```bash
# no credential material in log calls
grep -rnE 'log\.(info|debug|warning).*api[_-]?key' src/ tests/

# no credential material in outbound calls
grep -rnE 'publish.*password|send.*secret' src/
```

Both return nothing on a clean tree.

The test worth reading is `tests/test_credential_lifecycle.py`. It builds the
engine object graph the way a real deployment does, then walks it and asserts
no credential is reachable — which catches the case where a credential is not
logged but is quietly retained somewhere it could later be serialised.
<!-- disclosure-ok: auditable source location -->

`tests/test_per_key_vault.py` covers the decrypt path and the scope invariant.
<!-- disclosure-ok: auditable source location -->

See the [audit checklist](./audit-checklist) for the full procedure.

## What this does not cover

Custos cannot protect you from a credential that has withdraw permission in the
first place — it refuses to store one, but if you grant that permission
elsewhere, that is outside the runner. Nor can it help if the host itself is
readable by people you do not trust: the encrypted files and the age identity
are both on that machine, and `0600` only means something if the account is
yours.
