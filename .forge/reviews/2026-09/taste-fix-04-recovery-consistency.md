# 品味审查报告 — Fix 04 recovery consistency

- 扫描范围: Fix 04 计划列出的 Custos Python 与 Crucible Rust 代码文件
- 宪法版本: `.claude/rules/coding-taste.md`
- 检测: grep 机械层 + Codex 语义层，standard

## 🔴 高危害 (0)

无。

## 🟡 中 (1)

| 宪法 | 位置 | 现象 | 建议 |
|---|---|---|---|
| 二 类型 / 五 错误处理 | `src/custos/core/runner_command_runtime.py:307` | 对构造器已声明的 `EngineLifecycleSupervisor` 用 `getattr(..., None)`，缺少 terminal capability 时静默跳过；这是原生产缺陷的兼容分支 | 直接调用 typed method；测试替身实现明确 capability，缺失时 fail loud |

## 🟢 低 (0)

无。编号注释、装饰分隔条、print/println 与中文日志描述均为零命中。

## ⬆️ 上界·核心条款 (4) — 数据结构上界待审，非机械修复

| 宪法 | 位置 | 结构 / 职责 / 依赖方向问题 | handoff |
|---|---|---|---|
| 三 / 核心·文件 | `src/custos/core/runner_fact.py`（6395 行） | durable command、policy、reservation、outbox 与发布职责集中；Fix 04 继续增加 latch/terminal 逻辑 | 数据结构先行审查状态聚合边界；禁止按行数搬方法 |
| 三 / 核心·文件 | `src/custos/cli/_daemon.py`（1041 行） | composition root 累积多类 watcher 与 runtime 装配 | 先画资源所有权/关闭顺序，再按组合职责评估深模块 |
| 三 / 核心·文件 | `src/custos/core/runner_fact_producer.py`（1001 行） | event bridge、observability 与 reconciliation cadence 同文件 | 先明确 producer 状态模型；不得只为降行数拆文件 |
| 三 / 核心·文件 | Crucible `runner_fact_reconciliation_projector.rs`（3468 行） | 投影装配、范围选择与多账本比较集中 | 以 reconciliation input model 为上界审查入口；不在 Fix 04 内机械分拆 |

## getattr 候选复核

- 共 44 处候选。除 `runner_command_runtime.py:307` 外，均位于 argparse、第三方 Nautilus event、NATS ACK 或可选 runtime capability 的边界归一化路径，保留为边界兼容，不判 finding。
- `runner_command_runtime.py:486-487` 只读取异常的可选 code/value，用于脱敏 reason code，不是强类型业务对象的防御访问。

## 🔬 近似探针命中复核 (30)

| 近似类型 | 位置 | 复核判定 | 理由 |
|---|---|---|---|
| god 函数 | `runner_fact_producer.py:319` | 排除 | 单一 order-fill fact translation，行距含三个 fact 构造 |
| god 函数 | `runner_fact_producer.py:418` | 排除 | 单一 order lifecycle/signal translation |
| god 函数 | `runner_fact_producer.py:805` | finding → ⬆️ | reconciliation capture orchestration，归文件上界 |
| god 函数 | `runner_command_runtime.py:116` | 排除 | command process 状态机主流程，卫语句清晰 |
| god 函数 | `engine_lifecycle.py:288` | 排除 | bounded start/retry 原子流程，不宜切碎 |
| god 函数 | `signal_execution.py:85` | finding → ⬆️ | coordinator 文件的既有 entry path 积累，非本轮新增 |
| god 函数 | `order_reservation_boundary.py:215` | 排除 | typed event dispatch；按事件种类线性分支 |
| god 函数 | `runner_fact.py:862` | finding → ⬆️ | 归 runner_fact 文件上界 |
| god 函数 | `runner_fact.py:1011` | finding → ⬆️ | 归 runner_fact 文件上界 |
| god 函数 | `runner_fact.py:1411` | finding → ⬆️ | schema 初始化集中，归 runner_fact 文件上界 |
| god 函数 | `runner_fact.py:1856` | finding → ⬆️ | 归 runner_fact 文件上界 |
| god 函数 | `runner_fact.py:2013` | finding → ⬆️ | 归 runner_fact 文件上界 |
| god 函数 | `runner_fact.py:2165` | finding → ⬆️ | 归 runner_fact 文件上界 |
| god 函数 | `runner_fact.py:2344` | finding → ⬆️ | 归 runner_fact 文件上界 |
| god 函数 | `runner_fact.py:2499` | finding → ⬆️ | 归 runner_fact 文件上界 |
| god 函数 | `runner_fact.py:2728` | finding → ⬆️ | 归 runner_fact 文件上界 |
| god 函数 | `runner_fact.py:2974` | finding → ⬆️ | 归 runner_fact 文件上界 |
| god 函数 | `runner_fact.py:3112` | finding → ⬆️ | command outcome transaction，归 runner_fact 文件上界 |
| god 函数 | `runner_fact.py:3538` | finding → ⬆️ | 归 runner_fact 文件上界 |
| god 函数 | `runner_fact.py:3694` | finding → ⬆️ | 归 runner_fact 文件上界 |
| god 函数 | `runner_fact.py:4235` | finding → ⬆️ | fill/lot transaction，归 runner_fact 文件上界 |
| god 函数 | `runner_fact.py:4434` | finding → ⬆️ | 归 runner_fact 文件上界 |
| god 函数 | `runner_fact.py:4582` | finding → ⬆️ | 归 runner_fact 文件上界 |
| god 函数 | `runner_fact.py:4690` | finding → ⬆️ | rebuild transaction，归 runner_fact 文件上界 |
| god 函数 | `runner_fact.py:5864` | finding → ⬆️ | 归 runner_fact 文件上界 |
| god 函数 | `runner_fact.py:6092` | finding → ⬆️ | 归 runner_fact 文件上界 |
| god 函数 | `runner_fact.py:6284` | finding → ⬆️ | 归 runner_fact 文件上界 |
| 成组裸参数 | `runner_safety.py:285` | 排除 | `_ModifyIntent` 是明确的命令适配 DTO |
| 成组裸参数 | `runner_safety.py:395` | 排除 | Nautilus hook 的 variadic framework signature |
| 成组裸参数 | `runner_safety.py:459` | 排除 | refusal reporting 的窄内部 API |

## 统计与优先级

- 各级: 🔴0 🟡1 🟢0 ⬆️4。
- 近似探针: god 函数 27 = 22 个上界归并 + 5 个排除；成组裸参数 3 = 0 finding + 3 排除。
- 维度范围: all；无跳过维度。
- 建议先处理: 删除 terminal supervision silent fallback；四个上界项进入独立 architecture plan，不混入恢复 bug 修复。
