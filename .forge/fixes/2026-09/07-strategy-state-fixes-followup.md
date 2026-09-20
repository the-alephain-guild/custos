# 07 - strategy-state-fixes-followup

> **Status**: ⏳ In Progress
> **Created**: 2026-09-20
> **Project**: custos
> **Plan**: `.forge/fixes/2026-09/06-strategy-state-and-order-protection-fixes.md`
> **Source**: `.forge/reviews/2026-09/06-strategy-state-and-order-protection-review.md` + `.forge/reviews/2026-09/taste-fix-06-strategy-state.md`

## 修复来源

- 计划文件: `.forge/fixes/2026-09/06-strategy-state-and-order-protection-fixes.md`
- 代码审计: `.forge/reviews/2026-09/06-strategy-state-and-order-protection-review.md`（1 CRITICAL / 1 HIGH / 2 MEDIUM / 1 LOW）
- 品味审计: `.forge/reviews/2026-09/taste-fix-06-strategy-state.md`（2 🟡 / 3 ⬆️）
- 分诊: C1→P0、H1+M1→P1、T1/M2→P2；上界三项与既有 getattr 债务登记但不动

## 修复任务

### Fix 1: 补登记 fix 06 的计划外新增文件 [P0]

**Root Cause**: 计划不完整。Fix 5 的 Files 只列了四个既有文件，而取消与拒单是两个协调器上的两个回调，共用结算逻辑需要一个单一地址，实施时新建了 `entry_reservation.py`。文件本身正当，缺的是登记。

**Files**: `.forge/fixes/2026-09/06-strategy-state-and-order-protection-fixes.md`（仅文档）

1. 补进 Fix 5 的 Files。
2. 偏离日志登记第三条，写明为何需要独立模块而非塞进任一协调器。

### Fix 2: `observe()` 保留 trailing 激活，并补上让它守住的测试 [P1]

**Root Cause**: 实现错误。`observe()` 的设计意图是「不消费」，实施时把 `_trailing_manager.check()` 一并去掉了。但激活标记不是配额——与 scaled 层级不同，它不代表一次退出机会，丢弃其返回动作没有代价，丢掉标记本身却让重启后的 trailing stop 失效。

**Files**: `tick_monitor.py`、`tests/toolkit/test_strategy_state_and_order_protection.py`

1. 先写失败测试：恢复时价格已过激活线（bars 收 105，入场 100，激活线 2%），随后跌回激活线以下（101.5），必须退出。
2. `observe()` 对 trailing 走完整 `_check_trailing_tp` 并显式丢弃动作；scaled 仍不触碰——那才是有消费语义的部分。
3. 扰动验证：改回只调 `update_peak`，该测试须转红。

### Fix 3: 清掉 `_tiers` 的死值 [P2]

**Root Cause**: 实现错误。ST-8 把额度计算改走 Decimal 路径后，`_tiers` 的值不再被任何代码读取，只剩 keys 被权重报告使用，而 `_redivide_implicit_tiers` 仍在写入 `share`。注释解释了它为何存在，但注释养不活一个死值。

**Files**: `capital_allocator.py`

1. 删除 `_tiers` 字段与 `_redivide_implicit_tiers`。
2. 权重报告改遍历「显式 tier + 隐式 pair」的并集。
3. 既有 291 行 allocator 测试须全绿——本 Fix 不改变任何对外行为。

### Fix 4: 固定「撤单持续被拒」的取舍 [P2]

**Root Cause**: 测试缺失。Fix 2（ST-4）让入场在撤单未确认时让步到下一 bar，这是有意的保守取舍：旧单仍在交易所，停止新风险是正确方向。但取舍没有测试写下来，下一个读者读不出它是有意的，可能当成缺陷「修掉」。

**Files**: `tests/toolkit/test_strategy_state_and_order_protection.py`

1. 补一条：撤单连续被拒时入场持续让步，且旧单归属始终不丢。

## 本轮明确不修

| 项 | 理由 |
|---|---|
| 品味 T2（getattr ×6）| 全部先于 fix 06 存在（`order_reconciler.py:483,484`、`trade_event_handler.py:163`、`orders.py:434,438,753`）。夹带既有债务会让本轮 diff 失去焦点。下次触碰这些文件时再改 |
| 品味 T3（`tick_monitor.py` 687 行）| 上界。待审的是层级「状态 + 数量账本」是否应成为独立值对象，判据「抽出后特殊情况是否消失」。为凑行数搬类对品味贡献为负 |
| 品味 T4（`order_reconciler.py` 637 行）| 上界。待审拒单处置是否属独立的「venue 回报分诊」概念 |
| 品味 T5（`execute_entry_for_pair` 165 行）| 上界。待审入场是否应拆成「决定下多少」与「把这一单落地」。禁按行数切 |
| 审计 L1（plan Files 不含测试文件）| 流程问题，交 lessons |

> 上界三项须数据结构先行：摊开状态模型 → 按**职责**定缝 → 论证消除了哪些特殊情况。判据是「特殊情况是否消失 / 数据模型是否变简单」，不是行数。需独立 plan，不在本轮。

## 验证清单

- [ ] Fix 2 的失败测试先红后绿，且经扰动验证
- [ ] `make verify` 与 `make verify-nt` 均 exit 0
- [ ] 既有 allocator 测试全绿（Fix 3 不得改变对外行为）
- [ ] 无真实账户、下单、容器或生产操作

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 补登记新增文件 | P0 | 🔲 | | 仅文档 |
| 2 observe 保留激活 | P1 | 🔲 | | 含审计 M1 的测试覆盖 |
| 3 清死值 | P2 | 🔲 | | |
| 4 固定让步取舍 | P2 | 🔲 | | 仅测试 |

## 偏离与改进日志
