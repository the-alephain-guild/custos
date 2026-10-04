---
title: "Production preparation"
sidebar_position: 9
---

Live execution uses ARX signed deployment. Offline inputs cannot authorize it. Complete the same sequence for each exchange and product; spot acceptance does not cover perpetuals.

## Prepare the host

Use a Linux host with a supported `amd64` or `arm64` runtime image. Record the Ubuntu version, architecture, operator and recovery access. Synchronize the clock, allow the required exchange and TLS control-plane endpoints, and provision persistent storage for the encrypted vault, outbox and execution state. Keep trust material read-only and verify that the container user can read its own protected files.

Install the exact image selected for acceptance and verify its release signature. Enroll the runner and provision exchange keys locally. Confirm withdrawal is disabled at the exchange. Validate testnet deployment, fills, cancellation, reconnect, restart recovery and account reconciliation before requesting live approval.

## Runtime identity

Every capability the runner publishes declares the runtime it runs: the image digest, the source revision recorded in that image, and the engine and engine version installed in it. `publish-capability` writes this from what the publishing process observes; do not write it into the manifest yourself. A manifest that already declares a different runtime is refused.

A container cannot see its own image digest, so pass it in the runner's environment as `CUSTOS_RUNTIME_IMAGE_DIGEST`: the multi-platform index digest of the release, `sha256:` followed by 64 lowercase hexadecimal digits, the same value on `amd64` and `arm64`. Set it for both `publish-capability` and `start`. An empty or malformed value is refused, and so is an image whose recorded source revision is not a commit, such as a local build. Omit the variable only when running from a source checkout.

Without the variable the runner declares a development runtime, with no image digest and no source revision. A development runtime can run sandbox, but ARX never counts testnet runs on it toward live promotion. Run testnet acceptance on the released image you intend to take live.

The signed lane starts only when its capability receipt declares the runtime running now. After you change the image or the engine, publish the capability again from the new runtime, then restart the runner; until then it refuses to start. When live approval is configured, the capability must also declare the approved image and revision.

## Runtime approval

The signed daemon accepts these inputs together:

| Input | Purpose |
|---|---|
| `--runtime-promotion-receipt` | Signed approval document for the selected runtime |
| `--runtime-promotion-bundle` | Signature and verification evidence for that document |
| `--runtime-sigstore-trusted-root` | Trusted verification roots provisioned by the operator |
| `--runtime-image-digest` | Exact image digest selected by deployment configuration |
| `--runtime-source-revision` | Source revision recorded by that image |

No approval inputs leaves live disabled. Partial inputs, altered documents, an unexpected signer or a different image/revision fail startup. Use release-provided verification roots; a downloaded document must not supply its own trust authority. Keep these files readable only by the operator/runtime as appropriate.

Select `--enabled-mode live` only in the prepared signed configuration. This selects a transport session; it does not bypass approval. Per-deployment signed promotion, verified strategy material and an active runner risk policy remain mandatory. Runtime approval alone does not certify the whole production system.

## Exchange inputs

[SoDEX](/engines/sodex) requires an explicit wallet, account and settlement currency. [OKX](/engines/okx) requires its API passphrase, region and account settings. Perpetual configurations currently cover linear contracts. Keep unrelated manual trading and other strategies out of an acceptance account so account-level evidence can be reconciled to the intended deployment.


Spot base assets must be representable by the signed money contract. The current codes are `USD`, `USDT`, `USDC`, `BTC`, `ETH`, `VUSDC`, `VBTC` and `VETH`; the v-prefixed assets remain distinct. Unsupported spot assets are refused before node construction. Linear perpetual base symbols do not themselves represent collateral balances.

## First live acceptance

Record the approved account, symbols, strategy parameters, maximum order/total notional, loss threshold and stop procedure. Use these exact limits for a bounded first run, and verify venue orders, signed fills, fees, balances and positions through ARX. Include cancellation, disconnect and process restart. A health probe or a successful config build does not replace these checks.

Do not reuse sandbox or old-release acceptance as proof for a newly built image. Missing account evidence, truncated history or mismatched settlement units keeps reconciliation open. Liquidation, ADL and additional builder fees require explicit reconciliation support before those results can pass acceptance.
