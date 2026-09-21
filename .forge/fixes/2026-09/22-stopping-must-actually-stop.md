# 22 - stopping-must-actually-stop

> **Status**: 🔲 Not started
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

- [ ] 失败测试先红后绿
- [ ] 审查方探针 `ready_commit_failure_leaves_engine_running` 与
      `cancelled_watcher_restarts_engine` 不再成立
- [ ] 上表 11 条分句逐条有测试（C27：验收是并列多支，不是根因修完就算完）
- [ ] 扰动验证覆盖「删除方向」：把对照行为（真取消 node task）删掉要转红（C26）
- [ ] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 取消等待者 ≠ 节点终态 | P1 | 🔲 | | LB-2 |
| 2 提交失败是可恢复状态 | P1 | 🔲 | | LB-1 |
| 3 关停路径端到端 | P1 | 🔲 | | 两项的共同后果 |

## 偏离与改进日志
