---
title: "Emergency Playbook"
sidebar_position: 5
---

# Emergency Playbook

What to do when the platform is unreachable, the runner is unhealthy, or you
need to recover a process. For diagnosing a specific failure, see
[troubleshooting](./troubleshooting).

## Losing the connection

Readiness clears when the runner's subscription is unavailable. What does
**not** happen is as important as what does:

- Local safety enforcement keeps running. Exposure limits, the fallback breaker
  and the zombie watchdog do not depend on the platform being reachable.
- The runner retries its subscription with bounded backoff.
- It never silently switches to an unsigned topic or a different authority.

Applied observations stay in the signed fact outbox. When connectivity returns,
the publisher resumes without changing fact identity or sequence ownership —
nothing is lost and nothing is renumbered.

Losing the connection is not a reason to stop trading, and it is not a licence
to trade unguarded. See
[safety survives a disconnect](/trust-model/safety-survives-disconnect).

## Health inspection

```bash
arx-runner health
arx-runner health --json | jq .
du -h ~/.arx/state/runner-fact-outbox.db
```

The JSON form is the single runtime-health projection. It is refreshed
atomically from the fact database and reports each enabled transport mode,
SQLite quick-check state, database/WAL/disk bytes, command outcomes and
in-progress leases, desired-versus-applied drift, restart and quarantine
counts, pending fact and acknowledgement age, signed policy expiry, artifact
cache and activation bytes, and transport authority expiry or revocation.

It is a projection, not a second journal — it never holds business state.

## Alert thresholds

**Page immediately** when any of these hold:

- `sqlite_quick_check != "ok"`
- `overdue_in_progress_commands > 0`
- `quarantined_deployments > 0`
- `invalid_transport_authorities > 0` — local expiry, revocation, or broker
  authorization denial
- `quarantined_artifacts > 0`

**Warn**, and escalate if it persists:

| Signal | Warn at | Page at |
|---|---|---|
| `oldest_desired_applied_drift_age_seconds` | 30 | 120 |
| `oldest_pending_fact_age_seconds` | 30 | 120 |
| `disk_free_bytes` | below 2 GiB | stop new risk-increasing work below 1 GiB |
| `next_policy_expiry_seconds` | below 900 | an expired testnet/live policy stays fail closed |
| `next_transport_expiry_seconds` | below 900 | — |

A missing `transport_modes` entry invalidates the whole document. A false entry
keeps diagnostics available but sets `ready=false`, so one failed mode cannot
hide behind a healthy one.

## Before you recover anything

Preserve the fact database and its `-wal` / `-shm` siblings.

A failed SQLite quick check, exhausted disk or a stale WAL is an operator
recovery event. **Do not delete the database to make health turn green** — that
discards the record of what the runner actually did, which is the one thing
that cannot be reconstructed from upstream.

## Process recovery

1. Inspect `journalctl -u custos -n 200`, or the container logs.
2. Preserve `.arx/runner.toml`, the machine vault, the venue vault, the
   domain-event public key, and the fact outbox.
3. Restart the service.
4. Confirm `arx-runner health` succeeds.
5. Inspect `arx-runner health --json` for drift, quarantine, policy expiry and
   pending acknowledgement age.
6. Confirm the expected lifecycle fact generation was received upstream.

The runner resumes from its enrolled machine authority and the upstream desired
state. No long-term credential lives in `runner.toml`, and no local file is the
canonical record of deployment lifecycle — so a clean restart converges rather
than guessing.

## Stopping execution

To stop trading, change the desired state upstream. That path is signed,
audited and reversible.

If you need to stop **now**, without waiting for the platform, stop the runner
process. Local positions are unaffected by the runner exiting; the venue does
not know or care that a process ended. What you lose is the local safety
enforcement that was watching those positions — so treat process-level stop as
the last resort, not the first.
