# 26 — 重启后仍相信上个进程的附着状态（一因两症：结构性重配被拒 + false healthy）

> **Status**: 🔲 Not started
> **Created**: 2026-07-30
> **Project**: custos (`tesseract-trading/custos/`)
> **Depends on**: 无 —— 现有代码即可复现
> **Blocks**: 离线通道重启后的正常部署（philosophers-stone 当前用一层 workaround 绕过，见 §4）
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

**推荐 B**，并在实施中顺带落地 A 的效果：`attached()` 对本 lane 的两个 host 都返回「本进程内是否
有活节点」，于是重启后自然为 False，`container_id` 被校准为空。这样既消除两症，也把「附着是进程
作用域的」从一个隐含假设变成一个**被协议表达出来的事实**。

若 owner 选 A，则必须把那个假设显式写下来（协议 docstring + 一条断言或测试），否则下一个 engine
实现会在不知情的情况下踩中。

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
不要在测试里复刻分派逻辑（本仓 C10：手抄等于测自己的假设）。

### Task 2 — 落选定的修法

按决策选 B：协议加附着查询，两个 host 各自实现为「本进程内是否有该实例的活节点」
（`NtTradingNodeHost` 查 `_active_nodes`，`SandboxSimulationHost` 查它自己的记账），reconciler
在首次触碰某 spec_id 时据此校准 `container_id`。

`==` 分支同步改为：附着 → 报 healthy（现状）；未附着 → 走 engage 路径。

### Task 3 — 把假设写进代码

`AppliedStore` 的 docstring 已经说它保的是 generation；把「`container_id` 不跨进程可信」这条写进
`_Applied` 或 store 记录的说明，并让 Task 1 的两条测试成为它的守护。

若最终选 A 而非 B，本 Task 变成**必须**：假设没有协议表达，就只能靠文字加测试兜住。

### Task 4 — 两个 host 都要被测到

`--engine nautilus` 与 `--engine sandbox-sim` 的附着语义各自实现，各自要有测试。不要只测一个然后
推断另一个（参考本仓 Plan 25 的 DEV-25-TASK-4-PREMISE-CORRECTED：sandbox 实际有两条路径，
其中一条根本不建 NT 节点）。

## 验证清单 (Verification)

- [ ] `make verify` 全绿
- [ ] Task 1 两条测试修复前红、修复后绿（两侧输出都记进 close-out）
- [ ] 两个 host 的附着查询各有测试（Task 4）
- [ ] **真机证据**：重启后不带任何 workaround 直接部署 —— 即在 philosophers-stone 侧临时禁用它的
      `clear-recorded-deployment`，`compose down` 后 `make start` 仍能起来且不出现
      `structural reconfigure`。**这是本 plan 唯一能证明症状一真被修掉的证据**，单测证不了
      （它证的是分派选择，不是重启后的真实行为）
- [ ] 重复 generation 的真机行为：重启后重发同一 generation，**不得**出现「`wait-status` 通过而
      没有策略在跑」

## 偏离与改进日志 (Deviations & Improvements)

- 若实施中发现某个 host 的附着状态确实可能长于进程，记在这里 —— 那会推翻本 plan 的前提，且意味着
  归零式修法（选项 A）从一开始就不安全。
- 若选 A，记下当时对「引擎不长于进程」的判断依据，以及它被写在了哪里。

## Follow-up hooks（不属于本 plan scope，登记以防遗漏）

- **philosophers-stone 的 workaround 应在本 plan 落地后拆除。** 它在 `start` 里发 `stopped` spec
  清 `container_id` 并等回报（PS Plan 61）。拆除前应确认本 plan 的真机证据已取得，否则会把 PS
  退回到一整天前的状态。
- **`-4015` 的真机接单判据仍在 PS 侧**（本仓 Plan 25 的唯一未完成项），且还差一个含该修复的
  runner 镜像 —— PS 实跑取的是镜像里的 toolkit，不是 PS 仓的 wheel。与本 plan 无关，但两者都在
  等同一个「重建镜像」动作。
