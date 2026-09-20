# 15 - risk-state-survives-restart

> **Status**: ✅ Completed
> **Completed**: 2026-09-20
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

- [x] 失败测试先红后绿（6 红 → 8 绿）
- [x] 审查方探针 `restart_resets_risk_budget` 不再成立
- [x] 既有快照测试仍绿（on_save/on_load、warmup snapshot、warmup on_load 共 24 项）
- [x] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 风控状态入快照 | P1 | ✅ | 2026-09-20 | RS-6 B，两条恢复路径 |

## 移交下一轮

| 项 | 需要先定的事 |
|---|---|
| RS-6 A：熔断冻结跨重启 | 冻结作用域（instance / runner）、解除条件与解除者、与现有 `runner_risk_latch` 的关系 |

## 偏离与改进日志

### DEVIATION: 恢复点落在两处，不是一处
- **等级**: 低
- **原因**: 真实顺序是 `on_load`（kernel build，stash 快照）→ `on_start`（建 controller）。所以
  恢复该在建 controller 时做——那条路每次启动都走，且**不受 warmup mode 限制**。但审查方的探针
  是先建 controller、再 `load_state`，顺序相反，恢复点就被跳过了。
- **决定**: 两处都恢复。`restore_state` 是纯赋值，幂等；`on_start` 期间不会有成交，所以第二次
  恢复不会覆盖掉新记的账。
- **判据**: 主恢复点是建 controller 时（与 warmup 配置无关，满足验收）；快照应用路径上的那次是
  补齐顺序颠倒的情形。

### DEVIATION: 一次正则插入落到了错误的类里
- **等级**: 低（当场发现，未进入任何 commit）
- **原因**: 给测试替身补属性时用了 `class _FakeStrategy.*?def __init__` 配 `re.S`，`.*?` 跨过整个
  文件匹配到了两百行之外的 `_MockSnapshotIndicator.__init__`。测试照旧红，因为真正的目标没被改到。
- **决定**: 改用唯一的多行锚点定位，并把插入位置连同「真实类在哪声明它」一起写进注释。
- **与 C10 同源**: 批量替换的作用域必须是语法结构，不能靠一个会跨越结构的正则。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-20
- **总 Task 数**: 1
- **偏离数**: 2（均为实现过程中的调整）
- **验证结果**: 全部通过
- **实施 commit 范围**: `1945803`（plan 自身 `88c69a6`，早于实施）
- **契约影响**: 快照新增顶层 `risk` 段。旧快照没有该段时按空处理、走正常预热，不抛错。
  `make check-authority` 通过。
- **红线守护**: 四条红线全数守住。本项让日亏损与回撤限额跨重启继续生效，是收紧而非放松。

### 用审查方自己的探针验收

| 探针 | 结果 |
|---|---|
| `restart_resets_risk_budget`（RS-6 B）| 断言失败 → 缺陷不再成立 |
| `signed_breaker_restart_forgets_trip`（RS-6 A）| **仍然成立——本 plan 不做这一半，见下** |

### 测试计数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/toolkit/test_risk_state_survives_restart.py` | 8 |
| `tests/toolkit/test_warmup_on_load.py` | 14 |
| `tests/test_plan_closeout_counts.py` | 51 |

上表合计 73 条。第一行是本轮新建；第二行本轮补了替身属性，按规则重新计数认领；第三行由本 plan
的表格从 49 推到 51。

### 移交：RS-6 A 仍然开着

daemon 重启会清掉 `FallbackBreaker` 的冻结与权益峰值，这违反熔断器「冻结直到人工干预」的契约。
**本 plan 没有碰它**，因为它需要先定两件事：

1. **冻结的作用域**：现在 breaker 挂在 per-deployment-instance 的内存 registry 上，而 store 里
   已有的 `runner_risk_latch` 是 per (tenant_scope, trading_mode, runner_id)。两者选哪个，决定了
   重启后「谁被冻住」。
2. **解除条件与解除者**：审查方的验收要求区分「崩溃恢复、正常重启、热更新、跨日恢复和**显式
   解除**」。谁有权解除、凭什么解除，是策略决定。

它还落在 non-custodial 红线 0.3 的区域，按 `deviation-protocol.md` 属高风险偏离。

实现路径已探明，留给下一轮：`RunnerStateStore._latch_runner_risk` 与
`_require_runner_risk_unlatched`（`src/custos/core/runner_fact.py:5011` 与 `:4991`）是现成的模板，
表结构见 `:1603`。

### 功能验证（主路径）

1. 让策略触发日亏损限额而停止入场，然后重启进程（触发 on_save / on_load）。
2. 预期：同一交易日内恢复后仍然拒绝入场，日志出现 `Risk state restored from snapshot`。
   修复前恢复后额度回满。
3. 跨过 UTC 日界再检查一次，预期正常放行——carry 的是当天的账，不是永久禁令。
