# 26 — 重启后仍相信上个进程的附着状态（一因两症：结构性重配被拒 + false healthy）

> **Status**: ⏳ In Progress —— 代码侧四个 Task 全绿; **两症的真机证据已于 2026-07-30 在 PS 侧取得**
> (见 §验证清单前两项); 仍未完成: 遗留项 3 (`container_id` 已无人读) / 4 (自终止节点的清理路径 +
> `attached()` 与 `deploy()` 幂等守卫口径不一, **本身需要真机证据且这两轮未覆盖**) / 5 (同步 engine
> 查询的异常边界, 独立 plan)
> **Created**: 2026-07-30
> **Project**: custos (`tesseract-trading/custos/`)
> **Depends on**: 无 —— 现有代码即可复现
> **Blocks**: 离线通道重启后的正常部署（philosophers-stone 当前用一层 workaround 绕过，见 §Follow-up）
> **选型**: **B —— 给 `OfflineEngine` 加附着查询**（owner 决定 2026-07-30，理由与范围边界见 §决策）
> **For Claude**: `/forge:execute`；单 session（4 Task，无跨仓库改动）
> **multi_session_scope**: false

## 上下文 (Context)

`OfflineReconciler` 在构造时把上个进程的 **`container_id` 一并载入**：

```python
self._applied: dict[str, _Applied] = {
    spec_id: _Applied(generation=record.generation, container_id=record.container_id)
    for spec_id, record in (applied_store.load() if applied_store else {}).items()
}
```
（`src/custos/offline/reconciler.py:145-148`）

而 `container_id` 描述的是**引擎的附着状态**，它随进程消失：`NtTradingNodeHost._active_nodes`
与 `SandboxSimulationHost` 的记账都在内存里。于是新进程一起来就相信自己附着着一个它从未创建的
东西。

值得注意的是 `AppliedStore` 自己的 docstring 写的是「Where applied **generations** survive a
restart」（`:107`）—— 它声明要跨重启保住的是 generation，而记录里搭车带过来了附着状态。

### 症状一：新 generation 在重启后被当成结构性重配而拒绝

`_engage`（`:299-309`）用 `container_id` 是否为空来分派：

```python
if spec.lifecycle_state in _TERMINAL_STATES:
    await self._engine.stop(...); return ""
if not applied.container_id:
    return await self._engine.deploy(...)
await self._engine.reconfigure(document)
```

重启后 `container_id` 非空（来自上个进程），所以走 `reconfigure`；而
`NtTradingNodeHost.reconfigure`（`src/custos/engines/nautilus/host.py:639-661`）只接受带
`reconfigure.runtime_tunable_only` 标记的 spec，其余一律：

```
structural reconfigure of instance '<id>' requires stop + re-deploy
  (v1 NtTradingNodeHost does not hot-swap strategy / venue / symbol)
```

**这正是 philosophers-stone 一整天在对付的那个报错**（2026-07-30 首次 testnet 实跑）：
`compose down` 之后再 `start`，同一份策略、同一个 venue、同一个 symbol —— 没有任何结构性变化 ——
却被当作结构性重配拒绝。真正变化的只是「引擎不再附着」，而记录说它还附着着。

### 症状二：同一 generation 在重启后报 healthy 而引擎没跑

```python
if spec.generation == applied.generation:
    await self._report(spec, healthy=True)
    return Settlement.APPLIED
```
（`:229-231`）

重启后重复发送同一个 generation（固定 generation 做可重复测试就是这种用法），走到这里**不调引擎
就报 healthy**。消费者的 `wait-status` 会通过，而实际没有任何策略在跑。**这比失败更糟：它给出的
是错误的成功信号。**

### 一个原因

两症同源 —— **跨越了一个进程边界，而引擎在这个边界上丢掉了自己的状态，记录却没有**。
`generation` 跨重启保留是对的（"apply each generation once" 依赖它）；`container_id` 跨重启保留
是错的（它描述的是这个进程有没有附着）。

### 为什么 reconciler 现在无法自查

`OfflineEngine` 协议（`:63-72`）只有 `deploy` / `reconfigure` / `stop` /
`supports_trading_mode` —— **没有任何「这个实例还附着吗」的查询**。所以 reconciler 不是忘了查，
是查不了，只能信自己持久化的那个字段。

## 决策 (Decisions)

### 选型

- **A — load 时把 `container_id` 归零，只保留 `generation`。** 最小改动，直接消除两症：新
  generation 走 deploy（正确）；相同 generation 因 `container_id` 空而重新 engage 而非空报 healthy。
  **前提是「引擎的附着绝不长于进程」**。本 lane 当前成立（NT 节点与策略跑在同一进程内），但这是
  一个未被写下的假设 —— 若将来某个 engine 真的 spawn 出比 runner 长命的容器，归零就会重复部署。
- **B — 给 `OfflineEngine` 协议加一个附着查询**（如 `def attached(self, deployment_instance_id: str) -> bool`），
  reconciler 首次触碰某 spec_id 时用它校准持久化的 `container_id`。修的是「无法自查」这个根因，
  不依赖任何关于引擎寿命的假设，且让 `==` 分支能诚实作答（附着才报 healthy，否则重新 engage）。
- **C — 只改 `==` 分支**（不附着就重新 engage）。只治症状二，症状一照旧，不推荐。

**选定 B**（owner 决定 2026-07-30）。实施中顺带落地 A 的效果：`attached()` 对本 lane 的两个 host
都返回「本进程内是否有活节点」，于是重启后自然为 False，`container_id` 被校准为空。这样既消除两症，
也把「附着是进程作用域的」从一个隐含假设变成一个**被协议表达出来的事实**。

### 加在哪个协议上：`OfflineEngine`，不是 `ExecutionEngineProtocol`

仓里有两个引擎协议，这个选择不是风格问题：

| 协议 | 位置 | 是否 `@runtime_checkable` |
|---|---|---|
| `OfflineEngine` —— 本 lane 用的窄切片 | `src/custos/offline/reconciler.py:63` | **否** |
| `ExecutionEngineProtocol` —— 全量宿主契约 | `src/custos/core/engine_protocol.py:308-309` | **是** |

`@runtime_checkable` 的 `isinstance` 只看**方法是否存在**，而仓里有活的断言依赖这一点 ——
`tests/core/test_engine_protocol_tier2.py:56-57` 正向断言两个 host 通过，`:60` / `:171` 用
`_MissingGetOpenNotional` / `_MissingGetEngineStatus` 反向断言「少一个方法就不再 isinstance 通过」。
也就是说往 `ExecutionEngineProtocol` 加方法，会**静默改变 `isinstance` 对每一个实现者的含义**，包括
`tests/` 下若干 fake（`test_engine_lifecycle.py` / `test_engine_protocol_contract.py` /
`test_engine_protocol_tier2.py` 各有一份），它们都得跟着长出这个方法才能维持现状。

而 `OfflineEngine` 无该装饰器，是纯结构类型，改动面只到离线通道自己的两个 fake
（`tests/test_offline_reconciler.py:98`、`tests/test_offline_lane_daemon.py:62`）。reconciler 需要的
就是这一个能力，**加在窄切片上**。

副作用一条要记得处理：`SandboxSimulationHost` 的 docstring 现在写着「method signatures exactly
match ExecutionEngineProtocol」（`host.py:114`）。多一个协议外的方法后这句话就 stale 了 —— 结构类型
容许多出方法，不是错误，但那句断言得改，否则是留给下一个读者的假话。

### 上游修好之后，下游要拆掉 workaround

philosophers-stone 目前在 `start` 里发一份 `stopped` spec 来清掉 `container_id`，再等它回报，
以此绕开症状一（PS Plan 61）。**本 plan 落地后那层 workaround 应当拆除** —— 否则每次 start 都多
一次发布加一次等待，且它绕过的问题已经不存在。这是本 plan 的下游 follow-up，登记在 §Follow-up。

## 目标 (Goal)

重启后的第一次部署走 deploy 而不是被当成结构性重配；重复 generation 不再在引擎没跑的情况下报
healthy。且「附着状态是进程作用域的」这件事由代码表达，而不是靠读者自己想到。

## 非目标 (Non-goals)

- **不改 `reconfigure` 拒绝结构性变更这件事本身** —— 它拒得对，是分派到它这里的判断错了。
- **不动 exposure guard 的 latch 语义** —— 它的「清除需要重启」是有意设计（已实证）。
- **不在本 plan 拆 PS 的 workaround** —— 跨仓，见 §Follow-up。
- **不引入「引擎可长于进程」的新能力** —— 只把当前事实表达清楚。

## 实现任务 (Tasks)

### Task 1 — 先写两条会红的测试，一症一条

1. **重启后新 generation 应走 deploy**：构造一个 `applied_store` 返回非空 `container_id` 的
   reconciler（模拟上个进程），送入一个更高 generation 的 running spec，断言调用的是
   `engine.deploy` 而非 `engine.reconfigure`。
2. **重启后相同 generation 不得空报 healthy**：同样的 store，送入与 `applied.generation` 相等的
   spec，断言要么重新 engage，要么报 unhealthy —— **不允许「没 engage 却 healthy」**。

两条都必须**在修复前是红的**，并把红的输出记进 close-out。用真实的 reconciler 与 fake engine，
不要在测试里复刻分派逻辑 —— 手抄一遍等于测自己的假设（本仓 C4「mock 绕过 public surface 形成双重
假绿」+ C7「自洽的假绿」；起 plan 时我把这条错标成 C10，C10 实为正则作用域，指针已更正）。

### Task 2 — 落选定的修法

给 `OfflineEngine`（`reconciler.py:63`，**不是** `ExecutionEngineProtocol`，理由见 §决策）加一个同步
附着查询，两个 host 各查自己的内存记账 —— 两处都已 grep 实证存在：

| Host | 记账字段 | 写入 / 清除 |
|---|---|---|
| `NtTradingNodeHost` | `_active_nodes`（`host.py:249`） | deploy 时 `:379` 写入，stop 时 `:514` pop |
| `SandboxSimulationHost` | `_lifecycle_authorities`（`host.py:120`） | deploy 时 `:129` 写入，stop 时 `:146` pop |

两者都是纯内存 dict，所以重启后天然为空 —— 这正是要表达的语义，不是巧合。

⚠️ `NtTradingNodeHost` **两个 dict 都有**：`_active_nodes`（`:249`）与 `_lifecycle_authorities`
（`:250`），stop 时 `:514` / `:515` 一起 pop。附着语义要查的是 **`_active_nodes`** —— 它持的是活的
`(node, task)`，即「真的有个节点在跑」；`_lifecycle_authorities` 只是授权记录。两者当前同生同死，所以
查错了今天也看不出来，但语义上只有前者回答得了「还附着吗」。

reconciler 在首次触碰某 spec_id 时用它校准载入的 `container_id`：未附着即视为空。`==` 分支
（`:229-231`）同步改为：附着 → 报 healthy（现状）；未附着 → 走 engage 路径。

### Task 3 — 把假设写进代码

`AppliedStore` 的 docstring 已经说它保的是 generation；把「`container_id` 不跨进程可信」这条写进
`_Applied` 或 store 记录的说明，并让 Task 1 的两条测试成为它的守护。

同时修掉 §决策 点出的那句 stale docstring：`SandboxSimulationHost` 的「method signatures exactly
match ExecutionEngineProtocol」（`host.py:114`）在多出一个协议外方法后不再成立。改动代码顺手改掉描述
它的注释 —— 否则就是留给下一个读者的假话（本仓 lesson #24 同型）。

### Task 4 — 两个 host 都要被测到

`--engine nautilus` 与 `--engine sandbox-sim` 的附着语义各自实现，各自要有测试。不要只测一个然后
推断另一个（参考本仓 Plan 25 的 DEV-25-TASK-4-PREMISE-CORRECTED：sandbox 实际有两条路径，
其中一条根本不建 NT 节点）。

## 验证清单 (Verification)

- [x] `make verify` 全绿 —— **除 C6 记录的既有恒红**：`fmt-check` 报 3 个被 `docs/authority/**`
      按字节 pin 住的文件（`core/runner_fact.py` + 2 个 integration 测试）。实施前实测同样 3 个、
      同样内容，与本 plan 无关；本 plan 触碰的文件全部 format-clean。`make test-baseline` 全绿
- [x] Task 1 两条测试修复前红、修复后绿（红侧输出见下方 close-out）
- [x] 两个 host 的附着查询各有测试（Task 4），且各自被扰动证伪过
- [x] `ExecutionEngineProtocol` **未被改动**：`git diff` 对 `src/custos/core/engine_protocol.py`
      与 `tests/core/test_engine_protocol_tier2.py` 均为零改动，两个文件的 isinstance 正反断言全绿
- [x] **真机证据（症状一）** —— 2026-07-30 于 PS 侧取得。用 `CLEAR_GENERATION=0` 命中 skip 分支禁掉
      `clear-recorded-deployment`（不改文件，日志有 `leaves no older generation to retract with;
      skipping`），`compose down` 后直接 `start-detached`。**跑前**状态库确实留着上个进程的附着
      （`applied_generation` 表：`supertrend-testnet` → `container_id` = `dcb00e52-0b45-569e-83b0-0e7a1cb27db9`，
      36 字符），即这次跑**有可能失败**。跑后 runner 容器日志 266 行中 `structural reconfigure`
      **0 次**、`reconfigure` 任意大小写 **0 次**（根本没被尝试），`nt_deploy_started` 在，`MAKE_EXIT=0`。
      镜像 revision `3085244`，且**进容器核验过内容**：`attached` 在协议与两个 host 上，NT 那份含
      `.done()`（即 codex 复审后那版，不是最初查 dict 成员那版）
- [x] **真机证据（症状二）** —— 同日同一批。`compose down` 后把 generation **钉死**在已 applied 的
      `1785412898760827000` 重发，runner 日志出现
      `{"spec_id": "supertrend-testnet", "generation": 1785412898760827000, "lifecycle_state": "running",
      "event": "offline_applied_generation_not_in_place"}` 并**重新 engage**（`nt_deploy_started` +
      `TradingNode: RUNNING`），而非空报 healthy。该事件在同批新 generation 那轮 **0 次**，说明它只在
      本症状的路径上触发、不是恒发

> ⚠️ 上述两轮日志里同时有 `fallback_breaker_fail_closed`（reason=`portfolio_equity_ambiguous`）与
> `offline_exposure_guard_latched` 两条 error。**与本 plan 无关**：那是 Plan 27 记录的另两个根因
> （多币种账户 equity 币种未声明 + flatten 在持仓到达前空转），在这两轮里各自第二、第三次复现。
> 读本 plan 证据时不要把它们当成本修复的失败。

## 偏离与改进日志 (Deviations & Improvements)

- **DEV-26-OPTION-B-SELECTED**（2026-07-30，owner）：选 B（协议加附着查询）而非 A（load 时归零）。
  理由：A 依赖「引擎附着绝不长于进程」这个当时未被写下的假设，B 把它变成协议表达的事实。
- **DEV-26-NARROW-PROTOCOL-ONLY**（起 plan 时 grep 实证得出）：方法只加 `OfflineEngine`，不加
  `ExecutionEngineProtocol` —— 后者 `@runtime_checkable` 且有 presence-based isinstance 正/反向断言
  在跑，加方法会静默改变它对所有实现者（含多个 test fake）的含义。
- 若实施中发现某个 host 的附着状态确实可能长于进程，记在这里 —— 那会推翻本 plan 的前提，也说明
  `attached()` 不能简单实现为「查内存 dict」。**未发生**：两个 host 的记账都在内存，本 plan
  前提成立。
- **DEV-26-DISPATCH-ON-THE-ENGINE-NOT-THE-RECORD**（低风险，实施中）：Task 2 写的是「首次触碰某
  spec_id 时用 `attached()` **校准** 载入的 `container_id`，未附着即视为空」，实施改为**分派处直接
  问引擎**，`container_id` 不再被校准、也不再被任何分派读取。两者对重启场景等价，但校准版有两个
  问题：(a) 它只修「记录来自上个进程」这一种失真，修不了**同一进程内节点自己死掉**
  （`_on_node_task_done` 会摘掉活节点，而 `container_id` 仍非空 → 下一个 generation 仍走
  reconfigure 被拒）；(b) 校准之后 `_engage` 成功必然重写 `container_id`，校准的效果无法被任何
  断言观察到 —— 那就是本仓 C9/#28 说的死分支。现在 `container_id` 只作 provenance，`_Applied` 与
  `AppliedRecord` 的 docstring 明说它不参与决策（Task 3）。
- **DEV-26-RESTART-REDEPLOY-TEST-PREMISE-CORRECTED**（低风险，实施中）：`test_forgets_nothing_across_a_restart`
  （Plan 22 计入）断言「重启后重发同一 generation **不得** deploy」—— 那正是症状二，它把错误信念
  写成了契约。按本仓 C8（删面时连测试一起删就没人变红），不删断言，拆成两条：一条守它真正在保护的
  性质（applied generation 跨重启记得住，因而仍拒更旧的 generation），一条写下更正后的信念（引擎已经
  没了的那个 generation 会被重新部署）。plan 起草时未预见到这条既有测试需要改写。
- **DEV-26-TWO-REGISTRIES-DO-NOT-DIE-TOGETHER**（低风险，实测更正 plan 前提）：Task 2 的 ⚠️ 说
  `NtTradingNodeHost` 的 `_active_nodes` 与 `_lifecycle_authorities`「当前同生同死，所以查错了今天
  也看不出来」。**实测不成立**：`stop()` 两个都摘，但 `_on_node_task_done`（`host.py:815-818`）
  只摘 `_active_nodes`。所以节点自己结束时两者分岔，查错了今天就能看出来 ——
  `test_a_node_that_ended_on_its_own_is_no_longer_held` 在把实现改指 `_lifecycle_authorities` 后
  确实变红（已实跑）。选 `_active_nodes` 仍然对，但理由是它有观察得到的差别，不是「反正一样」。

## Follow-up hooks（不属于本 plan scope，登记以防遗漏）

- **philosophers-stone 的 workaround 应在本 plan 落地后拆除。** 它在 `start` 里发 `stopped` spec
  清 `container_id` 并等回报（PS Plan 61）。拆除前应确认本 plan 的真机证据已取得，否则会把 PS
  退回到一整天前的状态。
- ~~**`-4015` 的真机接单判据仍在 PS 侧**~~ **已取得，Plan 25 于 2026-07-30 标完成。** 那个共同的
  「重建镜像」动作已经做了（revision `3085244`，按 custos HEAD），本 plan 与 Plan 25 的真机证据
  都出自这同一批 testnet 实跑。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-07-30（代码侧；真机判据未取，故 plan 不标 ✅）
- **总 Task 数**: 4，全部完成
- **偏离数**: 3（DEV-26-DISPATCH-ON-THE-ENGINE-NOT-THE-RECORD /
  DEV-26-RESTART-REDEPLOY-TEST-PREMISE-CORRECTED / DEV-26-TWO-REGISTRIES-DO-NOT-DIE-TOGETHER）
- **验证结果**: 本仓全绿（`make test-baseline`）；`fmt-check` 停在 C6 的既有恒红，与本 plan 无关
- **实施 commit 范围**: `19b1735`..`0b26b9f`
- **契约影响**: `OfflineEngine`（离线通道自己的窄切片协议）多一个 `attached()`。
  `ExecutionEngineProtocol` 零改动 —— 该协议 `@runtime_checkable`，加方法会静默改变 `isinstance`
  对每个实现者的含义。docs-site 与 `docs/authority/nautilus-host-contract.md` 描述的都是后者，
  未因此 stale，`make check-authority` 通过

### 修的到底是什么

`container_id` 被当成「引擎还附着着吗」的答案用了。它不是 —— 它是上一次 deploy 的返回值，
而附着活在引擎的内存里。跨一次进程边界，引擎丢了状态，记录没丢，于是新进程相信自己附着着一个
它从未创建的东西：新 generation 被当成结构性重配拒掉，同一 generation 不调引擎就报 healthy。

现在分派处直接问引擎。`_engage` 用 `attached()` 决定 deploy 还是 reconfigure；`==` 分支问的是
「引擎在不在 spec 要它在的地方」—— running 要附着，stopped 要不附着 —— 只有答案相符才报 healthy。
`container_id` 留作 provenance，两个 dataclass 的 docstring 明说它不参与决策。

### Task 1 的红（修复前）

```
FAILED test_a_new_generation_after_a_restart_is_deployed_not_reconfigured
E   assert 0 == 1        # engine.deployed 为空 —— 它走了 reconfigure
FAILED test_the_applied_generation_after_a_restart_is_not_healthy_on_paper
E   assert (False or 'healthy' == 'unhealthy')   # 一次引擎调用都没有，却是 healthy
FAILED test_an_attached_generation_is_still_reported_healthy_without_redeploying
E   assert 0 == 1
```

第三条不是症状，是**给修法立的界**：它盯住进程内重投仍走便宜路径，防止「让每次重投都重新部署」
这种把两症一起盖过去的修法。

### 扰动证伪（每条守卫都真的会咬）

| 把实现改成 | 变红的测试 |
|---|---|
| NT host 查 `_lifecycle_authorities` 而非 `_active_nodes` | `test_a_node_that_ended_on_its_own_is_no_longer_held` |
| sandbox host 答成整机一个开关（`bool(self._lifecycle_authorities)`） | `test_holding_one_instance_is_not_holding_another` |
| `==` 分支只看附着、不看 spec 要什么（`return bool(attached)`） | `test_a_terminal_generation_after_a_restart_is_healthy_without_stopping_again` |

第一行同时推翻了 plan 的一个前提，见 DEV-26-TWO-REGISTRIES-DO-NOT-DIE-TOGETHER。

### 测试条数（`pytest --collect-only` 实跑）

| 测试文件 | 条数 |
|---|---|
| `tests/test_offline_reconciler.py` | 37 |
| `tests/test_offline_lane_daemon.py` | 17 |
| `tests/test_nt_trading_node_host.py` | 22 |
| `tests/engines/nautilus/test_sandbox_host_attachment.py` | 4 |
| `tests/test_plan_closeout_counts.py` | 13 |

上表合计 93 条。这是这些文件**今天各自的总数**，不是本 plan 的增量 —— 本 plan 在
`test_offline_reconciler.py` 加了 4 条、`test_nt_trading_node_host.py` 加了 3 条、
`test_sandbox_host_attachment.py` 是新文件 4 条，并把 `test_offline_lane_daemon.py` 的一条
拆成两条。

前两行在 codex 审查之后各多了一条（36 → 37、21 → 22）：`attached()` 不再把已结束的 task
算作附着（Fix 1），terminal 判据加测 `archived`（Fix 2）。这里重数而不是另起一份记录 ——
fix cycle 是本 plan 自己的交付，plan 也还停在 ⏳，close-out 尚未定稿。

最后一行是被本 plan **间接**改变的：`test_plan_closeout_counts.py` 按「带条数表格的 plan」参数化，
本 close-out 一加表就多出两个用例（11 → 13）。规则是「动了别人数过的文件就在自己的 close-out
里重数」，间接变动同样算，所以这里重数，而不是回去改 Plan 22 / Plan 25 的行 —— 那些行记的是
它们当时交付了什么。

### 红线 gate 满足度

| 红线 | code 覆盖 | runtime 接线 | 本 plan 影响 |
|---|---|---|---|
| 0.1 Key/KEK 不出进程 | 未触碰 | 未触碰 | 无。`attached()` 只答一个 bool，不经凭证路径 |
| 0.2 G6 host gate | 未触碰 | 未触碰 | 无。admission 与 `_build_exec_plan` 的 mode 分支一字未改；离线通道本就 live 禁入 |
| 0.3 失联 ≠ 停止 | 未触碰 | 未触碰 | 无。敞口守卫的 latch 语义未动 —— `watch()` 对已在守的 spec 只重放限额、不解锁；且 `allows_new_generations()` 在跳闸后更早一步就拒了 |
| 0.4 Decimal money math | 未触碰 | 未触碰 | 无 |

说清它**不**是什么：本 plan 让重启后的部署走得通，这既不放宽也不兑现任何一条红线。

### 失败模式覆盖

| 场景 | 测试 |
|---|---|
| 重启后新 generation（记录带上个进程的 container id） | `test_a_new_generation_after_a_restart_is_deployed_not_reconfigured` |
| 重启后同一 generation（false healthy） | `test_the_applied_generation_after_a_restart_is_not_healthy_on_paper` |
| 重启后同一 generation 且 spec 已终局（`stopped` / `archived` 各一次） | `test_a_terminal_generation_after_a_restart_is_healthy_without_stopping_again` |
| 重启后更旧的 generation 仍被拒 | `test_a_restart_still_refuses_a_generation_it_has_already_passed` |
| 进程内重投不重新部署 | `test_an_attached_generation_is_still_reported_healthy_without_redeploying` |
| 节点自己死掉（无人调 stop） | `test_a_node_that_ended_on_its_own_is_no_longer_held` |
| 附着按实例回答、不是整机开关 | `test_holding_one_instance_is_not_holding_another` |

### 遗留项

1. ~~**真机证据（阻塞完成）。**~~ **已取得（2026-07-30，PS 侧）** —— 两条都有，证据见 §验证清单
   前两项。镜像已按 custos HEAD 重建（revision `3085244`），Plan 25 的 `-4015` 判据在同一次重建后
   一并取得（0 次 `-4015`、0 次 `OrderRejected`、client order id 32 字符）。
   **注意这不等于本 plan 可以标完成** —— 遗留项 3 / 4 / 5 仍在，其中第 4 项自己就要求真机证据，
   而这两轮**没有覆盖它**：两轮里都没有出现「NT 节点自行终止」的情形，所以
   `run_async()` 自行返回后还剩什么没释放、以及 `attached()` 与 `deploy()` 幂等守卫的口径不一
   会不会真的踩到，这两轮都答不了。
2. **PS 的 workaround 待拆**（跨仓，见 §Follow-up）。拆除前必须先拿到第 1 项，否则 PS 会退回到
   2026-07-30 之前的状态。
3. **`container_id` 现在没有任何读它做决策的地方。** 本 plan 按计划保留它作 provenance（删它要
   动 sqlite 表与 `AppliedRecord`，超出本 plan）。下一个动这块的 plan 应当判断它是否还值得存 ——
   一个没人读的字段迟早会被下一个读者当成它不是的东西，这正是本 plan 修的那件事。codex 补了
   两点：终局 generation 的早返回不会清掉上个进程留下的值；与其删不如先更名为
   `last_deploy_receipt` 之类，让字段名自己说明它是回执而不是当前状态。
4. **自终止的 NT 节点没有明确的清理路径。**（codex MEDIUM）`_on_node_task_done`
   （`host.py:831-839`）只摘 registry，不做 `stop()` 里的 `node.dispose()` 与 task reap。
   本 plan 把这条路径从**永久卡死**（旧行为：`container_id` 非空 → reconfigure 被拒 → 不再恢复）
   换成了**可以重新部署**，方向是对的，但重新部署前上一个节点还剩什么没释放，本仓给不出答案 ——
   要判断 NT `run_async()` 自行返回后的语义，需要真机证据。与第 1 项同批处理。

   同一处缺口还留下一个**两个调用点口径不一**：Fix 1 之后 `attached()` 认为「entry 在但 task 已
   结束」= 未附着，而 `deploy()` 的幂等守卫（`host.py:296`）仍按原始 membership 拒绝，所以在那个
   调度窗口里 reconciler 会去 deploy 而 deploy 会抛「already deployed; call stop first」。后果是
   一次 unhealthy + RETRYABLE，下一次投递（callback 早已跑完）即自愈，**比修复前的空报 healthy 好**。
   不把守卫一起放宽是有意的：放宽等于允许静默顶掉一个**从未 dispose 过的**节点，而那正是本项
   要先弄清的事。两处口径应当在这一项里一并收敛。
5. **同步 engine 查询没有统一的异常边界。**（codex MEDIUM）`_engine_is_where_the_spec_asks`
   （`reconciler.py:247`）在 engine-op 的 `try` 之外，抛异常会终结 lane task 而不是走
   RETRYABLE/NAK。**这是既有形态而非本 plan 引入**：紧挨着的 `supports_trading_mode`（`:261`）
   暴露完全相同。只包本 plan 新加的那一次调用会让两者处置不一致；正确的修法是一次性给所有同步
   engine 查询定边界，那是独立的 plan。两个当前 host 都是 dict lookup，抛不出异常。
