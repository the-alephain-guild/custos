# 21 - decompositions-the-line-counts-were-pointing-at

> **Status**: ✅ Completed
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

- [x] 四项各自说明消除了哪些特殊情况，不以行数为目标
- [x] 新不变量有测试且经扰动验证（越界层级、判定互斥、承重的缝）
- [x] 行为不变；改动的测试共四处，逐条说明见下
- [x] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| T3 层级台账 | P2 | ✅ | 2026-09-21 | 四个并行集合收一 |
| T4 拒单分诊 | P2 | ✅ | 2026-09-21 | 分类与处置分开 |
| T5 入场分段 | P2 | ✅ | 2026-09-21 | 承重的缝变成类型边界 |
| T2 getattr ×6 | P3 | ✅ | 2026-09-21 | 仅这六处 |

## 偏离与改进日志

### 改动了四处既有测试——逐条说明为什么这不是改行为

重构的纪律是「既有测试不动」。动了四处，每一处都是测试在测实现而不是测行为：

1. **`test_fixed_risk_sizing.py::test_execute_entry_guards_zero_size`** —— 它
   `inspect.getsource(execute_entry_for_pair)`，断言源码里有 `final_size <= 0` 这个字符串，
   并且它出现在 `create_entry_order` 之前。一次不改任何行为的拆分让它转红，这正是
   `coding-taste.md` 正交特例给的判据。改成问决策段决定了什么，并配一条正控
   （同样的调用在正常尺寸下要产出 plan），否则那条否定断言可能绿在别的原因上。
2. **`test_tick_monitor.py::test_init_position_resets_scaled_levels`** —— 它直接
   **赋值** `manager._tp_level_states` 与 `_level_orders` 造前提，然后断言它们被清空。
   赋值造出的是实例上的游离属性；台账化之后生产代码根本不读它，所以那条测试断言的是它自己刚
   写下的东西。改成用 `check` / `bind_level_order` / `confirm_level_order` 把层级真的推到
   COMPLETED 与 PENDING，再断言 `level_state()`。
3. **`test_strategy_state_and_order_protection.py`** 一行 `_tp_level_states[0].value`
   改为 `level_state(1)`。断言的行为没变。
4. **三个 `PositionClosed` 替身补 `ts_event`** —— 真实事件必有该字段（NT
   `crates/model/src/events/position/closed.rs:91`），是那道 getattr 防御让替身比场所送来的
   东西少给了一样。补齐使替身更忠实，方向与 C4 一致。

### IMPROVEMENT: 给监视器加两个查询，而不是让测试伸手进去

`level_state(level)` 与 `carries_level_order(order_id)`。这不是为测试开的后门——「第 3 档现在
什么状态」「这张单还有层级在等它吗」是对监视器的正当提问。加了它们，测试才不必再读私有列表。

### 没做的事

`trading_strategy.py:521` 还有一处同型 getattr，不在 fix 07 点名的六处里，按 plan 的范围纪律
留着。仓里另有约三十处同型防御，同理。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-21
- **总 Task 数**: 4
- **偏离数**: 1 改进 + 1 说明（见上）
- **验证结果**: 全部通过
- **实施 commit**: `bc80b53`（T2）· `4e74e1c`（T5）· `f0925d9`（T4）· `b2469f6`（T3）
- **契约影响**: 无。三个新模块都是内部结构；`TickMonitorManager` 的公开方法一个没改、
  一个没少，调用方零改动。`EntryPlan.order` 从 `object` 收紧为 `Order` 协议（strict mypy 要求）。
- **红线守护**: T5 正是在加固红线 0.2 的一条实现细节——「拒绝之后不得已经拆掉旧保护」
  以前靠注释，现在靠类型边界。其余三项不触及红线。

### 四项各自消除了什么特殊情况

| 项 | 消除的特殊情况 |
|---|---|
| T3 层级台账 | ① 四个入口各写一遍 `index = level - 1` 与边界检查，其中两处量 `_tp_level_states`、两处量 `_tp_levels`；② 1-based↔0-based 换算散在五个方法；③ `_reset_levels` 要记得清四个集合 |
| T4 拒单分诊 | ① 分类结果靠四个散落布尔量表达，互斥性只由 `return` 的先后维持；② 入场那一支不是 `elif`，是从 reduce-only 分支**落下来**的；③ 分类要有真 strategy 才能测 |
| T5 入场分段 | 「每一道拒绝都必须在拆掉旧保护之前」这条承重约束，从注释变成 `_plan_entry` / `_commit_entry` 的类型边界 |
| T2 getattr | 对必然存在的字段做防御，读者无从判断哪一处是真有可能缺；留下的那一处（缓存可能没有这张单）现在是显式的 `order is None` |

### 行数（副产物，不是目标）

| 文件 | 前 | 后 |
|---|---|---|
| `adapter/tick_monitor.py` | 712 | 610 |
| `adapter/scaled_exit_ladder.py` | — | 231（新）|
| `adapter/coordinators/order_reconciler.py` | 637 | 633 |
| `adapter/coordinators/rejection_triage.py` | — | 76（新）|
| `adapter/coordinators/signal_execution.py` | 431 | 479 |

`execute_entry_for_pair` 从 165 行变成 13 行的调度 + `_plan_entry` 113 + `_size_against_open_position` 39
+ `_commit_entry` 84。**总量是增加的**——拆出了值对象、文档和两个新模块的样板。
`order_reconciler.py` 仍在 600 行软上限之上，`handle_order_rejected` 仍有 119 行：剩下的是处置
本身，四种处置各自成段、互不交叉，再切就是按行数切了。按本 plan 的判据，这里停手。

### 测试条数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/toolkit/test_scaled_exit_ladder.py` | 14 |
| `tests/toolkit/test_rejection_triage.py` | 13 |
| `tests/toolkit/test_tick_monitor.py` | 54 |
| `tests/toolkit/test_strategy_state_and_order_protection.py` | 70 |
| `tests/toolkit/test_fixed_risk_sizing.py` | 10 |
| `tests/toolkit/test_trade_event_handler.py` | 12 |
| `tests/test_plan_closeout_counts.py` | 63 |

### 扰动验证

| 扰动 | 结果 |
|---|---|
| T3 边界检查容忍负下标（Python 索引的默认行为）| 2 红 |
| T3 `reset` 忘记清 order 索引 | 1 红 |
| T3 认领层级时不标 PENDING | 12 红 |
| T3 末档不再吸收四舍五入余量 | 1 红 |
| T4 reduce-only 判断提到止损判断之前 | 7 红（含两条既有保护测试）|
| T4 缓存缺订单被读作 reduce-only | 1 红 |
| T5 把反向拆保护挪回资金分配之前（即 EE-2 的形态）| 1 红 |
| 还原 | 全绿 |

其中 T3 的前两轮扰动里，「负下标」与「末档余量」两条**最初没有咬**——那两条不变量当时根本
没有测试，是抽出台账之后才写得出来的。这正是这次分解的收益：它们以前藏在一个 712 行的类里，
要驱动一整个持仓才够得着。

### 功能验证（主路径）

本轮不改任何行为，没有新的用户可见功能面。要确认没改坏：跑 `make verify-nt`，2821 项通过；
或在 sandbox 下跑一遍分批止盈（逐档成交 + 跳空）与一次反向入场，行为应与修改前完全一致。
