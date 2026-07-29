# 审计报告: Plan 22 — 离线通道的本地敞口守卫

> **审计日期**: 2026-07-29
> **计划文件**: `.forge/plans/2026-07/22-offline-lane-local-exposure-guard.md`
> **审计员**: Claude Code
> **审计范围**: `3aaa318`（plan）→ `ee1389d`（close-out）

## Executive Summary

四个 Task 全部落地，文件清单与实现一一对上，测试全绿（824 passed / 0 failed）。核心机制
经得起查：tick 与传输的解耦有断言而非推断，锁死语义有引擎零调用的证据，限额的两层拒绝
各测一次且内层有 relaxed-double。

问题集中在**声明层**而非代码层。一处计划文本与实现不符且未登记（`asyncio.gather`）；
一处 close-out 的红线兑现声明在重启场景下不成立 —— 正是 lesson #40 要防的那类措辞。
另有一处接线在测试上是变异不敏感的。

## 整体匹配率: 90%

| 严重度 | 数量 |
|---|---|
| 🔴 CRITICAL | 1 |
| 🟠 HIGH | 1 |
| 🟡 MEDIUM | 4 |
| 🔵 LOW | 1 |

---

## 🔴 CRITICAL

### C1: `asyncio.gather` 的偏离未登记，计划正文至今指向不存在的实现

- **文件**: `.forge/plans/2026-07/22-offline-lane-local-exposure-guard.md:96` vs
  `src/custos/offline/daemon.py:163`
- **计划定义**: 「`run_offline_lane` 用 `asyncio.gather` 并行跑订阅与 tick」
- **实际代码**: `_run_together()`，内部是 `asyncio.wait(..., return_when=FIRST_EXCEPTION)`
  + `stop.set()` + 二次 drain + `task.result()` 重抛。
  `grep asyncio.gather src/custos/offline/daemon.py` 零命中
- **影响**: 二者语义不同 —— `gather` 默认在首个异常时把异常抛给调用者但**不会**让另一个
  loop 停下来，正是"guard 死了 lane 还在交易"的形态。实现选了更强的语义，但偏离日志
  8 条里没有这一条，计划正文也没改。读计划的人会去找一个不存在的 `gather`
- **修复**: 偏离日志补一条 + Task 2 正文改为描述实际语义。**不改代码** —— 把代码改回
  `gather` 会让它变差

---

## 🟠 HIGH

### H1: 重启后 lane 报 healthy，却既没部署也没守卫；close-out 的 0.3 声明未划出这个口子

- **文件**: `src/custos/offline/reconciler.py:220-224`（`generation == applied` 分支）
- **实证**（脚本实跑，非推理）：持久化状态为 generation 1 的新进程收到同一 generation 1 时 ——

  ```
  settlement: applied      deployed: []
  guard ticks: []          engine asked: []
  ```

  引擎那侧的敞口设成 `$9999`、上限 `$200`，守卫**一次都没被问过**。
- **计划定义**: close-out 红线表 0.3 行写「离线通道已兑现（周期敞口评估 + 越限 flatten +
  锁死）」，无条件
- **影响**: "不重复部署已应用的 generation" 是 Plan 21 的既有设计
  （`test_forgets_nothing_across_a_restart` 明文固定），本 plan 没引入它。但本 plan 让它有了
  新后果：重启后交易所仍持有仓位，而这条 lane 报 healthy、不部署、也不守卫。按 lesson #40，
  红线兑现声明必须显式降级到实际范围，不能承袭红线名
- **修复**: close-out 红线表 0.3 行补一句"重启后未重新部署的 generation 不在守卫范围内"，
  并进遗留项；是否让重启重新 watch 属独立设计决策，不在本 plan 范围

---

## 🟡 MEDIUM

### M1: 没有测试证明 spec 抬高的上限真的经 reconciler 到达 guard

- **文件**: `src/custos/offline/reconciler.py:279` · `tests/test_offline_reconciler.py`
- **实证**: `grep 25000 tests/test_offline_reconciler.py` 零命中；reconciler 层唯一碰
  `risk_config` 的测试用的是读不出的值
- **影响**: 把 `_update_guard` 里的 `limits` 换成硬编码 strictest，全套测试仍绿 —— guard 层的
  上限测试是直接调 `watch` 的，绕过了这段接线。变异不敏感
- **修复**: 加一条经 `reconciler.apply` 应用一个抬高上限的 spec、再 `evaluate_once` 断言不
  flatten 的测试

### M2: File Inventory 说 reconciler 要"暴露活跃 instance id"，实际没有

- **文件**: 计划 File Inventory 第 2 行 · `src/custos/offline/reconciler.py`
- **实证**: `grep active_instances src/custos/offline/reconciler.py` 零命中
- **影响**: 该职责落到了 guard 上，是"接 guard 而非 breaker"那条已登记偏离的自然结果，但
  File Inventory 的描述没同步，读起来像漏做

### M3: 两个改动文件不在 File Inventory

- **文件**: `tests/test_plan_closeout_counts.py`、`.forge/README.md`
- **影响**: 探针作用域那次改动在偏离日志里有（"改进 | 计划外"），所以不是静默偏离；但
  File Inventory 没补，两处记录不一致。`.forge/README.md` 由 Task 4 动作清单覆盖，属惯例

### M4: guard 调引擎无超时，卡死的引擎会同时拖住 tick 和关停

- **文件**: `src/custos/core/engine_safety.py:42` · `src/custos/offline/daemon.py:171`
- **影响**: `get_engine_status` 无超时。引擎卡住时 `evaluate_once` 永久挂起 —— tick 不再评估，
  且 `_run_together` 的 `finally` 里那次 `asyncio.wait(tasks)` 也永远等不回来。守卫对
  "引擎不答话"是 fail closed 的（异常路径），但对"引擎不返回"没有防线
- **注**: `ZombieWatchdog` 是这件事的指定负责人，两条通道都没接线，已在遗留项 3。本条是记录
  它在本 plan 引入的新表面

---

## 🔵 LOW

### L1: 非有限上限被 `is_finite` 拒了，但没测试

- **文件**: `src/custos/offline/safety.py:83`
- **影响**: `"NaN"` / `"Infinity"` 能过 `Decimal()` 构造，靠 `is_finite()` 拦下。参数化列表里
  没有它们，这条防线无覆盖

---

## 正向偏离（改进）

| # | 位置 | 描述 | 理由 |
|---|---|---|---|
| 1 | `safety.py:57` | 认不出的 `risk_config` 键拒绝而非忽略 | 拼错的键在操作者那边读起来和"限额已抬高"一样 |
| 2 | `safety.py` `_Watched` | 每个部署各持一个 breaker | 共用会让一个部署的权益高水位混进另一个的回撤 |
| 3 | `reconciler.py:206` | 限额在任何引擎动作前读，读不出即终局拒绝 | 自省抓出的真 bug：原实现先部署再抛错，引擎留在跑而 lane 已死 |
| 4 | `test_plan_closeout_counts.py` | 探针作用域改为"最新认领者负责" | 否则要么改写 plan 21 的历史数字，要么削弱门 |

## 逐 Task 匹配率

| Task | 匹配率 | 关键偏离 |
|---|---|---|
| Task 1 限额来源 | 95% | 异常类型措辞（已登记）；L1 |
| Task 2 tick 解耦 | 85% | **C1 未登记**；M4 |
| Task 3 锁死 | 95% | 接 guard 而非 breaker（已登记）；M1 |
| Task 4 close-out | 85% | **H1 声明未降级**；M2/M3 |

## 优先修复建议

1. **C1** — 补偏离日志 + 改 Task 2 正文（纯记录，不动代码）
2. **H1** — close-out 红线 0.3 行补重启口子 + 进遗留项
3. **M1** — 补那条变异敏感的接线测试
4. **M2/M3** — File Inventory 同步
5. **M4/L1** — 记入遗留项，不在本 plan 修

## 审计方法备注

- H1 的证据来自实跑脚本（scratchpad `probe_restart.py`），不是读代码推断的；C1/M1/M2 的
  证据是 grep 零命中
- 未跑 `--deep`（无并发/性能/依赖链扩展检查），未跑 `--peer`（无外部第二意见）
- 既有 3 处 `fmt-check` 红属 lesson C6（receipt 按字节 pin 住 `runner_fact.py` 等），
  与本 plan 无关，不计入本次发现
