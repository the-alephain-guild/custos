---
title: "Keys remain on the host"
sidebar_position: 2
---

Exchange secrets, the machine signing key and the age identity are stored on the runner host. They are not sent to ARX in telemetry or written to logs. Local venue clients use the credentials for the exchange's authentication protocol.

## Storage and decryption

Machine material lives in `runner-machine.enc`; venue credentials use one `<key-id>.enc` file each. `runner.toml` contains public binding metadata. Files use `0600` and private directories use `0700`.

Custos invokes local sops for encryption/decryption, using stdin for secret input and explicit JSON formats for `.enc` files. Decrypted material is used by the local runner/venue client. This is a host boundary: a functioning client must hold secret material in memory while authenticating.

`enroll`, credential rotation and `identity standalone` can write machine material. Standalone identity has no remote attestation and cannot authorize signed-lane operation.

## Permission scope

The vault accepts the declaration `trade_no_withdraw` at write/decrypt boundaries. The operator must also disable withdrawal on the actual venue key. Local validation does not query the exchange to prove that permission setting.

Use `vault verify` to test the runner's real decrypt and local validation path. See [vault operations](/operator-guide/credential-vault).

## Inspection

`tests/test_credential_lifecycle.py` checks credential exposure through logging/object paths, and `tests/test_per_key_vault.py` covers local vault behavior. Source searches can help locate egress and logging code but are not a complete security proof.

Protect host access and backups: possession of both the encrypted vault and its age identity permits decryption. File mode checks do not protect against compromise of the owning account.
