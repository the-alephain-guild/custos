# 21 — Restore the non-live offline deployment lane for strategy-logic verification

> **Status**: ⏳ In progress
> **Created**: 2026-07-29
> **Revised**: 2026-07-29 (Foundation Scan corrections + CEO scope decision; see Deviations)
> **Project**: custos (`tesseract-trading/custos/`)
> **Wave**: independent — unblocks the Philosophers Stone offline harness, does not touch the Crucible-signed path
> **For Claude**: `/forge:execute`; two slices (21a T1–T5, 21b T6–T8)
> **Depends on**: Plan 13 ✅ Completed (`deploy/custos` support), Plan 17 ✅ Completed (vault JSON contract)
> **Blocks**: `philosophers-stone/deploy/custos` on any image built after `324da6e`
> **multi_session_scope**: true (revised from false; 8 Tasks, ~1000 LOC restored across CLI, transport, reconcile)

## Context

The offline lane did not die in one commit, and it did not die where the first draft
of this plan said it did. Two commits removed it, a week apart:

| Removed | Role | Died at |
|---|---|---|
| `src/custos/cli/subcommands/nats.py` | `nats bootstrap --profile standalone` | `324da6e` (2026-07-14) |
| `src/custos/core/standalone_nats.py` | the JetStream topology that bootstrap creates | `324da6e` |
| `deployment publish` action + `DeploymentMessage` | publishing a spec to NATS | `324da6e` |
| `src/custos/core/reconcile.py` | unsigned reconcile loop | `324da6e` |
| `nats_client.publish_deployment_status` | the observed-state side the harness waits on | `324da6e` |
| `src/custos/cli/subcommands/deployment.py` (`validate`) | offline spec diagnostics | `8c4454f` (2026-07-21) |
| `src/custos/core/deployment_reconciler.py` | the remaining reconcile surface | `8c4454f` |
| `docs/gateway-contract/v1/deployment_spec.schema.json` + sandbox sample | the spec contract | `8c4454f` |
| `tests/test_cli_deployment.py`, `tests/test_deployment_contract.py`, `tests/test_deployment_reconciler.py` | the covering tests | `8c4454f` |

The covering tests went out with the code, which is why nothing turned red.

`324da6e` ("refactor: consume crucible deployment authority") is where the lane actually
ended. `8c4454f` only swept the remainder. Neither commit cites a plan for the removal.
Plan 19 names `deployment_spec_id` and `deployment_spec_digest`, but those are fields
inside `CrucibleRunnerDeploymentCommandV1`, not this CLI — the removal was incidental to
building the signed path, not a decision to end the unsigned one.

### What the consumer actually needs

Philosophers Stone drives the lane from `philosophers-stone/deploy/custos/`. Against
current `HEAD` it is broken in **five** places, not the two the first draft assumed:

| PS step | Needs | Status today |
|---|---|---|
| `nats-bootstrap` service | `arx-runner nats bootstrap --profile standalone` | no `nats` subcommand |
| `custos-runner` service | `arx-runner start --reconcile-strategy-id` | flag absent (0 hits in `src/`) |
| `spec-publisher` service | `arx-runner deployment publish` | action absent; `DeploymentMessage` 0 hits |
| `wait-status` probe | runner publishing `arx.<tenant>.deployment_status.<runner>.<spec>` | publisher absent |
| `spec-validate` target | `arx-runner deployment validate` | action absent |

The harness is not a duplicate of the v1.team lane. It answers a different question:

| | offline harness | v1.team lane |
|---|---|---|
| question | is the strategy logic right | does PS + Crucible + Custos work as one system |
| strategy source | bind-mounted from the PS checkout | wheel artifact plus manifest |
| delivery | spec published straight to NATS | Crucible-signed deployment command |
| external services | none | Crucible backend plus CONTROL PostgreSQL |

Philosophers Stone ratified that split on 2026-07-28 (`philosophers-stone/.forge/plans/README.md`
lane table, and its Plan 56, which forbids deleting or absorbing the offline harness) —
without noticing this removal a week earlier. This plan closes the gap from the Custos side.

### The spec shapes have diverged

PS renders the **pre-`324da6e`** shape: `spec_id` (a string, `"supertrend-sandbox"`),
`code_hash`, `provenance_ref.credential_id`, `connector`, `pairs`, `leverage`,
`generation`, `lifecycle_state`, `trading_mode`, `sandbox.starting_balances`,
`strategy_config`.

Today's `DeploymentSpec` (`src/custos/contracts/deployment.py:157`) is the canonical V1:
`deployment_spec_id`/`deployment_instance_id`/`strategy_id` as UUIDs,
`deployment_spec_digest`, an `artifact_source` discriminated union, `credential_scope`,
and `extra="forbid"`. The two cannot parse each other's bytes, and
`compute_strategy_code_hash` no longer exists anywhere in `src/`.

So the offline lane gets **its own named contract** in its own module tree. Restoring the
old shape under the canonical name would resurrect a predecessor parser for V1, which
`CLAUDE.md` §First-production V1 contract rule forbids. Two lanes, two contracts, one
guard between them.

### Why not simply pin an old image

`324da6e^` is the last commit where the whole lane works, and it predates the
reliable-equity fix (`0117cc2`) and `c4b2eee` (market orders reporting an absent limit
price as absence rather than zero). Pinning there is a workaround with no exit.

### Why this collides with the authority layer, and what changes

The lane is not merely absent — it is actively forbidden today:

- `authority-manifest.json` `doc_drift.forbidden_regex` bans `DeploymentMessage\.create`,
  `arx\.[^\s"]*\.deployment_(spec|status)`, `publish_deployment_status`,
  `from\s+custos\.core\.deployment_reconciler\s+import` and `standalone_nats`. The scan
  covers `src/custos/cli/subcommands/__init__.py`, `src/custos/cli/_daemon.py`,
  `src/custos/contracts/deployment.py`, `src/custos/core/nats_client.py` and
  `docs/gateway-contract/v1/README.md`. `Makefile:228` puts `check-authority` in `verify`.
- `.claude/rules/mandatory-rules.md` §Trust says desired state is accepted only after
  Crucible exact-byte and exact-subject signature verification, with no unsigned fallback
  and no mode carve-out.

Restoring the lane therefore requires amending the authority layer, not working around
it. That is a **high-risk deviation** under `.claude/rules/deviation-protocol.md`, taken
by CEO decision on 2026-07-29 and recorded under the four-part path of lesson C1
(decision + DEV entry + `.forge/README.md` footnote + `historical-lessons.md` entry).

The exception is drawn at **live**, not at testnet — which is where the red lines
themselves draw it. §Trust's non-negotiable sentence is *"Live mode fails closed without
signed promotion evidence"*; red line 0.2 locks the venue behind the G6 host gate for
live and permits `NoopHost` for sandbox and testnet; and the consumer already draws the
same line (`deploy/custos/scripts/init_runtime.py:147` refuses standalone identity for
live only). Testnet moves no real funds. The first draft's sandbox-only scope was
narrower than the harness it exists to serve.

## Goal

An operator running entirely on their own machine can publish a deployment spec to a
local NATS instance in `sandbox` or `testnet` mode and have a runner reconcile it and
report observed state, with no Crucible backend, no signing authority, and no network
dependency beyond the venue the strategy itself trades on. `live` is refused at the
boundary.

## Non-goals

- No change to the Crucible-signed path. Plan 19 keeps sole ownership of it.
- **`live` never runs on this lane.** Refused by an explicit guard, not by convention and
  not by template omission.
- No predecessor parser for canonical V1. The offline contract is a separate, separately
  named type; `src/custos/contracts/deployment.py` keeps owning V1 alone.
- No return of the `crucible-runner-deployment-command-v1` artifacts.
- No restoration of `tests/test_deployment_reconciler.py`. The reconciler it covered was
  reshaped by Plan 19; re-asserting its internal structure would pin a superseded shape.
  The offline reconciler gets its own tests against its own observable behaviour.
- No changes to `philosophers-stone/`. Subcommand names, flags and subjects are restored
  exactly as PS already invokes them; anything else is a silent break for the consumer
  this plan exists to serve.

## Tasks

### Task 1: Amend the authority layer to name the non-live offline lane

**RED**: `make check-authority` fails while the manifest still forbids the offline lane's
own module tree; a test asserts the amended §Trust text carves out **non-live only** and
still refuses live without signed promotion evidence.

**Implementation**: amend, in one commit, so no later Task lands code the gate forbids:

- `.claude/rules/mandatory-rules.md` §Trust — add the offline lane as a named exception,
  scoped to `sandbox` and `testnet`, stating that live remains signature-only.
- `authority-manifest.json` — keep every existing `forbidden_regex` (they protect against
  the *old* modules returning) and instead let the offline lane live at
  `src/custos/offline/**`, which the old regexes do not name. Add the offline lane's
  paths to `doc_drift.paths` with a forbidden pattern of its own: the lane must never
  reference `live`-mode execution.
- `.claude/rules/authority-docs.md` — record that Custos owns two delivery lanes and that
  the offline one is non-promotable.
- `CLAUDE.md` §2 and §5 — one line each; the mode vocabulary is unchanged.

**Verify**:

```bash
make check-authority
uv run pytest tests/test_offline_lane_authority.py -v
```

**Commit**: `docs(custos): admit a non-live offline deployment lane into the authority layer`

### Task 2: Fail-closed mode boundary before any entry point exists

**RED**: the guard refuses `live` from the mode carried in a spec and, independently,
from a mode passed at the command line; refusal happens before the spec is parsed,
published or written anywhere; a spec claiming sandbox is not publishable into a testnet
transport and vice versa.

**Implementation**: `src/custos/offline/mode_guard.py`, with nothing else in the module
tree yet. The guard exists before the surface it guards, so the surface can never exist
in a state that accepts live.

**Verify**:

```bash
uv run pytest tests/test_offline_mode_guard.py -v
```

**Commit**: `feat(custos): refuse live mode on the offline deployment lane`

### Task 3: The offline spec contract

**RED**: tests reject a spec whose `code_hash` does not match the strategy directory, a
spec for a directory that does not exist, a malformed generation, a spec whose mode is
live, and a subject that does not match the tenant and strategy in the spec.

**Implementation**: `src/custos/offline/spec.py` — `OfflineDeploymentSpec` in the shape PS
renders, `OfflineDeploymentMessage` (canonical subject plus validated envelope) and
`compute_strategy_code_hash`, all constructed through the Task 2 guard. Contract assets
under their own names: `docs/gateway-contract/v1/offline_deployment_spec.schema.json`
plus `samples/offline_deployment_spec_sandbox.json` and
`samples/offline_deployment_spec_testnet.json`. The canonical `deployment_spec.schema.json`
name is not reused.

**Verify**:

```bash
uv run pytest tests/test_offline_deployment_contract.py -v
```

**Commit**: `feat(custos): add the offline deployment spec contract`

### Task 4: `arx-runner deployment validate` and `deployment publish`

**RED**: `validate` exits non-zero on digest mismatch, missing strategy directory and
malformed spec; `publish` refuses live before connecting to anything; both are reachable
under the exact names PS invokes.

**Implementation**: `src/custos/cli/subcommands/deployment.py`, registered in the
dispatcher as `deployment` with actions `validate` and `publish`, flags exactly as
`philosophers-stone/deploy/custos/docker-compose.yaml` and its `Makefile` pass them.

**Verify**:

```bash
uv run pytest tests/test_cli_deployment.py -v
uv run --package custos-runner arx-runner deployment validate \
  --spec-file docs/gateway-contract/v1/samples/offline_deployment_spec_sandbox.json \
  --strategy-dir <any existing directory>
```

**Commit**: `feat(custos): restore the offline deployment publish and validate commands`

### Task 5: `arx-runner nats bootstrap --profile standalone`

**RED**: bootstrap creates the desired-state and observed-state streams idempotently,
refuses a stream that exists but is not owned by the standalone profile, and fails with a
named timeout rather than hanging when NATS never becomes ready.

**Implementation**: `src/custos/offline/transport.py` (stream configs plus
`bootstrap_standalone_nats`) and `src/custos/cli/subcommands/nats.py`, restored from
`324da6e^` and adapted to the current module tree.

**Verify**:

```bash
uv run pytest tests/test_offline_nats_bootstrap.py -v
```

**Commit**: `feat(custos): restore standalone JetStream bootstrap`

> **Slice 21a ends here.** After Task 5, PS `make spec-validate` and `make spec-publish`
> work; `make wait-status` does not yet.

### Task 6: The offline reconcile loop and its observed state

**RED**: a spec at a newer generation starts the engine and publishes an observed status
carrying that generation, phase and health; `lifecycle_state: stopped` stops it and says
so; a spec at an older generation is ignored; a live spec is refused without touching the
engine; a transport outage does not stop a running engine (red line 0.3).

**Implementation**: `src/custos/offline/reconciler.py` — subscribe the desired-state
subject, apply through the existing engine host protocol, publish
`arx.<tenant>.deployment_status.<runner>.<spec>`. Only the spec→engine→status path is
restored; the watchdog and breaker ticks the old `deployment_reconciler.py` also carried
are owned elsewhere now and are not duplicated here.

**Verify**:

```bash
uv run pytest tests/test_offline_reconciler.py -v
```

**Commit**: `feat(custos): reconcile offline specs and publish observed state`

### Task 7: Wire the lane into `arx-runner start`

**RED**: `--reconcile-strategy-id` starts the offline lane; without it the daemon composes
exactly as it does today; the flag is refused together with live mode; the daemon's
existing signed-path composition markers are unchanged.

**Implementation**: `src/custos/cli/subcommands/start.py` gains the flag;
`src/custos/cli/_daemon.py` composes the offline reconciler as one more supervised
long-running task. `check-authority` asserts required substrings in `_daemon.py`
(`scripts/check-authority-docs.py:1075,1255`) — those must survive untouched.

**Verify**:

```bash
uv run pytest tests/cli -v
make check-authority
```

**Commit**: `feat(custos): start the offline lane on an explicit reconcile flag`

### Task 8: Pin the surface so the next convergence cannot remove it silently

**RED**: a contract test fails if `deployment` or `nats` leaves the CLI surface, if the
offline schema file is absent, or if either sample stops validating.

**Implementation**: assertions derived from the real parser — not a hardcoded list, per
lesson C7. The docstring names `philosophers-stone/deploy/custos` as the consumer and
states that removing this surface breaks it, so the next refactor meets a red test rather
than a silent break. Record the lane in `.forge/README.md` next to the authority note.

**Verify**:

```bash
make lint && make test
```

**Commit**: `test(custos): pin the offline deployment lane surface`

## Verification

- [ ] `make lint` clean
- [ ] `make check-authority` green
- [ ] `make test` — no new failures against the pre-existing baseline. Two failures are
      known-red before this plan and unrelated to it:
      `test_runner_fact_contract_v1.py::test_v1_inventory_is_complete_and_byte_pinned`
      and `test_runner_policy_contract_consumer.py::test_runner_policy_pins_one_v1_producer_handoff`,
      both asserting cross-repository receipt commit pins.
- [ ] `make verify-local-v030` builds an image from this branch
- [ ] End-to-end from the consumer side, which is the only proof that matters here:
      ```bash
      cd philosophers-stone/deploy/custos
      make start STRATEGY=supertrend MODE=sandbox TENANT_ID=local
      make start STRATEGY=supertrend MODE=testnet TENANT_ID=local
      ```
      each reaching `wait-status` with the target generation
- [ ] `MODE=live` is refused by the Task 2 guard, not merely absent from the templates

## Red-line gate satisfaction

Filled at close-out, one row per red line, per lesson #40 — `code_coverage` and
`runtime_wire` reported separately, with any deferred wiring named.

| red line | code_coverage | runtime_wire | defer_status | follow_up |
|---|---|---|---|---|
| 0.1 Key/KEK never leaves the process | | | | |
| 0.2 G6 host gate not bypassed | | | | |
| 0.3 Reconcile outage ≠ stop | | | | |
| 0.4 Decimal money math | | | | |

## Deviations and improvements

### DEVIATION: DEV-21-RESTORE-SOURCE-CORRECTION
- **Level**: low (plan accuracy)
- **Cause**: the first draft named `514c130` as the restore source for `deployment publish`.
  `git show 514c130:src/custos/cli/subcommands/deployment.py` has only `validate`, and its
  docstring reads *"The runner CLI intentionally has no command-publish operation."*
  `publish` died at `324da6e`, a week earlier.
- **Decision**: restore source is `324da6e^` for `publish`, `nats bootstrap` and the
  standalone transport; `8c4454f^` for `validate`.

### DEVIATION: DEV-21-SCOPE-EXPANSION
- **Level**: medium
- **Cause**: the first draft scoped three Tasks to publish/validate while its own Goal and
  Verification required the harness to reach `wait-status` — which additionally needs
  `nats bootstrap`, `--reconcile-strategy-id`, the reconcile loop and the status publisher.
  Five breaks, not two.
- **Decision**: eight Tasks in two slices; `multi_session_scope` flipped to true.

### DEVIATION: DEV-21-TESTNET-INCLUDED (CEO)
- **Level**: high
- **Cause**: the first draft's Non-goal refused testnet as well as live. The consumer
  already permits testnet standalone and refuses only live
  (`deploy/custos/scripts/init_runtime.py:147`), and testnet moves no real funds.
- **Decision**: CEO 2026-07-29 — restore sandbox **and** testnet; draw the boundary at live.

### DEVIATION: DEV-21-AUTHORITY-AMENDMENT (CEO)
- **Level**: high
- **Cause**: `mandatory-rules.md` §Trust admits no unsigned lane in any mode, and
  `authority-manifest.json` forbids the lane's subjects and symbols. The lane cannot be
  restored without amending both.
- **Decision**: CEO 2026-07-29 — amend the authority layer rather than work around it,
  scoped to non-live. Recorded under lesson C1's four-part path: this entry, the CEO
  decision above, a `.forge/README.md` footnote (Task 8) and a `historical-lessons.md`
  entry at close-out.
- **Documents updated**: `.claude/rules/mandatory-rules.md`, `authority-manifest.json`,
  `.claude/rules/authority-docs.md`, `CLAUDE.md` (Task 1).

### DEVIATION: DEV-21-OFFLINE-SPEC-SEPARATE-TYPE
- **Level**: medium
- **Cause**: PS renders the pre-`324da6e` spec shape; today's `DeploymentSpec` is
  canonical V1 with `extra="forbid"`. Restoring the old shape under the canonical name
  would be a predecessor parser, which `CLAUDE.md` §First-production V1 contract rule forbids.
- **Decision**: the offline lane gets `OfflineDeploymentSpec` under `src/custos/offline/`
  and its own contract assets. `src/custos/contracts/deployment.py` keeps owning V1 alone.

## Close-out Report

（执行完成后填写）
