---
title: "Emergency recovery"
sidebar_position: 5
---

First identify the delivery lane, affected instance and venue exposure. Preserve local state before restarting or replacing the runner.

## Connection loss

Local exposure/drawdown checks continue independently of the message transport. A signed runner retains pending facts in its outbox and resumes publication when transport recovers. Offline status is best effort; use local logs when it cannot publish.

A connection failure alone does not request a close. Check engine connectivity and valuation separately from broker availability.

## Inspect

```bash
uv run arx-runner health --json
du -h "$HOME/.arx/state/runner-fact-outbox.db"
```

Use customized paths where applicable. For offline work inspect `offline.db` and the latest `arx.<tenant>.deployment_status.<runner-label>.<spec-id>` instead of expecting signed-fact metrics.

For the signed lane, investigate SQLite errors, overdue in-progress commands, quarantines and invalid transport authority immediately. Suggested initial warning thresholds are 30 seconds for desired/applied drift or pending-fact age, escalating at 120 seconds; less than 2 GiB free disk; and less than 900 seconds to policy/transport expiry. Tune operational thresholds to your workload. An expired authority remains a runtime rejection regardless of alert settings.

## Stop execution

On the signed lane, request a stopped desired state in ARX and wait for the corresponding lifecycle observation. On the offline lane, publish the same spec id with a higher generation and `lifecycle_state: stopped`, then inspect observed status.

Engine stop follows the supplied shutdown policy. With the default `preserve` policy, positions remain open; do not assume a stopped process has flattened them. When flattening is requested, verify the venue's actual fills and remaining positions. A cancel request also requires confirmation before treating the order as canceled.

If the normal stop path is unavailable, process termination removes local supervision. Use it only with an explicit plan for outstanding venue orders and positions; the runner cannot guarantee containment after it exits.

## Offline breaker trip

A trip latches the guard, attempts flattening and stops the deployment. Further generations are refused while the latch remains. Inspect the failure reason, close results, outstanding orders and any residual exposure.

The latch is process-local. Restart clears that memory and retained desired state can restart a strategy. Fix the cause and set an appropriate desired state before restarting; do not restart merely to make health pass.

## Recover durable state

Preserve the database together with its `-wal` and `-shm` files, identity metadata, encrypted vaults, age key and trust configuration. A consistent backup must account for SQLite writes; avoid copying a changing database as if it were a static file.

Do not delete an outbox or applied-state database to clear an error. Restore through an operator-reviewed recovery procedure, restart, then verify daemon health, subscription, applied generation and venue state separately.
