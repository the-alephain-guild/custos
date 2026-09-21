# 20 - a-freeze-must-outlive-the-process

> **Status**: 🔲 Not started
> **Created**: 2026-09-21
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-risk-state-deep-review.md` RS-6（**A 半**）
> **Depends on**: `.forge/fixes/2026-09/15-risk-state-survives-restart.md`（B 半，已 ✅）

## 决策（本 plan 得以开工的前提）

fix 15 把 RS-6 拆成两半，只做了 B（Toolkit 风控状态随快照跨重启），A 半挂起等两个决定。
**2026-09-21 wukai 定下作用域：per deployment instance。**

解除条件不另行征询——它已经写在熔断器自己的契约里（`fallback_breaker.py:5-7`）：

> once total open notional or peak-to-current drawdown breaches its ceiling it trips, which
> flattens positions and **freezes further orders until an operator intervenes**.

「until an operator intervenes」就是解除条件。本 plan 要做的是让这句话在重启之后仍然成立，
并给「intervene」一个真实的入口——今天它根本没有入口，因为重启就把冻结清了，等于每次重启都是
一次无人署名的解除。

作用域选 per deployment instance 也与现有求值形态一致：`EngineSafetySupervisor.evaluate_once`
接的是单个 `deployment_instance_id`，`open_notional` 与 `current_equity` 都取自那一个实例的组合
快照（`core/engine_safety.py:37-57`）。冻结的依据是按实例算出来的，冻结的作用域就该是实例。

> **与 `runner_risk_latch` 的关系**：那张表按 tenant + mode + runner 键，记的是「已执行成交把
> runner 总敞口顶破了上限」，并且**会在敞口降回上限内时自动清除**（`runner_fact.py:4982-4995`）。
> 熔断冻结不是同一件事：它按实例键，且**不自动清除**。两张表并存，语义不重叠。

## 根因

签名安全边界只从**内存** registry 复用 breaker（`cli/_daemon.py:415-421`）：

```python
prior_boundary = boundaries.get(instance_id) if boundaries is not None else None
breaker = (
    prior_boundary.fallback_breaker
    if prior_boundary is not None
    else FallbackBreaker(limits.breaker)
)
```

daemon 重启后 `boundaries` 是空 dict，于是走 `else` 分支新建一个 `FallbackBreaker`，而它的
`__init__`（`fallback_breaker.py:85-89`）把两样东西都清零：

```python
self._peak_equity: Decimal = Decimal("0")
self._frozen = False
```

后果有两层，第二层比第一层更隐蔽：

1. **冻结没了**。一个已经因为回撤触发、正在冻结中的实例，重启后立刻又允许下单。
2. **峰值权益没了**。`_peak_equity` 归零之后，`_drawdown_pct` 在下一次 evaluate 时用新的
   `current_equity` 重建峰值——也就是说**回撤是从重启那一刻重新起算的**。权益从 1000 跌到 800
   的路上重启一次，新峰值就是 800，回撤读数归零，20% 的跌幅从此不可见。冻结即使被人工恢复，
   这条也仍然错。

**复现**（审查方）：真实签名 boundary factory 建 breaker，在 1000→800 的权益变化后冻结；同进程
重新 build 仍冻结；模拟 daemon 重启（新 registry、同一 store/resolver）后，新 breaker 在权益仍为
800 时允许新订单，且构造路径没有读取任何持久化熔断状态。

## 修复任务

### Fix 1: 熔断状态落盘，按实例键 [P1]

**Files**: `src/custos/core/runner_fact.py`、`src/custos/core/fallback_breaker.py`、
`tests/test_a_freeze_must_outlive_the_process.py`

1. 先写失败测试：冻结 → 丢弃内存 registry → 重建 breaker → 仍须拒单，且峰值权益仍是 1000。
2. 新表 `runner_breaker_state`，主键 `deployment_instance_id`，存峰值权益、是否冻结、冻结原因、
   冻结时刻，以及解除的三件事（时刻、解除者、理由）。走 `CREATE TABLE IF NOT EXISTS`，不动
   `RUNNER_STATE_SCHEMA_VERSION`——加表不需要任何人重建库（加**列**才需要，见 fix 19 close-out）。
3. `FallbackBreaker` 接一个可选的状态变更回调，在 `evaluate` / `fail_closed` 里**同步**调用。
   冻结与落盘必须在同一步发生：先冻结后落盘之间若崩溃，重启就读不到这次冻结。
4. 峰值权益同样落盘——它单调上升，只在创新高时写。

### Fix 2: 准入之前装载 [P1]

**Files**: `src/custos/cli/_daemon.py`、测试

`_build_runner_safety_boundary_factory` 的 `else` 分支改为：先读该实例的持久状态，把
`frozen` 与 `peak_equity` 灌进新建的 breaker，再交出 boundary。装载必须在 boundary 被任何
下单路径看到**之前**完成。

### Fix 3: 显式解除的入口与身份 [P1]

**Files**: `src/custos/cli/subcommands/breaker.py`（新建）、`src/custos/cli/__init__.py` 或等价
注册点、测试

新增 `arx-runner breaker status` 与 `arx-runner breaker clear --deployment-instance-id <id>
--operator <who> --reason <why>`。`clear` 记下解除者与理由，并把 `released_at_ns` 写上。

**关于「身份」要诚实**：`--operator` 记的是操作者自称的名字，真正的门是**谁能写到这个状态库**
——在 non-custodial 形态下那就是拥有这台机器的人。不要把这个字段说成经过认证的身份。

解除**不是**赦免：下一次 evaluate 若仍在破限，会立刻重新冻结。这要有测试。

### Fix 4: 五种情形要能互相区分 [P1]

**Files**: 测试

审查方验收点名五种，逐一给独立断言：

| 情形 | 期望 |
|---|---|
| 崩溃恢复 | 冻结与峰值都还在 |
| 正常重启 | 同上——重启不是解除 |
| 热更新（`apply_config` 换上下限） | 冻结与峰值都不受影响，落盘行也不被清 |
| 跨日恢复 | 冻结仍在。熔断器没有日语义；按日重置的是 Toolkit 的 `RiskController`（fix 15） |
| 显式解除 | 只有这一种能解冻，且留下解除者与理由 |

**验收**（报告原文）：由明确所有者持久化锁定状态及其身份/解除条件；恢复准入前加载风控状态。
策略风控也要独立于"加速指标预热"配置持久化。验证崩溃恢复、正常重启、热更新、跨日恢复和显式
解除的区别。

> 验收第二句（策略风控独立于预热配置持久化）由 fix 15 交付，本 plan 不重复。

## 验证清单

- [ ] 失败测试先红后绿
- [ ] 审查方复现 A 不再成立
- [ ] 五种情形各有独立断言
- [ ] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 熔断状态落盘 | P1 | 🔲 | | RS-6 A |
| 2 准入前装载 | P1 | 🔲 | | RS-6 A |
| 3 显式解除入口 | P1 | 🔲 | | RS-6 A，解除者与理由 |
| 4 五情形区分 | P1 | 🔲 | | RS-6 A 验收 |

## 偏离与改进日志
