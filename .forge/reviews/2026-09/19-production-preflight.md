# SuperTrend production preflight

Date: 2026-09-19. This is a verification report, not a production approval or an implementation plan.

## Requested scope

- Strategy: SuperTrend.
- Venues: Binance, SoDEX and OKX; both spot and perpetuals on all three.
- Production control model: ARX signed deployment, confirmed by the user.
- Deployment platform: Ubuntu; the user has not prepared a host yet. Version and architecture remain to be selected.
- No live switch, credentials, running strategy, production database or remote deployment was changed.

## Inspected revisions

| Repository | HEAD | Worktree at inspection |
|---|---|---|
| Custos | `04cfb0175445c9ae01a89e9111ef9fa27498130d` | Clean |
| Crucible Rust | `bfe8233` | Existing tracked change in transport authority bundle |
| ARX | `a7007b27` | Existing tracked UI/e2e changes and untracked e2e helper |
| Philosophers Stone | `d497429` | Existing untracked strategy directory |

Other repositories' work was left untouched. Production evidence should be produced from selected release revisions, without committing or discarding unrelated development changes.

## Current implementation boundaries

| Venue | Spot | Perpetuals |
|---|---|---|
| Binance | Live config builder exists; runtime admission disabled | Live config builder exists; runtime admission disabled |
| SoDEX | Sandbox/testnet adapter; live builder explicitly refuses | Sandbox/testnet adapter; live builder explicitly refuses |
| OKX | No Custos venue module/registry entry | No Custos venue module/registry entry |

Source: `src/custos/engines/nautilus/venues.py`, `venue_binance.py:342`, `venue_sodex.py:267`. Toolkit OKX signal formatting does not establish a venue execution adapter.

`src/custos/cli/_daemon.py:880` still passes `live_execution_enabled=False`. The admission check in `src/custos/core/engine_lifecycle.py:430` rejects live before engine construction. No runtime consumer of the candidate-promotion receipt was found under `src/custos/`.

The promotion producer in `scripts/runtime_candidate_promote.py:404` sets only `artifact_runtime_ready=True` and retains `system_production_ready=False`. Its receipt must not be treated as a complete live authorization. Enabling live needs an explicit verified authorization boundary, designed and included before the candidate image is built and tested; changing a boolean after acceptance would create a different artifact.

## Checks actually run

| Check | Result | Scope |
|---|---|---|
| `make check` | PASS | 358 files format-clean; lint passed |
| `make check-runtime-lock` | PASS | Current dependency export agrees with committed runtime lock |
| `uv run --extra dev --extra nautilus pytest tests/ -m 'not integration and not docker and not ci_only' -q` | 2498 passed, 1 skipped, 27 deselected, 1 xfailed | Local source/engine suite; no external production acceptance |
| `make verify-runner-fact-publication` | 1 passed | Real isolated JetStream and durable PubAck handling |
| `make verify-nats-revocation CRUCIBLE_REPO=` | FAILED | Real isolated broker test cannot complete its daemon lifecycle phase |

Both isolated NATS tests used the cached image id `sha256:dcadf8f23b60edaaafbe901db7773e2c07947f269c475d8d33d3b46a18b0a7f9` through `CUSTOS_NATS_TEST_IMAGE`. Test containers were removed by the tests. The existing strategy and service containers were not stopped or restarted.

### Revocation gate failure

`tests/integration/test_nats_revocation.py` starts `runner_daemon_lifecycle_process.py` without the currently required `--artifact-input-file`, `--artifact-material-dir`, and `--deployment-authority-file` arguments. The child exits at argument parsing; the suite ends with one failure. Required arguments are defined at `tests/integration/runner_daemon_lifecycle_process.py:1062`.

Repair the fixture/harness to supply real current contract material, then rerun the complete revocation/rotation and command/fact path. Do not remove the required arguments, bypass verification or count the partial broker exercise as a passing gate.

## Existing local runtime

Read-only inspection found an active standalone SuperTrend testnet runner:

- Connector: `binance_perpetual`; pair: `BTC-USDT`.
- Image source revision: `c1d27024312210590bc0716fe1ddff592e5f8012`.
- Local health probe passed and deployment subscription was present.
- Compose directory: `alchymia-labs/philosophers-stone/deploy/custos`.

This is the offline lane. Its health output is not current venue reconciliation, signed production admission or production acceptance evidence. No new order was submitted during this inspection.

## Release-path gaps

- The checked-in cross-repository ledger still reports 0/6 accepted groups and marks previous local acceptance stale. This is the recorded state, not a fresh probe of the future Ubuntu environment.
- Custos's promotion workflow still selects an older published candidate; it does not automatically select the current source/image.
- The two required external acceptance JSON files named by that workflow are absent in the Custos checkout.
- The expected strategy-owner runtime-acceptance workflow was not found at its named path in the current strategy repository.
- Ubuntu host and production configuration have not been supplied; no production DB, identity, broker, strategy artifact or venue-account acceptance was run.

## Proposed verification order

1. Use the confirmed ARX signed control model; prepare the Ubuntu target, select release revisions and scope every test to its own state/account.
2. Repair the real-transport acceptance harness and implement the receipt/authorization-to-live admission path with negative tests before building a candidate.
3. Implement missing SoDEX live/account/ledger delivery and OKX spot/perpetual execution; verify SuperTrend's venue-specific instrument, sizing and position behavior.
4. Build the Linux candidate; run image, persistence/restart, credential rotation/revocation, signed-command, order/fill/reconciliation, risk and shutdown tests. Track all six venue/product combinations independently.
5. Run the signed full-chain testnet acceptance in the target environment and collect new revision-bound evidence. Existing offline testnet activity is supplementary only.
6. Validate the production bundle/environment/browser evidence through existing ARX/Crucible entry points. Keep artifact readiness, canary authorization and whole-system production acceptance distinct.
7. Prepare a bounded live canary specifying venue/account, symbols, size/leverage/exposure limits, stop behavior, monitoring, rollback and operator authorization. Execute only after those concrete inputs and the final enable action are authorized; complete live evidence before expanding deployment.

The existing Custos 18/19, strategy-owner 54/56, Crucible acceptance and ARX production workstreams should retain their ownership. This report does not replace them or refresh historical receipts.
