# 11 - close-cleanup-respects-what-survives

> **Status**: ⏳ In Progress
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

- [ ] 两项失败测试先红后绿，经扰动验证
- [ ] 审查方探针 `native_reversal_resets_new_tick_protection` / `partial_entry_survives_close_without_ownership` 不再成立
- [ ] 既有反向与平仓测试仍绿
- [ ] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 新仓存在时不重置 | P1 | 🔲 | | RS-7 反向分支 |
| 2 live 入场单保留归属 | P1 | 🔲 | | RS-7 部分入场分支 |

## 偏离与改进日志
