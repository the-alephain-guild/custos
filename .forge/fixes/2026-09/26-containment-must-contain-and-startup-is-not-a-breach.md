# 26 - containment-must-contain-and-startup-is-not-a-breach

> **Status**: 🔲 Not started
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

- [ ] 失败测试先红后绿
- [ ] 运行中失联仍 fail closed（对照，不得被 readiness 等待一并关掉）
- [ ] 超时后照常评估（「永不盲目」）
- [ ] 减仓保护单在熔断时不被撤（对照）
- [ ] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 就绪等待 | P1 | 🔲 | | RR-4 |
| 2 熔断撤增险单 | P1 | 🔲 | | RR-8（部分，见偏离）|
| 3 与持久冻结的交互 | P1 | 🔲 | | fix 20 之后新增的面 |

## 偏离与改进日志
