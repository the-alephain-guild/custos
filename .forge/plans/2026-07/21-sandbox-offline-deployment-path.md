# 21 — Restore the non-live offline deployment lane for strategy-logic verification

> **Status**: ✅ Completed (2026-07-29) — code complete; the consumer-side end-to-end run is
> the one verification step left, and it needs Docker plus the PS checkout (see Close-out)
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
`bootstrap_standalone_streams`) and `src/custos/cli/subcommands/nats.py`, restored from
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

**Implementation**: `src/custos/cli/subcommands/start.py` gains the flag and branches to a
separate composition in `src/custos/offline/daemon.py`, which connects, subscribes, builds
the reconciler and marks readiness. `src/custos/cli/_daemon.py` is left untouched — see
`DEV-21-COMPOSITION-OUTSIDE-SIGNED-DAEMON` for why composing inside it was rejected. Its
required substrings (`scripts/check-authority-docs.py:1075,1255`) therefore stay intact by
construction rather than by care.

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
| 0.1 Key/KEK never leaves the process | `test_no_credential_is_read_for_a_mode_the_lane_refuses` proves the vault is not opened for a refused mode | live: `_credential_reader` is what `run_offline_lane` passes to the reconciler; lane log events carry subject, generation and mode only | none | — |
| 0.2 G6 host gate not bypassed | `test_an_engine_that_cannot_run_the_mode_is_not_asked_to`; the whole `test_offline_mode_guard.py` suite | live: the lane refuses live outright, so the live venue path is unreachable from it; admission additionally honours `engine.supports_trading_mode` | **partial** — admission here is `supports_trading_mode`, not `check_g6_gate`, which no longer exists (removed in `8c4454f`). Sound while live is refused; it would not be if that ever changed | any future widening past testnet must restore a real gate first |
| 0.3 Reconcile outage ≠ stop | `test_losing_the_status_channel_does_not_stop_a_running_engine` | live: `_report` logs and returns on publish failure rather than propagating | **partial** — the offline lane composes no `FallbackBreaker`, `RunnerNotionalCap` or `ZombieWatchdog`. Losing the channel does not stop trading, but nothing caps exposure while it is gone | wire the local guards into the offline composition before testnet is used for anything but a short supervised run |
| 0.4 Decimal money math | not exercised | not exercised | none — the lane carries no money field: it moves specs and lifecycle states, and every price and quantity stays inside the engine | — |

## Deviations and improvements

### DEVIATION: DEV-21-RESTORE-SOURCE-CORRECTION
- **Level**: low (plan accuracy)
- **Cause**: the first draft named `514c130` as the restore source for `deployment publish`.
  `git show 514c130:src/custos/cli/subcommands/deployment.py` has only `validate`, and its
  docstring reads *"The runner CLI intentionally has no command-publish operation."*
  `publish` died at `324da6e`, a week earlier.
- **Correction (Task 3)**: this entry's own first answer — `324da6e^`, i.e. `cd41bbb` — was
  also wrong, and reached by inference rather than by reading. `cd41bbb` already requires
  `deployment_instance_id` and `deployment_spec_digest`, which the consumer's renderer
  never emits. The shape PS actually renders is the one at `cec0f8a`, the revision Plan 17
  closed out and PS pins; `5cf7340` added the two fields afterwards.
- **Decision**: the offline spec shape is restored from `cec0f8a`; the publish action,
  `nats bootstrap` and the standalone transport come from `324da6e^`, where they last
  existed. `compute_strategy_code_hash` is restored self-contained, because the module it
  used to live in (`custos.engines.nautilus.strategy_loader`) no longer exists and is
  itself a banned import today.

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

### DEVIATION: DEV-21-COMPOSITION-OUTSIDE-SIGNED-DAEMON
- **Level**: medium
- **Cause**: Task 7 said `_daemon.py` would compose the offline reconciler as one more
  supervised task. `run_daemon` first verifies a machine credential against a control-plane
  backend and loads a transport authority set per mode; the offline lane has neither. Adding
  it there means stubbing exactly the checks that make the signed lane worth having.
- **Decision**: a separate composition in `src/custos/offline/daemon.py`, selected by a
  branch in `start.py`. `_daemon.py` is untouched, so the substrings `check-authority`
  requires of it survive by construction. Recorded after the fact: the audit found this
  unlogged, which is a CRITICAL under the deviation rule regardless of the decision's merit.

### DEVIATION: DEV-21-DURABLE-APPLIED-STATE
- **Level**: medium
- **Cause**: not in the plan at all. Two things forced it. The readiness document that
  `arx-runner health` reads requires a SQLite verdict, and reporting one for a database that
  does not exist is a claim rather than a check. Separately, the reconciler held applied
  generations in memory only, so a restart redeployed a generation already running.
- **Decision**: `src/custos/offline/state.py` — one small store keyed by spec id, loaded at
  construction and written after each applied generation. A recording failure is logged and
  does not fail the apply, since forgetting is worse than not recording.
- **Follow-up**: behaviour on a corrupt store file is undefined; `quick_check` reports it but
  nothing acts on the report.

### DEVIATION: DEV-21-BOOTSTRAP-SYMBOL-NAME
- **Level**: low
- **Cause**: Task 5 named `bootstrap_standalone_nats`, carried over from `324da6e^`.
- **Decision**: the restored function is `bootstrap_standalone_streams`, which says what it
  reconciles. Task 5's text is corrected; this entry keeps the old name findable.

## Close-out Report

- **完成日期**: 2026-07-29
- **总 Task 数**: 8（起草时 3；Foundation Scan 后扩到 8，见 DEV-21-SCOPE-EXPANSION）
- **偏离数**: 5（含两条 CEO 高风险决定）
- **实施 commit 范围**: `34f7307`（计划修订）→ `7c92ca9` `48c5643` `210e5fb` `22122ef`
  `468ce00` `86bd823` `7d26d0f` → Task 8 close-out commit
- **验证结果**: 部分通过 —— 代码与门禁全绿，消费者侧端到端未跑

### 验证证据

| 项 | 结果 |
|---|---|
| `make lint` | 绿 |
| `make check-authority` | 绿（含新增 `verify_offline_lane`） |
| `make test` | 764 passed / 24 skipped / 1 xfailed / **0 failed** |
| `make fmt-check` | 3 处红，**既有**：`runner_fact.py` 与两个 integration 文件被 `docs/authority/**` 按字节 pin 住，`make fmt` 会修好门禁但毁掉证据链（lesson C6） |
| `make verify-local-v030` | **未跑**（需 Docker） |
| PS 侧 `make start MODE=sandbox` / `MODE=testnet` 到 `wait-status` | **未跑**（需 Docker + NATS + PS checkout） |
| `MODE=live` 被 Task 2 guard 拒绝 | 单元层已证（`test_offline_mode_guard.py` 全套 + `_credential_reader` 拒绝在开 vault 之前） |

计划自己说过"消费者侧端到端是唯一算数的证明"。它没跑，所以这份 close-out 按 lesson #40
降级声明：**代码完成、门禁全绿、跨仓真跑未做**。恢复出来的面与 PS 实际传的 flag 逐条对齐
并写成了测试（`test_the_consumer_validate_invocation_is_accepted`、
`test_the_consumer_bootstrap_invocation_is_accepted`、`test_start_offers_the_flag_the_consumer_passes`），
但对齐 flag 不等于跑通 harness。

### 契约影响

- 新增 `docs/gateway-contract/v1/offline_deployment_spec.schema.json` + sandbox/testnet 两个样本；
  canonical `deployment_spec.schema.json` 名字未被占用（既有断言仍在守）
- `docs/gateway-contract/v1/README.md` 增"The offline lane"节，明说它不替代签名通道
- `.claude/rules/mandatory-rules.md` §Trust、`.claude/rules/authority-docs.md`、
  `CLAUDE.md` §2/§5、`authority-manifest.json` `offline_lane`、`scripts/check-authority-docs.py`
- `.github/workflows/scripts/verify-release.sh` 补探两个新命令（由 C7 的 parser 推导门抓出）

### 失败模式覆盖

下表由 `tests/test_plan_closeout_counts.py` 逐行核对 —— 数字来自 pytest 实际 collect，
不是手写。（初版这里写的是"新增 91 个"，实际 117；自列分项之和 108 与自报总数也不自洽，
且整组漏掉了权威门那 8 条。审计以 C3 记之，本表是修正。）

| 测试文件 | 条数 |
|---|---|
| `tests/test_offline_lane_authority.py` | 8 |
| `tests/test_offline_mode_guard.py` | 23 |
| `tests/test_offline_deployment_contract.py` | 23 |
| `tests/test_cli_deployment.py` | 15 |
| `tests/test_offline_nats_bootstrap.py` | 17 |
| `tests/test_offline_reconciler.py` | 17 |
| `tests/test_offline_lane_daemon.py` | 10 |
| `tests/test_gateway_contract_v1_samples.py` | 7 |

上表合计 120 条。末行那个文件早于本 plan 存在，其 7 条里 3 条是既有的，本 plan 在其中加了
4 条面存在性断言 —— 故本 plan 净增 117 条。

含两类"证明门会咬人"的 relaxed-double：
`verify_offline_lane` 的绕过/未分类用例，以及 reconciler 自身 live 拒绝（模型永不产出 live
spec，所以用跳过校验的 spec 证明该分支不是死代码）。

### 遗留项

1. **消费者侧端到端未跑** —— 需 Docker。这是唯一能证明五处断裂真的接回去的验证。
2. **红线 0.3 只兑现一半** —— 离线通道未组合 `FallbackBreaker` / `RunnerNotionalCap` /
   `ZombieWatchdog`。sandbox 无所谓，testnet 长时间无人值守运行前必须补。
3. **NT artifact 桥仅走 registry 发现** —— `BindMountedStrategy.strategy` 依赖
   `STRATEGY_INJECT_PATH` 这个既有 env seam + toolkit registry，未在本仓做过真实 NT 加载
   （NT 测试在本机 importorskip）。第 1 项跑通才算证实。
4. `RUNNER_RUNTIME_METRICS_SCHEMA_V1` 在离线通道里是第三处同串字面量。`runner_fact.py`
   被三份 receipt pin 住不能动（lesson C6），因此靠 `read_health_file` 的拒收 + 端到端
   readiness 测试兜漂移，而不是靠共享常量。
