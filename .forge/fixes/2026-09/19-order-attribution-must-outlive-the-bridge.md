# 19 - order-attribution-must-outlive-the-bridge

> **Status**: 🔲 Not started
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

- [ ] 失败测试先红后绿
- [ ] 审查方探针 `new_bridge_drops_existing_order_fill` 不再成立
- [ ] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 归属落盘并装载 | P1 | 🔲 | | EE-3 |
| 2 隔离回归 | P1 | 🔲 | | EE-3 验收第三条 |

## 偏离与改进日志
