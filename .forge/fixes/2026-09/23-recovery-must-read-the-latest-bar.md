# 23 - recovery-must-read-the-latest-bar

> **Status**: ✅ Completed
> **Created**: 2026-09-21
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-lifecycle-boundary-deep-review.md` LB-3

## 根因

`adapter/coordinators/order_reconciler.py:105-106`，重启后恢复 tick 监视器时：

```python
bars = s.cache.bars(ctx.bar_type)
if bars:
    current_price = Decimal(str(bars[-1].close))
```

`bars[-1]` 取的是**缓存里最旧的一根**，不是最新的。

**上游实证**（不是推理）：NautilusTrader 的 `Cache::add_bar` 是 `bars.push_front(bar)`
（`crates/common/src/cache/mod.rs:4415`，`add_bars` 在 `:4444` 同理），而它自己的 doc 注释写着
**"Index 0 is the most recent"**（`:1567`、`:7816`）。所以 `[0]` 最新、`[-1]` 最旧。按默认
`bar_capacity`，那可能是几千根之前的价格。

**后果**：重启后追踪止损用一个陈旧价格去恢复状态。报告复现的形态是**漏激活**——真实行情已到 104
（超过 2% 激活线），恢复却喂 101，激活标记没置上，随后回落到 101.5 不触发退出；读对最新价则会触发。
反方向同样成立：若最旧那根恰好是个高点，恢复出的高水位就偏高，退出会在错误的位置触发。

**覆盖现状**：`grep bars tests/toolkit/test_strategy_state_and_order_protection.py` **零命中**——
这一行目前完全没有测试，连单根替身都没有。报告说「缓存只有一根 K 线的替身测试无法发现顺序错误」
是客气了。

**与 C26 的区分**（报告自己点明了）：fix 07 修的是 `observe()` 内部丢了 trailing 激活标记；
这一条是**调用方喂错了价格**。不是那个缺陷复发，两者独立。

## 修复任务

### Fix 1: 用说得清自己在做什么的 API 取最新 bar [P1]

**Files**: `adapter/coordinators/order_reconciler.py`、`adapter/runtime_types.py`、
`tests/toolkit/test_recovery_reads_the_latest_bar.py`

1. 先写失败测试：缓存里两根（最新在前），恢复后的 trailing 峰值必须来自**最新**那根。
2. 改用 `cache.bar(bar_type)` —— NT 的「最新一根」原生入口（`cache/mod.rs:7810` 返回
   `bars.front()`）。这比 `bars[0]` 好：它把「我要最新的」写进调用本身，不依赖读者记得
   deque 的方向。`runtime_types.Cache` 协议同步加这个方法。
3. **不改 `observe()` 内部**——报告明写「不要仅调整 observe 内部逻辑」，而且那里没有毛病。

### Fix 2: 验收点名的三种输入 [P1]

**Files**: 测试

| 分句 | 要断言什么 |
|---|---|
| 激活后回落 | 最新价已越过激活线 → 恢复后激活标记为真 → 回落到阈值下方触发退出 |
| 旧高价不属于当前持仓 | 缓存里有一根比最新高得多的旧 bar → 恢复出的峰值**不得**是它 |
| 行情缺失 | 缓存空 → 不炸、不留下半个状态 |

**验收**（报告原文）：通过原生 API 明确读取最新 bar，或按 ts_event 选择；增加至少两根原生缓存
bar 的恢复用例，覆盖激活后回落、旧高价不属于当前持仓及行情缺失。不要仅调整 observe 内部逻辑。

## 验证清单

- [x] 失败测试先红后绿 —— 实现前 7 红 3 绿，实现后 11 绿
- [x] 审查方探针 `restart_uses_oldest_cached_bar` 不再成立（中止在它的核心断言上，但见下方说明）
- [x] 三条验收分句各有独立断言
- [x] **每一条**用例都喂两根以上，另有一条用真实 BacktestEngine 缓存
- [x] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 取最新 bar | P1 | ✅ | 2026-09-21 | LB-3 |
| 2 三种输入 | P1 | ✅ | 2026-09-21 | LB-3 验收 |

## 偏离与改进日志

### 更正：我有一条断言写错了设计，不是发现了缺陷

第一版 `test_recovery_without_any_bars_leaves_a_clean_monitor` 断言无行情时 `peak_price is None`。
它红了，但不是因为有缺陷——`TrailingStopManager.init_position` 本来就把峰值设成入场价，那是对的
基线：无行情时用入场价起算，既不是「不设」也不是「沿用别人的」。

改成断言实际契约（峰值 == 入场价、未激活）。**红的测试不等于代码有问题**，这条差点被我当成第四个
缺陷报出去。

### 探针中止的证据强度要说清楚

`restart_uses_oldest_cached_bar` 现在中止在 `:206` 的 `assert monitor.peak_price == 101`，
是它的核心缺陷断言。但**它的中止只有一半算数**：探针只把 `h.cache.bars` 换成了真实引擎的缓存，
而修复后生产代码读的是 `cache.bar` —— 探针里那个仍然是 Harness 自己的替身。所以峰值变成 105
（Harness 默认值），断言失败。

这是 C29 那种「探针的输入形态不是生产形态」。真正的证据是另外两样：

1. **上游源码**：`Cache::add_bar` 是 `push_front`（`crates/common/src/cache/mod.rs:4415`），
   NT 自己的 doc 注释写 "Index 0 is the most recent"（`:1567`、`:7816`）。
2. **`test_against_a_real_engine_cache_the_newest_bar_is_the_one_restored`**：喂两根给真实
   `BacktestEngine`，先断言缓存确实是 newest-first，再让恢复走**那个**缓存（`bars` 与 `bar`
   两个入口都指向真货，Harness 的列表答不了任何问题）。把实现改回 `bars[-1]` 它就转红。

### IMPROVEMENT: Harness 的 cache 替身从「一根」变成「一串」

原来是 `bars=lambda _: [NS(close=Decimal("105"))]` —— 一根。**一根替身分不出两端**，这正是这个
缺陷能活下来的原因之一。现在替身持一个列表、按 newest-first 存放，并同时提供 `bars` 与 `bar`，
与真实缓存一致。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-21
- **总 Task 数**: 2
- **偏离数**: 1 更正 + 1 改进
- **验证结果**: 全部通过
- **实施 commit**: `d07c093`
- **契约影响**: `runtime_types.Cache` 协议新增 `bar(bar_type)`，与 NT 真实 cache 的既有方法对齐；
  无 wire / schema 变动。

### 红线 gate 满足度

| 红线 | code 覆盖 | runtime wire | defer | follow-up |
|---|---|---|---|---|
| 0.1 Key/KEK 不出进程 | N/A（未触及） | N/A | 无 | — |
| 0.2 执行门不绕过 | N/A（未触及） | N/A | 无 | — |
| 0.3 失联 ≠ 停止 | 重启后的追踪保护不再按陈旧价恢复——「失联后本地继续守护」要求守护状态是对的，不只是存在 | `OrderReconciler.recover_from_existing_positions` 是策略重启时实际走的恢复路径 | 无 | — |
| 0.4 Decimal money math | 价格全程 `Decimal(str(...))`，未引入 float | 同上 | 无 | — |

### 验收分句逐条对照

| 分句 | 覆盖它的测试 |
|---|---|
| 激活后回落 | `test_an_activation_the_market_already_crossed_is_restored` + `test_a_slip_after_that_activation_actually_exits` |
| 旧高价不属于当前持仓 | `test_an_old_high_does_not_become_this_position_s_peak`（缓存里放一根 140 的旧 bar，峰值必须是 103）|
| 行情缺失 | `test_recovery_without_any_bars_falls_back_to_the_entry` |
| 「通过原生 API 明确读取最新 bar」 | 改用 `cache.bar`，并由 `test_against_a_real_engine_cache_...` 对真货验证 |
| 「不要仅调整 observe 内部逻辑」 | `observe()` 一行未动（`git diff` 只触及调用方与协议）|

另加两条对照：没到激活线时不得凭空激活；恢复的其余部分（入场价、方向）照常完成。

### 测试条数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/toolkit/test_recovery_reads_the_latest_bar.py` | 11 |
| `tests/test_plan_closeout_counts.py` | 67 |

`_strategy_harness.py` 是 fixture 不是测试文件，不计数；`test_strategy_state_and_order_protection.py`(70)
与 `test_tick_monitor.py`(54) 本轮未改动，不重数。

### 扰动验证

| 扰动 | 结果 |
|---|---|
| 回到 `bars[-1]`（原样缺陷） | 8 红，含真引擎那条 |
| 完全不喂价格（`observe` 从不被调） | 7 红 |
| 还原 | 11 绿 |

每次扰动都用独立的 `PYTHONPYCACHEPREFIX`。

### 功能验证（主路径）

1. sandbox 下开一个 TICK 模式的多头，配 trailing（激活 2% / 回撤 1%），让价格涨过激活线。
2. 重启策略进程，等待行情缓存回填。
3. 让价格从高点回落超过 1%。应当触发 trailing 退出。修复前这里会用缓存里最旧的一根恢复，
   激活标记没置上，退出不触发。
