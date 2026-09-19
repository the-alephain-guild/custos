# Plan 19 production preparation — local implementation evidence

Scope: ARX signed deployment, SuperTrend, Binance/SoDEX/OKX spot and linear
perpetuals. The Ubuntu target and venue account inputs have not been provisioned.
No live order, production schema mutation or runtime promotion was performed.

## Implemented

- Cryptographic runtime approval consumption, exact configured image/revision
  binding and an embedded image source revision. Missing approval leaves live
  disabled; signed deployment promotion, artifact verification and risk policy
  remain independent requirements. Offline live remains rejected.
- Native SoDEX/OKX mode configuration, explicit account settings, local encrypted
  OKX passphrase provisioning, log redaction, early refusal of unrepresentable
  spot base assets, and independent read-only ledger
  collectors with bounded pagination and account/unit checks.
- Native OKX instrument naming and contract sizing in the strategy toolkit;
  contract quantities converted to base quantity in signed fill facts; actual fee
  currency, rebates and distinct SoDEX assets preserved.
- Coordinated Crucible consumer changes plus forward mode/control migrations
  0135/0038. Existing acceptance receipts were not refreshed. Downgrade refuses
  incompatible retained asset/rebate data; unrelated checks remain enforced.
- Public English/Chinese production-preparation and venue guides, with disclosure
  checks retaining the public documentation boundary.

## Verified checkpoints

| Check | Observed result |
|---|---|
| Custos complete source suite | 2566 passed, 1 skipped, 27 deselected, 1 expected failure |
| Final strategy/instrument regression | 75 passed |
| Final native configuration and clean-commit wheel build suites | 23 passed |
| Actual PS SuperTrend strategy construction against all six host instrument mappings | 6/6; construction only, no node or order |
| Toolkit strict typing and typing-closure gate | Passed |
| Linux arm64 image runtime contract | 23 passed |
| Linux image native venue configuration matrix | 18/18 (six connectors across three modes); no client connection or order |
| Real isolated NATS revocation/reconnect/publication gate | 1 passed |
| PostgreSQL money migrations | Both role guards, asset identity, rebate insertion, unrelated checks and guarded downgrade passed |
| Rust signed commission evidence | Domain and consumer focused regression passed |
| Custos/Crucible local authority checks | Passed |
| Documentation verification | Bilingual build and TypeScript passed; source check 51 pages per locale; disclosure scan 108 files, zero issues |

The checked Linux artifact is local image
`sha256:047c57aaf1a2a2ec0d60486cd349c5b977e8e185e9ca4567acd088033ab66faf`,
built from Custos `51d4ac6db2578016277e0ff3e5188b1695958fca`.
It is not an externally signed/promoted multi-platform production release.

Full service-issued daemon acceptance passed (`1 passed`, 90.85 seconds):
real PostgreSQL/NATS and server/signer/provisioner processes, producer-owned
StrategyRelease/product/spec, initial policy activation before daemon startup,
exact capability publication, signed command/fact projection, encrypted-vault
restart and transport rotation/revocation. Artifact verification fixtures remain
local and non-promotable; this is not external owner production acceptance.
The separate exact-release artifact-resolution integration passed (`1 passed`).

The repaired harness now uses canonical stream configuration, complete current
producer capability input, exact runtime venue names and consistent release BOM
build inputs. Crucible also rejects an empty scheduling policy before writing a
spec that its own authority reader would reject.

Coordinated Crucible changes are committed at
`9b80b262f0560b5c8dd13d6bbf699fb35be303f8` (including the monetary migration commit
`c865bfc`). The pre-existing local credential-expiry drill edit was not staged,
changed or published. No historical production receipt was regenerated.

The clean-checkout Docker test target now requests its own development test
dependencies explicitly. The final image was built from an isolated clean Git
worktree; its runtime contract and 18-case native matrix passed, and that
worktree was removed after verification.

## Production gates still open

1. Select and provision Ubuntu host/version/architecture, storage, time sync,
   network access and operator recovery; configure venue credentials locally.
2. Build/publish and independently verify the selected multi-platform runtime,
   then obtain new owner acceptance and promotion bound to that exact artifact.
   The retained promotion workflow still identifies the earlier candidate; no
   historical receipt or candidate identity has been silently repointed.
3. Exercise actual account cycles for all six combinations: orders/cancel,
   reconnect/restart, cash inventory/cost basis, partial/full closes, funding,
   rebates, fee currency and balances/positions. Configuration construction is not
   execution acceptance. Foreign-currency fee settlement needs valid valuation
   evidence; liquidation/ADL/additional builder-fee paths remain fail closed.
4. Supply a signed current runner policy and per-deployment promotion. Authorize
   a bounded first live run with exact account, symbols, order/total notional,
   loss threshold and stop/recovery procedure. Retain deployed evidence before
   asserting whole-system production readiness.
