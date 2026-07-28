---
title: "Readiness & Health Probes"
sidebar_position: 3
---

# Readiness & Health Probes

```bash
arx-runner health          # exit 0 when ready
arx-runner health --json   # the exact state document
```

Readiness is not "the process is up". The daemon can be running, connected, and
still not ready — and in that state it is correct for your orchestrator to keep
it out of service.

## The probe

`arx-runner health` reads a state file, evaluates the readiness predicate, and
exits `0` or `1`. It performs no network calls, so it is cheap enough to run on
a tight interval and cannot itself fail because an upstream is slow.

| Situation | Exit | `--json` output |
|---|---|---|
| Ready | `0` | the full state document |
| Not ready | `1` | the full state document |
| State file missing | `1` | `{"ready": false, "path": "…"}` |

The file lives at `~/.arx/state/runner-ready.json` by default; `--ready-file`
overrides it. It is mode `0600` in a `0700` directory and written atomically —
to a uniquely named temporary file, fsynced, then renamed — so a probe never
reads a half-written document.

## What "ready" actually asserts

Eight conditions must hold simultaneously:

1. the runner marked itself ready;
2. transport is connected;
3. the credential state is `active`;
4. the credential binding is valid;
5. the credential has not expired;
6. every enabled transport mode is up;
7. the local database passes a SQLite `quick_check`;
8. there are zero invalid transport authorities.

Two of these are worth dwelling on. Condition 7 means a corrupted local store
makes the runner unready rather than letting it keep accepting work it may not
be able to record. Condition 8 means a transport authority that failed
verification takes the runner out of service instead of being skipped.

Readiness is also refused outright for an expired machine credential — and in
that case the state file is **deleted** rather than rewritten as not-ready. An
absent file and an unready file both fail the probe, so nothing is lost by
removing it, and nothing stale is left behind claiming an identity that has
lapsed.

## The state document

```json
{
  "ready": true,
  "tenant_id": "acme",
  "runner_id": "018f8b5f-…",
  "credential_id": "b0e4a8f2-…",
  "credential_version": 2,
  "credential_valid_until": "2026-12-31T23:59:59Z",
  "machine_key_id": "ed25519-7f3a1c",
  "credential_state": "active",
  "credential_binding_valid": true,
  "strategy_id": null,
  "nats_connected": true,
  "deployment_subscription": true,
  "transport_modes": {"sandbox": true},
  "runtime_metrics": { … }
}
```

Everything here is public metadata. No credential, no key material, and no
strategy parameters — the file is safe to mount, scrape and log.

## Runtime metrics

`runtime_metrics` carries thirty fields covering four areas. They are the
operator's view of whether the runner is keeping up, without needing access to
anything upstream.

| Area | Fields include |
|---|---|
| Local storage | `database_bytes`, `wal_bytes`, `disk_free_bytes`, `sqlite_quick_check` |
| Fact delivery | `pending_fact_batches`, `oldest_pending_fact_age_seconds`, `fact_publish_attempts`, `published_fact_batches`, `last_fact_puback_age_seconds` |
| Deployment convergence | `desired_deployments`, `desired_applied_drift`, `oldest_desired_applied_drift_age_seconds`, `quarantined_deployments`, `restart_count_total`, `in_progress_commands`, `overdue_in_progress_commands`, `command_outcomes`, `terminal_command_outcomes` |
| Authority expiry | `policy_heads`, `expired_policy_heads`, `next_policy_expiry_seconds`, `transport_authorities`, `invalid_transport_authorities`, `next_transport_expiry_seconds` |

The three worth alerting on first:

- **`oldest_pending_fact_age_seconds` climbing** — facts are accumulating
  locally because they cannot be delivered. Execution is unaffected, but your
  view of it is going stale.
- **`desired_applied_drift` non-zero and not falling** — the runner has accepted
  a desired state it has not converged to.
- **`next_transport_expiry_seconds` shrinking** — an authority is approaching
  expiry, and expiry makes the runner unready.

Note the first one is deliberately *not* a readiness failure. A runner that
cannot currently deliver facts is still trading correctly and still protected
locally; taking it out of service for a reporting backlog would turn an
observability problem into an execution one. See
[safety survives a disconnect](/trust-model/safety-survives-disconnect).

## Wiring it up

**Docker Compose**

```yaml
healthcheck:
  test: ["CMD", "arx-runner", "health"]
  interval: 30s
  timeout: 5s
  retries: 3
  start_period: 60s
```

Give it a `start_period`. Startup performs the full authority verification, so a
runner is legitimately unready for the first few seconds.

**systemd**

Run the probe from a timer or a supervising unit rather than as
`ExecStartPost` — readiness is a continuing condition, not a one-time startup
result.

**Kubernetes**

Use `arx-runner health` as the readiness probe, and prefer a liveness probe that
only checks the process. Restarting a runner because it went unready would
discard local state that the unreadiness is telling you to look at.

## When it says not ready

| First check | Meaning |
|---|---|
| File missing entirely | The daemon never reached readiness; read its startup output |
| `credential_state` not `active` | Rotated or revoked upstream — run `arx-runner credential verify` |
| `credential_valid_until` in the past | Expired; rotate it |
| `nats_connected` false | Transport unreachable, or the authority failed verification |
| `sqlite_quick_check` not `ok` | Local store is damaged; do not delete it before reading [troubleshooting](/operator-guide/troubleshooting) |
| `invalid_transport_authorities` non-zero | An authority did not verify |

The state document names the failing condition, so the probe's exit code is a
signal and the JSON is the diagnosis.
