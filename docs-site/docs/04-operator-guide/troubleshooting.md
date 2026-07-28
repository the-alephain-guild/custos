---
title: "Troubleshooting"
sidebar_position: 6
---

# Troubleshooting

Symptom-driven diagnosis for a runner that will not start, will not accept a
deployment, or cannot reach a venue. For connectivity loss and recovery, see
the [emergency playbook](./emergency-playbook).

## Reading the logs

Logs are structured JSON. Runtime events are keyed by
`deployment_instance_id`; `spec_id` appears as provenance only and is not a
runtime handle.

Custos never logs API secrets, opaque machine credentials, private keys,
enrollment tokens or decrypted vault values. If you believe you have found any
of these in a log line, treat it as a security finding — see
[SECURITY.md](https://github.com/the-alephain-guild/custos/blob/main/SECURITY.md).

## Startup authority failure

**Symptom**: `Runner startup authority check failed`, and no ready file
appears.

Check, in order:

1. `runner.toml` exists, has mode `0600`, and contains only public metadata.
2. Its `machine_vault_path` points at the enrolled machine vault.
3. `SOPS_AGE_KEY_FILE` exists with mode `0600` and actually decrypts that
   vault.
4. Credential ID, version, expiry, tenant, runner and machine key all match the
   metadata exactly.

Do not hand-edit authority files. Revoke or rotate upstream, or enroll a new
machine principal with a new one-time token. A runner whose identity has been
edited by hand cannot prove anything, which is the whole point of the check.

## Venue credential failure

**Symptom**: credential decrypt or permission-scope failure before the engine
deploys.

```bash
arx-runner vault verify --key-id binance-testnet --tenant-id acme
```

Each venue credential must be its own sops+age document scoped
`trade_no_withdraw`. Replace a bad entry with `arx-runner vault put`. Never put
a secret in argv, in `runner.toml`, or in the deployment itself.

## Deployment command rejected

Common causes:

- invalid signature or key ID;
- subject tenant, runner or instance does not match the signed payload;
- canonical digest mismatch;
- stale generation, or changed strategy identity for an existing instance;
- strategy release snapshot, artifact or manifest binding mismatch;
- typed `execution_config` invalid or unsupported by the selected engine;
- a live command missing its promotion evidence;
- a live command routed to an engine that does not support live.

The fix is always upstream: correct the canonical state and let a new signed
generation be emitted. Never inject a command directly into the transport —
Custos would reject it anyway, and an accepted one would be unverifiable.

## Engine or venue failure

For authentication failures, check exchange key status, IP allowlists, clock
synchronization, and that the credential really is `trade_no_withdraw`.

For code-hash failures, deploy the reviewed strategy bytes matching the signed
deployment. Do not attempt to bypass the
[live execution gate](/concepts/live-execution-gate) — it is refusing
because the thing it checks does not match.

Fallback breakers, the local notional cap and the zombie watchdog are all keyed
by `deployment_instance_id`. A trip on one instance must never flatten or stop
a different one; if you observe that, it is a bug worth reporting.

## Event reference

| Event | Meaning |
|---|---|
| `runner_command_runtime_intake_failed` | Command subscription unavailable |
| `deployment_spec_decode_failed` | Signed event or subject failed verification or parsing |
| `deployment_reconcile_failed` | Local engine apply failed for an instance |
| `deployment_lifecycle_fact_enqueue_failed` | Applied generation was not durably reported |
| `engine_admission_live_capability_denied` | Host cannot execute live safely |
| `nt_stop_noop_unknown_instance` | Idempotent stop for an absent instance |
