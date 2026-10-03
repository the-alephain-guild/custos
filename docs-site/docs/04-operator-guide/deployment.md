---
title: "Deployment"
sidebar_position: 1
---

Choose one operating path and keep its identity, broker and state files together.

| Path | Guide | Required upstream |
|---|---|---|
| Local lifecycle rehearsal | [Standalone sandbox](/getting-started/standalone-sandbox) | Operator-owned NATS only |
| Strategy testnet execution | [Offline testnet](/operator-guide/offline-testnet) | Operator-owned NATS and supported venue testnet |
| Signed deployment | [Signed sandbox](/getting-started/first-sandbox-run) | ARX identity, transport, desired state and release material |

The signed lane consumes issued commands and does not create its own control topology. The offline lane provides `identity standalone`, `nats bootstrap` and `deployment validate/publish` for operator-owned work. Offline results cannot be promoted to production.

## Signed release trust

Before executing immutable release material, obtain an accepted runner-local policy authority and the Sigstore root and workflow identity for the expected publisher. The policy is verified separately from the artifact; an artifact cannot choose its own trust root.

The following command issues a policy using existing authority keys. Set every variable to approved inputs and choose output paths that do not already exist.

```bash
uv run arx-runner release-policy issue \
  --authority-private-key "$POLICY_PRIVATE_KEY_FILE" \
  --authority-public-key "$POLICY_PUBLIC_KEY_FILE" \
  --sigstore-trusted-root "$SIGSTORE_ROOT_FILE" \
  --policy-id "$POLICY_ID" --version 1 \
  --not-before "$POLICY_NOT_BEFORE" --expires-at "$POLICY_EXPIRES_AT" \
  --issuer "$SIGSTORE_ISSUER" --workflow-identity "$WORKFLOW_IDENTITY" \
  --source-repository "$SOURCE_REPOSITORY" \
  --envelope-output "$POLICY_ENVELOPE_FILE" \
  --receipt-output "$POLICY_RECEIPT_FILE" \
  --environment-output "$POLICY_ENV_FILE"
```

To accept releases from more than one producer, repeat `--issuer`, `--workflow-identity` and `--source-repository` together, once per producer, in the same order.

`release-policy generate-development-authority` can create keys for an isolated local exercise. Such an authority is explicitly development-only; it is not production approval.

Configure the runner with the resulting envelope, public key, derived key id and trusted root:

```bash
export CUSTOS_ARTIFACT_RELEASE_POLICY_ENVELOPE="$POLICY_ENVELOPE_FILE"
export CUSTOS_ARTIFACT_RELEASE_POLICY_PUBLIC_KEY="$POLICY_PUBLIC_KEY_FILE"
export CUSTOS_ARTIFACT_RELEASE_POLICY_KEY_ID="$POLICY_KEY_ID"
export CUSTOS_ARTIFACT_SIGSTORE_TRUSTED_ROOT="$SIGSTORE_ROOT_FILE"
export CUSTOS_ARTIFACT_REGISTRY=ghcr.io
```

Use the key id recorded in the generated policy output. For a private registry, set `CUSTOS_ARTIFACT_REGISTRY_USERNAME` and `CUSTOS_ARTIFACT_REGISTRY_TOKEN` together. Keep the token out of command arguments. Missing trust inputs or unavailable authenticated release material must be corrected before deployment; they do not select a development fallback.

## Persistent state

Persist identity metadata, machine/venue vaults, age identity, capability, transport authority and the fact database. Keep artifact cache, quarantine and activation paths on suitable local storage. `--production-state-root` can bind mutable signed-lane paths beneath one persistent root; it rejects offline selection and unsafe roots.

Do not share a state root between runner processes. The offline lane uses its own SQLite database through `--offline-state`; it does not use that database as signed business authority.

## One instance per runner process

A runner process runs one strategy instance at a time: the Nautilus engine supports one trading node per process, and the runner holds it for the instance it started. To run several instances, run several runner processes, each with its own identity and state root.

Within one process, instances can follow each other. Stop the running instance, and the runner can start another, including a new version of the same strategy or a different strategy with the same name. Each verified artifact is loaded with a strategy registry of its own, so a name registered by an earlier artifact does not block a later one.

What the runner does with a start it cannot carry out:

| Situation | What the runner does | What you see |
|---|---|---|
| Another instance is running in this process | Refuses the start before loading the new artifact | A signed lifecycle outcome `retry_exhausted` with state `stopped`; the running instance is untouched |
| The running instance is being stopped | Waits and retries the start | The start goes ahead once the stop completes |
| The runner's capability does not cover the new instance | Acknowledges and keeps the command; loads and starts nothing | Log event `runner_command_awaiting_capability_binding`; no lifecycle outcome yet |

Later commands, including a stop for the running instance, are processed normally in every case.

The capability receipt is read once, when the runner starts. A deployment created after that is not covered until a capability covering it is issued and the runner is restarted. On restart, the runner starts or refuses the commands it kept and reports the signed outcome; a command still not covered stays kept, and the runner logs `durable_command_recovery_skipped`. A stop, pause or archive for an instance the capability does not cover is kept the same way: such an instance was never started by this process, so there is nothing to stop. After a restart whose capability covers the instance, the runner applies the kept command and reports its signed lifecycle outcome, once. A kept command that a newer command for the same instance has replaced is not reported; the newer command is handled instead.

After a restart with more than one instance to recover, the runner recovers them one at a time, the instance that was running before the restart first. The others are refused as occupied and reported.

A recovery that fails for a reason that may pass, such as an artifact registry or strategy release service that does not answer, is retried on the schedule a delivered command gets: the same number of attempts and the same back-off. Each retry logs `durable_command_recovery_retry_scheduled` with the reason code and the cause. A start still failing after the last attempt is refused and reported as `retry_exhausted` with state `stopped`; a stop, pause or archive still failing stays kept for the next restart. A newer command for the instance cancels a recovery that is waiting to retry.

## Containers and verification

`make verify-local-v030` builds and checks the local image contract. Mount the runner state at `/home/custos/.arx` and provide the age identity at runtime. A full signed deployment still requires issued identity, transport and release inputs. Confirm the image revision before attributing results to current source.

Check health, subscription and applied instance separately using [readiness](/operator-guide/readiness-health). Consult [release status](/release-governance/release-status) for supported use and production limitations.
