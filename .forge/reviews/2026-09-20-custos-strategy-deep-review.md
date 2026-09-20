# Code Review: Custos 策略内部状态与订单保护

> **Depth**: deep
> **Date**: 2026-09-20
> **Reviewer**: Codex，单执行面。
> **范围**: Toolkit 的订单协调、SL/TP、状态恢复与多币对资金分配。
> **审查基线**: 从 `ca49055` 开始；修复方并行提交了 Fix 04。已检查 `8ba4821` 对退出路径的改动，并在包含该改动的工作树上重跑探针与测试。测试记录时 HEAD 为 `799140d`；交付前核对至 `0b0e8bd`，此后提交未再改动本轮 Toolkit 审查范围。

下文 `adapter/` 指 `packages/custos-strategy-toolkit-nautilus/src/custos_toolkit_nautilus/adapter/`。
本轮仅新增本报告与复现脚本，没有修改修复方的源码、测试或计划。

## Summary

新增 8 项策略层问题：4 项 P1、4 项 P2。涉及 ATR 止损补单、撤单失败后的成交归属、分批止盈状态、多币对资金分配。既有定向测试 162 项通过，新增离线探针确认了这些组合条件。

这些结论不重新报告 DR/RR/RD 中的宿主 API、签名监督和普通平仓降级问题。ST-2 与 RD-5 属于“派发结果与策略状态未同步”的同类根因，但代码路径是独立的 Tick 部分止盈；Fix 04/2 修正普通退出后，此路径仍可复现。

| Priority | Count |
| --- | --- |
| P1：保护或限额执行错误 | 4 |
| P2：退出数量或内部状态错误 | 4 |

## Strengths

- 订单 tracker、模式枚举、协调器已分离，可以在不连接交易所的情况下组合验证。
- 现有取消逻辑试图保留有效保护单，多批次止损及部分成交也有专门测试。
- 普通退出路径已开始处理本地派发结果；这一契约可扩展到 Tick 止盈和保护订单。

## Concerns

### ST-3 [P1] ATR 止损修复路径主动清空 ATR，导致持续无法补保护单

- **位置**：`adapter/coordinators/order_reconciler.py:174-177`；`adapter/coordinators/sltp.py:185-208`。
- **条件**：EXCHANGE 模式、ATR 止损、已有持仓缺少全部或部分止损保护。
- **根因**：修复函数写入合成 pending signal 时固定传 entry_atr=None。后续 StopLossSubmitter 读取这个空值；实际 OrderPriceCalculator 在 ATR 缺失时返回 None，因而不产生订单。
- **复现**：入场价 100，已知 ATR=2、倍数 2，计算器可得到止损 96。协调器却覆盖 ATR 为 None；跨过三次修复冷却窗口仍发送 0 笔止损，保护数量保持 0。
- **建议**：恢复或持久化策略定义所需的 ATR，并把保护修复参数与正在执行的入场信号状态分开。不能用合成信号覆盖已有入场上下文。
- **验收**：缺少止损时能够补足 ATR 保护；部分入场期间修复不得丢掉原 pending signal/ATR；数据确实不足时明确暴露未受保护状态。

### ST-4 [P1] 未确认或失败的撤单会丢掉活跃入场单身份，随后成交不再建立保护

- **位置**：`adapter/coordinators/order_reconciler.py:553-581`；`adapter/coordinators/signal_execution.py:51-81`；`adapter/coordinators/trade_event_handler.py:54-60`。
- **根因一**：handle_order_cancel_rejected 无条件把失败解释成“订单已不存在”，清掉 entry_order_id，却不核实缓存终态。订单仍可继续成交。
- **根因二**：替换入场单时，发出旧单撤销请求后立即继续创建新单，单一 entry_order_id 被覆盖，没有等待旧单取消确认。
- **复现**：缓存中的入场单仍 open，收到撤单失败后 tracker ID 变为 None。随后该单成交，pending_signal 尚在，但成交不匹配 tracked entry，TradeEventHandler 提前 return，没有创建 SL/TP。另一用例确认旧单仍 open 时新单已提交并覆盖身份。
- **建议**：取消请求与取消确认分离；只有确认终态才能释放归属。替换应等待确认，或同时跟踪多笔仍可能成交的入场单。
- **验收**：撤单失败、撤单中成交、换信号后旧单迟到成交都必须更新真实持仓并按需建立保护，不得把自己的成交误判为外部成交。

### ST-2 [P1] 分批止盈在成交前永久推进层级，拒单还可能取消有效止损

- **位置**：`adapter/tick_monitor.py:426-445`；`adapter/coordinators/execution.py:105-130`；`adapter/coordinators/order_reconciler.py:91-101`、`:490-527`。
- **条件与根因**：check() 一旦达到价格就设置 level hit；派发失败、本地返回 False 或未成交时没有恢复机制。恢复函数为了读取当前行情调用 check()，又丢弃它返回的动作，同样能在未发单时消费层级。
- **附加归属问题**：部分 TP 订单没有加入 tracker.tp_order_ids。HYBRID 模式下它被交易所拒绝，会落入“普通减仓平仓拒绝”的处理分支，取消全部订单并清空 tracker，连现有安全止损一起撤销。
- **复现**：本地提交返回 False 后同价不再重试；重启恢复直接消耗一个层级而发送 0 单；有正常安全止损时拒绝部分 TP，实际 reconciler 发出取消该止损的请求。
- **建议**：层级需要明确的未触发、待成交、已完成状态，依执行回报推进；恢复读取不得消费动作；部分 TP 必须有明确订单角色和拒绝处理。
- **验收**：拒单、本地拒绝、部分成交、重启均不丢目标数量；TP 失败不撤销独立有效的 SL。

### ST-5 [P1] 资金分配明确拒绝后，协调器仍提交超出币对额度的订单

- **位置**：`adapter/coordinators/signal_execution.py:193-207`；`adapter/capital_allocator.py:76-83`。
- **条件**：配置了 CapitalAllocator 的币对额度。
- **根因**：allocate() 返回 False 被忽略；上下文已提前增加 allocated_capital，代码仍继续 submit_order。
- **复现**：总资本 1000、BTC 比例 10%，币对限额 100；请求 200。allocator 没有扣款，available_cash 仍为 1000，但订单 200 已交给下游，上下文 allocated_capital=200。
- **建议**：分配成功必须是提交前置条件；失败不得记录已分配资金或虚拟入场。后续派发失败需要归还对应预留。
- **验收**：单币对额度和总可用资金两类拒绝均阻止提交，并保持 allocator、context、订单状态一致。

### ST-1 [P2] Tick 分批止盈按剩余仓位重复计算比例，与交易所模式不一致

- **位置**：`adapter/coordinators/execution.py:110`；对照 `adapter/orders.py:934-939`。
- **条件**：scaled TP，后一个层级在前一个层级实际成交后触发。
- **根因**：Tick 路径用当时剩余 quantity × exit_pct；交易所模式用初始总量 × exit_pct。相同配置因模式或成交时序产生不同退出总量。
- **复现**：初始 1，两个层级各 50%。Tick 退出 0.5、0.25，剩余 0.25，两个层级均已标记完成；EXCHANGE 同配置生成 0.5、0.5。
- **建议**：明确定义比例基数并跨模式统一，记录计划退出总量、已成交量和剩余量，最后一档处理舍入余量。
- **验收**：50%+50% 与 33%+33%+34% 在逐档成交、跳空触发、部分成交下的总退出量一致。

### ST-6 [P2] 未成交的入场单终结后，资金和入场计数未回滚

- **位置**：`adapter/coordinators/signal_execution.py:193-205`；`adapter/coordinators/trade_event_handler.py:219-230`；`adapter/coordinators/order_reconciler.py:543-550`。
- **根因**：发送前记录 position entry 并占用资金；完全未成交就 canceled/rejected 时，只清订单 ID 和 pending signal。资金释放与 PositionTracker.reset 依赖 PositionClosed，但这类订单从未形成持仓，不会产生该事件。
- **复现**：资本 1000、入场请求 400，完全未成交后分别走取消和拒绝回报。缓存无持仓，allocator 可用资本仍为 600，context 占用 400，entry_count=1、has_position=True。
- **建议**：区分预留、已成交持仓和终结订单；按未成交剩余量返还预留，不把委托尝试计为已成交入场。
- **验收**：零成交拒单/撤单完整回滚；部分成交后撤单仅释放未成交部分，不擦除实际持仓。

### ST-7 [P2] TICK 保本止损未入跟踪器，之后的完整止盈无法完成撤单前置条件

- **位置**：`adapter/sltp_mode.py:66-71`；`adapter/coordinators/sltp.py:103-110`；`adapter/coordinators/execution.py:168-178`。
- **条件**：TICK 模式、启用 break-even、止盈为 fixed 或其他完整退出动作。
- **根因**：TICK 被允许 move_stop_to_break_even，代码创建并发送交易所止损，但只在 EXCHANGE/HYBRID 分支登记订单。完整 Tick 退出看到缓存存在 reduce-only 单便等待取消，取消函数却只遍历 tracker，找不到刚才那笔单。
- **复现**：实际协调器创建保本止损，两个 SL 跟踪集合均为空；连续超过止盈价格的 tick 不平仓、也没有撤单。孤儿扫描认为该单方向正确而保留，不能解除阻塞。
- **建议**：统一 TICK 保本止损的执行方式和订单归属；若此组合不支持，应在配置阶段拒绝，而非创建不可追踪订单。
- **验收**：达到止盈后，有确认地处理所属保本单并完成退出；不能无限等待一个从未发出的撤单请求。

### ST-8 [P2] equal 分配取决于币对注册顺序，首个币对可占全部资金

- **位置**：`adapter/capital_allocator.py:50-58`。
- **条件**：mode=equal、未显式指定 tiers、至少两个币对。
- **根因**：注册第一个币对时赋比率 1，第二个赋 1/2，却不调整已注册币对，也未实际按 equal mode 全局计算。
- **复现**：资本 200、两个币对，额度为 200 和 100；首个币对成功占用全部 200，第二个可用为 0。
- **建议**：在完整币对集合上计算 equal 权重，校验权重总和；注册顺序不应改变配置含义。
- **验收**：两币对应为 100/100，交换注册顺序结果不变；新增币对和显式 tier 的行为需明确。

## Verification

```bash
uv run --extra dev --extra nautilus python .forge/reviews/2026-09-20-custos-strategy-deep-repro.py
uv run --extra dev --extra nautilus pytest tests/toolkit/test_tick_monitor.py tests/toolkit/test_native_trailing_mode.py tests/toolkit/test_sltp_coordinator.py tests/toolkit/test_trade_event_handler.py tests/toolkit/test_capital_allocator.py tests/toolkit/test_signal_execution_coordinator.py tests/toolkit/test_execution_coordinator.py tests/toolkit/test_snapshot_coordinator.py -q
```

- 8 项探针的缺陷现状断言成立；既有定向测试 162 passed。
- 使用真实 Toolkit 协调器、tracker、金额计算器和 CapitalAllocator；价格/数量精度使用原生 CurrencyPair、Price、Quantity。
- 策略环境、缓存和订单派发/回报是受控替身；ST-1 模拟逐档成交，不冒充真实回测撮合或交易所成交。
- ST-2 已按 Fix 04/2 引入的“本地拒绝返回 False”契约重新验证；普通退出分支的修复不解决 Tick 部分退出状态。
- 仅新增审查材料；并行修复的 core/daemon 变动不作为本轮新结论。没有真实账户操作或下单。
- Ruff 与报告 diff 检查通过；没有更改历史 review 或其复现脚本。

## File-by-File Summary

| 文件 | 结论 |
| --- | --- |
| `adapter/tick_monitor.py` | ST-2；层级状态在检查时提前修改 |
| `adapter/coordinators/execution.py` | ST-1、ST-2、ST-7 |
| `adapter/coordinators/order_reconciler.py` | ST-2、ST-3、ST-4、ST-6 |
| `adapter/coordinators/signal_execution.py` | ST-4、ST-5、ST-6；已核对并行退出修复 |
| `adapter/coordinators/trade_event_handler.py` | ST-4、ST-6 |
| `adapter/coordinators/sltp.py` | ST-3、ST-7 |
| `adapter/capital_allocator.py` | ST-5、ST-8 |
| `adapter/sltp_mode.py` | ST-7 的合法配置入口 |
| `adapter/orders.py` | 保护与入场归属、scaled 数量对照 |
| `adapter/coordinators/snapshot.py`、`adapter/state_persistence.py` | 交叉阅读恢复路径，本轮不另列未经确认的问题 |

## Suggestions

1. 先修 ST-2/3/4，保证提交、撤单、拒单和恢复都不会丢失有效保护。
2. 将分配成功、派发成功、实际成交分别建模，修复 ST-5/6 的预留与入场状态。
3. 为各 SL/TP 模式添加同配置的数量与生命周期对照，修复 ST-1/7。
4. 用注册顺序无关的测试约束 ST-8；将这些探针转换为修复后的正式回归。

## Risk Assessment

本轮问题集中在“意图已经产生”被当成“动作已经成功”，以及不同执行模式对同一配置的解释不同。单独验证计算器或方法存在性无法覆盖这类错误，需要把状态变化、派发结果和实际回报连起来验证。

已明确提示的 TICK 模式固定止损限制没有作为新增发现；ST-7 指的是另外被允许的保本组合所产生的具体阻塞。
