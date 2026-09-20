# 审计报告: 04 - recovery-and-post-execution-consistency-fixes

> **审计日期**: 2026-09-20
> **计划文件**: `.forge/fixes/2026-09/04-recovery-and-post-execution-consistency-fixes.md`
> **审计员**: Codex，同一执行面内部审计
> **实施基线**: Custos `706c621..45c44a4` 加自省 commits；Crucible `13babad`

## Executive Summary

Fix 04 的五项主缺陷均有真实实现与回归，plan-first 顺序正确，双仓验证通过。整体匹配度较高，但 terminal supervision 仍有一个 generation 竞态和一个 silent capability fallback：旧 watcher 在 stop 前没有重新验证 durable desired authority；传入不含 `supervise_once` 的 lifecycle 时会静默不监督。这两项必须在 chain-fix 中关闭。另有两个计划明确要求但未覆盖的时序测试。

## 整体匹配率: 89%

## 严重度分布

| 严重度 | 数量 |
|---|---:|
| 🔴 CRITICAL | 1 |
| 🟠 HIGH | 1 |
| 🟡 MEDIUM | 2 |
| 🔵 LOW | 0 |

## 问题列表

### 🔴 CRITICAL

#### C1: 旧 generation watcher 在 stop 前未重新验证 durable desired authority

- **文件**: `src/custos/core/engine_lifecycle.py:233-252`
- **计划定义**: Task 4 要求“每次动作前重新确认 desired generation/fingerprint 仍为当前 running”。
- **实际代码**: `supervise_once` 在 `wait_terminal` 返回后立即调用 `engine.stop()`；durable generation/fingerprint 只在后续 `record_engine_restart` 才被验证。
- **影响**: 新 generation 已写 desired、旧 watcher 尚未收到 cancellation 的窄窗口中，旧 watcher 可能先停止新节点。该计划偏离未记录，按 plan-driven 审计规则为 CRITICAL。
- **修复方向**: 在任何 stop/restart/quarantine 动作前原子读取 current lifecycle state；authority 不再匹配时让 watcher 正常退休，不能触碰引擎。

### 🟠 HIGH

#### H1: 关键 terminal supervision capability 缺失时静默降级

- **文件**: `src/custos/core/runner_command_runtime.py:303-310`
- **计划定义**: running command 必须进入 terminal supervision，监督失败由 daemon fail closed。
- **实际代码**: `_start_engine_supervision` 用 `getattr(..., "supervise_once", None)`；不可调用时直接 `return`。
- **影响**: 错误组合或测试替身可以重新得到“applied 但无人监督”的原缺陷形态，且没有日志或失败。
- **修复方向**: 直接依赖 typed lifecycle method；缺失即 fail loud。测试替身应实现明确的 terminal 行为，不能靠生产代码兼容缺口。

### 🟡 MEDIUM

#### M1: heartbeat 永不返回的有界清理没有行为测试

- **文件**: `src/custos/core/runner_command_runtime.py:395-429`
- **计划定义**: heartbeat 挂起时取消并有界等待，不能阻塞 command completion。
- **实际代码**: 已实现 timeout/done-callback，但测试只覆盖 `in_progress()` 抛异常。
- **影响**: 后续重构可能重新引入 command completion 卡死，现有套件不会变红。
- **修复方向**: 增加忽略第一次 cancellation、稍后完成的 heartbeat 替身，断言主操作按 interval 返回且异常被消费。

#### M2: generation 切换只覆盖短停机，没有长于采集间隔的 gap

- **文件**: `tests/test_runner_fact_production_loop.py:220`
- **计划定义**: Task 5 明确要求停机短于/长于采集间隔都做到每笔一次。
- **实际代码**: 当前测试只覆盖 30 秒切换、61 秒关闭。
- **影响**: period-gap 分支可能重置 coverage 后漏旧代或重复，但没有回归约束。
- **修复方向**: 增加跨过一个完整 period 的 generation 切换，断言 gap 不伪造 close，恢复窗口从新边界开始且旧成交不会被重复归入。

## 正向偏离（改进）

| # | 位置 | 描述 | 理由 |
|---|---|---|---|
| 1 | `runner_fact.py` | 增加 durable risk latch | 比仅依赖进程内 breaker 更能守住重启 |
| 2 | `engine_lifecycle.py` / `runner_fact.py` | 自省补充 degraded → recovered-ready 状态 | 修复了 replacement handle 无法写回 |
| 3 | `runner_fact.py` | applied 覆盖保护收窄到 intake terminal | 保留合法 post-apply lifecycle quarantine |

## 逐 Task 匹配率

| Task | 匹配率 | 关键偏离 |
|---|---:|---|
| 1 | 100% | 无 |
| 2 | 100% | 无 |
| 3 | 92% | 缺挂起 heartbeat 测试 |
| 4 | 72% | 缺动作前 authority recheck；capability 可静默跳过 |
| 5 | 88% | 缺长 gap 覆盖 |

## 优先修复建议

1. 先修 C1，保证旧 watcher 永远不能停止新 generation。
2. 修 H1，删除 silent supervision fallback。
3. 补 M1、M2 两条时序回归后重跑双仓门禁。
