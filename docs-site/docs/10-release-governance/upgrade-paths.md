---
title: "Upgrade paths"
sidebar_position: 2
---

Record the current Git revision or image digest, dependency lock and state locations before upgrading. Preserve identity, encrypted credentials and consistent database backups. Read [release status](/release-governance/release-status) to distinguish a source version from a published candidate or supported stable line.

## Nautilus 2 checkout

The current adapter uses the NautilusTrader 2 API on Python 3.12. Use `make install-nt` to install compatible dependencies; do not independently upgrade the engine package.

1. Stop the affected deployment through its normal desired-state path and inspect venue positions/orders.
2. Install the intended checkout with `make install-nt`.
3. Run the relevant source/typing and engine checks.
4. Rehearse startup, readiness and stop in sandbox before a testnet run.
5. Rebuild any local image and confirm its revision label.

The host now admits only one active node per event loop. An initialized portfolio must also have reliable valuation before engine readiness passes. SoDEX support has mode and input limits described [here](/engines/sodex).

## Signed and offline input

Signed deployments continue to come from ARX. The local `deployment validate/publish` commands handle a distinct `OfflineDeploymentSpec`, only for sandbox/testnet. They do not validate or publish canonical signed deployment commands.

`identity standalone` is available for offline operation. Do not convert an enrolled identity by editing `runner.toml`; use a separate state root. Offline code-directory hashes and signed immutable release digests serve different contracts and are not interchangeable.

## Older state layouts

Older single-file venue vaults require deliberate per-key reprovisioning with `vault put`. Keep decrypted migration material in a protected local location, feed secrets through stdin, and remove temporary plaintext after verification. Do not overwrite a machine vault while migrating venue keys.

State uses `~/.arx`. Preserve each runner's metadata/vault binding and re-enroll if its old identity format is no longer accepted. Copying an old enrollment record alone does not create a valid current `runner.toml` and encrypted machine identity.

## Rollback

Restore a tested source/image revision with its matching dependencies. Check state-format and contract compatibility before reusing state written by a newer version. A previous binary does not necessarily understand a newer database or signed payload. Retain the backup and recorded evidence until recovery has been verified.

## Production and 1.0

Production requires independent deployed acceptance, not a local image check. A future 1.0 also requires the documented compatibility and support commitments, including an exercised support window. Local sandbox/testnet success cannot authorize production promotion.
