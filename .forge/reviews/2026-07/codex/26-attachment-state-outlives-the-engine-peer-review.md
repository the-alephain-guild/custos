# 同行审查报告: Plan 26 — 重启后仍相信上个进程的附着状态

> **审查日期**: 2026-07-30
> **计划文件**: `.forge/plans/2026-07/26-attachment-state-outlives-the-engine.md`
> **审查 CLI**: codex（`model_reasoning_effort=high`，默认模型）
> **审查范围**: plan 全文 + `39ff798`..`0347c5c` 的代码 diff（337 增 / 19 删，7 文件，不含 `.forge/`）
> **gemini**: 不可用（`command -v gemini` 未命中），故本轮只有一个外部意见

## Codex Review

## Summary

实现准确修复了计划描述的两个重启症状：dispatch 不再依赖持久化的 `container_id`，相同 generation 也会根据真实 attachment 决定是否重新 engage。直接查询 engine 的偏离总体优于“一次性校准”，还能覆盖同进程 node 自行结束的情况。窄 `OfflineEngine` protocol、terminal predicate、两个 host 的独立测试都设计合理。本轮实跑相关 78 条测试全部通过，ruff 与 format check 也通过。不过仍有三个重要边界：NT 自终止后的清理、`attached()` 与真实 liveness/readiness 的差距，以及相同 generation 查询异常绕过 RETRYABLE/NAK。真机证据仍未取得，因此当前 `In Progress` 状态是正确的。

## Strengths

- 根因与实现吻合：持久化记录仍保留 generation，但 deploy/reconfigure dispatch 改为查询 engine 当前状态；新 generation 在无 attachment 时走 deploy，相同 generation 也不会仅凭旧记录报告 healthy。`src/custos/offline/reconciler.py:236-287`、`src/custos/offline/reconciler.py:335-357`

- 直接查询 engine 的偏离是合理改进。相较只在首次触碰时校准记录，它可以持续发现同进程内 attachment 漂移，例如 `_on_node_task_done` 摘除 node 后的状态变化。`src/custos/engines/nautilus/host.py:824-832`

- protocol 放置正确。`attached()` 只加入离线 lane 所需的窄 `OfflineEngine`，没有扩大 `ExecutionEngineProtocol` 的 runtime structural typing 契约。`src/custos/offline/reconciler.py:63-80`

- 两个 host 都按各自真实记账实现：sandbox 按实例查询 `_lifecycle_authorities`；NT 查询持有 `(node, task)` 的 `_active_nodes`，没有错误使用仅代表授权记录的 registry。`src/custos/engines/nautilus/host.py:120-156`、`src/custos/engines/nautilus/host.py:254-258`、`src/custos/engines/nautilus/host.py:548-554`

- terminal 语义处理正确：`stopped` 和 `archived` 都要求“不 attached”；相同 generation 已经不存在时可以直接 healthy，新 generation 则执行幂等 stop。`src/custos/offline/reconciler.py:42`、`src/custos/offline/reconciler.py:289-303`、`src/custos/offline/reconciler.py:341-345`

- 正常 reconcile loop 是串行的：处理完一条 delivery 才读取下一条，因此当前 daemon 接线不会让两个 desired-state delivery 并行竞争 deploy。`src/custos/offline/reconciler.py:167-181`

- exposure guard 的 latch 仍先于 generation/attachment 判断，跳闸后相同 generation 也不会借新的 healthy 分支重新进入。`src/custos/offline/reconciler.py:209-220`、`src/custos/offline/safety.py:116-120`

- 测试不是只验证 method 存在：覆盖了重启新 generation、相同 generation、terminal absence、进程内廉价重投、按实例 attachment，以及 NT node 自行结束。`tests/test_offline_reconciler.py:285-347`、`tests/test_nt_trading_node_host.py:414-455`、`tests/engines/nautilus/test_sandbox_host_attachment.py:37-69`

- 四条红线的正常路径没有被放宽：live 仍在 reconciler boundary 拒绝；attachment 查询不接触 credential 或 money math；guard latch 仍有效。`src/custos/offline/reconciler.py:209-220`、`src/custos/offline/reconciler.py:346-355`

## Concerns

- **HIGH — 真机验收仍是 release blocker。** 当前测试证明的是 dispatch 选择，不证明实际 runner 镜像、NT startup 和 PS `wait-status` 组合能够正常工作。计划也正确地保留了这两个未完成 gate；在取得证据前不能拆除 PS workaround。`.forge/plans/2026-07/26-attachment-state-outlives-the-engine.md:189-203`、`.forge/plans/2026-07/26-attachment-state-outlives-the-engine.md:334-341`

- **MEDIUM — NT node 自行结束后会直接 redeploy，但代码没有证明旧 node 已清理。** done callback 只从 `_active_nodes` 和 `_runner_fact_contexts` 摘除条目，没有走 `stop()` 中的 `node.dispose()`、task reap 及其他清理；随后 `attached()` 返回 False，reconciler 会直接 deploy 新 node。是否会残留连接或其他 NT 资源取决于 `run_async()` 的外部语义，当前代码无法确认。`src/custos/engines/nautilus/host.py:517-545`、`src/custos/engines/nautilus/host.py:824-841`、`src/custos/offline/reconciler.py:346-355`。现有测试只断言 registry 被摘除，没有断言 dispose 后才能 redeploy。`tests/test_nt_trading_node_host.py:436-455`

- **MEDIUM — `attached()` 仍可能产生短暂 false healthy。** NT 实现只检查 dictionary membership，没有检查其中 task 是否已经 `done()`；task 完成与 done callback 摘除 registry 之间存在调度窗口。相同 generation 在该窗口内可能直接报告 healthy。`src/custos/engines/nautilus/host.py:548-554`、`src/custos/engines/nautilus/host.py:824-832`、`src/custos/offline/reconciler.py:247-250`。此外 deploy 在启动 background task 后立即返回，reconciler 随即报告 healthy，并没有调用已有的 readiness 检查，因此这里证明的是 attachment，而不是 venue connectivity 或策略已经 ready。`src/custos/engines/nautilus/host.py:379-398`、`src/custos/engines/nautilus/host.py:556-575`、`src/custos/offline/reconciler.py:283-287`

- **MEDIUM — 相同 generation 的 `attached()` 异常绕过 RETRYABLE/NAK。** `_engine_is_where_the_spec_asks()` 在 engine-operation 的 `try` 之外调用；如果查询抛异常，`apply()` 会直接退出，delivery 不会进入 `_settle()`，daemon 的 `FIRST_EXCEPTION` 路径还会结束 guard loop。两个当前 host 只是 dict lookup，实际概率很低，但这是新增 protocol 能力没有完整接入 retry boundary。`src/custos/offline/reconciler.py:247-260`、`src/custos/offline/reconciler.py:271-281`、`src/custos/offline/reconciler.py:181-205`、`src/custos/offline/daemon.py:158-189`

- **LOW — `container_id` 仍是持久化的 dead decision state。** 它被 load、save，并在 reconfigure 后原样延续，却不再参与任何决策；相同 terminal generation 的早返回甚至不会清除上个进程留下的值。docstring 已降低误用风险，但这个字段仍可能被未来代码再次误读，同时维持了无实际用途的 SQLite schema 成本。`src/custos/offline/reconciler.py:122-134`、`src/custos/offline/reconciler.py:162-165`、`src/custos/offline/reconciler.py:324-357`、`src/custos/offline/state.py:15-34`

- **LOW — 边界测试仍有小缺口。** `archived` 与 `stopped` 共用路径，因此风险不高，但新 predicate 只显式覆盖了 stopped；也没有覆盖 `attached()` 抛异常、task 已 done 但 callback 尚未执行、以及自终止后 cleanup→redeploy 的完整序列。`src/custos/offline/reconciler.py:42`、`tests/test_offline_reconciler.py:332-347`、`tests/test_nt_trading_node_host.py:436-455`

## Suggestions

- 在 NT `attached()` 中至少同时检查 registry entry 和 `not task.done()`；为“task 已结束、callback 尚未执行”的窗口增加定向测试。

- 明确区分 attachment 与 health。最小方案是限定 status 文档中的 healthy 含义；更完整的方案是在相同 generation 报 healthy 前检查一个窄的 liveness/readiness predicate，而不是仅检查 registry membership。

- 为自终止 node 建立明确 cleanup 路径：done callback 中可靠 dispose，或保留 terminal/orphan entry，要求 reconciler 先执行 cleanup/stop，再允许 redeploy。增加“自终止 → 资源已清理 → 同 generation 成功 redeploy”的测试。

- 把 `_engine_is_where_the_spec_asks()` 包入与 `_engage()` 相同的异常边界：报告 unhealthy、返回 `Settlement.RETRYABLE`，并验证 delivery 被 NAK，而不是终止 reconciler 和 guard。

- 单独登记 schema follow-up：删除 `container_id`，或至少重命名为明确的 `last_deploy_receipt`/provenance 字段，并停止在 `_Applied` 中把它塑造成当前状态。

- 增加 `archived` predicate 测试；若 `handle/apply` 被视为可供其他调用者使用，还应显式串行化或记录“只允许 daemon loop 单调用者”的 invariant。

- 在两条 PS 真机 gate 完成前，保持 plan 为 `In Progress`，不要拆 workaround，也不要把本变更描述为 production-validated。

## Risk Assessment

**MEDIUM**

变更范围窄、根因判断正确、正常 restart dispatch 与 terminal/guard 行为有较强测试支撑，也没有直接放宽四条红线。主要风险来自 direct engine dispatch 扩展出的自终止 redeploy 行为、attachment 被等同于 healthy，以及查询异常尚未纳入 retry boundary。代码合入风险为 MEDIUM；在 PS 真机证据取得前，部署并拆除 workaround 的操作风险应视为 HIGH。
---

## 分诊 (Triage)

逐条实证与处置见 `.forge/fixes/2026-07/26-attachment-state-outlives-the-engine-fixes.md`。
本报告只保留 reviewer 原文，不在此处下结论——审查者的事实性断言同样需要先读权威源
再采纳（本仓 C2 / 生态 lesson #9/#11）。
