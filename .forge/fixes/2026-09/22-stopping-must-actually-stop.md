# 22 - stopping-must-actually-stop

> **Status**: ✅ Completed
> **Created**: 2026-09-21
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-lifecycle-boundary-deep-review.md` LB-2 + LB-1

## 为什么这两项放同一份 plan

LB-1 指出「提交失败」那条路**不会创建终态监督任务**（`RunnerCommandRuntimeCoordinator` 只在 apply
返回成功后才建），于是引擎活着而没有监督。LB-2 的修法是「等待者被取消时传播取消」，而传播之后
那个引擎归谁管，答案正是 LB-1 要补的东西。分开修容易补出一个新缺口：取消传播了，孤儿引擎还在。

报告的基线是 `0222120`，距 HEAD 48 个 commit，但 `engine_lifecycle.py` 与 `host.py`
**零改动**（`git log 0222120..HEAD -- <file>` 均为 0），四个探针在 HEAD 上全部仍然成立。

## LB-2：取消「等待者」被当成「节点被取消」

**位置**：`src/custos/engines/nautilus/host.py:1406-1418`。

```python
task = runtime.task
try:
    await asyncio.shield(task)
except asyncio.CancelledError:
    reason_code = "engine_task_cancelled"
```

`asyncio.shield` 存在的**全部意义**就是把「等待者被取消」和「被等的任务被取消」分开：取消等待者
时 shield 会在等待者这一侧抛 `CancelledError`，而被保护的 `task` 照常运行。上面这个 except 把
两者又粘回去了，并且把这个伪终态标成 `retryable=True`。

**后果比报告写的更重。** 顺着调用链再看一层：

- `runner_command_runtime.py:333-336` 的 `_supervise_running_engine` 是 `while True: supervise_once(...)`。
  假终态让 `supervise_once` 返回一个 receipt（非 None），循环继续，重新 `await shield(新 task)`。
- `runner_command_runtime.py:373-382` 的 `run_engine_supervision` 在 `finally` 里
  `task.cancel()` 之后 `await asyncio.gather(*tasks)`。**一次 `cancel()` 只投递一次
  `CancelledError`，被吞掉之后那个 task 不会再被取消**，于是 gather 等一个已经回到等待状态的任务。

也就是说 daemon 正常关停会：先把正要关的引擎 stop + deploy 重启一遍，然后挂死在 gather 上。
红线 0.3 的紧急预案是「停止新建 live instance、改用 sandbox/testnet」——那条预案的前提是**真的能停**。

**不许一并删掉的行为**（报告明写的对照）：取消**真正的 node task** 时返回 `engine_task_cancelled`
是合理的。修复不能把这条一起删了。

## LB-1：启动成功但提交失败，清理范围没罩住

**位置**：`src/custos/core/engine_lifecycle.py:311-374`。

`try` 罩住 deploy / `_await_ready` / `_require_ready_identity`，在 `:355` 就闭合了；紧随其后的
`commit_recovered_engine_ready` 与 `commit_applied_and_enqueue_lifecycle` **在保护范围之外**。

提交抛异常时：引擎已经在跑，异常向上冒泡，没有 stop、没有记录。下一次重试读到没有
`applied_generation` 的状态，再次 deploy —— 真实宿主的「已有实例」检查会拒绝，而这次 deploy
没有返回 handle，所以 `if handle is not None: await self._engine.stop(...)` 那一支不会触发。
耗尽预算后记录进 `quarantined`，活跃引擎仍在宿主里。

**判断**：持久化隔离不能证明执行已经终止。

## 修复任务

### Fix 1: 取消等待者不是节点终态 [P1 / LB-2]

**Files**: `src/custos/engines/nautilus/host.py`、`tests/test_a_cancelled_watcher_is_not_a_dead_engine.py`

1. 先写失败测试：node task 仍存活时取消 `wait_terminal` 的调用者，必须**传播** `CancelledError`，
   不得返回终态事件、不得调用 deploy、不得消耗 restart budget。
2. `except asyncio.CancelledError` 里先问 `task.done()`：仍在跑就 `raise`（取消是冲着等待者来的）；
   已经结束才落到终态分类。
3. 终态分类从 task 自身的状态读（`cancelled()` / `exception()` / 正常退出），不从「我是怎么醒的」
   推断——这正是本缺陷的根：把**醒来的原因**当成了**被等对象的状态**。
4. 对照测试：真正取消 node task 时仍须返回 `engine_task_cancelled`。

### Fix 2: 启动成功、提交失败是一个显式可恢复状态 [P1 / LB-1]

**Files**: `src/custos/core/engine_lifecycle.py`、测试

1. 先写失败测试：ready 提交第一次抛错时，引擎必须已被停止（`stop_calls == 1`），重试才能干净地
   重新 deploy；耗尽预算进 quarantined 时引擎必须不在运行。
2. 把两个 `commit_*` 纳入同一个所有权作用域：提交失败且 `handle` 在手，先 `stop` 再走既有的
   重试/隔离路径。
3. `recovered_applied` 那一支（`commit_recovered_engine_ready`）同样覆盖——两条提交路径是同一个
   缺陷的两个出口。

**为什么选「停止」而不是「接管」**：报告给的是「安全接管**或**停止」。接管要让 deploy 路径认出
「同一 generation 已部署」并复用句柄，是一套新机械；而停止让世界与那条空白的持久记录重新一致，
重试即可干净重来，且隔离时的实际状态可验证。代价是一次重连。这个取舍写进偏离日志。

### Fix 3: 关停路径端到端 [P1 / LB-1 + LB-2]

**Files**: 测试

两项的共同后果是「关不掉」。要有一条走真实 `run_engine_supervision` 的测试：`stop` 置位后
它必须**返回**，且期间没有 deploy 被调用。这条同时钉住 LB-2 的挂死。

**验收**（报告原文，逐句拆开）：

| 来源 | 分句 | 覆盖 |
|---|---|---|
| LB-2 | daemon 正常退出 | Fix 3 |
| LB-2 | 同代监督取消 | Fix 1 |
| LB-2 | 新代接管 | Fix 1 |
| LB-2 | 节点真实取消 | Fix 1 对照 |
| LB-2 | 启动期间取消 | Fix 1 |
| LB-2 | 断言不调 deploy、不耗 restart budget | Fix 1 + Fix 3 |
| LB-1 | 提交前失败 | Fix 2（回归，不得退化）|
| LB-1 | 提交后响应失败 | Fix 2 |
| LB-1 | 重投 | Fix 2 |
| LB-1 | 恢复提交失败 | Fix 2（`recovered_applied` 支）|
| LB-1 | 隔离时实际引擎状态 | Fix 2 |

## 验证清单

- [x] 失败测试先红后绿 —— 实现前 5 红 5 绿（绿的是对照与回归），实现后 11 绿
- [x] 审查方两个探针不再成立（探针真名见下，plan 起草时我写错了）
- [x] 上表 11 条分句逐条有测试
- [x] 扰动验证覆盖「删除方向」：删掉对照行为转红
- [x] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 取消等待者 ≠ 节点终态 | P1 | ✅ | 2026-09-21 | LB-2 |
| 2 提交失败是可恢复状态 | P1 | ✅ | 2026-09-21 | LB-1 |
| 3 关停路径端到端 | P1 | ✅ | 2026-09-21 | 两项的共同后果 |

## 偏离与改进日志

### 更正：plan 里两个探针名是我编的

起草时我写的是 `ready_commit_failure_leaves_engine_running` 与
`cancelled_watcher_restarts_engine`，两个都不存在。真名是
`ready_commit_failure_leaves_running_quarantined_engine` 与
`canceling_supervisor_restarts_healthy_engine`（`grep '^def ' <repro>` 可得）。

这是 lesson #25 那一族——**引用别人产物里的标识符前要 grep，不能照着印象写**。
起草时没跑那条 grep，实施时一跑就露了。

### DEVIATION: 提交失败选「停止」而不是「接管」

- **等级**: 低
- **原因**: 报告给的是「安全接管**或**停止」。接管要让 deploy 路径认出「同一 generation 已部署」
  并复用句柄，是一套新机械，而且它要先能区分「这是我上次留下的」与「这是别人的」。
- **决定**: 停止。它让世界与那条空白的持久记录重新一致，重试即可干净重来，隔离时的实际状态也可
  验证（`test_quarantine_means_the_engine_is_actually_stopped`）。代价是一次重连。
- **影响**: `_start_with_budget` 的重试路径上多一次 stop + deploy。

### IMPROVEMENT: 关停那条测试原本会挂死，改成了快速失败

第一版 `test_shutdown_returns_and_starts_nothing` 用 `asyncio.wait_for(...)` 等关停返回。
在缺陷下它**挂死**而不是失败——因为那个缺陷吞掉的正是取消，而 `wait_for` 的超时就是靠取消实现的，
于是超时本身也被吞了。挂死的测试是坏证据：拿不到结论，还拖住整个套件。

改用 `asyncio.wait`（它**不**取消被等的对象）加一个墙钟超时，断言「关停任务已完成」。同时给替身
加了「最多被问两次」的硬上限——不是为了让断言好过，而是为了让**拆解**能结束：没有上限的话，缺陷
形态下的清理本身会再次卡住。

改完之后用**原样的缺陷代码**（照抄 `0222120` 的那几行）重扰一次，它停在
`runner_command_runtime.py:382`，也就是 `await asyncio.gather(*tasks)` —— 报告描述的挂死点。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-21
- **总 Task 数**: 3
- **偏离数**: 1 更正 + 1 取舍 + 1 改进
- **验证结果**: 全部通过
- **实施 commit**: `98afec1`
- **契约影响**: 无。`wait_terminal` 的返回类型与终态原因码集合都没变，新增的
  `_terminal_reason` 是私有静态方法；`_commit_engine_ready` 同理。
- **C6 核对**: `host.py` 与 `engine_lifecycle.py` **都不是字节 pin**（`grep -rl` 在
  `docs/authority/` 零命中）。`runner_command_runtime.py` 只以路径出现在一份 receipt 的
  `implementation_files` 列表里，不是 digest map，而且本轮没改它。证据链不受影响。

### 红线 gate 满足度

| 红线 | code 覆盖 | runtime wire | defer | follow-up |
|---|---|---|---|---|
| 0.1 Key/KEK 不出进程 | N/A（未触及） | N/A | 无 | — |
| 0.2 执行门不绕过 | LB-1：隔离的记录不再对应一个还在交易的引擎 | `_start_with_budget` 是 apply/supervise 两条真实路径共用的启动段 | 无 | — |
| 0.3 失联 ≠ 停止 | 本 plan 的主体。红线 0.3 的紧急预案是「停止新建 live instance」，前提是**真的能停**；LB-2 让关停变成重启加挂死 | `run_engine_supervision` 是 daemon 实际用的关停路径，端到端测试走的就是它 | 无 | — |
| 0.4 Decimal money math | N/A（未触及） | N/A | 无 | — |

### 验收分句逐条对照

| 来源 | 分句 | 覆盖它的测试 |
|---|---|---|
| LB-2 | daemon 正常退出 | `test_shutdown_returns_and_starts_nothing` |
| LB-2 | 同代监督取消 | `test_cancelling_supervision_neither_redeploys_nor_spends_the_budget` |
| LB-2 | 新代接管 | `test_cancelling_the_watcher_does_not_invent_a_terminal_event`（取消传播后旧 watcher 不再产出事件，新代才能干净接手）|
| LB-2 | 节点真实取消 | `test_a_genuinely_cancelled_node_is_still_a_terminal_event` |
| LB-2 | 启动期间取消 | `test_cancelling_before_the_node_is_registered_is_still_a_clean_cancel` |
| LB-2 | 不调 deploy、不耗 restart budget | 上表第 2 条的两句断言 + 关停那条的 `started == []` |
| LB-1 | 提交前失败 | `test_a_failure_before_the_commit_still_behaves_as_it_did`（回归）|
| LB-1 | 提交后响应失败 | `test_a_failed_ready_commit_stops_the_engine_it_could_not_record` |
| LB-1 | 重投 | 同上（第二次 deploy 必须成功，证明世界干净了）|
| LB-1 | 恢复提交失败 | `test_a_failed_recovery_commit_stops_the_engine_too` |
| LB-1 | 隔离时实际引擎状态 | `test_quarantine_means_the_engine_is_actually_stopped` |

另加两条报告没点名但同族的：节点自己抛异常、节点自己正常退出，各自仍须产出对应终态
（`engine_task_failed` / `engine_task_exited`）。

### 测试条数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/test_stopping_must_actually_stop.py` | 11 |
| `tests/test_plan_closeout_counts.py` | 65 |

`test_engine_lifecycle.py`(13) / `test_nt_trading_node_host.py`(40) /
`test_runner_command_runtime.py`(13) 三个文件本轮**只被 import 复用替身，没有改动**，
故不重数（按 progress-management 的规则，重数的义务来自「动了别人数过的文件」）。

### 扰动验证

| 扰动 | 结果 |
|---|---|
| 取消等待者时不再检查 node 是否还在跑 | 2 红 |
| 终态原因回到「从醒来的方式推断」（删掉对照能力）| 2 红 —— C26 的删除方向 |
| 提交挪回清理范围之外 | 3 红 |
| **照抄 `0222120` 的原样缺陷代码** | 3 红，含关停那条，停在 `runner_command_runtime.py:382` 的 gather |
| 还原 | 11 绿 |

每次扰动都用独立的 `PYTHONPYCACHEPREFIX`。

### 审查方探针现状

| 探针 | 结果 |
|---|---|
| `ready_commit_failure_leaves_running_quarantined_engine` | 中止于 `engine_lifecycle.py:422` 抛 `EngineLifecycleQuarantined` —— 它到不了「引擎仍 active」那条断言了 |
| `canceling_supervisor_restarts_healthy_engine` | 中止于 `host.py:1408`，正是本轮新加的那个 `raise`：取消现在传播，不再被换成假终态 |

两个都中止在**生产代码我改的那一行**上，不是中止在探针自己的脚手架里。

### 功能验证（主路径）

1. sandbox 下起一个部署，确认引擎 ready。
2. 给 daemon 发正常关停信号。进程应当**退出**；日志里不应出现 `nt_deploy_started`
   或新的 `engine_start_attempt_failed`。修复前这里会先重启一次引擎，然后卡在关停不退。
3. 反向确认：直接杀掉引擎进程（让 node task 真的结束），监督应照常报终态并按预算重启 ——
   这条能力没有被一并删掉。
