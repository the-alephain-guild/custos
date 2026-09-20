# 18 - a-price-improvement-must-not-keep-the-reservation

> **Status**: 🔲 Not started
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

- [ ] 失败测试先红后绿
- [ ] 审查方探针 `improved_fill_price_leaks_runner_reservations` 不再成立
- [ ] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 预留按数量收回 | P2 | 🔲 | | EE-6 |
| 2 终态清零端到端 | P2 | 🔲 | | EE-6 验收四输入 |

## 偏离与改进日志
