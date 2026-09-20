# Code Review: Custos 生命周期提交、取消与恢复边界

> **Depth**: deep
> **Date**: 2026-09-20
> **Reviewer**: Codex，单执行面
> **基线**: `0222120`。Fix 09 正并行修改 signal_execution.py、sltp_mode.py 和 tick_monitor.py；本轮四个探针在包含其当前改动的工作树复跑成立。行号按固定基线标注。
> **范围**: 引擎就绪提交、异步终态监督、重启行情恢复、宿主构建失败后的清理。

## Summary

新增 **4 项：3 项 P1、1 项 P2**，均有离线复现。固定提交 `0222120` 的 195 项相关既有测试通过。

本轮没有修改业务源码，没有连接真实交易所。前轮 RS/EE 尚未修复的问题不重复计数；并行修复文件保持原样。

## Strengths

- 监督器已有明确的 ready/terminal 身份和 generation 检查，能测试“节点状态”和“监督任务状态”是否被混淆。
- 持久化提交和真实引擎启动被分成独立接口，能对二者之间的失败窗口做故障注入。
- Fix 07 修复了 observe 的 trailing 激活逻辑；本轮发现的是恢复调用方选错 K 线，不是旧的 observe 缺陷复发。
- 宿主在多个构建失败出口已经清理 node 与账户标记，但覆盖范围仍遗漏一个正常校验出口。

## Concerns

### LB-1 [P1] 就绪状态落盘失败后，重试只隔离记录，已经启动的引擎仍在运行

**位置**：`src/custos/core/engine_lifecycle.py:311-374`，尤其 `:335-336`、`:357-373`；宿主已部署保护 `src/custos/engines/nautilus/host.py:607-613`。

_start_with_budget 的清理 try/except 只包住 deploy、wait_ready 和 receipt 校验。后面的 commit_applied_and_enqueue_lifecycle / commit_recovered_engine_ready 在该保护范围之外。提交失败向上冒泡时，引擎已经存在，却没有停机或接管记录。

下一次重试读到没有 applied_generation 的状态，再次调用 deploy；真实宿主的“已有实例”检查会拒绝。因为这次 deploy 没有返回 handle，异常分支不会调用 stop。耗尽重试后，记录进入 quarantined，活跃引擎仍留在宿主中。

**复现**：实际 EngineLifecycleSupervisor，store 仅第一次 ready 提交抛 sqlite3.OperationalError；受控 engine 复现 NtTradingNodeHost 的已有实例拒绝语义。第一次调用后 engine.active=True、stop_calls=0、applied_generation=None。第二次调用耗尽预算后 desired_status=quarantined，但 engine.active 仍为 True，stop_calls 仍为 0，实际成功启动次数只有 1。

**影响**：持久化隔离不能证明执行已经终止。RunnerCommandRuntimeCoordinator 也只有在 apply 返回成功后才创建终态监督任务，因此这条失败路径不会启动正常监督。独立资金上限并不等同于停止该实例。

**建议及验收**：把“启动成功、提交未完成”作为显式可恢复状态；提交异常后核对 durable 结果与实际句柄，按同一 generation 安全接管或停止，不能盲目重复 deploy。覆盖提交前失败、提交后响应失败、重投、恢复提交失败和隔离时实际引擎状态。

**证据边界**：故障和 engine registry 使用受控接口，未在真实 LiveNode 上制造磁盘故障；被审查的 Supervisor 分支是真实代码。

### LB-2 [P1] 取消终态监督任务被当作节点取消，主动关闭反而触发重启

**位置**：`src/custos/engines/nautilus/host.py:1406-1418`；调用链 `src/custos/core/runner_command_runtime.py:339-344`、`:375-382` → `src/custos/core/engine_lifecycle.py:248-289`。

wait_terminal 使用 asyncio.shield(node_task)，随后把所有 CancelledError 都转换成 engine_task_cancelled。shield 下取消“等待者”也会在这一行抛 CancelledError，但被保护的 node_task 仍在正常运行。代码没有区分这两种来源，并把伪终态声明为 retryable。

**复现**：建立仍存活的 node task，调用实际 NtTradingNodeHost.wait_terminal 并接入实际 EngineLifecycleSupervisor.supervise_once。主动 cancel 监督任务，结果没有传播取消，而是返回新的 ready receipt，调用了 stop 一次、deploy 一次，restart_count 从 0 增加到 1；shield 中的原节点任务并没有自行退出。

**对照**：取消真正的 node task，wait_terminal 返回 engine_task_cancelled 是合理的，这条行为不能在修复时一并删除。

**影响条件**：当 durable 状态仍是当前 generation 的 applied/ready 时，例如 daemon 关闭 supervision 的 finally 分支，会进入重启路径。新命令已经改变 desired 状态时，后面的身份检查可能挡住重启，因此不是每次取消都必然重启。run_engine_supervision 在取消后 gather 这些任务，还可能因为任务重新进入监督循环而无法退出。

**建议及验收**：外层监督任务被取消时应传播取消；仅对实际 node task 的终态生成 EngineTerminalEvent。测试 daemon 正常退出、同代监督取消、新代接管、节点真实取消和启动期间取消，断言取消监督不会调用 deploy、不会消耗 restart budget。

**证据边界**：使用真实异步任务、真实 host.wait_terminal 与真实 Supervisor；stop/deploy 端是计数替身，没有实际重启交易所连接。

### LB-3 [P1] 恢复取缓存最旧 K 线，追踪止损可能漏激活和漏退出

**位置**：`packages/custos-strategy-toolkit-nautilus/src/custos_toolkit_nautilus/adapter/coordinators/order_reconciler.py:102-107`。

原生 cache.bars() 返回最新在前的序列；恢复代码用 bars[-1] 作为 current_price，实际取的是缓存中最旧一根。Fix 07 的 observe 虽然已经会更新 trailing 激活状态，但输入价格错误时仍会恢复出错误状态。

**复现**：真实 BacktestEngine 顺序输入两根 bar，close 依次为 101、104；读取原生缓存得到 [104, 101]。实际 OrderReconciler 使用这份缓存恢复 entry=100、激活阈值 2%、回撤阈值 1% 的 monitor，恢复出的 peak 为 101，没有激活。随后价格到 101.5，不触发退出。对照读取最新 104 后再到 101.5，会产生追踪退出。

**影响**：重启后持仓的 Tick/HYBRID 追踪退出可能被延迟或按错误高水位触发。缓存只有一根 K 线的替身测试无法发现顺序错误。

**建议及验收**：通过原生 API 明确读取最新 bar，或按 ts_event 选择；增加至少两根原生缓存 bar 的恢复用例，覆盖激活后回落、旧高价不属于当前持仓及行情缺失。不要仅调整 observe 内部逻辑。

**证据边界**：缓存顺序由真实回测引擎验证，持仓恢复由实际协调器配合受控持仓完成；未执行完整 LiveNode 断线重连。

### LB-4 [P2] 构建后事实校验失败，账户占用标记泄漏，修正后的新实例被误拦截

**位置**：`src/custos/engines/nautilus/host.py:646-688`，尤其 `:682-687`；对照 `:1030-1039`。

宿主先 claim 账户 scope 并 build node，再调用 _build_runner_fact_context。这个调用位于前后两个清理 try/except 之间；它因 capability、timeframe 或其他事实绑定校验失败时，既没有释放账户分区，也没有 dispose 已建 node。此时 _active_nodes 还没有登记该实例，stop(instance) 又会直接 no-op。

**复现**：实际 host.deploy，使用项目 FakeLiveNode 避免网络。签名侧 timeframe=1-MINUTE、实际策略配置=5-MINUTE，真实 metadata 校验失败。随后 active_nodes 为空，node.disposed=False，但账户 scope 仍登记在失败实例上；调用 stop 后标记也没有释放。将 timeframe 修正为 5-MINUTE，以新实例使用同一个 scope，会被“已有活跃部署”拒绝。相同修正配置在干净宿主上可以部署并停止。

**建议及验收**：node 构建后、登记运行前的所有校验都应位于同一个所有权清理范围；只回滚本次持有的分区。测试每个事实校验失败出口，以及修正配置后同 scope 的新实例能够启动。

**证据边界**：实际宿主控制流与 metadata 校验，节点为替身；不推断原生对象析构是否另有资源回收，账户分区泄漏已直接验证。

## Verification

复现脚本：`2026-09-20-custos-lifecycle-boundary-deep-repro.py`，与本报告同目录。

```sh
uv run --extra dev --extra nautilus python .forge/reviews/2026-09-20-custos-lifecycle-boundary-deep-repro.py
```

实际用现有 `.venv/bin/python` 执行，设 20 秒子进程超时；**四个探针全部成立**。脚本通过 Ruff check/format。

既有测试在 `git archive 0222120` 的临时快照中运行，PYTHONPATH 指向快照内源码与 Toolkit，避免并行修复污染基线：

```sh
python -m pytest tests/test_engine_lifecycle.py tests/test_runner_command_runtime.py tests/test_nt_trading_node_host.py tests/test_strategy_signal_bridge.py tests/toolkit/test_strategy_state_and_order_protection.py tests/toolkit/test_tick_monitor.py tests/toolkit/test_execution_coordinator.py tests/toolkit/test_snapshot_coordinator.py -q
```

结果：**195 passed in 1.59s**。固定快照测试有 45 秒超时；没有因为测试需要修改应用源码。

## File-by-File Summary

| 范围 | 结论 |
| --- | --- |
| engine_lifecycle / runner_command_runtime | LB-1、LB-2；就绪提交和监督取消没有完整覆盖 |
| host.deploy / host.stop / fact context | LB-4；构建后的校验出口遗漏清理 |
| host.wait_terminal | LB-2；等待者取消与节点取消混淆 |
| OrderReconciler / native bar cache / TickMonitor | LB-3；原生缓存顺序与替身假设不同 |
| 相邻场所账本、准入与 artifact activation | 本轮未新增独立严重 finding；不替代前轮尚待修复项 |

## Suggestions

先处理 LB-1/2 的执行状态与监督生命周期，再修 LB-3 的行情恢复语义；LB-4 同批补齐构建清理。回归应断言进程/任务是否仍存在、是否触发 stop/deploy、账户 scope 是否释放和恢复后的实际退出动作，不能只看持久化状态或方法调用存在。

## Risk Assessment

本轮仍发现生产前应解决的严重问题。通过的是固定版本的既有测试及本轮离线探针，不是 Ubuntu、LiveNode 持久化恢复或交易所实盘验收。
