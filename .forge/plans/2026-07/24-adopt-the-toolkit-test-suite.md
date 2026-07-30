# 24 — Take ownership of the toolkit's test suite

> **Status**: ✅ Completed
> **Completed**: 2026-07-30
> **Created**: 2026-07-30
> **Project**: custos (`tesseract-trading/custos/`)
> **Authority**: owner decision 2026-07-30, recorded in PS Plan 60 (§Slice D 结果, option (b))
> **Depends on**: PS Plan 60 Slice D ✅ (imports already name the toolkit)
> **Blocks**: PS Plan 60 Slice E — PS cannot delete `shared/` while this coverage lives only there
> **multi_session_scope**: **true** (83 files, ~1500 tests, cross-repo)

## Context

Plan 06 vendored philosophers-stone's `shared/` tree into this repository as
`custos_toolkit` / `custos_toolkit_nautilus`, and Plan 07 declared this repository its
authority. **The code came; the tests did not.**

Measured 2026-07-30:

| | tests covering that code |
|---|---|
| philosophers-stone | **1720** (~100 files) |
| custos | **2** (`tests/toolkit/test_nautilus_strategy_registry.py`) |

So the toolkit that executes live trades is protected by two tests inside the repository
that owns it. Everything else guarding it sits in a repository that cannot change it.

That asymmetry is not merely untidy. **This repository can break its own toolkit with a
green CI**, because nothing here exercises the filters, the config loader, the risk
manager, the warmup machinery, the coordinators or the sizing paths. PS would go red, but
PS cannot fix the toolkit — it can only file a plan here.

PS Plan 60 Slice D already rewrote those tests to name `custos_toolkit*`, so they run
against this repository's code as they stand. What remains is moving them to where the
code lives.

## Goal

The toolkit's coverage lives in this repository and runs in its CI. Breaking the toolkit
here turns this repository's own suite red.

## Non-goals

- **Not a rewrite.** These tests already pass against the toolkit. Porting them is a move
  plus whatever fixture wiring this repository needs, not a redesign.
- **Not PS's consumer tests.** Tests that exercise PS strategies, the artifact chain, the
  deploy paths or the Hummingbot subtree stay in PS. See the classification below.
- **Not the toolkit's own source.** No behaviour change to `custos_toolkit*` in this plan.
  If a ported test fails, that is a finding to triage, not a licence to edit the test until
  it passes.

## Scope (PS-side scan, 2026-07-30)

Classified by the top-level packages each test file imports:

| category | count | disposition |
|---|---|---|
| toolkit only | **83** | **move here** |
| mixed (toolkit + PS's own) | 18 | stay in PS — they import `trend` / `momentum` / `portfolio` / `deploy` / `scripts`, i.e. they test PS's *consumption* of the toolkit |
| PS only | 42 | stay in PS (Hummingbot, sidecar/deploy, artifact chain) |

The 83 cover: filters (both the platform-neutral set and the engine-backed set), config
loader and validators, risk manager / controller / equity / order calculators, position
sizing and tracking, signals, warmup, protocols, and the Nautilus adapter's coordinators,
orders, pair context, tick monitor, sltp, snapshot and startup validator.

## What the move has already taught us — read before starting

**An import scan does not find every dependency.** PS's Slice D residue check swept import
statements and reported clean. `tests/test_filter_manager.py` was still loading its subject
by filesystem path:

```python
spec_from_file_location("filter_manager", "shared/nautilus/filter_manager.py")
```

It therefore tested **PS's copy**, leaving this repository's `filter_manager` at zero
coverage while 39 tests passed. Pointing it at the toolkit immediately exposed two layering
assertions that only held for PS's copy.

So the port must check, per file, that the subject under test is this repository's module:

- no `spec_from_file_location` / `Path(...).read_text()` against a repo-relative source path
- `inspect.getsource(module)` rather than reading a path, where source is inspected at all
- after porting, assert the module origin: a filter created from the wrong package still
  satisfies every behavioural test, which is precisely why the layering guards exist

## Tasks

### Task 1: Land a landing zone and prove one file end to end

**RED**: port a single non-trivial file — `test_filter_manager.py` is the right first pick,
since it is the one already known to have had a path dependency — and have it fail before
the fixtures exist.

**Implementation**: decide where ported tests live (`tests/toolkit/` already exists with one
file) and what they may depend on. Establish whether this repository's `conftest.py` needs
anything PS's provides. Do not port in bulk before one file passes here.

**Commit**: `test(toolkit): adopt the first of the toolkit's own tests` — `1221103`

**Landing zone, decided**: `tests/toolkit/`. Engine-backed files take the `test_nautilus_*`
prefix and a module-level `pytest.importorskip("custos_toolkit_nautilus")`, matching
`tests/toolkit/test_nautilus_strategy_registry.py`, which is the only convention this
repository already has for engine-dependent toolkit tests. No `conftest.py` is added,
because nothing asked for one.

**Evidence the coverage binds to this repository's module**: with the manager's pass/fail
decision inverted, this repository's prior toolkit coverage stayed green at 2 passed and 4
of the ported assertions went red. Reverted to the byte (`git diff -- packages/` empty)
before commit.

### Task 2: Port the platform-neutral set

**Implementation**: filters, config, risk, position, signals, warmup, protocols. These need
no engine and should be the cheapest. Record any test that fails on arrival rather than
adjusting it into passing — a failure here means the vendored copy diverged from what PS
was testing, which is a finding worth its own entry.

**Commit**: `test(toolkit): adopt the platform-neutral coverage` — `bc53513`

22 files, 423 assertions, all green on arrival. Three files an import scan called
toolkit-only stayed in philosophers-stone: they read a strategy's `config.yaml` or walk
the strategy roots, so their subject is a strategy.

### Task 3: Port the Nautilus adapter set

**Implementation**: coordinators, orders, pair context, tick monitor, sltp, snapshot,
startup validator, filter manager, indicators. These need `nautilus_trader`; mirror
whatever skip/gate convention this repository already uses for engine-dependent tests.

**Commit**: `test(toolkit): adopt the engine adapter coverage` — `c8b8e51`

61 files. Eleven assertions failed on arrival, all one finding: they walk the set of
strategies and their sentinels refused to pass in a repository with none. Returned to
philosophers-stone rather than adjusted.

### Task 4: Make the coverage load-bearing, then let PS drop its copy

**Implementation**: the ported tests must run in this repository's default verification —
coverage that exists but is not run is the situation this plan set out to fix. Then notify PS that
Plan 60 Slice E may delete `shared/`.

**Verify**: mutate one toolkit source file deliberately (e.g. invert a filter threshold
comparison) and confirm this repository's suite goes red. Exit-code-zero is not the
evidence; the red is.

**Commit**: `docs(custos): record that the toolkit's coverage now lives with the toolkit`

## Verification

- [x] Both gates green with the ported tests included: `make test-baseline` 1263 passed,
      `make test-nt` 2165 passed. `make fmt-check` is red on main for an unrelated,
      documented reason (receipt-pinned files are frozen outside the formatter).
- [x] Four deliberate mutations, four reds, each reverted to the byte. Table in the
      close-out.
- [x] No ported test loads its subject from a filesystem path. The five that do stayed
      in philosophers-stone; two of them were invisible to the import scan.
- [x] Reported, with the correction that Slice E is **not** fully unblocked — five files
      still read `shared/` by path. Listed under Leftovers.

## What the Foundation Scan measured (2026-07-30, this repository)

The scope table above was written from philosophers-stone. Re-measured here by parsing
every test file's imports rather than by trusting the count:

| category | plan said | measured |
|---|---|---|
| toolkit only | 83 | **92** |
| mixed | 18 | 8 |
| PS only | 42 | 41 |
| neither (no toolkit and no PS import) | — | 23 |
| total | 143 | **164** |

The plan's three numbers do not sum to the number of test files philosophers-stone has,
which is how the drift showed up. The measured list is the one this plan works from.

### The import scan is not the whole dependency graph — again, and wider

The plan already knew of one file loading its subject by path. Measured, there are more,
and the worst of them are invisible to an import scan because they import nothing at all:

| file | how it reaches its subject | consequence |
|---|---|---|
| `tests/test_core_contract.py:56` | `Path("shared/nautilus/strategy_core.py").read_text()` | tests PS's copy |
| `tests/test_order_reconciler.py:190,282` | `Path(__file__).parent.parent / "shared" / ...` | tests PS's copy |
| `tests/test_stale_order_sweep.py:305` | same | tests PS's copy |
| `tests/test_filter_config_scope.py:23` | `spec_from_file_location(..., "shared/nautilus/config/filters.py")` | classified "neither"; actually a toolkit test |
| `tests/test_risk_equity_wiring.py:16-24` | reads four `shared/nautilus/...` files | classified "neither"; actually a toolkit test |

These cannot be ported as they stand, and they will break in philosophers-stone the moment
Slice E deletes `shared/` — so Slice E is blocked on them being rewritten to reach the
module by import, not merely on this plan finishing.

`tests/test_strategy_entry_points.py` reads `_template/refinement/nautilus/strategy.py`.
Its subject is a philosophers-stone template, so despite importing the toolkit it belongs
there. Misclassified by import alone.

### The conftest question, answered

This repository has **no `conftest.py`**, and no ported test needs one: of the 92
toolkit-only files, **zero** request any fixture philosophers-stone's two conftest files
define. Its root conftest imports `shared.hummingbot.config`, which is philosophers-stone's
own code and never was the toolkit's.

### The port is also a translation

30 of the 92 files carry **504 lines containing CJK**. This repository's language rule
covers test artifacts and `scripts/check-code-english.py` blocks new CJK lines at commit,
so every one of those lines is rewritten in English on arrival. The plan called this "a
move plus whatever fixture wiring this repository needs"; the fixture wiring turned out to
be nothing and the translation turned out to be the real cost.

### Task 4's mutation recipe is sound, for a reason worth writing down

`docs/authority/strategy-toolkit-*.json` record a `target_sha256` for every toolkit source
file, so mutating one looked likely to fire the authority gate and produce a red that
proves nothing. It does not: `scripts/check-toolkit-extraction.py:158-161` hashes
`_git_blob(implementation_commit, ...)` — a **historical commit**, not the working tree.
Verified by mutating `filter_manager.py` and watching `make check-toolkit-extraction` pass.

So the mutation experiment yields one unambiguous red. The same fact says something less
comfortable: those hashes attest that the extraction was faithful **when it happened**, and
nothing in `make verify` notices that a toolkit source has changed since. That is this
plan's own thesis one layer deeper than it stated it.

### Which gate the ported coverage lands in

`custos-strategy-toolkit` is a base dependency; `custos-strategy-toolkit-nautilus` is only
in the `nautilus` extra (`pyproject.toml:7,32`, confirmed against `uv export` for both
profiles). So:

- platform-neutral coverage (Task 2) is load-bearing under `make verify`;
- engine adapter coverage (Task 3) `importorskip`s out of `make verify` and is load-bearing
  under `make verify-nt`.

Both run in the release gate (`.github/workflows/release.yml:56,58`). Task 4 must state it
that way rather than claim "the default verification", which would be true of half of it.

## Deviations & improvements

- Any test that fails on arrival: record it here with the divergence it exposes. Those are
  the most valuable output of this plan, since they are differences between the vendored
  copy and the code PS was actually testing.
- If a ported test needs a fixture this repository does not have, note whether the fixture
  belongs here or the test belongs in PS after all.
- PS keeps `test_taste_guard_nautilus_base.py`-style code-style guards out of scope: PS
  should not police this repository's style, and the subject it scans is disappearing.

### DEV-24-CLASSIFICATION-DRIFT
- **等级**: 低
- **原因**: 计划的 scope 表来自 PS 侧，三个类别之和小于 PS 的测试文件总数。
- **决定**: 重量为 92 / 8 / 41 / 23（合计 164），按实测清单执行。已记入「Foundation Scan」段。

### DEV-24-SUBJECT-IS-THE-STRATEGIES
- **等级**: 中
- **原因**: 十一条断言到岸即红，另有两个文件 import 扫描完全看不见。它们遍历**策略集合**
  ——glob `config.yaml`、AST 扫子类、按路径读 schema——主体是策略而非 toolkit。
- **决定**: 退回 PS，不调整成通过。计划已授权（Non-goals 第二条）。其中一个参数化在空
  glob 上：在这里它不会失败，而是整条消失——那正是它旁边那条哨兵存在的理由。
- **影响**: 八个文件 + 三处函数级切分。清单在 close-out 遗留项。

### DEV-24-PORT-IS-ALSO-A-TRANSLATION
- **等级**: 低
- **原因**: 30 个文件带 504 行 CJK；本仓语言红线由 pre-commit 拦新增 CJK 行。计划把成本
  记为「搬运 + fixture 接线」，实际 fixture 接线为零，翻译是真成本。
- **决定**: 逐行手写英文，另剥 65 处内部追踪号（读者无从查起 `plan 36 T8` 是什么）。

### DEV-24-REGEX-CORRUPTED-THE-PORT
- **等级**: 中
- **原因**: 剥追踪号时我用正则扫**裸行**，其中 `\(\s*\)` 把 `f.is_ready()` 改成属性访问，
  `\s+\)` 压掉了缩进的收尾括号。9 个文件语法错，更多文件是静默的语义改动。
- **决定**: 已提交的 24 个文件用 `git show HEAD:<path>` 写回；未提交的 61 个从 PS 重取并
  按序重放（重放脚本对每步先断言再改）。恢复后 1308 passed / 2 skipped，与损坏前逐条一致。
  追踪号改为逐条手写，`apply_exact.py` 要求整行唯一匹配、写前 `ast.parse`。
- **教训**: 对源码做批量文本改写时，正则的作用域必须是**语法结构**而不是行。中途我改用
  tokenize 限定「只碰注释与 docstring 行」，仍然出错——带行尾注释的**代码行**整行合格。
  散文修复也留下 "deleted in ." 这类断句。机械改写在这类任务上不可信。

### DEV-24-VACUOUS-LAYERING-GUARDS
- **等级**: 中
- **原因**: 七处断言查的是字面量 `shared.filters` / `shared.nautilus.snapshot`——一个本仓
  **完全不存在**的包（`find_spec('shared')` 为 None）。Slice D 改了 import，没改断言里的
  字符串。守卫因此永远不会失败。这正是计划「read before starting」警告的那一类。
- **决定**: 重指到 `custos_toolkit.filters` 与 `custos_toolkit_nautilus.adapter.*`，并证明
  它会咬：给引擎 adx filter 注入一条 `custos_toolkit.filters` 导入后守卫变红。函数名与
  docstring 里的 `shared` 一并改掉（那个包名对这里的读者没有指代对象）。

### DEV-24-COVERAGE-THAT-NEVER-RAN
- **等级**: 中
- **原因**: `pytest --collect-only` 显示两个刚翻译完的文件**一条都不跑**：
  `test_nautilus_filter_adx.py` 卡在 `importorskip("pandas_ta")`（上游包，本仓正是把它
  vendor 进来才不依赖），`test_msgbus_stream_e2e.py` 需要 `redis` 与 6380 上的活 Redis
  （custos 无 redis 依赖，走 NATS）。若只看「1308 passed」就收尾，46 条断言会永久沉默。
- **决定**: adx 指向 vendored `pandas_ta`（运行时用的就是它，见 `_pandas_ta.py:7`）；
  msgbus 退回 PS（主体是 sidecar 的 Redis 传输）。
- **教训**: 「全绿」不含「有没有在跑」。收尾必看 collect 计数，不只看 passed。

## Close-out Report

- **完成日期**: 2026-07-30
- **总 Task 数**: 4
- **偏离数**: 6（详见下方偏离日志）
- **验证结果**: 全部通过
- **实施 commit 范围**: `1221103..HEAD`
- **契约影响**: 无。本计划不改 `custos_toolkit*` 任何行为，也不动 `docs/authority/`。

### 覆盖落在哪道门

| 门 | 结果 | toolkit 覆盖 |
|---|---|---|
| `make test-baseline`（base profile） | 1263 passed / 222 skipped / 1 xfailed | platform-neutral 部分真跑 |
| `make test-nt` | 2165 passed / 27 skipped / 1 xfailed | 引擎适配部分真跑 |
| `make check-authority` | passed | 未受影响 |
| `make lint` | passed | — |
| `make fmt-check` | **主干恒红**，与本计划无关 | 三个被 receipt 按字节 pin 的文件不是 format-clean，见 `historical-lessons.md` C6 |

计划的 Task 4 要求覆盖进入「默认验证」。**准确说法是分两道门**：platform-neutral
部分在 `make verify` 里承重，引擎适配部分在 `make verify-nt` 里承重，因为适配层
distribution 只在 `nautilus` extra 里。两道门都在发布 CI 中执行
（`.github/workflows/release.yml:56,58`）。

一处诚实的边角：`make verify` 的 `check-authority` 内部用 `uv run --extra nautilus`，
会把 extra 装回 venv。所以同一次 `make verify` 里，`test-baseline` 看到的 profile
取决于它启动时 venv 的状态，而不是 target 定义。这不是本计划引入的，但既然现在有
测试依赖 profile，就该写下来。

### 承重证明（红才是证据）

| 变异 | 反应 |
|---|---|
| 反转 `filter_manager` 的 pass/fail 决策 | 移植前 2 passed 全绿；移植后 4 条变红 |
| 反转 `custos_toolkit/filters/adx.py` 阈值方向 | `make test-baseline` 2 failed + 3 errors |
| 反转 `adapter/tick_monitor.py` long 侧 peak 方向 | 跨 2 文件 6 条变红 |
| 给引擎 adx filter 注入 `custos_toolkit.filters` 导入 | 重指后的 layering 守卫变红 |

每次变异后都以 `git diff --stat -- packages/` 为空确认字节级还原。

**这些哈希不保护工作区。** `docs/authority/strategy-toolkit-*.json` 为每个 toolkit
源文件记了 `target_sha256`，但 `scripts/check-toolkit-extraction.py:158-161` 哈希的是
`_git_blob(implementation_commit, ...)` —— 一个历史 commit。实测：变异
`filter_manager.py` 后 `make check-toolkit-extraction` 仍然通过。所以那些哈希证明的是
「抽取当时是忠实的」，不是「此后没被改过」。这正是本计划的论点，只是深了一层。

> **更正（2026-07-30，Plan 25 实施时发现）。** 上一段说「哈希不保护工作区」是对的，但当时
> 由此推出的「本仓没有任何东西会注意到 toolkit 源码被改了」**过头了**。
> `tests/test_toolkit_release_candidate_build.py` 会拦：它经
> `scripts/toolkit_rc_build.py:98-109` 比对**工作区与 HEAD**（`git diff --quiet <HEAD> --
> packages/…` 加 untracked 扫描），不一致就报
> `toolkit package sources must exactly match the clean source commit`。
>
> 分寸在这里：它守的是「从干净树构建」这条**可复现性**纪律，不是「toolkit 仍等于那次抽取」。
> 所以**未提交**的漂移会被它拦下，**已提交**的漂移一路全绿 —— 上面那条实质结论对后者成立，
> 而后者才是真正会长期存在的情形（Plan 25 提交了一处 toolkit 改动，2181 项全绿）。
>
> 这条更正之所以必要，是因为 Plan 24 做变异实验时它其实已经响过：`make test-baseline` 当时
> 报 `2 failed, 1258 passed, 222 skipped, 1 xfailed, 3 errors`，我记下了「3 errors 是新的」
> 却把它整体归因为「变异的连带结果」，没有点开看是谁。结论（门会变红）没错，**红在哪里**
> 认得不全。看到一个没预料到的 error 计数就该点开，而不是归因了就算数。

### 测试条数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/test_plan_closeout_counts.py` | 9 |
| `tests/toolkit/test_adx_filter.py` | 15 |
| `tests/toolkit/test_allocation_config.py` | 11 |
| `tests/toolkit/test_base_strategy_filters.py` | 13 |
| `tests/toolkit/test_base_strategy_multi_pair.py` | 20 |
| `tests/toolkit/test_capital_allocator.py` | 15 |
| `tests/toolkit/test_config_loader.py` | 38 |
| `tests/toolkit/test_config_self_validation.py` | 32 |
| `tests/toolkit/test_config_summary_logger.py` | 3 |
| `tests/toolkit/test_cooldown_filter.py` | 8 |
| `tests/toolkit/test_coordinator_delegation.py` | 9 |
| `tests/toolkit/test_decimal_precision.py` | 29 |
| `tests/toolkit/test_deep_asdict.py` | 18 |
| `tests/toolkit/test_equity_provider.py` | 9 |
| `tests/toolkit/test_event_publisher.py` | 38 |
| `tests/toolkit/test_exchange_error_classify.py` | 11 |
| `tests/toolkit/test_execution_coordinator.py` | 7 |
| `tests/toolkit/test_execution_manager.py` | 10 |
| `tests/toolkit/test_exit_close_guard.py` | 3 |
| `tests/toolkit/test_filter_behavior.py` | 10 |
| `tests/toolkit/test_filter_coordinator.py` | 22 |
| `tests/toolkit/test_filter_direction.py` | 8 |
| `tests/toolkit/test_filter_registry.py` | 5 |
| `tests/toolkit/test_fixed_risk_config.py` | 6 |
| `tests/toolkit/test_fixed_risk_fail_fast.py` | 5 |
| `tests/toolkit/test_fixed_risk_sizing.py` | 9 |
| `tests/toolkit/test_momentum_filter.py` | 31 |
| `tests/toolkit/test_mtf_filter.py` | 41 |
| `tests/toolkit/test_multi_pair_integration.py` | 8 |
| `tests/toolkit/test_native_trailing_mode.py` | 26 |
| `tests/toolkit/test_native_trailing_submitter.py` | 16 |
| `tests/toolkit/test_nautilus_config.py` | 91 |
| `tests/toolkit/test_nautilus_filter_adx.py` | 7 |
| `tests/toolkit/test_nautilus_filter_manager.py` | 39 |
| `tests/toolkit/test_nautilus_filter_momentum.py` | 12 |
| `tests/toolkit/test_nautilus_filter_regime_t9.py` | 8 |
| `tests/toolkit/test_nautilus_filter_regime.py` | 10 |
| `tests/toolkit/test_nautilus_filter_volatility.py` | 10 |
| `tests/toolkit/test_nautilus_filter_volume.py` | 10 |
| `tests/toolkit/test_nautilus_signal_processor.py` | 14 |
| `tests/toolkit/test_nautilus_startup_validator.py` | 7 |
| `tests/toolkit/test_nautilus_strategy_registry.py` | 2 |
| `tests/toolkit/test_nautilus_utils.py` | 22 |
| `tests/toolkit/test_on_save_on_load.py` | 6 |
| `tests/toolkit/test_order_calculator.py` | 11 |
| `tests/toolkit/test_order_side_regression.py` | 7 |
| `tests/toolkit/test_order_submitters.py` | 20 |
| `tests/toolkit/test_order_tracker_close_guard.py` | 10 |
| `tests/toolkit/test_pair_context_coordinator.py` | 10 |
| `tests/toolkit/test_pair_context.py` | 16 |
| `tests/toolkit/test_position_sizer.py` | 13 |
| `tests/toolkit/test_position_tracker.py` | 20 |
| `tests/toolkit/test_protocols.py` | 3 |
| `tests/toolkit/test_regime_filter.py` | 17 |
| `tests/toolkit/test_risk_control_coordinator.py` | 9 |
| `tests/toolkit/test_risk_controller.py` | 13 |
| `tests/toolkit/test_risk_equity_glue.py` | 11 |
| `tests/toolkit/test_risk_equity.py` | 12 |
| `tests/toolkit/test_risk_manager.py` | 54 |
| `tests/toolkit/test_signal_execution_coordinator.py` | 14 |
| `tests/toolkit/test_signal_okx_compatibility.py` | 34 |
| `tests/toolkit/test_signals.py` | 38 |
| `tests/toolkit/test_sizing_coordinator.py` | 5 |
| `tests/toolkit/test_sltp_coordinator.py` | 13 |
| `tests/toolkit/test_sltp_mode.py` | 29 |
| `tests/toolkit/test_snapshot_coordinator.py` | 10 |
| `tests/toolkit/test_snapshot_subsystem_removed.py` | 9 |
| `tests/toolkit/test_startup_validator.py` | 12 |
| `tests/toolkit/test_strategy_core.py` | 12 |
| `tests/toolkit/test_supertrend_snapshot.py` | 4 |
| `tests/toolkit/test_tick_exit_close_position.py` | 4 |
| `tests/toolkit/test_tick_monitor.py` | 54 |
| `tests/toolkit/test_time_filter.py` | 19 |
| `tests/toolkit/test_toolkit_nautilus_indicators.py` | 3 |
| `tests/toolkit/test_trade_event_handler.py` | 10 |
| `tests/toolkit/test_trading_config.py` | 5 |
| `tests/toolkit/test_trailing_behavioral_equivalence.py` | 4 |
| `tests/toolkit/test_volume_filter.py` | 18 |
| `tests/toolkit/test_warmup_coordinator.py` | 19 |
| `tests/toolkit/test_warmup_integration.py` | 3 |
| `tests/toolkit/test_warmup_manager.py` | 14 |
| `tests/toolkit/test_warmup_on_load.py` | 14 |
| `tests/toolkit/test_warmup_protocol.py` | 3 |
| `tests/toolkit/test_warmup_snapshot.py` | 10 |
| `tests/toolkit/test_warmup_warmer.py` | 15 |

上表合计 1324 条。

引擎适配的文件在 base profile 下 collect 为 0（module 级 `importorskip` 生效），
所以 `tests/test_plan_closeout_counts.py` 本轮扩了一处：被 collect 期跳过的文件
不再被当作「计数为 0」判错，而是**点名报告为本 profile 未核验**。豁免只给真的
skip，且由 `test_the_probe_tells_a_skipped_file_apart_from_an_uncollectable_one`
证伪——它在 `tests/` 下写一个真会 skip 的文件跑一遍，同时断言一个真的 collect 了的
文件不被豁免。`tests/test_plan_closeout_counts.py` 因此从 6 条增到 9 条——新探针一条，加上两个按
「带表格的 plan」参数化的检查各多一个用例，因为本 close-out 自己就是第三份带表格的
plan。本表按规则重数（plan 21/22 的旧行是历史，不改）。

### 红线 gate 满足度

本计划只搬测试、不动 `src/custos/` 与 `packages/*/src/`，四条红线的兑现状态与
落地前**逐字节相同**（`git diff` 对这两处为空）。

| 红线 | code 覆盖 | runtime 接线 | 本计划影响 |
|---|---|---|---|
| 0.1 Key/KEK 不出进程 | 未触碰 | 未触碰 | 无 |
| 0.2 G6 host gate | 未触碰 | 未触碰 | 无 |
| 0.3 失联 ≠ 停止 | 未触碰 | 未触碰 | 无 |
| 0.4 Decimal money math | **增强**：`test_decimal_precision.py` 29 条 + `test_order_calculator.py` 11 条 + `test_risk_manager.py` 54 条现在在本仓 base 门内跑 | 未触碰 | 只增覆盖 |

0.4 那行是本计划对红线唯一的实质影响，而且是正向的：toolkit 的 money math 此前在
本仓**零覆盖**，现在 base 门里有 94 条守着它。

### 遗留项

1. **PS Slice E 尚未完全解锁。** 五个文件仍以硬编码路径读 `shared/`
   （`test_core_contract.py:56`、`test_order_reconciler.py:190,282`、
   `test_stale_order_sweep.py:305`、`test_filter_config_scope.py:23`、
   `test_risk_equity_wiring.py:16-24`），其中后两个 import 扫描完全看不见。删
   `shared/` 会让它们在 PS 侧变红。它们测的是 toolkit，应改为按 import 取模块后
   移过来，或明确留在 PS 并换 subject。
2. **八个文件留在 PS。** 主体是策略集合而非 toolkit：`test_ctx_hook_contract.py`、
   `test_strategy_entry_points.py`、`test_config_self_validation.py` 的策略 glob 段、
   `test_strategy_core.py` 的两条 AST 扫描、`test_base_strategy_multi_pair.py` 的仓库
   扫描、`test_nautilus_filter_regime_t9.py` 与 `test_sltp_mode.py` 的 schema 扫描、
   `test_supertrend_risk_controller_enabled.py`、`test_rebalancing_config_contract.py`、
   `test_strategy_trading_node_contract.py`、`test_msgbus_stream_e2e.py`（读 Redis，
   custos 无此依赖）。
3. **`test_taste_guard_nautilus_base.py` 不移。** 计划已声明：PS 不该管本仓风格，且它
   扫的目录正在消失。
