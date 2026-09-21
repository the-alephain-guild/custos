# 26 - containment-must-contain-and-startup-is-not-a-breach

> **Status**: ✅ Completed
> **Created**: 2026-09-21
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-fix03-recheck-review.md` RR-4 + RR-8

两项都在熔断语义上，方向相反：RR-4 是**不该冻的时候冻了**，RR-8 是**该收的时候没收干净**。
合一份 plan，因为它们共用同一条监督路径，且都由 `EngineSafetySupervisor` 驱动。

## RR-4：启动期的空余额被当成运行中失去可信快照

**位置**：`src/custos/cli/_daemon.py:467-480` 的 `_run_signed_safety_supervision`。

节点一登记进 `_active_nodes`，周期监督就开始评估组合，**不等**账户与持仓 reconciliation 就绪。
启动阶段余额还没到，快照判为 `portfolio_equity_missing:<CCY>`，`EngineSafetySupervisor` 据此
`fail_closed` —— 而 fail_closed 是**不可自愈的**：余额随后到齐、快照恢复可靠，breaker 仍冻着。

**这套机制本仓已经有了，而且验证过。** `host.deployment_ready()` 的 docstring 写着：

> 2026-08-01 它（离线通道的敞口守卫）在账户余额到达前 116ms 触发了 `portfolio_equity_missing`，
> 而 NautilusTrader 当时还在它自己预告过的启动对账里。

离线通道因此加了**限时等待**（`offline/safety.py:290-349`）：没就绪就跳过本轮、不评估也不冻结；
到 `READINESS_TIMEOUT_SECS`（120s，NT 自己的 `reconciliation_startup_delay_secs` 默认值 10s 的一个
数量级以上）仍未就绪就照常评估——「永不盲目」。**签名通道的监督没有接这套。**

**fix 20 之后这条更重**：熔断冻结现在是持久的（`runner_breaker_state`），一次启动期的误冻会
**跨重启存活**，而且只能靠 `arx-runner breaker clear` 人工解除。报告写时它还只是本进程内的问题。

## RR-8：熔断只请求平仓，不撤掉还挂着的增险单

**位置**：`src/custos/core/engine_safety.py:62-63`；`src/custos/engines/nautilus/host.py:1586-1628`。

`flatten_positions` 逐个 instrument 调 `close_all_positions`，**完全不碰挂单**。breaker 的 freeze
只拦后续提交，交易所已经接受的挂单不再经过那个入口。所以平完仓之后，那些挂单可以再次成交、
把风险重新打开，直到下一次监督 tick 才可能再处理一遍。

报告的实证：真实 supervisor 与 host 路径，缓存里有头寸和一笔开仓挂单；回撤触发后发出了平仓调用，
而 `cancel_order` / `cancel_all_orders` 调用数**均为 0**。

## 修复任务

### Fix 1: 签名监督复用已验证的 readiness 语义 [P1 / RR-4]

**Files**: `src/custos/cli/_daemon.py`、`tests/test_startup_is_not_a_breach.py`

1. 先写失败测试：宿主尚未 ready 时跑一轮监督，breaker **不得**冻结；随后 ready 且快照可靠时，
   监督照常评估。
2. 评估前先问 `host.deployment_ready(instance)`；未就绪则本轮跳过该部署。
3. **限时**，不是无限宽限：与离线通道同一个上界语义，超时后照常评估（那会落到不可靠快照并
   fail closed —— 这正是本改动要延后而不是取消的行为）。
4. 宿主没有 `deployment_ready` 时按原样评估，并**说一次**——「安静地不运行的安全检查比大声不运行
   的更糟」，与离线通道同一条判断。

**不做的事**：不加无限宽限，不在就绪后清空 breaker。报告明写这两条都不能替代 readiness 语义。

### Fix 2: 熔断要把增险挂单一起收掉 [P1 / RR-8]

**Files**: `src/custos/engines/nautilus/host.py`、测试

1. 先写失败测试：缓存里有头寸 + 一笔增险挂单，触发 flatten 后那笔挂单必须被撤销，而减仓保护单
   必须留着。
2. `flatten_positions` 在平仓**之前**撤掉本实例的增险挂单——先撤再平，否则平仓过程中它们还能成交。
3. **保留必要的减仓保护**：reduce-only 的单子不动。这与 preserve 停机是同一条判据，可复用。
4. 撤单按 id（2.0 的签名，fix 25 刚统一过）。

### Fix 3: 两者的交互要有断言 [P1]

**Files**: 测试

fix 20 让冻结持久化之后，RR-4 的误冻会跨重启。要有一条测试：启动期不就绪 → 监督跑一轮 →
**持久状态里没有冻结记录**。否则这条缺陷会以「重启也解不开」的形态留下来。

**验收**（报告原文）：

- RR-4：为初次启动和替换建立明确、限时的就绪等待；运行中失联仍须 fail closed。复用经过验证的
  readiness 语义，不能用无限宽限或清空 breaker 替代。
- RR-8：制定有确认、有重试的熔断控制流程，撤销所属实例的风险增加挂单并保留必要减仓保护；
  不能把提交平仓请求当作零风险确认。

> **RR-8 验收里「有确认、有重试」这一半本轮不做**，理由见偏离日志：确认需要一个可信的
> venue 状态读取回路，属于独立的工作面。本轮只做「撤销增险挂单 + 保留减仓保护」，并且**不把
> 发出平仓请求说成已确认**——现有的 `nt_flatten_containment_unconfirmed` 正是这条纪律，要保住。

## 验证清单

- [x] 失败测试先红后绿 —— RR-4 实现前 4 红 2 绿，RR-8 实现前 2 红 3 绿，之后各自全绿
- [x] 运行中失联仍 fail closed（`test_losing_the_view_of_a_ready_deployment_still_fails_closed`）
- [x] 超时后照常评估（`test_an_engine_that_never_reports_ready_is_guarded_anyway`，扰动会咬）
- [x] 减仓保护单在熔断时不被撤（`test_protective_orders_survive_containment`，扰动会咬）
- [x] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 就绪等待 | P1 | ✅ | 2026-09-21 | RR-4 |
| 2 熔断撤增险单 | P1 | ✅ | 2026-09-21 | RR-8 的「撤单」一半，「确认与重试」见偏离 |
| 3 与持久冻结的交互 | P1 | ✅ | 2026-09-21 | fix 20 之后新增的面 |

## 偏离与改进日志

### DEVIATION: RR-8 的「有确认、有重试」本轮不做

- **等级**: 中（验收分句未全覆盖，显式声明）
- **原因**: 报告要的是「制定有确认、有重试的熔断控制流程」。**确认**需要一条可信的 venue 状态读取
  回路——要能回答「这笔单真的撤掉了吗」，而不是「我发过撤单请求」；**重试**要建立在确认之上，
  否则只是重复发请求。这是独立的工作面，和 fix 22 里「隔离不等于已停止」是同一类问题。
- **本轮做到的**: 撤销本实例的增险挂单 + 保留减仓保护。
- **本轮没做的**: 撤单后的确认与重试。
- **不做的后果**: 撤单请求若在场所侧失败，本轮监督不会发现，要等下一次 tick 才可能再处理。
  比修复前好（修复前根本不撤），但不是报告要的完整形态。
- **怎么补**: 需要一条 venue 状态确认回路，与 fix 22 的「接管或停止」共用同一套基础设施；
  建议同批做。
- **没有假装做到**: 现有的 `nt_flatten_containment_unconfirmed` 纪律保住了——发出请求**不**被
  记成已确认；新加的 `nt_containment_cancelled_risk_increasing_orders` 措辞同样只说「asked」。

### IMPROVEMENT: 加了收缴之后，差点让熔断整个失败

`_open_venue_state` 在读不到挂单时抛 `RuntimeError("shutdown venue state could not be confirmed")`。
我把收缴放进 `flatten_positions` 之后，这个异常会让**平仓本身也不发生**——为了「更彻底」而加的
一步，反倒能让最要紧的那一步不执行。

这是 C9 的形状：fail-closed 不能变成 fail-to-contain。改成读不到就大声记
（`nt_containment_orders_unreadable`）并继续平仓，并给这条兜底单独写了测试——否则它是条死分支。

四条既有测试因此转红（它们的 cache 替身没有 `orders_open`），是这个改动把问题暴露出来的，
不是我改坏了。

### IMPROVEMENT: 新日志行不用 `extra={...}`

`_daemon.py` 用 `logging.getLogger("custos")`，而本仓的 `configure()` 是
`logging.basicConfig(format="%(message)s")` —— stdlib 的 `extra={...}` **一个字段都不会渲染**
（`common-errors.md` 记过这条，`CredentialDecrypted` 审计事件就曾因此只输出一个事件名）。

新加的四条日志改走 `custos.core.log.get_logger` + kwargs。**既有的 11 处不动**：C6 说它们被签名
资产覆盖，改动必须与 receipt 重新签发同批进行。改完跑了 C6 点名的字节 pin 测试，仍然通过。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-21
- **总 Task 数**: 3
- **偏离数**: 1 显式未做（RR-8 确认/重试）+ 2 改进
- **验证结果**: 通过（RR-8 部分交付，已声明）
- **实施 commit**: `4bdcb65`
- **契约影响**: `_run_signed_safety_supervision` 新增可选形参 `readiness_timeout_secs`（有默认值）；
  `NtTradingNodeHost` 新增私有 `_cancel_risk_increasing_orders`。无 wire / schema 变动。
- **C6 核对**: 改了 `_daemon.py`，跑过 `test_machine_request_consumer_assets_are_exactly_pinned`
  与 `test_runner_policy_contract_consumer`，均通过。

### 红线 gate 满足度

| 红线 | code 覆盖 | runtime wire | defer | follow-up |
|---|---|---|---|---|
| 0.1 Key/KEK 不出进程 | N/A（未触及） | N/A | 无 | — |
| 0.2 执行门不绕过 | RR-8：冻结拦不住场所已接受的挂单，现在熔断会把增险单收掉 | `flatten_positions` 是 `EngineSafetySupervisor` 触发containment 的唯一出口 | 撤单后的**确认**未做 | 需要 venue 状态确认回路 |
| 0.3 失联 ≠ 停止 | RR-4：启动期缺数据不再被读成「运行中失去可信视图」；运行中真失联仍 fail closed | `_run_signed_safety_supervision` 是 daemon 实际跑的周期监督 | 无 | — |
| 0.4 Decimal money math | N/A（未触及） | N/A | 无 | — |

### 验收分句逐条对照

| 来源 | 分句 | 覆盖 |
|---|---|---|
| RR-4 | 为初次启动和替换建立明确、限时的就绪等待 | `test_a_deployment_that_is_still_starting_is_not_frozen` + `SIGNED_SUPERVISION_READINESS_TIMEOUT_SECS` |
| RR-4 | 运行中失联仍须 fail closed | `test_losing_the_view_of_a_ready_deployment_still_fails_closed` |
| RR-4 | 复用经过验证的 readiness 语义 | 走 `host.deployment_ready()`——离线通道 2026-08-01 起在用的同一个 |
| RR-4 | 不能用无限宽限替代 | `test_an_engine_that_never_reports_ready_is_guarded_anyway`；把上界改成 `if False` 会红 |
| RR-4 | 不能用清空 breaker 替代 | 没有任何地方清 breaker；`test_a_startup_trip_does_not_become_a_durable_freeze` 断言的是**没冻**而不是**冻了又解** |
| RR-8 | 撤销所属实例的风险增加挂单 | `test_a_resting_risk_increasing_order_is_cancelled_by_containment` |
| RR-8 | 保留必要减仓保护 | `test_protective_orders_survive_containment` |
| RR-8 | 不能把提交平仓请求当作零风险确认 | 既有 `nt_flatten_containment_unconfirmed` 保持不变；新日志只说「asked」 |
| RR-8 | **有确认、有重试** | ❌ **本轮未做**，见偏离日志 |

### 测试条数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/test_startup_is_not_a_breach.py` | 6 |
| `tests/test_containment_cancels_the_orders_that_reopen_risk.py` | 6 |
| `tests/test_plan_closeout_counts.py` | 73 |

### 扰动验证

| 扰动 | 结果 |
|---|---|
| 监督不再等就绪（RR-4 原形态） | 4 红 |
| 上界变成无限宽限 | 1 红（「永不盲目」那条） |
| 熔断不再撤挂单（RR-8 原形态） | 3 红 |
| 连减仓保护一起撤（过度收缴） | 1 红 |
| 还原 | 12 绿 |

每次扰动都用独立的 `PYTHONPYCACHEPREFIX`。

### 功能验证（主路径）

1. 起一个 testnet 部署，观察启动到 ready 这段时间的日志：应出现
   `signed_supervision_awaiting_readiness`，随后 `signed_supervision_evaluating`，
   **不应**出现 `fallback_breaker_fail_closed`。修复前这里会冻结，且 fix 20 之后跨重启不解。
2. 让部署起来之后拔掉行情/账户来源制造真失联：仍应 fail closed 并 flatten —— 这条没被削弱。
3. 挂一笔增险单，人为把权益打到回撤上限以外触发熔断：那笔单应被撤掉，
   日志出现 `nt_containment_cancelled_risk_increasing_orders`；止损单应还在。
