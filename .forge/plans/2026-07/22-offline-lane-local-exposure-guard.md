# 22 — 离线通道的本地敞口守卫

> **Status**: 🔲 Not started
> **Created**: 2026-07-29
> **Project**: custos (`tesseract-trading/custos/`)
> **Depends on**: Plan 21 ✅ Completed（离线通道落地）
> **Blocks**: 离线通道在 testnet 上的长时间无人值守运行
> **For Claude**: `/forge:execute`；单 session（4 Task，无跨仓库改动）
> **multi_session_scope**: false

## 上下文 (Context)

Plan 21 的红线满足度表把 0.3 记为 **partial**：断线不停机做到了（`src/custos/offline/reconciler.py`
的 `_report` 在发布失败时只记日志、不动引擎），但断线期间没有任何敞口上限。本 plan 补齐后半句。

### 契约证据（Step 1.5 gate）

| 被引用契约 | 锚点 | 实测结论 |
|---|---|---|
| `EngineSafetySupervisor` | `src/custos/core/engine_safety.py:26,37` | 已存在且有测试（`tests/test_portfolio_snapshot.py:243,266`）；快照异常或不可信即 `fail_closed` + `flatten_positions`；只依赖 `get_engine_status` 与 `flatten_positions` |
| `FallbackBreaker` | `src/custos/core/fallback_breaker.py:80,99,110,124` | 跳闸后 `_frozen` 自锁；`allows_new_orders()` 返回 False —— 现在没有任何代码读它 |
| 无签名策略时的限额出口 | `src/custos/core/runner_safety_policy.py:47,77-83` | `resolve_runner_safety_limits(resolver=None, …)` → `RunnerSafetyLimits.strictest_local_fallback(mode)`；对 live 抛 `RunnerSafetyPolicyUnavailableError` |
| strictest 实测值 | `src/custos/core/local_cap.py:21-22`、`src/custos/core/fallback_breaker.py:29` | 单笔 $50 / 总额 $200 / 回撤 10% |
| 签名通道的执行边界 | `src/custos/cli/_daemon.py:401` | 硬性要求 `owner_policy`，离线通道永远拿不到 → 该路径不可复用 |
| Tier-2 方法 | `src/custos/core/engine_protocol.py:340,342,349` | `check_engine_connected` / `flatten_positions` / `get_engine_status` 均在协议内，两个 host 都实现 |
| 离线通道已应用状态 | `src/custos/offline/reconciler.py` `_applied` | 以 `spec_id` 为键；instance id 可由同一 `uuid5` 推导，无需新增状态 |

### 两处对 Plan 21 说法的更正

Plan 21 的 close-out 写"离线通道未组合 `FallbackBreaker` / `RunnerNotionalCap` /
`ZombieWatchdog`"，暗示签名通道组合了。实测：

- `EngineSafetySupervisor` **两条通道都未接线**；签名通道只在
  `src/custos/core/order_reservation_boundary.py:231` 用 `FallbackBreaker` 做逐单拒绝，
  没有周期性敞口评估。
- `RunnerNotionalCap` 与 `ZombieWatchdog` **全仓零接线**（各自单测除外）。

所以原措辞高估了签名通道的覆盖面。本 plan 只负责离线通道这一侧。

## 目标 (Goal)

离线通道在与云端连接无关的节奏上持续评估本地敞口；越限即 flatten 并锁死；锁死后拒绝一切
新 generation，观测状态持续报 unhealthy。

## 架构 (Architecture)

复用 `EngineSafetySupervisor`，不新写评估逻辑。限额默认取 `strictest_local_fallback`，
spec 的 `risk_config` 可抬高。tick 循环是与订阅并行的独立 task，只调用引擎、不碰 NATS，
因此传输断开时照常运行 —— 这正是红线 0.3 要的东西。跳闸后 reconciler 读
`allows_new_orders()` 拒绝新 generation。

## 关键设计决策 (Key Design Decisions)

| 问题 | 决策 | 理由 |
|---|---|---|
| 跳闸后怎么办 | 锁死：flatten + 拒绝后续一切 generation，需重启 runner 才清 | breaker 本就自锁；只 flatten 会被下一个 generation 立刻把敞口开回去，守卫形同虚设 |
| 限额从哪来 | 默认 strictest，spec `risk_config` 可抬高 | 通道已接受未签名 desired state 且 live 已在边界拒绝；红线要的是"断线时仍有守卫在跑"，不是某个特定数字。照搬 $200 会让 10k 余额的 sandbox 策略一开仓就跳闸，通道用于验证策略逻辑的目的当场失效 |
| 三个守卫都接吗 | 只接 breaker | `RunnerNotionalCap` 的逐单拦截在全生态没有 hook（已知 defer）；`ZombieWatchdog` 两条通道都没接，属签名通道范围，Plan 19 所有。把它们塞进本 plan 是越界 |
| 限额在哪一层解析 | 新建 `src/custos/offline/safety.py`，经 `mode_guard` 收口 | manifest 要求通道内每个模块归类；本模块按 mode 取限额，天然属 `guarded_modules` |

## 文件清单 (File Inventory)

| 文件路径 | 操作 | 描述 |
|---|---|---|
| `src/custos/offline/safety.py` | Create | 限额解析（live 由 guard 收口）+ 构造 breaker |
| `src/custos/offline/reconciler.py` | Modify | 接受可选 breaker；frozen 时拒新 generation；暴露活跃 instance id |
| `src/custos/offline/daemon.py` | Modify | 组合 `EngineSafetySupervisor` + 起 tick task |
| `authority-manifest.json` | Modify | `safety.py` 归入 `offline_lane.guarded_modules` |
| `tests/test_offline_lane_safety.py` | Create | 限额来源与 tick 契约 |
| `tests/test_offline_reconciler.py` | Modify | 锁死语义 |
| `tests/test_offline_lane_daemon.py` | Modify | tick 与传输解耦 |
| `.forge/plans/2026-07/21-sandbox-offline-deployment-path.md` | Modify | 红线表 0.3 行更新兑现范围 |

## 实现任务 (Tasks)

### Task 1: 离线通道的限额来源

**RED**: live 抛 `RunnerSafetyPolicyUnavailableError`（经 `mode_guard` 先拒）；spec 无
`risk_config` 时得到 strictest（总额 `Decimal("200")`、回撤 `Decimal("10")`）；spec 写了
`risk_config` 时该值被采纳；非法 `risk_config`（负数 / 不可解析为 `Decimal`）被拒绝而非
静默回落到 strictest —— 静默回落会让操作者以为限额生效了。

**实现**: `src/custos/offline/safety.py`，入口函数接 `OfflineDeploymentSpec` 返回
`FallbackBreakerConfig`。money 值一律 `Decimal(str(...))`（红线 0.4）。

**Verify**: `uv run pytest tests/test_offline_lane_safety.py -v`

**Commit**: `feat(custos): resolve the offline lane's own exposure limits`

### Task 2: 与传输解耦的 tick

**RED**: NATS 连接持续抛错期间 tick 仍在跑（红线 0.3 的核心断言）；快照不可信即 fail
closed 并 flatten；已跳闸后不重复 flatten；`stop` 置位后 tick 结束且不吞异常。

**实现**: `run_offline_lane` 用 `asyncio.gather` 并行跑订阅与 tick；tick 只调
`EngineSafetySupervisor.evaluate_once`，对每个活跃 instance 依次评估。间隔取常量，
可由参数覆盖以便测试。

**Verify**: `uv run pytest tests/test_offline_lane_daemon.py tests/test_offline_lane_safety.py -v`

**Commit**: `feat(custos): guard offline exposure on a tick the transport cannot stall`

### Task 3: 跳闸即锁死

**RED**: 跳闸后新 generation 被拒（`Settlement.REJECTED`）、引擎零调用、观测状态报
unhealthy；未跳闸时行为与今天一致；新进程（重启）恢复可应用。

**实现**: `OfflineReconciler` 接可选 breaker，`apply` 在任何引擎动作之前读
`allows_new_orders()`。`REJECTED` 而非 `RETRYABLE`：重投不会解冻。

**Verify**: `uv run pytest tests/test_offline_reconciler.py -v`

**Commit**: `feat(custos): refuse new generations once the offline breaker has tripped`

### Task 4: 文档收尾 (close-out)

1. 本 plan 顶部 `Status: ⏳ → ✅ Completed` + 完成日期
2. `.forge/README.md` 索引状态更新
3. Plan 21 红线表 0.3 行更新为本 plan 兑现的范围，并保留仍未兑现的部分（逐单 cap、watchdog）
4. 完成报告章节，含**逐文件测试计数表**（由 `tests/test_plan_closeout_counts.py` 核对，
   数字取自实跑，见 `progress-management.md` §"数字类声明必须来自实跑"）
5. `git add` 上述文件 + `git commit -m "docs(custos): mark plan 22 as completed"`

## 验证清单 (Verification)

- [ ] `make lint` 绿
- [ ] `make check-authority` 绿（`safety.py` 已归类，`verify_offline_lane` 通过）
- [ ] `make test` 无新增失败
- [ ] 所有引用契约有 `file:line` 证据锚（见上下文表）
- [ ] tick 在传输故障下仍运行 —— 有测试，非推断
- [ ] 锁死后引擎零调用 —— 有测试
- [ ] close-out 计数表经探针核对
- [ ] 无死代码：`safety.py` 的每个导出都有消费者

## 进度追踪 (Progress)

| Task | Status | Completed | Notes |
|---|---|---|---|
| Task 1 | 🔲 | | 限额来源 |
| Task 2 | 🔲 | | tick 解耦 |
| Task 3 | 🔲 | | 锁死 |
| Task 4 | 🔲 | | close-out |

## 偏离与改进日志 (Deviations & Improvements)

| 类型 | 位置 | 描述 | 已批准 |
|---|---|---|---|
| 决策 | 全局 | 跳闸后锁死，拒绝后续一切 generation | CEO 2026-07-29 |
| 决策 | 全局 | 限额默认 strictest，spec `risk_config` 可抬高 | CEO 2026-07-29 |
| 更正 | 上下文 | Plan 21 close-out 高估了签名通道的守卫覆盖；实测三个守卫在签名通道亦未接线 | 实测（本 plan Foundation Scan） |

## 已知的下游动作（不在本 plan 范围）

PS 侧若要在离线通道跑实际策略，需自行在 `deploy/custos/conf/<strategy>/spec-override.yaml`
补 `risk_config`，否则限额停在 strictest（总额 $200），supertrend 一开仓即跳闸。这是对方
仓库的独立动作，本 plan 不改 PS。
