# 21 - decompositions-the-line-counts-were-pointing-at

> **Status**: 🔲 Not started
> **Created**: 2026-09-21
> **Project**: custos
> **Source**: `.forge/fixes/2026-09/07-strategy-state-fixes-followup.md` 品味 T2–T5（移交 backlog）

## 这份 plan 的判据

fix 07 把四项登记为「须数据结构先行」的上界，并写明判据：

> 上界三项须数据结构先行：摊开状态模型 → 按**职责**定缝 → 论证消除了哪些特殊情况。
> 判据是「特殊情况是否消失 / 数据模型是否变简单」，不是行数。

`coding-taste.md` §八 4 也明说「拒绝硬性行数指标……它会把内聚逻辑切碎成浅函数，制造新的阅读
负担」。所以下面每一项都先给出**要消失的特殊情况**，行数变化只作副产物记录，不作目标。

**这是重构，不是修缺陷。** 行为不变，所以不存在「先写一条失败测试」——没有缺陷可复现。纪律换成：
每项先确认既有覆盖能守住行为（不足就先补特征测试），再动结构，全程保持绿；对**新引入的不变量**
（超界层级、未经决策不得提交）另写测试并扰动验证。

## T3 — 分批止盈的「层级台账」是四个并行集合

**Files**: `adapter/tick_monitor.py`、新建 `adapter/scaled_exit_ladder.py`、测试

`TickMonitorManager` 用**四个按同一个下标并行的集合**记一个层级：

```python
self._tp_levels: list[_TakeProfitLevel]             # 配置：target_pct / exit_pct
self._tp_level_states: list[TakeProfitLevelState]   # armed / pending / completed
self._level_dispatched: dict[int, Decimal]          # 派发了多少
self._level_filled: dict[int, Decimal]              # 成交确认了多少
self._level_orders: dict[str, int]                  # order id -> 下标
```

**要消失的特殊情况**：

1. **每个入口自己做边界检查**。`bind_level_order` / `release_level` / `record_dispatch` /
   `planned_exit_quantity` 各写一遍 `index = level - 1` 加 `0 <= index < len(...)`，
   而且守卫的对象**不一致**——两处用 `len(self._tp_level_states)`，两处用 `len(self._tp_levels)`。
   两者今天恰好等长，没有任何东西保证它们一直等长。收成一个台账后只剩一次查找，越界返回 `None`。
2. **1-based 与 0-based 的换算散在五个方法里**。收到台账边界上做一次。
3. **`_reset_levels` 要记得重置四个集合**。漏一个是静默的错。收成重建一个台账。

做法：`ScaledExitLevel`（target_pct / exit_pct / state / dispatched / filled）与
`ScaledExitLadder`（持有列表 + order id 索引，对外用 1-based 层级号）。`TickMonitorManager`
保留它现在的对外方法，改为委托——调用方一行不动。

## T4 — 拒单处置里混着「这是什么拒单」与「拒单了怎么办」

**Files**: `adapter/coordinators/order_reconciler.py`、新建
`adapter/coordinators/rejection_triage.py`、测试

`handle_order_rejected` 一个方法 128 行，做的是两件事：先从事件、缓存里的订单和 tracker 算出
**这是哪一种拒单**（受跟踪的止损 / 受跟踪的止盈 / reduce-only 平仓 / 入场单），再对每一种做处置。

**要消失的特殊情况**：

1. **分类结果靠四个散落的布尔量表达**（`is_tracked_stop` / `is_tracked_take_profit` /
   `is_reduce_only` / `tier`），在不同深度被消费，互斥性只靠 `return` 的先后顺序维持。
2. **入场那一支不是 `elif`，是落下来的**。reduce-only 分支不 `return`，于是它会继续往下走到入场
   判断。今天无害（入场单不会是 reduce-only），但这是靠外部事实成立的，不是靠结构。
   分类返回一个**互斥的判定**之后，这种「落下来」在结构上不可能发生。
3. **分类不可单独测**。它现在要一个真 strategy 才能跑；抽成纯函数后，判定本身可以直接测。

做法：`RejectedOrderKind` 枚举 + `classify_rejected_order(event, order, tracker) -> RejectedOrderKind`
放进新模块，与既有的 `classify_rejection_reason` / `is_reduce_only_refusal`
（`custos_toolkit/risk/exchange_errors.py`）是同一族概念。`handle_order_rejected` 改为对判定分派。

## T5 — 入场的「决策段」与「落地段」之间那道缝只写在注释里

**Files**: `adapter/coordinators/signal_execution.py`、测试

`execute_entry_for_pair` 165 行，中间有一道**承重**的缝，而它今天只由注释表达：

> This part only reads the position. Clearing the way for a reversal cancels the old
> position's protection, and that must not happen until every check that can still refuse
> this entry has passed -- a refusal afterwards would leave the old position open with no
> stop and no replacement on the way.

> Past every refusal. Only now is it safe to take down the old position's protection.

这正是 EE-2 那个回归的位置：反向入场被资金分配拒绝之前，旧仓的唯一止损已经被撤销了。修好之后，
守住它的只有「读完这 165 行并且按顺序理解」。

**要消失的特殊情况**：把顺序约束变成类型边界。`_plan_entry(...) -> EntryPlan | None` 只读不写，
拿不到任何可以改变世界的东西；`_commit_entry(ctx, signal, bar, plan)` 只在拿到 plan 时被调用，
结构上无法拒绝。以后新增一道拒绝自然落在前者；落在后者会一眼看出是错的。

`allocate()` 是**最后一道闸门，且通过它就花了钱**——它留在决策段末尾（今天就是这样），
`EntryPlan` 带上 `reserved_capital`，本地派发被拒时的回滚由落地段负责。

## T2 — 六处对强类型的 getattr 防御

**Files**: `adapter/coordinators/order_reconciler.py`、
`adapter/coordinators/trade_event_handler.py`、`adapter/orders.py`

`coding-taste.md` §二：对强类型对象用 `getattr(obj, "field", default)` 是把强类型当 dict 用。
逐处核实过属性确实必然存在（不是凭推理）：

| 位置 | 表达式 | 实证 |
|---|---|---|
| `order_reconciler.py:483` | `getattr(event, "reason", "")` | `OrderRejected.reason` 是 `Ustr` 必填字段（NT `crates/model/src/events/order/rejected.rs:60`）|
| `order_reconciler.py:484` | `getattr(order, "is_reduce_only", False)` | `is_reduce_only` 是 `Order` trait 方法，各订单类型都实现。**`order` 本身可能是 `None`**（缓存未命中），所以这里要留的是 `order is None` 判断，不是属性防御 |
| `trade_event_handler.py:172` | `getattr(event, "ts_event", 0)` | `PositionClosed.ts_event` 是 `UnixNanos` 必填字段（`crates/model/src/events/position/closed.rs:91`）|
| `orders.py:434` | `getattr(order, "is_reduce_only", False)` | 同上，且此处 `order` 由调用方保证非空 |
| `orders.py:438` | `getattr(order, "ts_init", 0)` | 构造必填参数 + pyo3 getter（`crates/model/src/python/orders/market.rs:66,184`）|
| `orders.py:753` | `getattr(trailing_cfg, "trigger_price_type", "mark")` | 我们自己的 `StopLossTrailingConfig.trigger_price_type: str = "mark"`（`adapter/config/risk.py:122`），dataclass 字段自带默认值。同一函数 `:720` / `:742` 已经直接取 `trailing_pct` / `activation_pct`——这一处与它的邻居不一致 |

**范围纪律**：只动这六处。仓里还有三十余处同型 getattr，夹带进来会让本轮 diff 失去焦点
（与 fix 07 拒绝夹带的理由相同）。

## 验证清单

- [ ] 四项各自说明消除了哪些特殊情况，不以行数为目标
- [ ] 新不变量有测试且经扰动验证（越界层级、判定互斥、未经决策不得提交）
- [ ] 全程行为不变：既有测试一条不改地保持绿（改测试替身不算改行为，须逐条说明）
- [ ] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| T3 层级台账 | P2 | 🔲 | | 四个并行集合收一 |
| T4 拒单分诊 | P2 | 🔲 | | 分类与处置分开 |
| T5 入场分段 | P2 | 🔲 | | 承重的缝变成类型边界 |
| T2 getattr ×6 | P3 | 🔲 | | 仅这六处 |

## 偏离与改进日志
