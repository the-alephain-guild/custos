---
title: "Credential vault operations"
sidebar_position: 2
---

Custos stores credentials as separate sops+age encrypted files. Machine signing keys and exchange keys remain on the runner host.

| Path | Content | Created by |
|---|---|---|
| `~/.arx/vault/runner-machine.enc` | Machine credential and Ed25519 private key | Enrollment, rotation or standalone identity creation |
| `~/.arx/vault/<key-id>.enc` | One exchange credential | `vault put` |

Use directory mode `0700`, file mode `0600`, and `SOPS_AGE_KEY_FILE` for the age identity. Runtime checks reject or report unsafe permissions according to the credential path; do not rely on a warning to secure the directory.

## Add and verify

Set `KEY_ID`, `TENANT_ID`, `API_KEY` and `SCOPE_DIGEST` to the intended credential binding. Supply the secret through stdin from a secure local source:

```bash
uv run arx-runner vault put \
  --key-id "$KEY_ID" --tenant-id "$TENANT_ID" --api-key "$API_KEY" \
  --api-secret-stdin --scope-digest "$SCOPE_DIGEST" \
  --permission-scope trade_no_withdraw
uv run arx-runner vault verify --key-id "$KEY_ID" --tenant-id "$TENANT_ID"
uv run arx-runner vault list
```

`--age-recipient` defaults to `SOPS_AGE_RECIPIENT`. Add `--vault-dir` consistently for a custom location. Key ids must match `^[a-zA-Z0-9_-]{1,64}$`.

The CLI and runtime use the same JSON decrypt command, including `--input-type json --output-type json`; `.enc` is a filename convention, not a sops format. `vault verify` exercises that path and the local payload/scope checks. It does not verify permissions against the exchange API.

## Scope and lane differences

The only accepted permission declaration is `trade_no_withdraw`. Configure the actual exchange key without withdrawal rights as well; a local declaration cannot change venue-side permissions.

On the signed lane, the stored scope digest must match the scope bound by the deployment. On the offline lane, the spec names `provenance_ref.credential_id`, and the runtime derives a host credential scope from that id. Do not add a `credential_scope` key to an offline spec: its schema rejects it.

Both lanes resolve a real local vault entry, including sandbox simulation. Demo values in the standalone tutorial are non-trading fixtures only.

## Rotation and recovery

Use `credential rotate --reason` for an enrolled machine credential. Replacement is written only after remote acceptance. Standalone identity creation has no remote attestation or rotation endpoint; use a separate local state root when replacing it and retain any state needed for recovery.

To update a venue entry, deliberately provision the replacement with `vault put`, verify it, and coordinate the deployment's scope binding. Never print decrypted payloads into logs or paste them into deployment specs. See [emergency recovery](/operator-guide/emergency-playbook).
