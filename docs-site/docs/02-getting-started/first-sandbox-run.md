---
title: "First Sandbox Run"
sidebar_position: 3
---

# First Sandbox Run

The goal here is a runner that starts, proves its identity, reports ready, and
touches no venue. Nothing in this chapter can place an order.

You should already have completed [enrollment](/getting-started/enrollment) —
`~/.arx/runner.toml` and the encrypted machine vault must exist.

## 1. Provision a credential

Even a sandbox run resolves a credential, because the resolution path is the
same one a real deployment uses. Rehearsing it here means the first time it
matters is not the first time it runs.

```bash
export SOPS_AGE_KEY_FILE="$HOME/.arx/age.key"
export SOPS_AGE_RECIPIENT="$(age-keygen -y "$SOPS_AGE_KEY_FILE")"

printf '%s\n' '<sandbox-api-secret>' | arx-runner vault put \
  --key-id binance-sandbox \
  --tenant-id acme \
  --api-key '<sandbox-api-key>' \
  --api-secret-stdin \
  --scope-digest '<64-hex-scope-digest>' \
  --age-recipient "$SOPS_AGE_RECIPIENT" \
  --permission-scope trade_no_withdraw
```

`--api-secret-stdin` is the form to use. The alternatives exist for
non-interactive contexts, but a secret passed as `--api-secret` is visible in
`ps` output and in your shell history.

Confirm the runner can actually read it back:

```bash
arx-runner vault verify binance-sandbox
```

This runs the real decrypt path rather than a simulation of it. Calling `sops`
by hand proves something different — see
[credential vault](/operator-guide/credential-vault).

## 2. Start the daemon

```bash
arx-runner start \
  --enabled-mode sandbox \
  --engine sandbox-sim
```

`--enabled-mode` is required and takes exactly one of `sandbox`, `testnet`,
`live`. There is no default, because a default would be a mode nobody chose.

`--engine sandbox-sim` selects the simulation host: it exercises artifact
activation, credential resolution, durability, readiness and fact publication
for real, and never connects to a venue. It declares `sandbox` and nothing else,
so it cannot be pointed at a real-money mode even by mistake — see
[the live execution gate](/concepts/live-execution-gate).

Use `--engine nautilus` instead when you want a real sandbox session against
live market data with locally simulated fills. Both are safe; they differ in
whether real market data is involved.

## 3. Check readiness

```bash
arx-runner health
arx-runner health --json
```

Readiness is not "the process is up". It means the machine vault and age
identity were found, the credential is unexpired, tenant, runner, credential id,
version, expiry and key id all agree, the authority confirmed the credential is
still active, and a capability receipt bound to the same public key validated.

A non-zero exit means one of those failed. That is the intended behaviour: a
runner that cannot prove its own authority does not start.

## What happens next is not yours to do

The runner is now waiting for a signed desired-state command. It cannot create
one. Deployments are authored and approved in ARX, and arrive over the
subscription — see
[your first DeploymentSpec](/getting-started/first-deployment-spec).

If nothing arrives, the runner keeps waiting. That is correct: an idle runner
with no instructions is a healthy runner, not a stuck one.

## When it does not start

| Symptom | Cause |
|---|---|
| Exits complaining about the machine vault | Enrollment did not complete, or `SOPS_AGE_KEY_FILE` is not set |
| Exits on credential binding | `runner.toml` and the vault disagree — do not hand-edit either |
| `vault verify` fails but `sops` works | You are testing a different path; the CLI is the acceptance surface |
| Starts, never becomes ready | Capability receipt missing or not bound to this key |

More in [troubleshooting](/operator-guide/troubleshooting).
