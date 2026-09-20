# 19 - order-attribution-must-outlive-the-bridge

> **Status**: ✅ Completed
> **Created**: 2026-09-20
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-execution-edge-deep-review.md` EE-3

## 根因

`RunnerFactEventBridge` 用三个**进程内**字典记住自己认领过哪些订单
（`src/custos/core/runner_fact_producer.py:298-300`）：

```python
self._order_directions: dict[str, str] = {}
self._order_roles: dict[str, str] = {}
self._owned_order_ids: set[str] = set()
```

只有本对象亲自收到 `OrderInitialized` 才会往里写（`:456-458`）。而每条回报进来先过这道认领
检查（`:325-329`）：

```python
if client_order_id and client_order_id not in self._owned_order_ids:
    return
```

这道检查本身是对的——实盘的回报流是**账户级**的，同一账户上的兄弟实例订单、手工下的单都会
出现在这里，不能让它们混进这个实例签名的事实流。问题是它依赖的记忆活不过桥接器重建：新对象
从空集合起步，`bootstrap` 只挂回调，宿主（`src/custos/engines/nautilus/host.py:832-847`）没有
注入任何已有订单的归属。

于是重启后仍在挂单的旧订单——入场单的剩余成交、早就挂在那里的止损被触发——它们的回报直接
`return`：不产事实、不报错、也不触发 sink failure。云端看到的是这笔单从此人间蒸发。

**复现**（审查方）：桥接器 A 收下 `OrderInitialized` 与一笔部分成交；用同一个 deployment
authority 新建并 bootstrap 桥接器 B，再投同一订单的剩余成交。B 产出 0 个事实批次；同一个事件
交回仍持有归属的 A，正常产出 execution_fill / settlement_fill / settlement_fee。

**审查方标注的边界**：探针直接驱动回调，没有跑完整的 LiveNode + Redis 重启。结论成立的条件是
「旧订单恢复后没有重新投递 `OrderInitialized`」，不能写成所有恢复配置都会丢成交。

## 修复方向

归属必须是**持久的、按 deployment instance 绑定的**，并且在接第一条回报**之前**装载完毕。

落点选 RunnerFact 的那个 SQLite 库：它已经是这个实例的持久状态所在，桥接器通过
`RunnerFactEmitter` 已经握着它。新建一张表而不是给已有表加列——建表语句每次 `_initialize`
都跑且带 `IF NOT EXISTS`，已有库会自动长出这张空表，语义等同于「还没记住任何订单」，与今天
的行为一致，不需要动 `RUNNER_STATE_SCHEMA_VERSION`、也不会逼任何人重建库。

**不能做的事**：不能把那道过滤去掉。它同时承担账户级回报隔离，去掉等于让兄弟实例和手工订单
进入本实例的签名事实流。

## 修复任务

### Fix 1: 订单归属落盘并在 bootstrap 时装载 [P1]

**Files**: `src/custos/core/runner_fact.py`、`src/custos/core/runner_fact_producer.py`、测试

1. 先写失败测试：桥接器 A 认领订单后，用同一 deployment instance 新建桥接器 B，B 必须为该订单
   的剩余成交产出事实。
2. outbox 新增 `runner_order_identity` 表（deployment_instance_id + client_order_id 主键，
   存 direction 与 order_role），配 `remember_order_sync` / `recall_orders_sync`。
3. `RunnerFactEmitter` 暴露窄接口转发，桥接器不直接伸进 `_outbox`。
4. `_on_order_initialized` 写盘；`bootstrap` 读回三个字典；终态（rejected / canceled / expired）
   连同落盘记录一起删。

### Fix 2: 隔离不能被削弱 [P1]

**Files**: 测试

审查方点名要覆盖：入场单剩余成交、已有止损触发、以及**手工或其他实例的订单仍被排除**。
第三条是这次改动最容易伤到的东西——记忆变持久了，就要证明它没有顺带把别人的订单认下来。

**验收**（报告原文）：从持久化、实例绑定的订单身份恢复归属，并在接收回报之前装载。不要简单
移除过滤，因为它还承担账户级回报隔离。补上原生缓存/持久化重启场景：入场单剩余成交、已有止损
触发，以及手工/其他实例订单仍被排除。

## 验证清单

- [x] 失败测试先红后绿 —— 实现前 1 红 3 绿（3 绿是隔离与终态的回归保护），实现后 5 绿
- [ ] 审查方探针 `new_bridge_drops_existing_order_fill` **仍然成立** —— 见下方「探针为什么还绿」
- [x] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 归属落盘并装载 | P1 | ✅ | 2026-09-20 | EE-3 |
| 2 隔离回归 | P1 | ✅ | 2026-09-20 | EE-3 验收第三条 |

## 偏离与改进日志

### IMPROVEMENT: 放手一张已终结的订单不该归日志管

`_on_order_lifecycle` 开头是 `if self._runtime_log_emitter is None: return`——也就是说没有
runtime log sink 的实例，收到取消/拒绝/过期时**整段都不执行**，包括末尾那几行清理归属。

以前这只是漏几个进程内字典项，进程一退就没了。归属改成落盘之后，同一个漏法会让
`runner_order_identity` 只增不减。所以把「记日志」和「放手」拆开：前者仍受 sink 有无约束，
后者无条件执行。`test_a_terminal_order_is_forgotten_everywhere` 钉住这一点——把放手改回
「有 sink 才做」即转红（已实测）。

计划里没写这条，是实施时才看见的。它不是 EE-3 的原文要求，但不处理的话这次修复自己会制造
一个新的无界增长。

### 探针为什么还绿

审查方的 `new_bridge_drops_existing_order_fill` 给两个桥接器各建了一个 `_Emitter()`
（`.forge/reviews/2026-09-20-custos-execution-edge-deep-repro.py:210,215`）。那是我们测试里的
内存替身，两个实例 = 两块互不相通的内存。换句话说探针复现的是「两个桥接器背后各有一块独立的
记忆」，而生产里它们背后是同一个文件。

生产形态是：宿主全程只有一个 `self._runner_fact_emitter`，而真正跨重启承载归属的是它背后那张
`runner_order_identity` 表。本次的测试按**重启**形态写——`_restarted()` 对同一个数据库路径新建
一个 `RunnerFactOutbox` 和 `RunnerFactEmitter`，复用第一个 emitter 对象等于让它自己的内存回答
问题，证明不了任何事。

扰动验证给出了对应的反证：删掉 `bootstrap` 里的 `self._recall_orders()`，
`test_a_rebuilt_bridge_still_owns_an_order_it_did_not_initialize` 与
`test_a_resting_stop_still_belongs_to_the_instance_after_a_rebuild` 立刻转红——正是探针描述的
那两种丢失。

### 关于「不改 schema 版本号」

新表走 `CREATE TABLE IF NOT EXISTS`，每次 `_initialize` 都执行，已有库会自动长出这张空表，
语义等同「还没记住任何订单」，与今天的行为一致。`RUNNER_STATE_SCHEMA_VERSION` 因此没有动——
动它会让所有已存在的 pre-production 库在启动时抛 `RunnerStateMigrationError` 要求重建，而这次
改动并不需要任何人重建。加**列**才需要走那条路，加**表**不需要。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-20
- **总 Task 数**: 2
- **偏离数**: 1（一项改进，见上）
- **验证结果**: 全部通过
- **实施 commit**: `8c3c5f4`
- **契约影响**: `RunnerFactOutbox` 新增 `runner_order_identity` 表与三个 sync 方法；
  `RunnerFactEmitter` 新增三个窄转发方法（桥接器不直接伸进 `_outbox`）。wire 契约、签名口径与
  `runner_fact` schema 版本均未变动。测试替身随之补齐（`_Emitter` 与宿主测试里的 emitter stub）。
- **红线守护**: 四条红线均未触及。新表只存 client order id、方向与角色，不含任何凭据材料。

### 红线 gate 满足度

| 红线 | code 覆盖 | runtime wire | defer | follow-up |
|---|---|---|---|---|
| 0.1 Key/KEK 不出进程 | 新表字段不含凭据 | N/A | 无 | — |
| 0.2 执行门不绕过 | N/A（本次不经执行门） | N/A | 无 | — |
| 0.3 失联 ≠ 停止 | 重建后仍认领旧订单，正是「断了也要接着记账」的一面 | `RunnerFactEventBridge.bootstrap` 是宿主 deploy 时实际调的那条路径（`host.py:832-847`） | 无 | — |
| 0.4 Decimal money math | N/A（未触及金额） | N/A | 无 | — |

### 测试条数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/test_order_attribution_must_outlive_the_bridge.py` | 5 |
| `tests/test_strategy_signal_bridge.py` | 13 |
| `tests/test_nt_trading_node_host.py` | 40 |
| `tests/test_plan_closeout_counts.py` | 59 |

中间两行不是新增测试：只补了测试替身（条数未变，但本份是最新数它们的 close-out）。最后一行的
增量来自本份 close-out 自己。

### 扰动验证

| 扰动 | 结果 |
|---|---|
| `bootstrap` 不再装载已记住的订单 | 2 红（剩余成交、静止止损各一） |
| 装载不再按 deployment instance 过滤 | 1 红（邻居实例泄漏那条） |
| 终态只在有 log sink 时放手 | 1 红 |
| 还原 | 5 绿 |

每次扰动都用独立的 `PYTHONPYCACHEPREFIX`。

### 功能验证（主路径）

1. 在 sandbox 下开仓并挂上止损，确认云端收到 execution_fill 与保护单的初始化事实。
2. 重启 runner（或让宿主重建引擎），期间不要动那张止损单。
3. 让止损触发。云端应照常收到这笔成交的 execution_fill / settlement_fill / settlement_fee；
   修复前这里是静默的空白。
4. 反向确认：用同一交易所账户手工下一笔单，它的回报**不应**出现在本实例的事实流里。
