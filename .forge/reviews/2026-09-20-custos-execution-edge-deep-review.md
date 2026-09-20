# Code Review: Custos 执行边界、恢复归属与事实队列

> **Depth**: deep
> **Date**: 2026-09-20
> **Reviewer**: Codex，单执行面
> **源码基线**: `e46bd4e`，已包含 Fix 06/07。下文行号按该提交。
> **并行修改**: `513e612` 新增 Fix 08 计划，工作树中的 execution.py 和对应测试继续变化。本轮没有修改、暂存或提交这些文件；六个探针在当前工作树上重跑仍成立。

## Summary

新增确认 **6 项：4 项 P1、2 项 P2**。重点是普通平仓并发产生反向仓位、反向入场拒绝后的保护空窗、桥接器恢复归属，以及独立事实流之间的发送阻塞。

固定基线 `e46bd4e` 的相关测试 **1,452 passed**。本轮六个复现入口全部通过；普通平仓问题使用真实 Nautilus 回测撮合，并有 reduce-only 对照组。没有连接真实账户、下单或联网调用场所 API。

这些问题不替代前轮 RS 报告。本轮没有将仍待修复的 RS 问题重复计数，也没有把修复方正在进行的扰动测试当成最终代码缺陷。

## Strengths

- Fix 06 已将资金拒绝转为入场前置条件，普通撤单失败也保留订单归属；需要继续覆盖这些修复与既有反向交易流程的交互。
- 新的分批止盈有 pending/filled 状态，并处理部分退出后的剩余配额；缺口转移到分批入场时如何维护基数。
- 事实 outbox 在失败时保留数据，PubAck 后才删除；本轮问题是调度公平性，不是失败时丢弃队列。
- 已有 SQLite、桥接器、原生引擎测试基础，能用确定性离线场景扩展覆盖。

## Concerns

### EE-1 [P1] 普通平仓没有扣除在途平仓数量，冻结状态下仍可反向开仓

**位置**：`src/custos/engines/nautilus/runner_safety.py:149-170`、`:518-531`；`src/custos/core/order_reservation_boundary.py:345-347`。

非 reduce-only 订单只要方向与当前仓位相反、单笔数量不超过缓存仓位，就被判定为减仓。判定没有减去已经获准、尚未成交的普通平仓数量；边界因此跳过冻结检查与金额预留。

**原生引擎复现 A**：缓存有多仓 1，breaker 已冻结；在同一回调内通过实际 RunnerSafetyOrderGate 发送两笔 SELL 1、reduce_only=False。两次都返回 True，两次都未调用 reservation store。原生撮合完成后，实际持仓为 **空头 SHORT、数量 1**：第一笔平多，第二笔开空。

**原生引擎复现 B**：安装实际 install_order_gate 后，连续两次调用原生 close_position(position, reduce_only=False)，仍得到 SHORT 1。这个接口没有被安装清单包装，不能只修 submit_order 的判断而忽略它。

**对照**：同一撮合场景改用 reduce_only=True，最终 positions_open 为空，不会开出反向仓位。

**影响边界**：该问题要求普通平仓重叠或另一个退出先消耗旧仓。不是断言所有 reduce-only 平仓都有风险，也不要求策略恶意操作；重复退出回调/并行保护动作就可能满足条件。当前探针验证真实策略缓存、实际门控与模拟成交，不涉及生产授权签发。

**建议及验收**：普通平仓也需要可串行化的剩余可平数量归属，订单终态后释放；穷举原生 close_position/close_all_positions 出站路径。测试两笔普通平仓、普通平仓与止损竞争、部分成交后补单，以及冻结期间最终净仓不得翻向。

### EE-2 [P1] 反向入场被资金分配拒绝前，旧仓唯一止损已被撤销

**位置**：`packages/custos-strategy-toolkit-nautilus/src/custos_toolkit_nautilus/adapter/coordinators/signal_execution.py:151-160`、`:202-212`。

反向分支先 cancel_all_orders 并 clear tracker，然后才构造订单、申请资金。Fix 06 让 allocation=False 正确阻止提交，但此时旧仓的保护撤单已经发出，失败路径没有恢复它。

**复现**：HYBRID 模式，已有多仓 1，价格 100，止损 95；资本 100 已分配给旧仓。请求反向新空仓金额 100，流程申请 close+open 的 200 并被拒绝。结果新入场发送 **0 笔**，旧止损却收到取消请求，tracker 覆盖数量为 0；确认该取消后，旧多仓仍持有且没有止损。

**影响边界**：后续 bar 的自愈可能重新补单，因此这里是保护空窗，不能宣称永久无法恢复。配置拒绝本身也不应以先撤掉有效保护为代价。后续本地派发拒绝/订单构造失败具有相邻风险，需一起检查。

**建议及验收**：无副作用检查和所需资源预留应先完成；替换保护需要明确的交接流程及失败恢复。验证所有拒绝出口都保留旧仓已接受的保护，不能仅断言“未发送新入场”。

### EE-3 [P1] 事件桥接器重建后丢失旧订单归属，后续成交被静默过滤

**位置**：`src/custos/core/runner_fact_producer.py:298-300`、`:324-329`；宿主构造入口 `src/custos/engines/nautilus/host.py:836-848`。

_owned_order_ids 仅在当前对象收到 OrderInitialized 时填充。新的桥接器从空集合开始，bootstrap 只注册回调；宿主没有注入缓存或持久化订单归属。对于恢复后继续工作的旧订单，若原始初始化事件没有被重新投递，其后续成交直接 return，既不产出执行/结算/费用事实，也不触发 sink failure。

**复现**：桥接器 A 接受初始化和部分成交；同一 deployment authority 创建并 bootstrap 桥接器 B，再投递同一订单剩余成交。B 产生 0 个事实批次；同一个事件交给仍持有归属的 A，正常产生 execution_fill / settlement_fill / settlement_fee。

**验证边界**：这是实际桥接器与宿主构造路径的恢复状态缺口；探针控制回调，未运行完整 LiveNode+Redis 重启。结论的明确条件是“旧订单恢复后没有重新投递 OrderInitialized”，不能把它写成所有恢复配置都会丢成交。策略对重启后现有保护单的认领不会更新这个独立集合。

**建议及验收**：从持久化、实例绑定的订单身份恢复归属，并在接收回报之前装载。不要简单移除过滤，因为它还承担账户级回报隔离。补上原生缓存/持久化重启场景：入场单剩余成交、已有止损触发，以及手工/其他实例订单仍被排除。

### EE-4 [P1] 故障流占满第一页后，健康流永久得不到发送机会

**位置**：`src/custos/core/runner_fact.py:2103-2115`、`:5867-5870`、`:5903-5906`；信号队列具有同类结构 `:2128-2140`、`:5915-5918`。

pending 默认全局取 64 条，按 stream_key 再按序号排序。发送器虽然用 blocked_streams 避免在一轮中继续发送已失败流，但没有继续读取其他页；下轮仍取完全相同的前 64 条。

**复现**：真实 SQLite outbox 入队 64 个 sandbox 批次和 1 个 testnet 批次，两个模式都有有效测试 transport profile。受控 broker 仅让 sandbox 失败、testnet 正常。连续三轮 drain_once 均为 0，只尝试 sandbox 首批；testnet 批次 attempts 仍为 0。把同一 testnet 批次放入没有前序积压的 outbox，立即成功。

**影响**：切换模式或部署后，旧流的持久化积压可以长期阻断健康流的成交、心跳、生命周期事实。数据仍在磁盘，但健康部署失去事实发布进展。

**建议及验收**：在保留每个流内部顺序的同时，按流公平调度或继续翻页跳过本轮受阻流。验证超过页大小的故障积压、健康模式发布、多个部署实例和 StrategySignal 队列。

### EE-5 [P2] 分批入场只以第一笔成交量初始化止盈基数，后续成交没有补入

**位置**：`packages/custos-strategy-toolkit-nautilus/src/custos_toolkit_nautilus/adapter/sltp_mode.py:103-110`、`:130-135`；`adapter/orders.py:172-189`；`adapter/tick_monitor.py:493-531`。

Fix 06 的固定止盈基数取 monitor 初始化时的 position.quantity。第一笔入场部分成交就触发初始化，后续入场成交 initialize_position=False，不再更新该基数。

**复现**：同一入场单先成交 0.5，再成交 0.5，真实仓位 1，但 initial_quantity 固定为 0.5。两个 50% 层级分别平掉 0.25、0.25，所有层级完成，仍留下 **0.5 持仓**，后续更高价格也没有退出动作。

**与 ST-1 的区别**：ST-1 修的是每次按剩余仓位重复乘百分比；本轮是修复后的固定基数在分批入场时取小了。两者需要不同的回归输入。

**建议及验收**：维护最终开仓暴露或按每笔新增成交追加止盈配额，同时保留已完成量，不能粗暴 init_position 重置已有层级。覆盖入场分批成交、部分取消、加仓以及入场尚未结束时已经止盈。

### EE-6 [P2] 完全成交的价格改善仍留下资金预留，平仓后持续占用上限

**位置**：`src/custos/core/runner_fact.py:4357`；回报接入 `src/custos/core/order_reservation_boundary.py:236-289`。

剩余预留计算为 reserved_notional - fill_notional，未结合源订单是否已全部成交。假设下单估价 100，整笔数量 1 实际以 90 成交，代码把多出的 10 当成仍等待成交的金额。完全成交路径没有后续 cancel/reject 去释放这部分；平仓只减少 filled_exposure，保留 reserved_notional。

**复现**：真实 SQLite 状态库中，单笔上限 100、总上限 150。六次“预留 100→整笔数量 1 在 90 成交→数量 1 全部平仓”，每次遗留 10。最终 open_exposure=0、reserved_notional=60；下一笔 100 的正常订单因为 160>150 被拒绝。实际没有持仓或待成交数量。

**验证边界**：探针直接驱动实际持久化生命周期函数；没有模拟真实价格改善撮合。边界回调当前不传入最终剩余数量或终态，也没有在全成后额外释放，因此这种输入不能被正确区分为“全成”与“金额部分成交”。

**建议及验收**：预留对应未成交数量；终态必须清除未使用的价差预留。验证多价格分批成交、整单价格改善、取消剩余，以及最终平仓后无活跃订单时预留归零。

## Verification

复现入口：

```sh
uv run --extra dev --extra nautilus python .forge/reviews/2026-09-20-custos-execution-edge-deep-repro.py
```

本次使用现有 `.venv/bin/python`，六个探针全部通过；EE-1 包含两个缺陷路径与一个 reduce-only 对照，EE-4 包含健康流独立发送对照。脚本通过 Ruff check/format。

既有测试命令：

```sh
python -m pytest tests/toolkit tests/test_order_reservation.py tests/test_runner_fact_outbox.py tests/test_strategy_signal_bridge.py tests/engines/nautilus/test_runner_safety_execution_boundary.py -q
```

- 最初在共享工作树运行，期间 execution.py 正在修改/扰动，测试停滞后已停止；仅观察到 1,150 passed，**不作为完整通过证据**。
- 随后将 `git archive e46bd4e` 展开到临时目录，PYTHONPATH 指向该快照的 src 与两个 Toolkit 包，用同一依赖环境重新运行：**1,452 passed, 1 warning in 6.57s**。warning 为 vendored pandas-ta 的无效转义 SyntaxWarning。
- 固定快照测试使用 45 秒超时；本轮探针另外在当前工作树复跑。没有修改共享源码来获得通过结果。

## File-by-File Summary

| 范围 | 结论 |
| --- | --- |
| runner_safety / order_reservation_boundary | EE-1；原生出站 API 与并发普通平仓都要覆盖 |
| signal_execution / capital_allocator | EE-2；单独修好拒绝分支后，副作用顺序仍不成立 |
| RunnerFactEventBridge / host wiring | EE-3；恢复订单的身份表独立于策略 tracker |
| RunnerFactOutbox / JetStream publisher | EE-4；每流有序不应造成跨流饥饿 |
| sltp_mode / TickMonitor / TradeEventHandler | EE-5；分批入场与分批退出需要组合验证 |
| RunnerStateStore / FIFO reduction | EE-6；数量终态与金额结算应分开 |

另有两条怀疑经原生实测排除，未计入问题：typed on_order_filled 抛异常后，当前引擎仍调用 generic 事件桥；小额 reduce-only 退出没有按普通入场的最小金额限制被拒绝。没有据此制造额外 finding。

## Suggestions

优先处理 EE-1 的普通平仓归属/原生 API 覆盖和 EE-2 的保护交接顺序；EE-3 与 EE-4 决定重启后事实链能否继续；随后完成 EE-5/6 的数量与金额生命周期回归。所有修复都应增加真实状态交互断言，而不是只确认某个函数被调用。

## Risk Assessment

**本轮仍确认存在严重问题，不构成生产放行。** 目前证据包含原生离线撮合、真实 SQLite 与受控传输，尚未包含 Ubuntu 实机、LiveNode 持久化重启或交易所联调。以上限制已逐项注明，不把本地测试通过当作实盘验收。
