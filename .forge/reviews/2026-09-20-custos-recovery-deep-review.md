# Code Review: Custos 恢复与账务链路深挖

> **Depth**: deep
> **Date**: 2026-09-20
> **Reviewer**: Codex，单执行面审查。
> **Custos 基线**: `4b9f9c9f7b9facb3985a4efec2964bffb5f08d61` 加尚未提交的 Fix 03 工作树。
> **消费端交叉阅读**: Crucible `2936a22dcd708b5051a8081d2418cd9bbc46c0a3` 加当前未提交对账投影修改。
> **范围**: 节点退出、命令 ACK 续租、成交记账、跨 generation 对账、Toolkit 平仓调度。

## Summary

本轮新增 5 项 P1 功能或一致性问题，不重复上一报告 RR-1 至 RR-9。均附离线复现。RD-4 执行了真实生产 SQL 的过滤逻辑，但使用 SQLite 验证关系过滤，没有运行 PostgreSQL 完整投影。

101 项已有定向测试通过。新增探针断言的是当前缺陷，不是修复验收。用户修复中的源码、规则和计划未修改；本轮只新增本报告及复现脚本。

| Priority | Count |
| --- | --- |
| P1 | 5 |
| P2 / Info | 0 |

## Strengths

- 命令、应用结果和事实 outbox 使用同一持久化状态体系，可以直接检查状态转换是否一致。
- 生命周期已有 typed terminal 接口与有界重启方法，具备补齐运行接线的基础。
- Toolkit 已有一次性平仓降级控制，能避免在真实已发送后重复提交。当前缺口在本地拒绝与“已发送”的衔接。

## Concerns

### RD-3 [P1] 已发生的超限成交被风险记账事务回滚

- **位置**：`src/custos/core/runner_fact.py:4293-4301`；`src/custos/core/order_reservation_boundary.py:280-291`。
- **触发**：市场成交价偏离预估。原预留 90，实际成交名义额 120，策略每单上限 100、总上限 150。
- **问题**：成交已经发生，store 却因超过限额抛错并回滚。边界只在内存冻结 breaker，持久化账面仍是预留 90、已成交数量 0、已成交敞口 0。源码注释所说的重建没有生产调用入口。
- **本地实证**：原生 OrderFilled、实际 RunnerReservationBoundary 和真实 SQLite。回调后 breaker 冻结，但成交 120 未记入。重开数据库后再预留 50 成功，账面总额为 140；按已收到的实际成交加新预留则是 170，超过总上限 150。
- **影响**：异常成交恰好是最需要保留的事实；拒记会让恢复后的额度计算失真，平仓核销也缺少对应数量。
- **建议**：把已执行成交与超限状态一并持久化，再拒绝未来风险增加操作。恢复必须保留冻结/待对账状态，或在可信重建完成前禁止恢复接单。
- **验收**：90 预留、120 成交必须保留实际数量与成本；重开数据库后新增 50 仍应因真实总额超限而拒绝。重复成交事件不得重复记账。

### RD-5 [P1] 平仓降级单被当作新增敞口拒绝，且未发送就耗尽唯一机会

- **位置**：`packages/custos-strategy-toolkit-nautilus/src/custos_toolkit_nautilus/adapter/coordinators/signal_execution.py:258-294`；`src/custos/core/order_reservation_boundary.py:339-352`。
- **触发**：已持有敞口 100、总限额 150，已有交易所拒绝 reduce-only 的记录，Toolkit 准备用同数量普通反向订单平仓。
- **问题**：降级单的 reduce_only=False，被订单边界按新增 100 预留，因 100+100>150 拒绝。gate 返回 None；调度器却在调用前已标记 plain_close_submitted，调用后又标记 closing，无法区别“成功提交”和“本地未发送”。
- **本地实证**：真实 SignalExecutionCoordinator、OrderTracker、订单边界及 SQLite；现有持仓已使用新格式 lot。平仓订单没有到达下游，tracker 却已消耗唯一普通平仓机会。超过 in-flight 时间再次触发退出，仍不产生第二次尝试，持仓保留。
- **影响**：正常退出可能在额度接近上限时被阻止；一次本地拒绝还会永久耗尽该持仓的降级机会。
- **建议**：为受控平仓保留可验证的减仓语义；将本地拒绝传回调度器，只有实际 dispatch 后才能消耗一次性机会及设置 closing。
- **验收**：已有敞口 100、总限额 150 的等量平仓能够到达执行端；本地拒绝不消耗 plain-close 机会；真实提交后仍不得重复发送。

### RD-1 [P1] 节点异常退出后脱离监督，持久化 ready 未更新且重投不恢复

- **位置**：`src/custos/engines/nautilus/host.py:1724-1745`；`src/custos/core/engine_lifecycle.py:233`；`src/custos/cli/_daemon.py:918-985`。
- **触发**：已经应用成功的节点运行任务自行结束或抛异常，而 daemon 的 NATS、观测、周期任务仍正常。
- **问题**：完成回调删除 active node 和 RunnerFact context，只记录日志。daemon 的监督对象不含节点任务，EngineLifecycleSupervisor.supervise_once 在生产源码中没有调用者；观测和风险循环也因 context 已删除而不再枚举它。
- **本地实证**：实际宿主完成回调、故障 asyncio task、已签名命令和真实 SQLite。节点消失后 durable observed_status 仍为 ready；同命令重投经实际 intake 被判定为已终结幂等重放，仅 ACK。源码 AST 交叉检查没有 supervise_once 调用。
- **影响**：没有触发既有重启预算、终止事实或隔离流程；需要其他外部机制、全进程重启或新 generation 才可能恢复。下游可能后来根据心跳超时发现异常，本报告不声称其永远保持 online。
- **建议**：为每个活跃实例接入 terminal 监督并更新 durable outcome；仅当当前 desired 仍要求同一代 running 时进行有界恢复，避免复活已停止或被替代的 generation。
- **验收**：ready 后注入 node task failure，必须产生终止/退化观察并触发有界恢复或隔离；同一命令重投不能掩盖已经失效的引擎。

### RD-2 [P1] ACK 续租失败可以覆盖已提交的部署成功结果

- **位置**：`src/custos/core/runner_command_runtime.py:285-304`、`:349-354`；`src/custos/core/runner_fact.py:3255-3277`。
- **触发**：最后一次消息投递期间，in_progress 因传输瞬时失败抛错；部署操作随后成功并持久化 applied。
- **问题**：heartbeat 子任务先失败，主操作仍继续。finally 中 await heartbeat_task 又抛出该错误，覆盖成功返回。外层按 apply failure 处理，在 delivered_count 达到 max_deliver 时写 retry_exhausted/quarantined，但没有终止已运行引擎。
- **本地实证**：实际命令签名验证、intake、runtime coordinator 与 SQLite；部署端口用受控成功操作替身。注入续租错误后，持久化 outcome 先 applied，后 retry_exhausted；状态变为 quarantined，交付执行 TERM，而执行替身仍为 running。
- **影响**：传输层短暂错误被错误解释为业务应用失败，生成相互矛盾的运行状态和事实。
- **建议**：协调续租任务和主操作的失败语义；终态决定必须检查已经持久化的成功结果。中途失败应进入明确的取消或幂等恢复路径，不能由 finally 覆盖成功结果。
- **验收**：续租失败、随后 applied 成功，且 delivered_count=max_deliver 时，不得把同一个成功部署改写为“失败隔离而仍运行”；补测续租挂起与主操作取消的交接。

### RD-4 [P1] 同周期切换 generation 后，对账包含旧代外部成交却过滤旧代内部成交

- **位置**：Custos `src/custos/core/runner_fact_producer.py:765-800`；Crucible `crates/store/src/runner_fact_reconciliation_projector.rs:1160-1175`。
- **触发**：一个对账周期内，实例由 generation 1 切换为 generation 2，旧代在切换前已经成交。
- **问题**：周期状态以不含 generation 的 instance stream_key 保存，关闭时使用新的 authority，但仍从旧周期开始采集交易所流水。消费端内部事件查询则限定当前 generation/spec/digest，删除旧代同周期成交。
- **本地实证**：实际 run_periods，带独立估值检查点；12:00:10 为旧代成交，12:00:30 换代，12:01:01 关闭周期。gen2 批次仍包含覆盖自 12:00:00 的外部旧成交。将消费端原查询在临时 SQLite 执行，gen1 查询返回该有效成交，gen2 查询返回空。未运行 PostgreSQL 完整 freeze_input_set。
- **影响**：两边金额不同不是交易异常，而是比较集合不同；正常部署更新就可能产生假对账差异。
- **建议**：在 generation 切换时划清可对账区间，或在实例域中按已验签来源与业务时间安全组合跨代事实；生产与消费侧必须采用同一范围。
- **验收**：同一周期内有换代前后成交、停机短于/长于采集间隔的场景，均应做到每笔成交比较一次，既不漏旧代，也不重复结算。

## Verification

```bash
uv run --extra dev --extra nautilus python .forge/reviews/2026-09-20-custos-recovery-deep-repro.py
uv run --extra dev --extra nautilus pytest tests/test_runner_command_runtime.py tests/test_runner_fact_store.py tests/test_engine_lifecycle.py tests/test_order_reservation.py tests/test_runner_fact_production_loop.py tests/toolkit/test_close_reduce_only_fallback.py tests/test_nt_trading_node_host.py -q
```

- 5 项新增探针的缺陷现状断言成立。
- 既有定向测试 101 passed，说明当前测试未覆盖这些组合条件。
- RD-1/2 使用真实命令验签、真实 SQLite 和实际协调方法；RD-2 的引擎应用端口为替身。
- RD-3 使用原生成交事件和实际风险边界；额外 50 是本地预留，不是实际市场下单。
- RD-4 使用真实生产循环与检查点生成；消费端 SQL 在 SQLite 验证，未冒充 PG 集成测试。
- RD-5 使用实际 Toolkit 调度、tracker、边界及 SQLite，下游提交端为可观测替身。
- 所有测试离线；没有连接交易账户、读取真实密钥、下单或操作运行中的容器。
- 复现脚本 Ruff 检查通过。Custos `src/custos` 审查前后未发生变化；仅提交审查材料。

## 对上一报告的范围补充

上一报告 RR-2 的停机撤单类型问题适用于直接继承原生接口、没有对象到 ID 兼容层的策略。
Toolkit 的 `NautilusStrategyCore._venue_cancel_order` 已在
`packages/custos-strategy-toolkit-nautilus/src/custos_toolkit_nautilus/adapter/strategy_core.py:498-505`
完成转换。修复方应保留这段有效适配；不要把原生裸接口复现推广为所有 Toolkit 策略均无法撤单。
单笔 modify_order 的接口差异与本轮 RD-5 是独立问题。

## File-by-File Summary

| 文件 | 新发现 |
| --- | --- |
| `src/custos/engines/nautilus/host.py` | RD-1 节点退出移除监管对象 |
| `src/custos/core/engine_lifecycle.py` | RD-1 terminal supervisor 没有生产调用者 |
| `src/custos/cli/_daemon.py` | RD-1 组合层缺少节点生命周期任务 |
| `src/custos/core/runner_command_runtime.py` | RD-2 续租异常覆盖成功 |
| `src/custos/core/runner_command_intake.py` | RD-1 重投的实际 ACK 路径；RD-2 终态衔接 |
| `src/custos/core/runner_fact.py` | RD-2 状态改写、RD-3 成交回滚 |
| `src/custos/core/order_reservation_boundary.py` | RD-3 内存冻结、RD-5 错计新增预留 |
| `src/custos/core/runner_fact_producer.py` | RD-4 跨代周期状态 |
| Toolkit `adapter/coordinators/signal_execution.py` | RD-5 本地拒绝仍消耗一次性机会 |
| Toolkit `adapter/strategy_core.py` | 核实对象到 ID 的既有撤单兼容层 |
| Crucible `runner_fact_reconciliation_projector.rs` | RD-4 内部来源 generation 过滤 |

## Suggestions

1. 先修 RD-3、RD-5，保证真实成交不丢、平仓不会因本地状态失配被永久阻断。
2. 修 RD-1、RD-2，统一实际引擎、持久化状态和消息交付状态。
3. 协调修复 RD-4，用跨 generation 的生产/消费组合用例验证，不只单测一侧。
4. 把本报告探针改写为正式回归测试，断言修复后的行为；保留原探针作为本次审查时点证据。

## Risk Assessment

这 5 项主要是事件已经发生后，记录、状态机或比较范围没有同步。继续增加孤立方法的 happy-path 测试不足以防止复发，需要验证节点退出、消息错误、异常成交、换代与本地拒绝这些组合路径。
本报告是个人项目的本地代码审查，不是运行中交易账户的验收结论。
