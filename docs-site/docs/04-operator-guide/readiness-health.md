---
title: "Readiness and health"
sidebar_position: 3
---

```bash
uv run arx-runner health --json
```

The probe reads `~/.arx/state/runner-ready.json` locally and exits 0 or 1. It makes no network request. Use `--ready-file` when the daemon uses a different path.

## Three different checks

| Check | What it establishes | What to inspect |
|---|---|---|
| Daemon health | The persisted readiness document passes its predicate | `ready`, credential expiry/binding, transport modes, SQLite check |
| Deployment application | Desired generation reached the local apply boundary | Signed lifecycle fact or offline `observed_generation` |
| Engine readiness | Node, connectivity, reconciliation and portfolio conditions hold | Typed engine ready receipt or local engine diagnostics |

A signed daemon without `--reconcile` may be healthy with `deployment_subscription: false`. Check this field when expecting deployments. Offline readiness is written when the subscription is established; it is not a continuously refreshed portfolio/transport monitor. Check the process, recent status and local logs as well.

## Readiness predicate

The document must report ready, connected transport, an active and correctly bound unexpired credential, all enabled transport modes connected, `sqlite_quick_check: ok` and zero invalid transport authorities. Malformed or missing documents fail. Expiry also fails the probe.

The file contains public identity/expiry metadata, subscription state and `runtime_metrics`, without keys or strategy parameters. It is atomically written with mode `0600` in a `0700` directory. The offline composition fills the common metrics shape but has no signed outbox or policy authorities; zero counts for those components do not prove signed delivery.

## Engine readiness

The current engine checks all eight conditions: live node task, data connection, execution connection, portfolio initialization, reliable portfolio valuation, reconciliation initialization, strategy lifecycle acceptance and active mandatory capabilities.

A missing mark or equity value keeps valuation unreliable. Inspect errors such as `portfolio_prices_missing:<instrument>` and the named instrument. An initialized portfolio can still lack enough prices for equity calculation.

## Alerts and supervision

Monitor signed-lane `oldest_pending_fact_age_seconds`, desired/applied drift, quarantines, policy/transport expiry and disk space. A reporting backlog alone is not an instruction to restart the engine.

```yaml
healthcheck:
  test: ["CMD", "arx-runner", "health"]
  interval: 30s
  timeout: 5s
  retries: 3
  start_period: 60s
```

After a restart the runner reports ready before its deployments have recovered: each recovers in the background, and a deployment that cannot reach its venue shows as not yet healthy (or, once its restart budget is exhausted, quarantined) while the runner itself stays ready and accepts commands. Use the matching ready-file path in container probes. Keep process liveness separate from readiness; investigate unready state before configuring automatic restarts. See [troubleshooting](/operator-guide/troubleshooting).
