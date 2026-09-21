# 20 - a-freeze-must-outlive-the-process

> **Status**: ✅ Completed
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

- [x] 失败测试先红后绿 —— 实现前 10 红，实现后 14 绿
- [x] 审查方复现 A 不再成立（中止于 restore 路径，见下）
- [x] 五种情形各有独立断言
- [x] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 熔断状态落盘 | P1 | ✅ | 2026-09-21 | RS-6 A |
| 2 准入前装载 | P1 | ✅ | 2026-09-21 | RS-6 A |
| 3 显式解除入口 | P1 | ✅ | 2026-09-21 | `arx-runner breaker status/clear` |
| 4 五情形区分 | P1 | ✅ | 2026-09-21 | RS-6 A 验收 |

## 偏离与改进日志

### DEVIATION: 两条断言原本是「碰巧成立」，扰动验证当场戳破

- **等级**: 低（测试质量，不涉及实现）
- **原因**: 第一轮扰动跑了四个点，**两个没转红**：
  - 把「仍在冻结时重新落盘保留原冻结时刻」的 CASE 改成无条件覆盖 —— 全绿。因为
    `test_a_day_boundary_is_not_a_release` 的两次读之间**根本没有重新落盘**，
    `after.frozen_at_ns == before.frozen_at_ns` 是恒真的。
  - 把解除的 `WHERE ... AND frozen = 1` 去掉 —— 全绿。因为
    `test_releasing_a_breaker_that_was_never_frozen_is_refused` 用的是一张**根本没有行**的表，
    `rowcount` 两种写法下都是 0。拒绝是对的，但拒绝的理由不是被断言的那个。
- **决定**: 补两条真会咬的：`test_a_still_frozen_breaker_keeps_the_moment_it_first_tripped`
  （权益创新高时在冻结中重新落盘）与
  `test_releasing_twice_is_refused_and_does_not_rewrite_the_first_release`
  （行存在但未冻结、以及重复解除）。第三轮又补一条：重新冻结必须清掉上一次解除的署名，
  否则那行会同时读作「冻结中」与「已由某人解除」。三条都经扰动验证转红。
- **归类**: C26 与 C28 同族——断言存在、测试全绿，但它保护的不是它自称保护的东西。

### 审查方复现 A 的现状

`signed_breaker_restart_forgets_trip` 用 `MagicMock()` 当 store，并以
`assert not store.mock_calls` 断言「构造路径没有读取任何持久化熔断状态」。现在它在
`fallback_breaker.py` 的 `if current_equity > self._peak_equity` 处以 `TypeError` 中止——
因为工厂**确实读了** store，而 MagicMock 把一个 mock 当成 peak_equity 交了回来。日志里能看到
中止前已经打出 `fallback_breaker_freeze_restored`。

换句话说，它断言的那件事（构造路径不读持久状态）已经不成立了。同一形态在真 store 下的行为由
`test_the_daemon_loads_the_freeze_before_it_hands_out_a_boundary` 钉住：同一份
`RunnerStateStore`、新的 registry，重建后仍冻结、峰值仍是 1000；把工厂里那行
`state_store.load_breaker_state_sync(instance_id)` 改成 `None` 即转红（已实测）。这是 C29 的
第三次实例。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-21
- **总 Task 数**: 4
- **偏离数**: 1（测试质量，见上）
- **验证结果**: 全部通过
- **实施 commit**: `1f307df`
- **契约影响**: 新表 `runner_breaker_state`（`CREATE TABLE IF NOT EXISTS`，未动
  `RUNNER_STATE_SCHEMA_VERSION`——加表不逼任何人重建库）。新 CLI 子命令 `breaker`，因此
  `.github/workflows/release.yml` 的两处命令矩阵与 `docs-site` 的两份生成参考同步更新——那两份
  不是手改的，是 `scripts/check-docs-site.py --write` 从真实 parser 重新生成的（C7：清单从权威源
  推导，不硬编码）。`FallbackBreaker` 新增可选的 `on_state_change`、`restore()` 与 `peak_equity`。
- **红线守护**: 本次是**加强**红线 0.3，不是放宽——原状态下每次重启都等于一次无人署名的解除。

### 红线 gate 满足度

| 红线 | code 覆盖 | runtime wire | defer | follow-up |
|---|---|---|---|---|
| 0.1 Key/KEK 不出进程 | 新表只存权益数字、原因码与解除署名 | N/A | 无 | — |
| 0.2 执行门不绕过 | 冻结即拒单，跨重启仍拒 | 工厂在交出 boundary 前装载 | 无 | — |
| 0.3 失联 ≠ 停止 | 本 plan 的主体：断线时本地守护的冻结不再被重启抹掉 | `_build_runner_safety_boundary_factory` 是守护进程实际用的那条构造路径 | 无 | — |
| 0.4 Decimal money math | 峰值权益全程 `Decimal`，落盘存字符串、读回 `Decimal(str(...))` | 同上 | 无 | — |

### 测试条数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/test_a_freeze_must_outlive_the_process.py` | 14 |
| `tests/cli/test_runner_safety_daemon_composition.py` | 8 |
| `tests/test_docker_runtime_contract.py` | 21 |
| `tests/test_plan_closeout_counts.py` | 61 |

第三行不是新增测试：那个文件的命令矩阵**从 parser 推导**，新增一个子命令就多一个参数化用例
（20 → 21）。这正是 C7 那套防护在工作——硬编码的清单不会这样变红。最后一行的增量来自本份
close-out 自己。

### 扰动验证

| 扰动 | 结果 |
|---|---|
| 工厂不再装载持久状态 | 1 红（daemon 那条） |
| 创新高时不再落盘峰值 | 1 红 |
| 冻结中重新落盘覆盖原冻结时刻 | 1 红（第二轮补的断言） |
| 解除不再要求该行处于冻结 | 1 红（第二轮补的断言） |
| 重新冻结不清掉上次解除的署名 | 1 红（第三轮补的断言） |
| 还原 | 14 绿 |

每次扰动都用独立的 `PYTHONPYCACHEPREFIX`。

### 五种情形的最终状态

| 情形 | 断言 |
|---|---|
| 崩溃恢复 / 正常重启 | `test_a_restart_keeps_both_the_freeze_and_the_high_water_mark`、`test_the_daemon_loads_the_freeze_before_it_hands_out_a_boundary` |
| 热更新 | `test_a_config_refresh_changes_the_ceilings_and_nothing_else` |
| 跨日恢复 | `test_a_day_boundary_is_not_a_release` |
| 显式解除 | `test_only_an_explicit_release_unfreezes_and_it_is_attributed`、`test_the_operator_surface_reports_and_lifts_a_real_freeze` |
| 解除不是赦免 | `test_a_release_is_not_a_pardon_for_a_breach_that_is_still_there` |

崩溃恢复与正常重启在本仓的代码路径上**是同一条**：两者都以空 registry 起步，都走
`load_breaker_state_sync`。把它们写成两条不同的测试会是装饰性的——真实差别在进程怎么死，
而那不改变这条路径。这里如实合并，不假装覆盖了两种机制。

### 功能验证（主路径）

1. 让某个实例的权益从峰值回撤超过上限，确认它冻结并被 flatten。
2. `arx-runner breaker status --deployment-instance-id <id>` 应打印 frozen since 与原因，
   退出码 1。
3. 重启 daemon，再跑一次 status：仍然 frozen，峰值权益不变。修复前这里会变成「无记录」。
4. `arx-runner breaker clear --deployment-instance-id <id> --operator <你> --reason <理由>`，
   再跑 status：not frozen，并显示上次由谁以什么理由解除。
5. 若行情仍在破限，下一轮 supervision 会重新冻结——这是对的，不是 bug。
