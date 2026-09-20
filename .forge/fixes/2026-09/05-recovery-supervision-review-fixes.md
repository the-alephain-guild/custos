# 05 - recovery-supervision-review-fixes

> **Status**: ⏳ In Progress
> **Created**: 2026-09-20
> **Project**: custos
> **Source**: `.forge/reviews/2026-09/04-recovery-and-post-execution-consistency-review.md` + taste report
> **For Codex**: 先红后绿；计划先提交；完成后进入 lessons。

## 修复来源

- 原计划：`.forge/fixes/2026-09/04-recovery-and-post-execution-consistency-fixes.md`
- 功能审计：`.forge/reviews/2026-09/04-recovery-and-post-execution-consistency-review.md`
- 品味审计：`.forge/reviews/2026-09/taste-fix-04-recovery-consistency.md`
- 分诊：C1→P0、H1→P1、M1/M2→P2；四个上界项登记但不机械修改

## 修复任务

### Fix 1: terminal 动作前重新确认 durable authority，删除 silent capability fallback [P0/P1]

**Root Cause**: 实现错误：依赖 watcher cancellation 缩小竞态，却没有在 destructive stop 前验证 current desired；测试便利分支让关键 capability 缺失时静默继续。

**Files**: `engine_lifecycle.py`、`runner_command_runtime.py`、生命周期/runtime tests

1. 写失败测试：`wait_terminal` 返回后 durable generation 已变，旧 supervisor 不得调用 stop/restart/quarantine。
2. 在 `supervise_once` 的 terminal identity 验证后、stop 前加载 lifecycle state；只有 applied generation/fingerprint 与 verified 完全一致才继续。
3. authority 已替换时返回 typed retired outcome，watcher 正常结束，不上报 daemon failure。
4. `_start_engine_supervision` 直接调用 typed `supervise_once`；缺失 capability 立即失败。
5. 验证新 generation、non-running replacement、同代 retryable terminal。

### Fix 2: heartbeat 挂起的有界清理回归 [P2]

**Root Cause**: 测试缺失：实现有 deadline，但只覆盖抛异常。

**Files**: `test_runner_command_runtime.py`

1. 构造忽略第一次 cancellation、稍后才完成的 `in_progress()`。
2. 证明成功 apply/ACK 在一个 heartbeat interval 后返回，不等待挂起 lease 无限期。
3. 证明迟到 task 的异常/完成被消费，无 unhandled-task warning。

### Fix 3: generation 切换长 gap 回归 [P2]

**Root Cause**: 测试缺失：只覆盖短于一个 period 的切换。

**Files**: `test_runner_fact_production_loop.py`

1. 模拟 generation 切换后停顿超过一个完整 period。
2. 断言 gap 分支不伪造 close；period/coverage 从恢复边界重置。
3. 下一周期固定成交只出现一次，旧 coverage 不被重复采集。

## 验证清单

- [ ] C1/H1 失败测试先红后绿
- [ ] M1/M2 时序测试通过
- [ ] Fix 04 定向测试通过
- [ ] `make verify` 通过
- [ ] `git diff --check` 通过
- [ ] 无真实账户、下单、容器或生产操作

## 偏离与上界 handoff

- taste 的四个巨型文件属于数据结构上界；Fix 05 不按行数拆分。
- `--nostop` 继续解释为不等待批次确认，不跳过硬门。
- Fix 1/2 共用 command runtime 时序测试，合并为一个原子 commit，避免人为拆 hunk。

## 进度追踪

| Fix | Priority | Status | Completed | Commit |
|---|---:|---|---|---|
| 1 | P0/P1 | ✅ | 2026-09-20 | pending close-out |
| 2 | P2 | ✅ | 2026-09-20 | pending close-out |
| 3 | P2 | 🔲 | — | — |
