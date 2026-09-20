# 15 - risk-state-survives-restart

> **Status**: ⏳ In Progress
> **Created**: 2026-09-20
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-risk-state-deep-review.md` RS-6（**B 半**）

## 本 plan 的范围，以及为什么只做一半

RS-6 点了两处同型缺口：

- **A：daemon 重启清除熔断器冻结**（`_daemon.py` / `fallback_breaker.py`）。
- **B：Toolkit 的 RiskController 状态不进快照**（`coordinators/snapshot.py` / `trading_strategy.py`）。

本 plan 只做 **B**。A 不是机械修复：它要先定下熔断冻结的**作用域**（per deployment instance 还是
per runner）与**解除条件**——审查方的验收明写要区分「崩溃恢复、正常重启、热更新、跨日恢复和显式
解除」，那是策略决定，不是实现细节。它还落在 non-custodial 红线 0.3 的区域，按
`deviation-protocol.md` 属高风险偏离，需要先有决定再动手。

实现路径已探明，留给 A 的 plan：store 里已有 `runner_risk_latch`（C23 的持久化风险闩），
`RunnerStateStore._latch_runner_risk` / `_require_runner_risk_unlatched` 可直接作模板。

## 修复任务

### Fix 1: 风控状态随快照存取 [P1 / RS-6 B]

**Root Cause**: 实现缺失。`RiskController` 的日损益、交易计数、连亏计数、暂停截止时间、权益峰值
和日界都只在内存里。快照的全局段来自 `get_snapshot_state()`——那是给策略覆盖用的 hook，默认返回
空 dict，所以正常的 on_save/on_load 根本不经过风控状态。

**复现**（审查方）：策略已因日亏损禁止交易，执行真实 SnapshotCoordinator 保存/加载，同日恢复后
又允许交易——当天剩余的亏损额度凭空回满。

**Files**: `custos_toolkit/risk/controller.py`、`adapter/state_persistence.py`、
`adapter/coordinators/snapshot.py`、`adapter/trading_strategy.py`、测试

1. 先写失败测试：因日亏损被拒 → 保存 → 加载 → 同日仍须被拒；跨日恢复则应放行。
2. `RiskController` 加 `export_state()` / `restore_state()`，覆盖日损益、交易数、连亏、
   `paused_until`、`peak_equity`、`next_reset_ns`。
3. 快照里给风控一个**独立的段**，不走 `get_snapshot_state()`——那个 hook 属于策略作者，
   覆盖它不该把风控状态一起带走（审查方点名「独立于『加速指标预热』配置持久化」）。
4. 恢复顺序：风控状态要在准入判断之前就位。

**验收**（报告原文，B 半）：由明确所有者持久化锁定状态；恢复准入前加载风控状态。策略风控也要
独立于「加速指标预热」配置持久化。

## 验证清单

- [ ] 失败测试先红后绿，经扰动验证
- [ ] 审查方探针 `restart_resets_risk_budget` 不再成立
- [ ] 既有快照测试仍绿
- [ ] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 风控状态入快照 | P1 | 🔲 | | RS-6 B |

## 移交下一轮

| 项 | 需要先定的事 |
|---|---|
| RS-6 A：熔断冻结跨重启 | 冻结作用域（instance / runner）、解除条件与解除者、与现有 `runner_risk_latch` 的关系 |

## 偏离与改进日志
