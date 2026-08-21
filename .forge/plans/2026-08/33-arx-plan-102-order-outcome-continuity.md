# 33 — ARX Plan 102：策略决策与订单结果必须跨过本地拒单边界

> **Status**: 🚧 In Progress — RC7 exposed partial-fill protection and reversal residue; RC8 pending
> **Created**: 2026-08-13
> **Project**: custos (`tesseract-trading/custos/`)
> **Parent plan**: ARX `.forge/plans/2026-08/102-arx-professional-workflow-contract-continuity-and-mode-safety.md` F18 / F21 / F22 / F23 / T3 / T4

## 运行时发现

ARX Plan 102 的 RC3 testnet 运行中，两个 SuperTrend 实例均形成真实
`ENTER_LONG` 决策并创建市场单，但订单在 Custos 本地安全边界被拒后，ARX 的
signals/orders/positions/TCA/flow 仍全部为空。运行日志同时证明同步 Nautilus
回调创建了未 await 的 reservation coroutine。

这不是“策略没有信号”，而是结果契约在本地执行边界断裂：

1. StrategySignal 只在 `OrderSubmitted` 产生，本地拒单永远到不了该事件；
2. 同步 MessageBus 回调调用 async store API，没有形成耐久 reservation lifecycle；
3. 市场单只尝试显式价格和 MID，没有使用已订阅的 mark price；
4. 下游只有 fill join，并把存在 client order ID 推断为 submitted，没有签名拒单结果。

RC4 随后再次形成两个真实 `ENTER_LONG` 决策，并把此前不可见的问题精确缩小到
执行命令 ABI：Nautilus 构造参数名为 `command_id`，公开属性却是 `id`；既有测试替身
虚构了 `command.command_id`，导致真实订单预留抛出 `AttributeError`。异常又被统一标成
“名义金额策略拒绝”；生成本地拒单后，终态订阅器还尝试释放从未建立的预留，最终
使 Custos runner 退出。联动回收后的 ARX 502 是次生现象，不是独立 owner-query 故障。

RC6 修复了成交滑点结算后，两个实例均完成真实成交并向 Binance 提交了保护性
`STOP_MARKET`，但又证明“同一凭证范围”不是两个独立执行账户：两个节点同时接收
账户级成交与净持仓，首笔成交被两个实例归属，两个保护单随后又被对方的 stale sweep
交叉撤销。场所最终存在 0.0140 BTC 净多仓而无任何普通或条件开放订单。精确
reduce-only 清理和零仓位/零开放订单复查已完成；该数据不能作为实例级 TCA/经济守恒证据。

单实例 RC7 随后证明 `OrderFilled` 是逐成交事件，不等于整单终态。`0.0070` BTC
开仓分成 `0.0031 + 0.0039` 两笔成交，首笔事件就清除了 pending signal/entry，因而
只提交 `0.0031` 保护；反向订单又只为首笔 `0.0015` 成交提供保护。canonical down
后交易所仍有 `-0.0070` BTC 空仓和一张 `0.0015` 算法保护单。按精确前置条件执行
一次性清理后，独立签名读取确认仓位、普通单和算法单均为零；RC7 仅是 F23 失败证据。

## 决策

- 策略决策在 `OrderInitialized` 时写入 StrategySignal；订单是否通过本地安全门或场所接受，属于后续独立结果。
- 同步执行回调只调用同步 SQLite authority API；async API 继续供 async 调用者使用。
- 市场单估值顺序为显式价格 → mark price → MID；没有可靠价格继续 fail-closed。
- 在既有、签名、实例级 `RunnerRuntimeLogFact.v1` 上承载结构化
  submitted/rejected/canceled/expired lifecycle。禁止解析 stdout，也不扩写已 byte-pin 的 RunnerFact union。
- 拒单 reason code 经 redactor 和下游白名单翻译；未知技术代码不能成为面向用户的主文本。
- 命令身份只通过边界适配器读取真实 Nautilus 公共 `id`，测试必须至少覆盖一个真实
  `SubmitOrder`，不能再由宽松替身定义生产 ABI。
- 无预留的本地拒单/风控拒单终态是合法 no-op；仅存在耐久预留时才执行释放。
- 名义金额限额、fallback breaker 冻结和安全边界不可用使用三个独立稳定 reason code，
  不把程序错误伪装成业务风控拒绝。
- 市场单以提交时 mark price 预留、以场所真实 fill notional 结算；真实成交略高于
  报价预留时，只要仍满足签名单笔和 Runner 总 cap，就按成交额入账，不能把正常滑点
  当成越界后杀死交易节点。
- 已经发生的场所成交不可回滚。若成交记账或 reduce-only 敞口归属异常，执行回调必须
  保持存活并冻结新增风险，由可信 portfolio rebuild 对账；保护性减仓和撤单仍可继续。
- reduce-only fill 通过 Nautilus Position 的 `opening_order_id` 回到原开仓预留，不要求
  减仓单自身拥有风险增加预留。
- 实例边界只结算本实例拥有预留或本地初始化的订单；账户流中的外部 fill/cancel 不得
  消耗本地 pending signal、生成本地保护单或进入实例级签名生命周期流。
- stale sweep 在没有本地所有权证据时保留同向保护单；空仓或方向错误仍可按原规则清理。
- testnet/live 同一 `(trading_mode, credential_scope)` 只允许一个活动实例，并在创建
  `TradingNode` 前拒绝冲突，避免失败清理停止共享 asyncio loop；sandbox 不受此限制。
- `credential_scope` 仍不是场所证明的 account/subaccount identity。多真实实例保持
  fail-closed，直到 owner contract 绑定场所、账户/子账户、持仓模式和持仓命名空间。
- 本地 entry 的每个部分成交都必须立即增加等量 reduce-only 保护 lot；不能等待整单
  终态，也不能撤旧换新制造保护空窗。所有保护 lot 都要进入 tracker、关闭清理和 stale
  sweep 所有权集合；entry/pending signal 仅在源订单终态后释放。
- 订单/成交/TCA 保留逐 trade 事实，保护覆盖量以实时净持仓为准；反向信号不能把“曾经
  短暂平仓”误认成终态 flat。canonical down 前后均执行仓位、普通单和算法单零残留门。

## Tasks

| Task | 状态 | 验收 |
|---|---|---|
| T1 决策时点 | ✅ | `OrderInitialized` 即产生实例级 StrategySignal；本地拒单也保留决策 |
| T2 reservation durability | ✅ | submit/modify/reject/cancel/fill/reduction 的同步回调均同步提交 SQLite；无 dropped coroutine |
| T3 市场单估值 | ✅ | mark price 可用于 notional；无可靠价格仍拒绝 |
| T4 签名订单生命周期 | ✅ | submitted/rejected/canceled/expired 走现有 RunnerFact authority channel |
| T5 跨仓结果投影 | ✅ | Crucible → ARX BFF → Web 保留 lifecycle time 和 rejection reason |
| T6 real testnet | ⏳ | RC7 已证明单实例真实成交链路，但部分成交只保护首笔并在 down 后留下反向仓位；须修复 F23 后由 RC8 取得逐 fill、保护覆盖和 owner API 终态 trace |
| T7 执行账户与订单所有权 | ✅ | 外部账户事件不再进入实例事实/消费 pending signal；未知同向保护单不被无权撤销；真实场所同 credential scope 单活，sandbox 保留多实例 |
| T8 部分成交与保护覆盖 | ⏳ | 每个 owned fill lot 立即得到等量保护；全部保护单可跟踪/取消；订单终态才清 entry/pending；反转与 down 均通过零残留门 |

## 当前验证

- F23 focused：107/107（逐 fill 保护量、反转 offset、终态关联、多保护单取消、
  `PositionClosed` 跨部分反转保持、重启后全 protection lot 认领、保护覆盖差额重建及
  单个保护单拒绝隔离）；完整 toolkit：1370/1370；执行边界、reservation、签名事实和
  host 组合：61/61。
- Ruff：本计划触及文件 format/lint 通过；`git diff --check` 通过。
- 全库 pytest：2400 passed、24 skipped、1 xfailed；另有一个既有 toolkit producer
  receipt SHA 漂移失败，以及三个 RC wheel authority 测试因当前任务源码尚未提交而
  正确拒绝 dirty source。本轮此前出现过的本地 OCI registry `127.0.0.1` bind `EPERM`
  未再复现。计数门已按本计划新增测试更新并单独复跑为 17/17。

### 测试文件计数（`pytest --collect-only`）

本计划给既有安全边界和 toolkit 文件增加订单所有权、账户级外部成交、保护单保守清理
与执行账户单活测试，因此按仓库纪律重新计数：

| 测试文件 | 条数 |
|---|---|
| `tests/engines/nautilus/test_runner_safety_execution_boundary.py` | 20 |
| `tests/toolkit/test_native_trailing_mode.py` | 35 |
| `tests/toolkit/test_order_tracker_close_guard.py` | 15 |
| `tests/toolkit/test_sltp_mode.py` | 31 |
| `tests/toolkit/test_sltp_coordinator.py` | 14 |
| `tests/toolkit/test_trade_event_handler.py` | 12 |
| `tests/test_nt_trading_node_host.py` | 26 |
| `tests/test_order_reservation.py` | 8 |
| `tests/test_strategy_signal_bridge.py` | 8 |
| `tests/test_plan_closeout_counts.py` | 17 |

## 边界

- RC4、RC5、RC6、RC7 都是本地 testnet、`promotable=false` 的失败/纠偏证据，不能解释为生产就绪。
- Nautilus 首次连接失败会停止共享 event loop 的 F17 仍是独立开放缺陷；成功的 happy path 不能关闭它。
- 策略 toolkit producer receipt 保留其当时记录的 source SHA 作为历史 revision
  证据；当前 toolkit 源码与内部契约由 Git/测试/CI 管理。普通源码变化既不由历史
  receipt 永久锁定，也不授权重写或刷新该历史 receipt。

## F17 重试循环继续验证（2026-08-14）

- F17 源码修复新增一条真实 host 生命周期回归，使
  `tests/test_nt_trading_node_host.py` 的实际收集数从 25 增至 26；上表已按
  本活计划重新计数，不改写更早计划的历史计数。
- 失败启动/停止清理只释放 Nautilus kernel/executor，不再调用会停止 Runner
  supervisor 共享 asyncio loop 的顶层 `TradingNode.dispose()`。本轮 focused
  receipt/count 回归 2/2、`make check-authority` 与全库 `make test` 均通过；全库
  结果为 2412 passed / 24 skipped / 1 xfailed。
- 这只修复并锁住代码路径。F17 的真实 testnet 首次连接失败注入、同一 Runner
  进程消耗 restart budget 后恢复，仍是开放验收门；不得由单测或成功 happy path
  替代。

## F17 实服务注入补充收尾（2026-08-14）

前一节的开放状态已由 ARX Plan 102 的同 Runner 实服务证据取代。真实首次连接失败继续
暴露两项二阶缺陷：节点 executor 被关闭后仍是 Runner loop 的默认 executor，以及重试
复用已 `DISPOSED` 的 strategy。两项均以失败测试先行修复；随后任务专用 testnet 外层
容器在真实 Binance DNS/TCP refusal 后，于同一 Runner PID 566、同一 start tick
`43466501` 内把 durable `restart_count` 从 0 消耗到 1 并恢复 `ready`。该结果只关闭
local/testnet/non-production 的 F17 验收，不构成生产就绪。

本补充新增一条 renewable strategy 回归，因此以最新 close-out 重新声明当前文件计数，
不改写上方历史 26 条记录：

| 测试文件 | 条数 |
|---|---|
| `tests/test_nt_trading_node_host.py` | 27 |

变更后计数门 17/17 通过；全库 `make test` 为 2413 passed / 24 skipped /
1 xfailed。此前一次全量运行的唯一失败正是该文件仍声明 26，追加最新声明后同一门与全库
均转绿。
