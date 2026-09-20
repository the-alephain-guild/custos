# 11 - close-cleanup-respects-what-survives

> **Status**: ✅ Completed
> **Completed**: 2026-09-20
> **Created**: 2026-09-20
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-risk-state-deep-review.md` RS-7

## 根因一句话

一笔仓位平掉时，清理动作按「这个 pair 现在什么都没有了」执行。两种情况下这不成立：净额反向时
新仓已经开出来了，部分入场被止损平掉时入场单还在场所挂着。清理照样 reset，于是刚建好的保护被
抹掉、仍会成交的入场单失去归属。

## 修复任务

### Fix 1: 新仓已经存在时不得重置监控状态 [P1]

**Root Cause**: 实现错误。净额反向的真实回调顺序是 `OrderFilled`（缓存已持有新空仓）→ 旧
`PositionClosed` → 新 `PositionOpened`。第一个回调已经把 tick 保护按新仓初始化，紧接着的旧
`PositionClosed` 无条件 `position_tracker.reset()` + `tick_monitor.reset()` 把它清空。没有通用的
`PositionOpened` 回调会重建，于是新空仓裸着——价格跌到应止盈的位置，monitor 返回 None。

**Files**: `coordinators/trade_event_handler.py`、测试

1. 先写失败测试：旧仓 PositionClosed 到达时缓存已持有新的反向仓位，tick monitor 必须仍然活着，
   且随后到达止盈价时给出退出动作。
2. 清理前查缓存：该 instrument 还有开放仓位时，保留 tick monitor 与 position tracker 的状态——
   它们描述的是**现在持有的东西**，不是刚结束的那一笔。
3. 需要重置的部分（close gate、close-reject 计数、break-even 标志）与需要保留的部分分开。

### Fix 2: 入场单还可能成交时不得丢掉它的归属 [P1]

**Root Cause**: 实现错误。既有的 `preserve_entry` 只在反向路径生效（判据是
`sl_tp_submitted_for_reversal or pending_entry_is_reversal`）。一笔普通入场单成交一半、这半仓被
止损平掉时走的是 `else` 分支：`cancel_sl_tp_orders(ctx)` 内部 `clear()` 抹掉 entry id 与 pending
signal，而那张入场单**仍在场所挂着**，也没有被撤销。它后来成交时不再匹配 tracked entry，
`handle_order_filled` 提前返回，不建任何保护。

**Files**: `coordinators/trade_event_handler.py`、测试

1. 先写失败测试：入场成交 0.5、该仓被止损平掉、剩余 0.5 随后成交——必须建立保护。
2. 判据从「这是反向吗」改成「入场单是否仍然可能成交」：问缓存要那张单，它还 open 就保留归属。
3. 反向路径的既有行为不变（它本就是这条判据的一个特例）。

**验收**（报告原文）：按仓位生命周期及订单归属清理；旧仓终结不得 reset 新仓，也不能遗忘仍可能
成交的入场单。覆盖一笔跨零反转、多笔部分反转、部分入场后先止损及迟到回报。

## 验证清单

- [x] 两项失败测试先红后绿，经扰动验证
- [x] 审查方探针 `native_reversal_resets_new_tick_protection` / `partial_entry_survives_close_without_ownership` 不再成立
- [x] 既有反向与平仓测试仍绿（替身补全后 12 项全过）
- [x] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 新仓存在时不重置 | P1 | ✅ | 2026-09-20 | RS-7 反向分支 |
| 2 live 入场单保留归属 | P1 | ✅ | 2026-09-20 | RS-7 部分入场分支 |

## 偏离与改进日志

### DEVIATION: 入场单仍 live 时连 first_entry_price 一并保留
- **等级**: 低（范围内的必要延伸）
- **原因**: 只保留 entry id 与 pending signal 不够。`submit_stop_loss` 用
  `position_tracker.first_entry_price` 定价，而 `reset()` 把它归零——剩余那笔成交进来时保护仍然
  发不出去，测试卡在最后一条。
- **决定**: 入场单仍可能成交时不 reset position tracker。那张单成交后会形成持仓，记录在语义上是
  连续的；把它清零等于让后半段入场失去定价基准。

### DEVIATION: 三个既有测试的替身需要补全
- **等级**: 低
- **原因**: 新的清理逻辑要查两次缓存（是否还有持仓、入场单是什么状态）。`test_trade_event_handler.py`
  的三个 ctx 替身没有 `instrument_id`，`cache` 是裸 `MagicMock`——后者对任何查询都返回真值，会告诉
  清理逻辑「有持仓活着、入场单还在」，与这些测试想表达的「这次平仓之后什么都不剩」相反。
- **决定**: 替身补上 `instrument_id`，`cache` 明确返回空持仓与 `None` 订单。三项断言本身一字未改。
- **注意**: 裸 `MagicMock` 作缓存替身是个陷阱——它不会报错，只会对每个新查询给出「有」，让测试悄悄
  测了另一种情形。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-20
- **总 Task 数**: 2
- **偏离数**: 2（均为范围内的必要延伸）
- **验证结果**: 全部通过
- **实施 commit 范围**: `4b0fc09`（plan 自身 `305a265`，早于实施）
- **契约影响**: 无。`make check-authority` 通过。
- **红线守护**: 四条红线全数守住。两项都在恢复保护的连续性。

### 用审查方自己的探针验收

| 探针 | 结果 |
|---|---|
| `native_reversal_resets_new_tick_protection`（RS-7a，**真实 Nautilus BacktestEngine**）| 断言失败 → 缺陷不再成立 |
| `partial_entry_survives_close_without_ownership`（RS-7b）| 断言失败 → 缺陷不再成立 |

RS-7a 用的是真实回测引擎与真实市场单，不是受控替身——它复现的是原生的回调顺序。

### 测试计数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/toolkit/test_strategy_state_and_order_protection.py` | 66 |
| `tests/toolkit/test_trade_event_handler.py` | 12 |
| `tests/test_plan_closeout_counts.py` | 43 |

上表合计 121 条。第一行 fix 10 认领时是 62；第二行本轮补了替身，按规则重新计数认领；第三行由本
plan 的表格从 41 推到 43。

### 扰动验证

把两处条件改回无条件重置（`if ctx.tick_monitor:` 与 `if True:`）：4 条新测试中 3 条转红。
还原后全绿。

### 功能验证（主路径）

1. 净额账户下持有多仓，发一个足以跨零的反向信号（卖出量大于持仓）。
2. 预期：反向后的新空仓仍然带着 tick 保护——价格走到它的止盈位时会退出。修复前那个监控在旧仓
   的平仓回调里被清空，新仓走到止盈价也没有动作。
3. 用一张会分批成交的限价入场单建仓，让第一批成交的部分先被止损平掉，剩余部分随后成交。预期
   剩余部分建立保护，而不是作为「外部成交」被忽略。
