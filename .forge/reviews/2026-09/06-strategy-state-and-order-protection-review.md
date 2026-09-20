# 审计报告: Fix 06 — 策略内部状态与订单保护

> **审计日期**: 2026-09-20
> **计划文件**: `.forge/fixes/2026-09/06-strategy-state-and-order-protection-fixes.md`
> **审计员**: Claude Code（**与实施方同一执行面，不构成独立保证**）
> **审计基线**: `efd1efd`..`7148595`

## Executive Summary

八项 finding 全部实现，验收逐条对应，`make verify` 与 `make verify-nt` 均 exit 0。审计发现**一项实施引入的行为回归**：Fix 3 把恢复路径从 `check()` 换成新增的 `observe()` 时，保留了峰值更新却丢掉了 trailing 的激活状态更新，导致重启后 trailing stop 在特定价格路径下不触发。该回归已由探针实证，不是推理。另有一处计划外新增文件未登记。

需要记下审计本身的局限：本轮修复由我实施，本报告也由我出具。lesson #48 的要害正是「实证者不能是断言者本人」——下面这项 HIGH 是自查出来的，但没有任何机制保证查全了。

## 整体匹配率: 96%

## 严重度分布

| 严重度 | 数量 |
|--------|------|
| 🔴 CRITICAL | 1 |
| 🟠 HIGH | 1 |
| 🟡 MEDIUM | 2 |
| 🔵 LOW | 1 |

## 问题列表

### 🔴 CRITICAL

#### C1: 计划外新增文件未登记

- **文件**: `adapter/coordinators/entry_reservation.py`（新建，53 行）
- **计划定义**: Fix 5 的 `**Files**` 声明为 `orders.py`、`signal_execution.py`、`trade_event_handler.py`、`order_reconciler.py`，不含新文件
- **实际代码**: 新增模块承载 `release_unfilled_entry()`，被取消与拒单两条终结路径共用
- **影响**: 实现本身合理——取消与拒单是两个协调器上的两个回调，共用结算逻辑需要单一地址。但它是未标注偏离。按审计 5 的判据，未标注的偏离一律 CRITICAL，与偏离是否合理无关。这是文档缺口而非实现缺陷，修复方式是补登记，不是改代码。

### 🟠 HIGH

#### H1: `observe()` 丢失 trailing 激活状态，重启后 trailing stop 可能不触发

- **文件**: `adapter/tick_monitor.py:431-442`（`observe`）；对照 `:595-604`（`_check_trailing_tp`）
- **计划定义**: Fix 3 第 3 条——「新增 `observe(price)`：只更新 trailing peak 等非消费状态，供恢复路径读取行情，不返回动作、不推进层级」
- **实际代码**: `observe()` 只调用 `_trailing_manager.update_peak()`。而原恢复路径调用的 `check()` 对 trailing 走 `_check_trailing_tp`，后者除 `update_peak` 外还调用 `_trailing_manager.check()`，其内部在 `:206-209` 设置 `_activated`
- **实测**（entry 100，activation 2%，trailing 1%）:

  | 恢复调用 | 恢复价 105（+5%，过激活线） | 随后 101.5（+1.5%，低于激活线） |
  |---|---|---|
  | `check()` | activated=True, peak=105 | **触发退出** |
  | `observe()` | activated=False, peak=105 | **不触发** |

- **影响**: 激活标记是**非消费性**状态——它不代表一次退出配额，丢弃其返回动作没有代价。把它一并去掉是过度收窄。重启时行情在高位、随后跌回激活线以下，trailing stop 该触发却不触发，利润回吐。这与本轮所修的 ST-2/3/4 同类：保护性退出失效。
- **修复方向**: `observe()` 对 trailing 走完整 `_check_trailing_tp` 并显式丢弃返回的动作；scaled 仍不触碰——那才是有消费语义的部分。

### 🟡 MEDIUM

#### M1: trailing 恢复路径无测试覆盖

- **文件**: `tests/toolkit/test_strategy_state_and_order_protection.py`
- **问题**: `test_recovery_reads_the_market_without_spending_a_level` 只覆盖 scaled。H1 正是从这个缺口逃逸的——37 项新测试与 2666 项全量测试都没有让它变红。
- **影响**: 修 H1 时必须同时补这条覆盖，否则同一形态会再次逃逸。

#### M2: 撤单持续被拒的路径无测试覆盖

- **文件**: `adapter/coordinators/signal_execution.py:103-113`
- **问题**: Fix 2 让入场在撤单未确认时让步到下一 bar。测试覆盖了「让步」与「确认后恢复入场」，但没有覆盖「撤单被持续拒绝」——那种情况下新入场会一直被推迟。
- **影响**: 这是有意的保守取舍（旧单仍在交易所，停止新风险是正确方向），但取舍没有被测试固定下来，下一个读者读不出它是有意的。

### 🔵 LOW

#### L1: 计划的文件清单不含测试文件

- **文件**: 计划各 Fix 的 `**Files**` 段
- **问题**: 八个 Fix 的 Files 只列源码，实际新增/修改 4 个测试文件。每个 Fix 的步骤 1 都写了「写失败测试」，但没有对应的文件清单。
- **影响**: 文件清单审计无法机械核对测试面。

## 正向偏离（改进）

| # | 位置 | 描述 | 理由 |
|---|---|---|---|
| I1 | `capital_allocator.py:86-98` | 额度计算由 float 比率改为 Decimal 算术 | 修 ST-8 时发现 `1/3 × 300 = 99.9999…`。额度是金额，属红线 0.4 范围，顺带收紧 |
| I2 | `runtime_types.py:108-113` | 新增共享 `Indicator` 协议 | 消除 `signal_execution` 里的私有副本，两处读指标走同一契约。已在偏离日志登记 |
| I3 | 全部八项修复 | 11 个扰动点逐一反向验证 | 确认测试会咬，而非靠别的原因绿 |

## 逐 Task 匹配率

| Task | 匹配率 | 关键偏离 |
|------|--------|---------|
| Fix 1 (ST-3) ATR 修复 | 100% | — |
| Fix 2 (ST-4) 撤单归属 | 100% | M2 覆盖缺口 |
| Fix 3 (ST-2) 层级回报推进 | 90% | **H1 trailing 激活回归** |
| Fix 4 (ST-5) 额度拒绝 | 100% | — |
| Fix 5 (ST-6) 预留回滚 | 95% | C1 新文件未登记 |
| Fix 6 (ST-1) 退出基数 | 100% | — |
| Fix 7 (ST-7) 保本止损归属 | 100% | — |
| Fix 8 (ST-8) 均分分配 | 100% | I1 正向 |

## 优先修复建议

1. **H1** — 立即修。保护性退出失效，与本轮所修问题同类。连同 M1 的覆盖一起做，否则修了也守不住。
2. **C1** — 同批补登记。改文档，不改代码。
3. **M2** — 补一条测试把取舍固定下来。
4. **L1** — 下次起 plan 时把测试文件纳入 Files。

## 审计方法

- 文件清单：`git diff --name-only efd1efd..HEAD` 与计划八处 `**Files**` 声明逐项对照
- 签名：对每个改过签名的符号 grep 全部调用点（`confirm_level_order`、`release_level_order`、`set_entry_order`、`init_position`、`submit_stop_loss`、`_cancel_pending_entry_order`、`planned_exit_quantity`）
- 行为：H1 由独立探针实测，两条路径的 `_activated` 与退出判定逐一对照，非读码推断
- 偏离：plan 首个 commit `efd1efd` 早于全部实施 commit（`1151889` 起），符合规则 6
