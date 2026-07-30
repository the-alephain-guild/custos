# 26 — attachment-state-outlives-the-engine 的修复

> **Status**: ✅ Completed（2026-07-30；`aade81a`..`8ab85c5`）
> **Created**: 2026-07-30
> **Project**: custos (`tesseract-trading/custos/`)
> **Plan**: `.forge/plans/2026-07/26-attachment-state-outlives-the-engine.md`
> **Source**: `.forge/reviews/2026-07/codex/26-attachment-state-outlives-the-engine-peer-review.md`
> **For Claude**: `/forge:execute`

## 修复来源

codex 报 0 CRITICAL / 1 HIGH / 3 MEDIUM / 2 LOW。逐条实证后：2 条修，4 条不修并写明理由。

审查者的事实性断言先实证再采纳（本仓 C2 / 生态 #9/#11）。四条被引用的代码事实都核过：

- `supports_trading_mode` 确实在 `try` 之外（`src/custos/offline/reconciler.py:261`）
- `run()` 确实只 guard `next_msg`，不 guard `handle()`（`:167-181`）
- `ARCHIVED` 确是 `LifecycleState` 的真实变体（`src/custos/contracts/deployment.py:77`）
- `_on_node_task_done` 确实只 pop registry、不 `dispose()`（`src/custos/engines/nautilus/host.py:831-839`）

## 修复任务 (Tasks)

### Fix 1: `attached()` 把已经结束的 task 仍算作附着 [P2 / MEDIUM]

**Root Cause**: 实现错误。`attached()` 只查 dict membership，而 `add_done_callback` 是
**调度**不是立即执行 —— task 结束到 registry 被摘除之间隔着一次 loop 迭代。窗口内
`attached()` 答 True，相同 generation 就会在节点刚退出时被报成 healthy。这正是本 plan 要
消灭的那种「错误的成功信号」，只是尺度从进程边界缩到了一次调度。

计划 Task 2 自己写的语义是「`_active_nodes` 持的是活的 `(node, task)`，即**真的有个节点在跑**」——
`entry is not None` 表达不了「在跑」，`not task.done()` 才行。

**Files**: `src/custos/engines/nautilus/host.py`、`tests/test_nt_trading_node_host.py`

**Step 1**: 写 `test_a_finished_node_is_not_held_before_its_callback_runs` —— deploy 后让
run 循环自行结束，只 `await asyncio.sleep(0)`（**不**等 callback），先断言 entry 仍在
registry（证明窗口真的存在、测试没有被 callback 抢先），再断言 `attached()` 为 False
**Step 2**: 验证测试失败（已实跑：`assert True is False`，且第一条断言通过 = 窗口真实）
**Step 3**: `attached()` 改为 entry 存在**且** task 未 done
**Step 4**: 验证测试通过，且既有 21 条 NT host 测试不回归
**Step 5**: 提交

### Fix 2: 新判据只覆盖了 `stopped`，没覆盖 `archived` [P3 / LOW]

**Root Cause**: 测试覆盖缺口。`_TERMINAL_STATES` 是 `{STOPPED, ARCHIVED}`
（`src/custos/offline/reconciler.py:42`），两者走同一条路径，但只有 `stopped` 被断言过。
两个变体今天同路，所以这条是**防将来分岔**，不是修今天的 bug。

**Files**: `tests/test_offline_reconciler.py`

**Step 1**: 把 terminal 侧那条测试按 `lifecycle_state` 参数化，`stopped` 与 `archived` 各跑一遍
**Step 2**: 两个变体都绿（`archived` 本就该绿 —— 这条是覆盖，不是修复）
**Step 3**: 提交

## 不修的四条（登记，写明理由）

### HIGH-1 — 真机验收仍是 release blocker

**同意，且已经是当前状态。** plan 停在 ⏳ 而非 ✅，验证清单里两条真机项未勾，close-out 遗留项
1 写明「阻塞完成」。reviewer 这条与本仓 C11「一条通道的完成判据必须是对端接受过」一致，无需
额外动作 —— 它要的东西本来就没被声称已完成。

### MEDIUM-1 — 自终止 node 直接 redeploy，但 done callback 不 dispose

**实证成立，但越出本 plan scope。** `_on_node_task_done`（`host.py:831-839`）只摘 registry，
不做 `stop()` 里的 `node.dispose()` 与 task reap。

值得说清它的**方向**：改动前，节点自终止后 `container_id` 仍非空 → 下一个 generation 走
reconfigure → 被拒 → 这条 lane 从此卡死不再恢复。改动后它会重新 deploy。所以本 plan 把一个
**永久卡死**换成了**可能残留资源的恢复** —— 后者更好，但 reviewer 指出的清理缺口是真的。

不在本轮修的理由有三条：(a) 它属 host lifecycle，不属离线通道的分派，本 plan 的非目标写明
「只把当前事实表达清楚」；(b) `_on_node_task_done` 同时服务签名通道，改它的清理语义要连带
判断 `RunnerFactMessageBusBridge` 与 breaker 的状态归属；(c) 判断 NT `run_async()` 自行返回
后还剩什么没释放，需要真机语义证据，本仓现在给不出 —— 凭推理改 dispose 正是 #9/#11 禁的那种
动作。登记为遗留项，与真机证据同批处理。

### MEDIUM-3 — 相同 generation 的 `attached()` 抛异常绕过 RETRYABLE/NAK

**实证成立，但是既有形态，不是本 plan 引入的。** `_engine_is_where_the_spec_asks` 在
engine-op 的 `try` 之外（`reconciler.py:247`），而**同一段里 `supports_trading_mode` 早就在
try 之外**（`:261`）—— 两者都是同步 engine 查询，暴露完全相同。`run()` 也确实不 guard
`handle()`（`:167-181`），所以异常会终结 lane task。

把本 plan 新加的那一次调用单独包进 try，会让它与紧挨着的 `supports_trading_mode` 处置不一致，
读者无从判断哪种才是本仓的规矩。正确的修法是**一次性给所有同步 engine 查询定一个边界**，那是
一个独立的 plan，不是本 plan 的补丁。两个当前 host 都是 dict lookup，抛不出异常。登记为遗留项。

### LOW-1 — `container_id` 仍是持久化的 dead decision state

**同意，已在 plan close-out 遗留项 3 登记。** reviewer 多给了两点，一并并入那条：终局
generation 的早返回不会清掉上个进程留下的值；以及与其删不如先更名为
`last_deploy_receipt` 之类，让字段名自己说明它是回执不是当前状态。删它要动 SQLite schema 与
`AppliedRecord`，超出本 plan。

## 验证清单 (Verification)

- [x] Fix 1 的测试修复前红、修复后绿 —— 红侧 `assert True is False`，且**同一测试的第一条断言
      通过**（entry 仍在 registry），证明红的原因是查询答错而不是 callback 恰好晚跑；改回
      membership-only 会再次变红（已实跑）
- [x] Fix 2 两个 terminal 变体都绿（`archived` 本就绿 —— 这条是覆盖不是修复）
- [x] `make test-baseline` 全绿（2201 passed / 25 skipped / 1 xfailed）
- [x] `ExecutionEngineProtocol` 仍零改动
- [x] 不修的四条各自写明理由，且引用的代码事实都有 `file:line` 实证

## 进度追踪 (Progress)

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 | P2 | ✅ | 2026-07-30 | `attached()` 不把已结束的 task 算作附着（`aade81a`） |
| 2 | P3 | ✅ | 2026-07-30 | terminal 判据覆盖 `archived`（`8ab85c5`） |

## 完成报告 (Close-out Report)

- **完成日期**: 2026-07-30
- **修 2 条 / 登记 4 条**，无一条被静默丢弃
- **实施 commit 范围**: `aade81a`..`8ab85c5`
- **测试条数变化**: `tests/test_nt_trading_node_host.py` 21 → 22、
  `tests/test_offline_reconciler.py` 36 → 37。两行都在 plan 26 的 close-out 表里重数过 ——
  fix cycle 是同一个 plan 的交付，且 plan 仍停在 ⏳，close-out 未定稿
- **红线影响**: 无。Fix 1 让 `attached()` 答得更严（原本会短暂多答一个 True），方向是收紧不是
  放宽；Fix 2 纯测试

### 关于 MEDIUM-2 值得记一句

它和本 plan 修的是**同一个错误，只是尺度不同**：plan 修的是「跨进程边界后仍相信旧记录」，
这条是「跨一次 loop 调度后仍相信旧 registry」。两次都是把一个**代表过去的东西**当成
**现在的答案**。写 `attached()` 时我按计划的措辞选对了 dict，却没把「持的是活的 `(node, task)`」
这句话读到底 —— `entry is not None` 表达不了「活」。

外部审查抓到它，而两轮自省没有：自省看的是自己刚写的代码对不对，看不见「我对这句话的理解本身
就少了一半」。
