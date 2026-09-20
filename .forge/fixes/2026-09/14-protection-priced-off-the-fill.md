# 14 - protection-priced-off-the-fill

> **Status**: ✅ Completed
> **Completed**: 2026-09-20
> **Created**: 2026-09-20
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-risk-state-deep-review.md` RS-3

## 根因

入场时把 `bar.close` 写进 `position_tracker.first_entry_price`——那是**信号参考价**，不是持仓成本。
成交后没有人校正它，而 EXCHANGE 的止损、止盈和保本都从它取价。TICK 那条路用的是 `last_px`，
于是同一笔成交在两种模式下用了不同的基准。

**复现**（审查方）：`bar.close=100`，买入限价偏移 10%，实际成交在 90，固定 2% 止损。真实计算器
给出的卖出止损是 **98**——已经在现价之上，场所会立刻触发或拒绝；按成交价应当是 **88.2**。

## 修复任务

### Fix 1: 保护按实际持仓成本定价 [P1]

**Files**: `coordinators/trade_event_handler.py`、测试

1. 先写失败测试：限价偏移成交（信号 100、成交 90）后，EXCHANGE 止损必须按 90 计算；
   再覆盖价格改善、滑点、多批成交。
2. 成交回报到达时用场所报告的持仓均价（`position.avg_px_open`）校正
   `position_tracker` 的入场价。它天然是加权平均，多批成交与部分成交都不用另算。
3. 保护提交仍读 `first_entry_price`——校正之后，它就是持仓成本，EXCHANGE 与 TICK 两条路因此
   对齐到同一个基准。

**验收**（报告原文）：区分信号参考价、委托价和实际持仓成本，保护以明确定义的成交基准计算；
测试限价偏移、价格改善、滑点与多批成交。不要只用 signal/bar/fill 三者相同的样例。

## 验证清单

- [x] 失败测试先红后绿，经扰动验证
- [x] 审查方探针 `limit_fill_stop_uses_signal_price` 不再成立
- [x] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 保护按成交价定价 | P1 | ✅ | 2026-09-20 | RS-3，含读不出均价时的兜底 |

## 偏离与改进日志

### IMPROVEMENT: 读不出均价时不中断成交处理
- **原因**: 实现后两个既有 native-trailing 测试转红，原因是它们的 position 替身是 `MagicMock`，
  `Decimal(str(avg_px_open))` 抛 `InvalidOperation`。替身不真实，但它暴露了一个真实的形状：
  **价格解析失败会把整个成交处理带走**，而外层回调的 `except` 会把它吞掉——成交是真的，仓位却
  没有保护。
- **决定**: 读不出均价时记 error 并保留参考价，继续把保护建起来。宁可用一个不够准的基准，也不能
  因为读价失败而让仓位裸着。
- **与 C9 同源**: 防线不能只守着「算得出」的那条路。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-20
- **总 Task 数**: 1（+1 项兜底改进）
- **偏离数**: 0
- **验证结果**: 全部通过
- **实施 commit 范围**: `012ad03`（plan 自身在其前一 commit）
- **契约影响**: `PositionTracker` 新增 `correct_entry_price`。无跨仓契约变化，`make check-authority` 通过。
- **红线守护**: 四条红线全数守住。本项修的是保护单的定价基准。

### 用审查方自己的探针验收

| 探针 | 结果 |
|---|---|
| `limit_fill_stop_uses_signal_price`（RS-3）| 断言失败 → 缺陷不再成立 |

### 测试计数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/toolkit/test_strategy_state_and_order_protection.py` | 70 |
| `tests/toolkit/test_native_trailing_mode.py` | 35 |
| `tests/test_plan_closeout_counts.py` | 49 |

上表合计 154 条。第一行 fix 13 时是 66；第二行本轮因兜底改进而行为改变，按规则重新计数认领；
第三行由本 plan 的表格从 47 推到 49。

### 扰动验证

去掉入场价校正：4 条新测试全部转红。还原后全绿。

### 功能验证（主路径）

1. 配置一个带限价偏移的入场（如低于信号价 10% 挂单）与固定百分比止损，让它在偏移价成交。
2. 预期：止损按**成交价**计算并落在多头入场价之下。修复前按信号 K 线的收盘价算，止损会落在现价
   之上，场所会立即触发或拒绝。
3. 用会分批成交的订单重复一次，确认止损基准是场所报告的加权均价。
