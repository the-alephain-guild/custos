# 21 — offline-lane-fixes

> **Status**: 🔲 Not started
> **Created**: 2026-07-29
> **Project**: custos (`tesseract-trading/custos/`)
> **Plan**: `.forge/plans/2026-07/21-sandbox-offline-deployment-path.md`
> **Source**: `.forge/reviews/2026-07/21-offline-lane-review.md`
> **For Claude**: `/forge:execute` 实施本修复计划

## 修复来源

- 计划文件: `.forge/plans/2026-07/21-sandbox-offline-deployment-path.md`
- 审查报告: `.forge/reviews/2026-07/21-offline-lane-review.md`（4 CRITICAL / 2 HIGH / 2 MEDIUM / 2 LOW）

## 分诊

| Finding | 严重度 | 优先级 | 根因类别 |
|---|---|---|---|
| C1 组合位置挪出 `_daemon.py` 未登记 | 🔴 | P0 | 有意改进未标注 + 计划文本过期 |
| C2 `state.py` 计划外新增功能 | 🔴 | P0 | 有意改进未标注 + 计划文本过期 |
| C3 close-out 测试数字与实际不符 | 🔴 | P0 | 实现错误（声明层）+ 规则缺失 |
| C4 `bootstrap_standalone_nats` 改名未登记 | 🔴 | P0 | 有意改进未标注 + 计划文本过期 |
| H1 失败 apply 被 ack，重投不重试 | 🟠 | P1 | 实现错误 |
| H2 `BindMountedStrategy` 零覆盖 + env seam 静默失效 | 🟠 | P1 | 实现错误 |
| M1 Verification 复选框与 Status 矛盾 | 🟡 | P2 | 实现错误（文档） |
| M2 面存在性断言依赖 argparse 私有内部 | 🟡 | P2 | 实现错误 |
| L1 无必要 import 别名 | 🔵 | P3 | 品味 |
| L2 `AppliedStore` Protocol 丢类型 | 🔵 | P3 | 品味 |

**C1/C2/C4 三条同源**：实施期做的都是正确的技术决定，错在没有按偏离协议登记，且计划文本
至今指向已不存在的结构与符号。修的是**记录**，不是代码 —— 把代码改回计划原文会让它变差。

**C3 是本轮唯一的诚信类问题**：数字未经计数即写下。除改正外，还要补一道机械 gate，
否则同类必然复发（lesson #25 已在生态记过一次，这是 custos 内首次复发）。

## 修复任务 (Tasks)

### Fix 1: 登记三条未记录的偏离，并修正计划中已过期的文本 [P0 · C1/C2/C4]

**Root Cause**: 有意改进未标注 + 计划文本过期。

**Files**: `.forge/plans/2026-07/21-sandbox-offline-deployment-path.md`

**Step 1**: 偏离日志新增三条 —— `DEV-21-COMPOSITION-OUTSIDE-SIGNED-DAEMON`（含为何不塞进
`_daemon.py`：会被迫桩掉控制面校验）、`DEV-21-DURABLE-APPLIED-STATE`（含两个驱动：
readiness 需真实 sqlite 判据、重启不得重复部署）、`DEV-21-BOOTSTRAP-SYMBOL-NAME`。

**Step 2**: 修正 Task 5 正文的 `bootstrap_standalone_nats` → `bootstrap_standalone_streams`；
修正 Task 7 正文，改为描述实际落地的组合位置。

**Step 3**: `grep -c 'bootstrap_standalone_nats'` 计划文件应为 0（除偏离条目内的历史引用）。

**Commit**: `docs(custos): record the three deviations plan 21 left unlogged`

### Fix 2: 按实测改正 close-out 的测试计数，并补一道计数 gate [P0 · C3]

**Root Cause**: 实现错误（声明层）+ 规则缺失。

**Files**: `.forge/plans/2026-07/21-sandbox-offline-deployment-path.md`、
`.claude/rules/progress-management.md`、`tests/test_plan_closeout_counts.py`（新建）

**Step 1**: 写测试证明缺陷存在 —— 一个探针，扫 close-out 里形如「新增 N 个测试」的声明，
按 plan 命名的测试文件实际 collect 计数核对，不符即红。先对当前（91 vs 117）跑出红。

**Step 2**: 验证测试失败。

**Step 3**: 改正 close-out 数字与分项（补上漏计的 8 个权威门测试）。

**Step 4**: 验证测试通过。

**Step 5**: `progress-management.md` 完成报告模板加硬要求：数字类声明必须来自实跑计数，
禁手写。

**Commit**: `test(custos): make a close-out test count prove itself`

### Fix 3: 失败的 apply 不再被确认 [P1 · H1]

**Root Cause**: 实现错误 —— `reconciler.py:144-147` 不看 `handle()` 返回值就 ack。

**Files**: `src/custos/offline/reconciler.py`、`tests/test_offline_reconciler.py`

**Step 1**: 写失败测试 —— apply 失败时应请求重投（`nak`）而非确认（`ack`）；成功时确认；
两者互斥；消息不带 ack/nak 时不崩且留下日志。

**Step 2**: 验证测试失败。

**Step 3**: 最小修复 —— `run()` 按 `handle()` 结果结算：成功 `ack()`，失败 `nak(delay=…)`，
都不可用时告警。

**Step 4**: 验证通过。

**Commit**: `fix(custos): stop acknowledging desired state the lane failed to apply`

### Fix 4: 策略目录选择改为显式且 fail fast，并补测试 [P1 · H2]

**Root Cause**: 实现错误 —— `daemon.py:60` 用 `setdefault`，而 `registry.py:70-72` 在模块
import 时就读掉了 `STRATEGY_INJECT_PATH`。两条静默错误路径：registry 已导入时设置无效；
第二个不同 `strategy_path` 的 spec 被静默忽略。

**Files**: `src/custos/offline/daemon.py`、`tests/test_offline_lane_daemon.py`

**Step 1**: 写失败测试 —— (a) 同一进程内第二个不同目录必须报错而非静默沿用；(b) registry
已导入且尚未选定目录时必须报错而非静默降级到内置发现路径；(c) 同目录重复调用幂等；
(d) `activation_id` 由摘要决定。

**Step 2**: 验证测试失败。

**Step 3**: 最小修复 —— 抽出 `_prepare_discovery()`，显式赋值 + 两处 fail fast。

**Step 4**: 验证通过。

**Commit**: `fix(custos): choose the strategy directory explicitly instead of by default`

### Fix 5: 面存在性断言改用公开入口 [P2 · M2]

**Root Cause**: 实现错误 —— 断言绑在 `argparse` 私有属性上。

**Files**: `tests/test_gateway_contract_v1_samples.py`

**Step 1**: 改为通过公开 `main([...,"--help"])` 的退出码探测（存在=0，不存在=2），
保留一个必然不存在的名字作负对照。

**Step 2**: 验证通过且负对照仍会红。

**Commit**: `test(custos): probe the offline lane surface through the public entry point`

### Fix 6: 对齐 Verification 段与 close-out，清理两处品味问题 [P2/P3 · M1/L1/L2]

**Root Cause**: 实现错误（文档）+ 品味。

**Files**: `.forge/plans/2026-07/21-sandbox-offline-deployment-path.md`、
`src/custos/offline/spec.py`、`src/custos/offline/reconciler.py`

**Step 1**: Verification 六项按实际勾选，未跑的两项显式标注为未执行并指向 close-out。

**Step 2**: 删 `model_validator as pydantic_model_validator` 别名；`AppliedStore` Protocol
改用 `AppliedRecord` 具体类型。

**Step 3**: `make lint` + 全量测试通过。

**Commit**: `refactor(custos): align plan 21 verification with its close-out and drop two smells`

## 验证清单 (Verification)

- [ ] `make lint` 绿
- [ ] `make check-authority` 绿
- [ ] `make test` 无新增失败（既有 3 处 `fmt-check` 红属 lesson C6，不在本轮范围）
- [ ] 计划文件内 `bootstrap_standalone_nats` 仅存在于偏离条目的历史引用中
- [ ] close-out 测试计数与实跑一致，并由探针守住
- [ ] 偏离日志条数 = 8（原 5 + 新 3）

## 进度追踪 (Progress)

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| Fix 1 | P0 | 🔲 | | C1/C2/C4 |
| Fix 2 | P0 | 🔲 | | C3 |
| Fix 3 | P1 | 🔲 | | H1 |
| Fix 4 | P1 | 🔲 | | H2 |
| Fix 5 | P2 | 🔲 | | M2 |
| Fix 6 | P2/P3 | 🔲 | | M1/L1/L2 |
