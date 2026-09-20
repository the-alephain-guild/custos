# 14 - protection-priced-off-the-fill

> **Status**: ⏳ In Progress
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

- [ ] 失败测试先红后绿，经扰动验证
- [ ] 审查方探针 `limit_fill_stop_uses_signal_price` 不再成立
- [ ] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 保护按成交价定价 | P1 | 🔲 | | RS-3 |

## 偏离与改进日志
