# Code Review: Custos deep follow-up

> **Depth**: deep
> **Scope**: 全库风险导向探索，重点阅读 23 个源码文件；不代表全库逐行覆盖。
> **Date**: 2026-09-20
> **Reviewer**: Codex，单执行面审查，无独立外部 reviewer。
> **Custos source**: `967257967315bdd2938573511b964481b88ba34a`；开始时工作区干净。
> **Consumer cross-check**: Crucible `81ba7209785df43991d730e5890eec16659d4417`；本轮未修改消费端。

## Summary

发现 9 项问题：8 项 P1，1 项 P2。关键缺口在签名运行通道的持续风控、风险策略换版后的敞口连续性、原生平仓事件 ABI，以及新 generation 替换。上一轮修复仍有价值，但局部测试通过没有证明完整运行链路闭合。

130 项既有定向测试全部通过。另一个离线复现脚本确认了本报告的触发条件与异常结果；脚本断言的是缺陷现状，不是修复验收。

| Severity | Count |
| --- | --- |
| Critical / P1，生产前必须修复 | 8 |
| Warning / P2，潜在账务口径问题 | 1 |
| Info | 0 |

## Strengths

- 事件转发器有明确的异常隔离与宿主故障回调；SQLite 对账周期已有原子写入。
- CASH 库存与保证金权益开始分离；已有原生账户测试和跨语言 checkpoint fixture。
- 默认 live 准入仍受签名材料控制。本报告没有发现或尝试绕过准入。

## Concerns

### DR-9 [P1] 策略换版后旧敞口从总额计算中消失

- **位置**：`src/custos/core/runner_fact.py:3714`、`:4628-4648`；`src/custos/cli/_daemon.py:414-418`。
- **触发与影响**：已有持仓时接受同一 runner 的下一版有效策略。head 切到新 policy_id，而 checkpoint 和订单预留仍按旧 policy_id 存储。新边界或恢复后的节点读取新 ID，旧持仓和活动预留均不进入新总额。旧边界则因策略不再是 head 而被拒绝，缺少连续切换过程。
- **复现**：实际验签的两版策略、真实 SQLite；旧已成交敞口 100、两版总上限均 150。换版后新策略显示总敞口 0，再预留 100 成功，实际已成交加活动预留合计 200。
- **修复建议**：敞口归属稳定的 tenant/mode/runner，策略版本仅是约束版本；或者在一个事务内完成旧敞口迁移与边界切换。恢复和策略更新不得在重建完成前接受新增风险。

### DR-2 [P1] 签名 daemon 创建熔断器但不执行周期风险评估

- **位置**：`src/custos/cli/_daemon.py:397-419`、`:920-938`；`src/custos/core/engine_safety.py:40-61`。
- **触发与影响**：签名通道长期运行且账户回撤、价格变化导致敞口超限。工厂创建 FallbackBreaker，订单入口只查询 frozen 状态；daemon 启动观测和周期采集任务，却未组合 EngineSafetySupervisor。生产源码中该 supervisor 只在 offline 通道实例化。
- **复现**：真实签名边界工厂、宿主和事实观测入口，账户权益从 1000 降至 500，宿主能计算出 50% 回撤，但边界 breaker 仍未冻结并接受新订单。该探针没有启动完整鉴权 daemon；缺失接线由调用链与 AST 扫描交叉确认。
- **修复建议**：在签名 daemon 为活跃实例组合独立、限时、受任务监管的风险评估循环，复用订单边界中的同一 breaker，覆盖断网、回撤、超限、节点恢复和 generation 切换。

### DR-6 [P1] 原生 PositionClosed 无 to_dict，真实平仓事实无法写入

- **位置**：`src/custos/core/runner_fact_producer.py:613-639`，特别是 `:617`。
- **触发与影响**：当前安装的 Nautilus 2.0 Python binding 发出真实 PositionClosed。该类型有公开属性，但没有 to_dict；事件桥直接调用 `type(event).to_dict(event)`，抛 AttributeError，无法产生 position_closed。已有测试替身自行提供了该方法，掩盖真实 ABI。
- **复现**：原生 CryptoPerpetual、OrderFilled、Position 和 PositionClosed.create；桥抛出缺少 to_dict 的 AttributeError，emitter 收到 0 条平仓事实。
- **修复建议**：使用当前原生事件的真实公开属性或支持的序列化接口；用原生 PositionClosed 覆盖事件转发至 durable outbox 的完整路径。

### DR-1 [P1] 签名新 generation 被隔离，旧节点却未停止

- **位置**：`src/custos/core/engine_lifecycle.py:176-178`、`:297-329`；`src/custos/engines/nautilus/host.py:607-612`。
- **触发与影响**：同一 instance 的 generation 1 正在运行，再收到 running generation 2。生命周期直接 deploy，宿主拒绝同实例重复部署。异常发生在新 handle 返回前，因此失败分支不停止旧节点，最终将新命令标为 quarantined；旧运行节点可继续执行。
- **复现**：真实宿主 deploy 前置守卫与生命周期 supervisor，使用轻量节点占位及存储替身；重试预算 2 时 deploy 3 次、stop 0 次，状态 quarantined、旧注册节点仍存在。没有启动实际交易节点。
- **修复建议**：明确新 generation 的有序替换流程，先确认旧代停止，再部署并确认新代就绪；隔离状态必须与引擎是否仍运行一致。

### DR-3 [P1] 审计已降级，对外签名心跳仍是 online

- **位置**：`src/custos/core/runner_fact_producer.py:675-693`；`src/custos/engines/nautilus/host.py:1428-1440`、`:1668-1686`。
- **触发与影响**：事件转发失败被宿主记录后，get_engine_status 返回 unreliable/degraded；但观测循环只取组合快照，快照不检查转发故障，再无条件构造 online 心跳。失去审计事实的部署因此仍可向控制面呈现在线健康。
- **复现**：同一宿主设置 runner_facts 故障后，状态 reliable=False；随后实际 `_emit_observability` 仍产生 heartbeat.status=online。持续磁盘故障可能连心跳也发不出；原生事件 ABI 错误或瞬时写入失败则可直接触发此分歧。
- **修复建议**：健康事实与本地安全决策消费统一的可靠状态，把审计故障纳入心跳退化条件，不能从价格可读推出部署健康。

### DR-4 [P1] 合法减仓改单被缺失的预留记录拒绝

- **位置**：`src/custos/core/order_reservation_boundary.py:143-154`；`src/custos/engines/nautilus/runner_safety.py:328-351`。
- **触发与影响**：reduce-only 订单提交时正确跳过风险预留，但改单不区分减仓，强制读取预留并要求可增加风险。正常修改减仓限价单或保护订单参数会被本地拒绝，冻结后同样无法修改。
- **复现**：真实 SQLite 预留存储；reduce-only submit 到达下游，预留不存在；随后 modify 未到达下游，记录 custos_runner_notional_policy_rejected。
- **修复建议**：根据缓存中真实订单的 reduce-only 语义分类修改，对风险降低操作保留可用路径；不能为绕过问题虚构风险预留。

### DR-8 [P1] 多次入场的净持仓平仓只扣第一笔订单的敞口

- **位置**：`src/custos/engines/nautilus/runner_safety.py:156-165`；`src/custos/core/order_reservation_boundary.py:226-235`；`src/custos/core/runner_fact.py:4273-4279`。
- **触发与影响**：同一净持仓由多笔开仓形成。平仓归属只取 position.opening_order_id，整笔减仓数量全部扣第一条预留。若总平仓量大于第一单已成交量，账务拒绝并冻结；真实已平仓敞口仍留在账上。
- **复现**：原生 Position 两次各买 1，随后卖 2 已平；真实 SQLite 两单各存 50 敞口。减仓回调冻结 breaker，账面仍保留 100 敞口。
- **修复建议**：建立持仓与所有开仓批次的持久化归属，按明确分配规则消减数量和成本；加入加仓、部分减仓、反向开仓与恢复后的回归。

### DR-5 [P1] Binance 现货基础币手续费阻断独立账本

- **位置**：`src/custos/engines/nautilus/binance_ledger.py:382-386`。
- **触发与影响**：BTCUSDT 成交的 commissionAsset 为 BTC。当前分支要求手续费币种等于报价币，直接抛错，即使 BTC 已在支持币种内且 RunnerFact 已支持 fee_currency。正常现货买入手续费可能属于这种输入，周期因此无法关闭。
- **复现**：合法成交字段、qty=1、price=100、commission=0.001 BTC；`_trade_rows` 抛 BinanceVenueLedgerError，理由仍称该币种无法表示。
- **修复建议**：保留成交报价币与实际手续费币种，沿已有 fee_currency 契约传递；不支持的币种才拒绝。
- **外部核对**：[Binance Commission FAQ](https://developers.binance.com/en/docs/products/spot/faqs/commission_faq) 说明手续费可从收到的资产扣除，BUY 的 received amount 是资产数量。

### DR-7 [P2] 平仓盈亏与 Binance 收益流水的费用及时间口径不一致

- **位置**：`src/custos/engines/nautilus/binance_ledger.py:419-445`；`src/custos/core/runner_fact_producer.py:614-634`；Crucible `crates/store/src/runner_fact_reconciliation_projector.rs:1224-1235`、`:1540-1565`。
- **触发与影响**：原生平仓值累计扣除手续费，Binance REALIZED_PNL 与 COMMISSION 分项处理；另外原生桥忽略 PositionChanged，独立流水却在部分减仓时产生已实现盈亏。消费端按期间直接比较两边数值，会把费用差或跨期部分平仓差当作账务不一致。
- **复现**：原生买入 1@100，两次各卖 0.5@110，三次手续费各 1；原生最终 realized_pnl=7，两笔独立 REALIZED_PNL 合计 10，部分平仓未产生内部盈亏事实。本项是账务语义复现，未运行真实账户或 PG 结算；当前原生上报还先被 DR-6 阻断，修好 ABI 后仍需修正口径。
- **修复建议**：定义统一的净/毛盈亏及事件归属周期；按实际实现收益的事件核对，或以完整持仓周期聚合并显式分离费用。
- **外部核对**：[Binance Income History](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account#get-income-history-user_data) 区分 REALIZED_PNL 与 COMMISSION 类型；[Binance PnL 说明](https://www.binance.com/ru/blog/futures/457299340443288694) 要求净利润计入开平仓手续费。

## Verification

```bash
uv run --extra dev --extra nautilus python .forge/reviews/2026-09-20-custos-deep-followup-repro.py
uv run --extra dev --extra nautilus pytest tests/test_engine_lifecycle.py tests/test_runner_policy_runtime.py tests/test_order_reservation.py tests/engines/nautilus/test_runner_safety_execution_boundary.py tests/test_strategy_signal_bridge.py tests/test_runner_fact_production_loop.py tests/engines/nautilus/test_binance_ledger_economic_rows.py tests/test_nt_trading_node_host.py -q
```

- 复现脚本 DR-1 至 DR-9 的现状断言全部通过；原生事件/持仓、真实 SQLite、真实签名策略验签均有参与。
- 既有定向测试：130 passed。没有把既有测试全绿等同于缺陷不存在。
- 脚本 Ruff 检查和格式检查通过。
- 全部探针离线，无真实交易凭据、无下单、无生产节点操作；交易所输入使用固定样本。
- 未修改应用源码、规则或旧审查报告，仅新增本报告与复现脚本。

## File-by-File Summary

| File | Review focus / findings |
| --- | --- |
| `src/custos/cli/_daemon.py` | DR-2；签名 daemon 组合 |
| `src/custos/core/engine_lifecycle.py` | DR-1；generation 与重试预算 |
| `src/custos/core/engine_protocol.py` | 身份、ready 与 terminal 类型交叉核对 |
| `src/custos/core/engine_safety.py` | DR-2；风险评估入口 |
| `src/custos/core/fallback_breaker.py` | DR-2；回撤与冻结状态 |
| `src/custos/core/order_reservation_boundary.py` | DR-4、DR-8；减仓与改单 |
| `src/custos/core/runner_command_runtime.py` | DR-1；签名命令到生命周期 |
| `src/custos/core/runner_control_consumer.py` | DR-9；策略更新落库后的运行时衔接 |
| `src/custos/core/runner_fact.py` | DR-8、DR-9；持久化敞口与策略换版 |
| `src/custos/core/runner_fact_producer.py` | DR-3、DR-6、DR-7；事件与心跳 |
| `src/custos/core/runner_safety_policy.py` | DR-9；有效策略解析 |
| `src/custos/core/runtime_admission.py` | 签名运行时准入边界 |
| `src/custos/core/zombie_watchdog.py` | 本地连接退化语义 |
| `src/custos/engines/nautilus/host.py` | DR-1、DR-3；节点和组合读取 |
| `src/custos/engines/nautilus/runner_safety.py` | DR-4、DR-8；改单、成交归属 |
| `src/custos/engines/nautilus/strategy_event_forwarding.py` | DR-3、DR-6；原生回调转发 |
| `src/custos/engines/nautilus/strategy_hooks.py` | 实例 hook 安装与验证 |
| `src/custos/engines/nautilus/portfolio_snapshot.py` | 现金和保证金口径复核 |
| `src/custos/engines/nautilus/binance_ledger.py` | DR-5、DR-7；手续费和收益 |
| `src/custos/engines/nautilus/okx_ledger.py` | 独立账本和时间窗口复核 |
| `src/custos/engines/nautilus/sodex_ledger.py` | 独立账本和分页复核 |
| `src/custos/engines/nautilus/ledger_http.py` | 只读请求与半开时间窗口 |
| `src/custos/artifacts/archive.py` | 解压路径、碰撞与资源限制抽查 |

额外交叉阅读了原生 Position 及 PositionClosed binding、现有测试夹具和 Crucible 对账投影。表中未列 finding 的文件只表示本轮未确认问题，不代表安全证明。

## Suggestions

1. 先修 DR-9 与 DR-2：保证风险策略换版、重启、价格变化后仍执行同一风险总额和回撤约束。
2. 联合修 DR-6、DR-3、DR-7：原生事件可靠入库、健康状态反映审计损失、跨账本经济口径一致。
3. 修 DR-1、DR-4、DR-8：覆盖已有节点更新、减仓改单与多笔入场；测试需包含真实宿主约束及原生事件。
4. 修 DR-5 并跑现货买卖双向、基础币手续费的账本回归。
5. 后续验收增加签名 daemon 组合层故障注入；保留默认 live 准入，完成源码和真实环境验收后再考虑生产启用。

## Risk Assessment

当前代码存在风险限额漏算、持续熔断未接线、平仓事实缺失和控制面状态失真。不能据上一轮本地通过数量认定可上生产。优先级是修复签名执行通道，再进行带持仓的策略换版、回撤、减仓和重启验收。

本报告中的“复现”是本地可重复的软件行为证据。真实交易所 API 连通、实际成交、部署中的 Ubuntu 环境以及本轮未覆盖的代码仍未验收。
