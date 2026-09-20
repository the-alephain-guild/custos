# 18 - a-price-improvement-must-not-keep-the-reservation

> **Status**: ✅ Completed
> **Created**: 2026-09-20
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-execution-edge-deep-review.md` EE-6

## 根因

下单时按估价预留金额，成交后按**金额**收回：`src/custos/core/runner_fact.py` 的

```python
remaining = max(reserved - fill, Decimal("0"))
```

这条式子把「预留了 100、成交了 90」读成「还有 10 块钱的量等着成交」。但整笔数量已经全部成交，
那 10 块不对应任何未成交数量——它是价格改善省下来的差额。

差额之后没有任何路径会释放它：全成路径不会再来 cancel 或 reject，而平仓只减 `filled_exposure`，
不动 `reserved_notional`。于是每笔价格改善都在上限里留下一块永久占用。

**复现**（审查方，真实 SQLite 状态库）：单笔上限 100、总上限 150。六次「预留 100 → 数量 1 以 90
全部成交 → 数量 1 全部平仓」，每次遗留 10。最终 `open_exposure=0` 而 `reserved_notional=60`；下一
笔正常的 100 订单因 160 > 150 被拒。此时既没有持仓，也没有待成交数量。

## 为什么不改 schema

`order_reservation` 没有「预留对应多少数量」这一列，而本仓的状态库**没有迁移机制**（建表一律
`CREATE TABLE IF NOT EXISTS`，加列对已存在的库不生效）。`authority-docs.md` 也明写这套存储契约
「no spec-keyed stream, cutover table, migration API or compatibility parser exists」。

不加列也能算对，因为这一笔成交自己就带着答案：成交**前**这张单还剩 `leaves_after + last_qty` 个
单位没成交，`reserved_before` 就是为这些单位留的钱；成交**后**只剩 `leaves_after` 个单位。按数量
等比缩放即可：

```
remaining = reserved_before * leaves_after / (leaves_after + this_fill_quantity)
```

`leaves_after == 0` 时 remaining 自然是 0——价格改善的差额在最后一笔成交上一次性还清。

`leaves_qty` 取自 Nautilus 缓存里的订单。引擎在发布 `OrderFilled` **之前**就更新了缓存
（`crates/execution/src/engine/mod.rs:2913-2923`：`update_cached_order(...)` 在
`publish_order_event(&event)` 之前），所以回调读到的是这笔成交之后的剩余数量。

## 修复任务

### Fix 1: 预留按未成交数量收回 [P2]

**Files**: `src/custos/core/runner_fact.py`、`src/custos/core/order_reservation_boundary.py`、
`src/custos/engines/nautilus/runner_safety.py`、测试

1. 先写失败测试：整单价格改善之后 `reserved_notional` 必须是 0；多价格分批成交每笔按数量等比收回。
2. `OrderSemantics` 增加 `fill_leaves_quantity(event)`，NT 实现读缓存订单的 `leaves_qty`；
   拿不到订单时返回 `None`。
3. 边界把它透传给 `record_order_fill_sync`。
4. store 收到 `leaves_quantity` 时按数量等比缩放；`None` 时退回原来的金额差（保守多留，不会少留），
   并在 docstring 里写明这是降级路径。

### Fix 2: 终态清零与上限恢复 [P2]

**Files**: 测试

审查方的验收点名四种输入：多价格分批成交、整单价格改善、取消剩余、最终平仓后无活跃订单时预留归零。
第三、四两种走的是已有的 `release_order_reservation_sync` 与 FIFO 平仓路径，需要的是**端到端**
断言：跑完整条生命周期后，总上限必须完全恢复，下一笔满额订单要能过。

**验收**（报告原文）：预留对应未成交数量；终态必须清除未使用的价差预留。验证多价格分批成交、
整单价格改善、取消剩余，以及最终平仓后无活跃订单时预留归零。

## 验证清单

- [x] 失败测试先红后绿 —— 实现前 4 红 2 绿（2 绿是降级路径与上限守卫的回归保护），实现后 7 绿
- [ ] 审查方探针 `improved_fill_price_leaks_runner_reservations` **仍然成立** —— 见下方「探针为什么还绿」
- [x] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 预留按数量收回 | P2 | ✅ | 2026-09-20 | EE-6 |
| 2 终态清零端到端 | P2 | ✅ | 2026-09-20 | EE-6 验收四输入 |

## 偏离与改进日志

### DEVIATION: 不报告未成交数量的调用方仍然会多留，只是不再沉默

- **等级**: 低
- **原因**: 「已全部成交」这个终态在持久层没有独立的事实可查。取消、拒绝、过期都有各自的事件会
  调 `release_order_reservation_sync`；全成没有后续事件，只有「这张单还剩多少没成交」能把它和
  「部分成交」区分开。调用方不报这个数量时，存储层**无法**判定终态——不是没做，是做不到。
- **影响**: `record_order_fill_sync` 的 `leaves_quantity` 保持可选。省略它时仍按金额差收回
  （多留，不会少留），价差会一直占着上限直到该单被取消或拒绝。
- **决定**: 生产路径必然会报——`NautilusCachedOrderSemantics.fill_leaves_quantity` 读缓存订单的
  `leaves_qty`，而引擎在发布 `OrderFilled` 之前就更新了缓存。只有订单不在缓存里才返回 `None`，
  这对一笔真实成交来说不该发生。为了不让它沉默，降级路径现在发
  `order_reservation_unfilled_quantity_unknown` 警告并带上被扣住的金额（零静默红线）。
- **未选方案**: 给 `order_reservation` 加一列「预留对应多少数量」。本仓状态库没有迁移机制
  （建表一律 `CREATE TABLE IF NOT EXISTS`），`authority-docs.md` 也明写这套契约不存在
  migration API；加列对已存在的库不生效，且会让运行中的 runner 在第一笔订单上 fail closed。

### 探针为什么还绿

审查方的 `improved_fill_price_leaks_runner_reservations` 直接调 `record_order_fill_sync` 且
**不传** `leaves_quantity`——也就是上面那条偏离描述的降级形态。它现在走的是「多留 + 发警告」，
所以断言 `reserved_notional == 10` 依旧成立，探针照常打印。

探针绿不等于缺陷还在。它复现的是**一个不报告未成交数量的调用方**，而修复之后生产路径不再是这种
调用方。同一段六轮序列，把未成交数量报上去之后的结果写在
`test_a_flat_account_gets_its_whole_cap_back` 里：每轮 `reserved_notional == 0`，跑完
`open_exposure == 0`、`reserved_notional == 0`，随后那笔 100 的订单在 150 上限下正常通过——正是
探针断言会被拒的那一笔。

真正把生产接线钉住的是 `test_the_boundary_reports_the_unfilled_quantity_to_the_store`：它走真实
`RunnerReservationBoundary` + 真实 `RunnerStateStore`，把边界那行
`leaves_quantity=semantics.fill_leaves_quantity(event)` 删掉即转红（已实测）。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-20
- **总 Task 数**: 2
- **偏离数**: 1（见上）
- **验证结果**: 全部通过
- **实施 commit**: `0b303eb`
- **契约影响**: `OrderSemantics` 协议新增 `fill_leaves_quantity`，两处测试替身随之补齐——这是有意
  的：协议是契约，加了方法就该让所有实现表态，用 `getattr` 兜底等于把契约变成建议。
  `record_order_fill_sync` 新增可选形参，wire 契约与 schema 均未变动。
- **红线守护**: 四条红线均未触及。

### 红线 gate 满足度

| 红线 | code 覆盖 | runtime wire | defer | follow-up |
|---|---|---|---|---|
| 0.1 Key/KEK 不出进程 | N/A（未触及） | N/A | 无 | — |
| 0.2 执行门不绕过 | 预留是执行门的额度来源；本次让它在全成后归零，额度不再被虚占 | `RunnerReservationBoundary.on_order_event` 是守护进程实际接的回调，已有端到端测试 | 无 | — |
| 0.3 失联 ≠ 停止 | N/A（未触及） | N/A | 无 | — |
| 0.4 Decimal money math | 等比缩放全程 `Decimal`，无 float 介入 | 同上 | 无 | — |

### 测试条数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/test_a_price_improvement_must_not_keep_the_reservation.py` | 7 |
| `tests/engines/nautilus/test_runner_safety_execution_boundary.py` | 38 |
| `tests/test_order_reservation.py` | 13 |
| `tests/test_plan_closeout_counts.py` | 57 |

后两行不是本次新增的测试：`test_order_reservation.py` 只补了一个测试替身的方法（条数未变，但
本份 close-out 是最新数它的一份），`test_plan_closeout_counts.py` 的增量来自本份 close-out 自己
（探针按带表格的 plan / fix 份数参数化）。

### 扰动验证

| 扰动 | 结果 |
|---|---|
| 存储层退回按金额收回 | 4 红 |
| 边界不再把未成交数量传下去 | 1 红（端到端那条） |
| Nautilus 语义把剩余数量谎报为 0 | 1 红 |
| 还原 | 45 绿 |

每次扰动都用独立的 `PYTHONPYCACHEPREFIX`。

### 功能验证（主路径）

1. 在 sandbox 下以限价单入场，让它整笔以优于报价的价格成交。
2. 查该单的 `order_reservation` 行：`reserved_notional` 应为 0、`state` 为 `filled`。
3. 平掉持仓后再下一笔接近上限的单——它应该能过。修复前这里会被「不存在的敞口」挡住。
