# 22 — offline-lane-guard-fixes

> **Status**: 🔲 Not started
> **Created**: 2026-07-29
> **Project**: custos (`tesseract-trading/custos/`)
> **Plan**: `.forge/plans/2026-07/22-offline-lane-local-exposure-guard.md`
> **Source**: `.forge/reviews/2026-07/22-offline-lane-guard-review.md`
> **For Claude**: `/forge:execute` 实施本修复计划

## 修复来源

- 计划文件: `.forge/plans/2026-07/22-offline-lane-local-exposure-guard.md`
- 审查报告: `.forge/reviews/2026-07/22-offline-lane-guard-review.md`（1 CRITICAL / 1 HIGH /
  4 MEDIUM / 1 LOW）

## 分诊

| Finding | 严重度 | 优先级 | 根因类别 |
|---|---|---|---|
| C1 `asyncio.gather` 偏离未登记，计划正文过期 | 🔴 | P0 | 有意改进未标注 + 计划文本过期 |
| H1 close-out 的红线 0.3 声明未划出重启口子 | 🟠 | P1 | 实现错误（声明层） |
| M1 抬高的上限经 reconciler 到 guard 这段无测试 | 🟡 | P2 | 实现错误（测试缺口） |
| M2 File Inventory 的 reconciler 描述过期 | 🟡 | P2 | 计划文本过期 |
| M3 两个改动文件不在 File Inventory | 🟡 | P2 | 计划文本过期 |
| M4 guard 调引擎无超时，卡死引擎拖住 tick 与关停 | 🟡 | P2 | 实现错误 |
| L1 非有限上限的防线无覆盖 | 🔵 | P3 | 测试缺口 |

**C1/M2/M3 同源**：实施期做的都是正确的技术决定，错在计划正文没跟着改。修的是**记录**，
把代码改回计划原文会让它变差 —— 与 plan 21 的 C1/C2/C4 是同一形态。

**H1 是本轮唯一的声明诚信问题**：红线名（"失联 ≠ 停止"）被当成了兑现声明。lesson #40
要求的正是分层：code 覆盖 / runtime 接线 / defer 范围。重启后是否重新 watch 属独立设计
决策 —— 盲目 watch 一个不存在的 instance 会让 `get_engine_status` 抛错、fail closed、
误 flatten 并锁死，比不 watch 更糟。所以本轮**只修声明**，把设计问题留成遗留项。

**M4 审查里写的是"不在本 plan 修"，本轮改判为修。** 理由：守卫存在的意义就是在东西坏掉时
仍然工作，而"引擎不返回"是它当前唯一没有防线的坏法；且 `_run_together` 的 `finally` 会因此
永远等不回来，关停也一起挂住。修法限定为**给每个 tick 加期限**，不做第二次 flatten 尝试 ——
一个挂住的引擎不会在第二次调用时回答，堆机制只会新增挂起路径。

## 修复任务 (Tasks)

### Fix 1: 登记 `asyncio.gather` 偏离并改正计划正文 [P0 · C1]

**Root Cause**: 有意改进未标注 + 计划文本过期。

**Files**: `.forge/plans/2026-07/22-offline-lane-local-exposure-guard.md`

**Step 1**: 偏离日志新增 `_run_together` 一条，写清为什么不是 `gather`：`gather` 在首个异常时
把异常抛给调用者但不会让另一个 loop 停下 —— 正是"guard 死了 lane 还在交易"的形态。

**Step 2**: Task 2 正文的 `asyncio.gather` 改为描述实际语义。

**Step 3**: `grep -c 'asyncio.gather' <plan>` 应为 0（除偏离条目内的历史引用）。

**Commit**: `docs(custos): record why the offline lane does not compose with gather`

### Fix 2: 红线 0.3 的兑现声明降级到实际范围 [P1 · H1]

**Root Cause**: 实现错误（声明层）—— 承袭红线名当兑现声明。

**Files**: `.forge/plans/2026-07/22-offline-lane-local-exposure-guard.md`

**Step 1**: close-out 红线表 0.3 行的"离线通道已兑现"补上重启口子，附实证（重投已应用的
generation 走 `==` 短路，不部署也不 watch）。

**Step 2**: 遗留项新增一条，写明这是 Plan 21 既有设计的新后果，以及为什么"重启就 watch"
不是可以顺手做的修复。

**Commit**: `docs(custos): scope the offline lane's red line 0.3 claim to what it covers`

### Fix 3: 补一条变异敏感的接线测试 [P2 · M1]

**Root Cause**: 实现错误（测试缺口）—— guard 层的上限测试直接调 `watch`，绕过了
reconciler 的接线。

**Files**: `tests/test_offline_reconciler.py`

**Step 1**: 写测试 —— 经 `reconciler.apply` 应用一个把上限抬到 `25000` 的 spec，再
`guard.evaluate_once()`，敞口取 `9000`（远超默认 `200`、低于抬高后的 `25000`），断言不
flatten 且未锁死。

**Step 2**: 证伪 —— 把 `_update_guard` 传的 `limits` 临时换成
`FallbackBreakerConfig.strictest_local_fallback(...)`，确认新测试变红而其余测试仍绿（这正是
现在的盲区）。改回。

**Step 3**: 确认测试通过。

**Commit**: `test(custos): prove the spec's ceiling reaches the guard through the reconciler`

### Fix 4: File Inventory 与实际改动对齐 [P2 · M2/M3]

**Root Cause**: 计划文本过期。

**Files**: `.forge/plans/2026-07/22-offline-lane-local-exposure-guard.md`

**Step 1**: reconciler.py 行的"暴露活跃 instance id"改为实际职责（接 guard、限额前置读取、
锁死拒绝），活跃 instance 由 guard 持有。

**Step 2**: 补 `tests/test_plan_closeout_counts.py` 与 `.forge/README.md` 两行。

**Commit**: `docs(custos): align plan 22's file inventory with what it changed`

### Fix 5: 每个 tick 加期限，不答话的引擎 fail closed [P2 · M4]

**Root Cause**: 实现错误 —— `get_engine_status` 无超时，卡死的引擎让 `evaluate_once` 永久
挂起，`_run_together` 的 `finally` 也跟着挂住。

**Files**: `src/custos/offline/safety.py`、`tests/test_offline_lane_safety.py`

**Step 1**: 写失败测试 —— (a) 引擎永不返回时 `evaluate_once` 在期限内返回而非挂起；
(b) 超时即锁死（`allows_new_generations()` 为 False）；(c) 超时的日志说明containment
未被确认；(d) `run` 在这种引擎下仍能结束。

**Step 2**: 验证测试失败（现在会挂到 pytest 超时）。

**Step 3**: 最小实现 —— 每次 `supervisor.evaluate_once` 包一层期限；超时即
`breaker.fail_closed(...)` + 记录 containment 未确认。不做第二次 flatten 尝试。

**Step 4**: 验证通过。

**Commit**: `fix(custos): bound each exposure tick so a wedged engine cannot stall it`

### Fix 6: 非有限上限补覆盖 [P3 · L1]

**Root Cause**: 测试缺口。

**Files**: `tests/test_offline_lane_safety.py`

**Step 1**: 参数化列表补 `"NaN"` / `"Infinity"` / `"-Infinity"`，确认 `is_finite()` 那条防线
真的会咬。

**Commit**: `test(custos): cover the ceilings decimal accepts but arithmetic cannot`

### Fix 7: 重数并收尾

**Files**: `.forge/plans/2026-07/22-offline-lane-local-exposure-guard.md`、本文件

**Step 1**: Fix 3/5/6 加了测试 → close-out 计数表按 `pytest --collect-only` 实跑重数
（`tests/test_plan_closeout_counts.py` 会拒绝对不上的数字）。

**Step 2**: 本文件进度表 + 完成报告。

**Commit**: `docs(custos): recount plan 22's close-out after the fix cycle`

## 验证清单 (Verification)

- [ ] `make lint` 绿
- [ ] `make check-authority` 绿
- [ ] `make test` 无新增失败
- [ ] 计划文件内 `asyncio.gather` 仅剩偏离条目内的历史引用
- [ ] Fix 3 的测试经变异证伪（改 `limits` 会红）
- [ ] Fix 5 的测试在实现前会挂/红，实现后绿
- [ ] close-out 计数表与实跑一致

## 进度追踪 (Progress)

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| Fix 1 | P0 | 🔲 | | C1 |
| Fix 2 | P1 | 🔲 | | H1 |
| Fix 3 | P2 | 🔲 | | M1 |
| Fix 4 | P2 | 🔲 | | M2/M3 |
| Fix 5 | P2 | 🔲 | | M4 |
| Fix 6 | P3 | 🔲 | | L1 |
| Fix 7 | — | 🔲 | | 重数收尾 |
