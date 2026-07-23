# Custos operations runbook

Custos consumes signed Crucible commands and emits signed RunnerFacts. It does
not own deployment business state, approval workflow, business NATS topology,
or unsigned telemetry publication.

## Logging rules

- Logs are structured JSON.
- Never log API secrets, opaque machine credentials, private keys, enrollment
  tokens, or decrypted vault values.
- Runtime events use `deployment_instance_id`; `spec_id` is provenance only.

## Startup authority failure

Symptoms: `Runner startup authority check failed` and no ready file.

Check:

1. `runner.toml` exists with mode `0600` and contains only public metadata.
2. Its `machine_vault_path` points at the enrolled machine vault.
3. `SOPS_AGE_KEY_FILE` exists with mode `0600` and decrypts that vault.
4. Credential ID, version, expiry, tenant, runner, and machine key match the
   metadata exactly.

Do not hand-edit authority files. Revoke or rotate through Crucible, or enroll
a new machine principal with a new one-time token.

## Venue vault failure

Symptoms: credential decrypt or permission-scope failure before engine deploy.

```bash
arx-runner vault verify --key-id binance-testnet --tenant-id acme
```

Each venue credential must be a separate sops+age document with
`trade_no_withdraw` scope. Replace a bad entry with `arx-runner vault put`; do
not place secrets in argv, runner.toml, or DeploymentSpec.

## Deployment command rejection

Common causes:

- invalid Crucible signature or key ID;
- subject tenant/runner/instance does not match the signed payload;
- canonical DeploymentSpec digest mismatch;
- stale generation or changed strategy identity for an existing instance;
- StrategyRelease snapshot/artifact/manifest binding mismatch;
- typed `execution_config` invalid or unsupported by the selected engine;
- live command missing Crucible promotion evidence;
- live command routed to a non-live engine host.

Correct canonical state in Crucible and let it emit a new signed generation.
Never inject a command directly into NATS.

## NATS or Crucible outage

Readiness clears when the exact runner subscription is unavailable. Custos
continues local safety enforcement and retries subscription with bounded
backoff. It does not silently switch to ARX or an unsigned topic.

Applied observations remain in the signed RunnerFact outbox. Once connectivity
returns, the outbox publisher resumes without changing fact identity or
sequence ownership.

Useful checks:

```bash
arx-runner health
arx-runner health --json | jq .
du -h ~/.arx/state/runner-fact-outbox.db
```

The JSON form is the sole runtime-health V1 projection. It is atomically
refreshed from the existing RunnerFact SQLite database and reports each enabled
transport mode, SQLite quick-check state, database/WAL/disk bytes, command
outcomes and in-progress leases, desired/applied drift, restart/quarantine,
pending RunnerFact/PubAck age and signed runner-policy expiry. It is not a
second journal or business-state store.

Operational thresholds:

- page immediately when `sqlite_quick_check != "ok"`,
  `overdue_in_progress_commands > 0`, or `quarantined_deployments > 0`;
- warn when `oldest_desired_applied_drift_age_seconds > 30` or
  `oldest_pending_fact_age_seconds > 30`, and page at 120 seconds;
- warn when `disk_free_bytes < 2147483648` and stop new risk-increasing work
  below 1073741824 bytes;
- warn when `next_policy_expiry_seconds < 900`; an expired testnet/live policy
  remains fail closed;
- any missing `transport_modes` entry invalidates the document; any false entry
  preserves diagnostics but sets `ready=false`, so one failed mode cannot hide
  behind another.

Preserve the database and its `-wal`/`-shm` siblings before recovery. A failed
SQLite quick check, exhausted disk or stale WAL is an operator recovery event;
do not delete the database to make health green.

## Engine or venue failure

For authentication failures, verify exchange key status, IP allowlists, clock
synchronization, and `trade_no_withdraw` scope. For code-hash failures, deploy
the reviewed strategy bytes matching the Crucible spec. Never bypass engine execution admission
live capability gate.

Fallback breakers, the local notional cap, and the zombie watchdog are keyed by
`deployment_instance_id`. A trip for one instance must not flatten or stop a
different instance.

## Process recovery

1. Inspect `journalctl -u custos -n 200` or container logs.
2. Preserve `.arx/runner.toml`, the machine vault, venue vault, Crucible public
   key, and RunnerFact outbox.
3. Restart the service.
4. Confirm `arx-runner health` succeeds.
5. Inspect `arx-runner health --json` for drift, quarantine, policy expiry and
   pending PubAck age.
6. Confirm Crucible receives the expected lifecycle fact generation.

The runner resumes from enrolled machine authority and Crucible desired state.
No long-term credential is stored in runner.toml, and no local file is the
canonical deployment lifecycle record.

## Canonical events

| Event | Meaning |
|---|---|
| `runner_command_runtime_intake_failed` | Crucible command subscription unavailable |
| `deployment_spec_decode_failed` | Signed event or subject failed verification/parsing |
| `deployment_reconcile_failed` | Local engine apply failed for an instance |
| `deployment_lifecycle_fact_enqueue_failed` | Applied generation was not durably reported |
| `engine_admission_live_capability_denied` | Host cannot execute live safely |
| `nt_stop_noop_unknown_instance` | Idempotent stop for an absent instance |

See [`05-deployment.md`](05-deployment.md) for provisioning and startup.
