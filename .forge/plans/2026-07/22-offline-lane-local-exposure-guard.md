# 22 — 离线通道的本地敞口守卫

> **Status**: ✅ Completed
> **Created**: 2026-07-29
> **Completed**: 2026-07-29
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
| `src/custos/offline/reconciler.py` | Modify | 接受可选 guard；引擎动作前先读限额，读不出即终局拒绝；跳闸后拒新 generation。活跃 instance 由 guard 持有，不在此暴露 |
| `src/custos/offline/daemon.py` | Modify | 组合 `OfflineExposureGuard` + 与订阅并行的 tick |
| `authority-manifest.json` | Modify | `safety.py` 归入 `offline_lane.guarded_modules` |
| `tests/test_offline_lane_safety.py` | Create | 限额来源与 tick 契约 |
| `tests/test_offline_reconciler.py` | Modify | 锁死语义 + 限额接线 |
| `tests/test_offline_lane_daemon.py` | Modify | tick 与传输解耦 |
| `tests/test_plan_closeout_counts.py` | Modify | 计数探针作用域（计划外，见偏离日志） |
| `.forge/plans/2026-07/21-sandbox-offline-deployment-path.md` | Modify | 红线表 0.3 行更新兑现范围 |
| `.forge/README.md` | Modify | plan 索引状态（Task 4 动作清单） |

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

**实现**: `run_offline_lane` 把订阅与 tick 交给 `_run_together` 并行跑，任一侧结束或抛错
都置位 `stop` 让另一侧收尾，随后重抛第一个失败；tick 只调
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

- [x] `make lint` 绿
- [x] `make check-authority` 绿（`safety.py` 已归类，`verify_offline_lane` 通过）
- [x] `make test` 无新增失败
- [x] 所有引用契约有 `file:line` 证据锚（见上下文表）
- [x] tick 在传输故障下仍运行 —— 有测试，非推断
- [x] 锁死后引擎零调用 —— 有测试
- [x] close-out 计数表经探针核对
- [x] 无死代码：`safety.py` 的每个导出都有消费者

## 进度追踪 (Progress)

| Task | Status | Completed | Notes |
|---|---|---|---|
| Task 1 | ✅ | 2026-07-29 | `c1bcba7` — 限额来源；`safety.py` 归入 manifest 前先看着门咬了一次 |
| Task 2 | ✅ | 2026-07-29 | `653f696` — tick 解耦；`_run_together` 让任一侧的失败终结另一侧 |
| Task 3 | ✅ | 2026-07-29 | `0653a26` — 锁死；另 `b0ad125` 修计数探针作用域 |
| Task 4 | ✅ | 2026-07-29 | close-out；自省轮抓出 `b87d763` |

## 偏离与改进日志 (Deviations & Improvements)

| 类型 | 位置 | 描述 | 已批准 |
|---|---|---|---|
| 决策 | 全局 | 跳闸后锁死，拒绝后续一切 generation | CEO 2026-07-29 |
| 决策 | 全局 | 限额默认 strictest，spec `risk_config` 可抬高 | CEO 2026-07-29 |
| 更正 | 上下文 | Plan 21 close-out 高估了签名通道的守卫覆盖；实测三个守卫在签名通道亦未接线 | 实测（本 plan Foundation Scan） |
| 偏离 | Task 1 | plan 写"live 抛 `RunnerSafetyPolicyUnavailableError`（经 mode_guard 先拒）"，两者不可兼得：guard 先拒即抛 `OfflineModeRefused`。改为两层各测一次，内层用 relaxed-double 证明它不是被外层遮住的死分支 | 低风险，实施记录 |
| 偏离 | Task 3 | plan 写"reconciler 接可选 breaker"，实际接的是持有 breaker 的 guard —— 限额来自 spec，而 reconciler 是唯一看得见 spec 的地方 | 低风险，实施记录 |
| 改进 | Task 1 | `risk_config` 里认不出的键**拒绝**而非忽略。plan 只要求拒绝非法值，但拼错的键在操作者那边读起来和"限额已抬高"一模一样 | 低风险，实施记录 |
| 改进 | Task 2 | 每个被守护的部署各持一个 breaker，而非共用一个 runner 级 breaker —— 共用会让一个部署的权益高水位混进另一个的回撤 | 低风险，实施记录 |
| 改进 | 计划外 | Plan 21 的计数探针在本 plan 长出测试时变红。改的是探针作用域：close-out 记录的是一个时刻，只有**最新**认领某文件的 plan 对今天的数字负责 —— 否则要么改写 plan 21 的历史，要么削弱门。见 `b0ad125` | 低风险，实施记录 |
| 修复 | 自省 Round 1 | `risk_config` 读不出时，原实现先部署再抛错，把引擎留在跑而 lane 已死。改为在任何引擎动作之前读限额，读不出即终局拒绝。见 `b87d763` | 低风险，实施记录 |
| 修复 | 审查 fix 轮 | guard 调引擎无超时，卡死的引擎会同时拖住 tick 与 `_run_together` 的关停。改为每个 tick 加期限，超时即 fail closed 并记 `offline_exposure_containment_unconfirmed` —— 不做第二次 flatten 尝试，挂住的引擎不会在第二次调用时回答。见 `5cccff5` | 审查 M4，本轮改判为修 |
| 偏离 | Task 2 | 计划正文原写「用 `asyncio.gather` 并行跑订阅与 tick」，实际是 `_run_together`（`asyncio.wait(FIRST_EXCEPTION)` + 置位 `stop` + 二次 drain + 重抛）。`gather` 会把首个异常抛给调用者却**不让另一个 loop 停下** —— 那正是"guard 死了 lane 还在交易"的形态。审查 C1 抓出，正文已改 | 低风险，审查后补记 |

## 已知的下游动作（不在本 plan 范围）

PS 侧若要在离线通道跑实际策略，需自行在 `deploy/custos/conf/<strategy>/spec-override.yaml`
补 `risk_config`，否则限额停在 strictest（总额 $200），supertrend 一开仓即跳闸。这是对方
仓库的独立动作，本 plan 不改 PS。

键名是 `max_total_notional` 与 `max_drawdown_pct`，值写十进制字符串或整数（`float` 与
拼错的键都会被拒，spec 不会被静默降回默认）。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-07-29（审查后 fix 轮同日完成，见下）
- **总 Task 数**: 4
- **偏离数**: 9（2 决策 + 1 更正 + 3 偏离 + 2 改进 + 1 自省修复，详见偏离日志）
- **验证结果**: 全部通过 —— `make lint` 绿、`make check-authority` 绿、`make test` 全绿
- **实施 commit 范围**: `3aaa318`（本计划）→ `c1bcba7` `653f696` `b0ad125` `0653a26` `b87d763`
- **审查与修复**: `.forge/reviews/2026-07/22-offline-lane-guard-review.md`（1C/1H/4M/1L）→
  `.forge/fixes/2026-07/22-offline-lane-guard-fixes.md`。代码只改了一处（Fix 5 给每个 tick
  加期限），其余是声明与记录的修正
- **契约影响**: `authority-manifest.json` `offline_lane.guarded_modules` 增 `safety.py`；
  离线 spec 的 `risk_config` 从自由字典变为有约束的两键契约

### 红线 gate 满足度

| 红线 | code 层覆盖 | runtime 接线 | 兑现范围 |
|---|---|---|---|
| 0.1 Key/KEK 不出进程 | 本 plan 未新增任何凭证路径 | 不变 | 不受影响 |
| 0.2 G6 host gate | 不变 | 不变 | 不受影响 —— 本 plan 不碰执行门 |
| 0.3 失联 ≠ 停止 | `test_the_exposure_tick_outlives_a_transport_that_has_failed` 断言传输持续抛错期间引擎仍被问了 ≥3 次 | `run_offline_lane` 用 `_run_together` 并行跑订阅与 tick，tick 只调引擎 | **离线通道在本进程内部署过的 generation 上已兑现**（周期敞口评估 + 越限 flatten + 锁死）。**不覆盖重启**（见下）。**签名通道仍未接** `EngineSafetySupervisor`，逐单 `RunnerNotionalCap` 与 `ZombieWatchdog` 两条通道皆零接线 |
| 0.4 Decimal money math | `test_a_float_ceiling_is_refused_because_money_is_not_binary_fractions` + 整数/字符串各一条 + `NaN`/`Infinity` 四条 | 限额一律 `Decimal(str(...))` 构造 | 已兑现 |

0.3 那格的措辞是刻意分层的：本 plan 兑现的是**离线通道的周期敞口守卫**，不是红线全部。
签名通道的同名缺口在 Plan 19 范围内，逐单 cap 全生态无 hook（Plan 21 已记为 defer）。

**重启不在覆盖范围内。** 审查（H1）实跑证实：持久化状态里 generation 已是 1 的新进程，收到
重投的 generation 1 会走 `reconciler.py` 的 `==` 短路分支 —— 报 healthy、不部署、也不 watch。
把敞口设成 `$9999`、上限 `$200`，守卫一次都没被问过。"不重复部署已应用的 generation" 是
Plan 21 的既有设计（`test_forgets_nothing_across_a_restart` 明文固定），本 plan 没引入它；
但本 plan 让它有了新后果 —— 交易所仍持仓，而这条 lane 报 healthy、不守卫。见遗留项 5。

### 失败模式覆盖

下表由 `tests/test_plan_closeout_counts.py` 逐行核对 —— 数字来自 pytest 实际 collect，
不是手写。

| 测试文件 | 条数 |
|---|---|
| `tests/test_offline_lane_safety.py` | 38 |
| `tests/test_offline_reconciler.py` | 32 |
| `tests/test_offline_lane_daemon.py` | 16 |
| `tests/test_plan_closeout_counts.py` | 6 |

上表合计 92 条（含审查后 fix 轮新增的 9 条：4 条卡死引擎、4 条非有限上限、1 条限额接线）。
第一个文件由本 plan 新建，38 条全部是新的。中间两个在 plan 21 close-out 时经同一探针核对为
22 与 14，故本 plan 在其中分别加了 10 条与 2 条。末行那个文件的条数会随"有计数表的 plan 数"
变化 —— 本 plan 写下这张表，它自己就从 4 涨到 6；这两条是参数化实例，不是新写的测试函数。

覆盖的失败模式：传输持续抛错、快照读不出（引擎抛异常）、**引擎收下问题却永不回答**、
快照不可信（`reliable=False`）、flatten 自身失败、`risk_config` 读不出 / 非正 / 是 `float` /
是 `NaN` 或 `Infinity` / 键拼错、跳闸后重投同一 generation、跳闸后新 generation、重启后未锁死。

含一条 relaxed-double：`test_the_shared_fallback_refuses_live_on_its_own_account` —— 离线
通道的 guard 永远先拒 live，所以内层 `strictest_local_fallback("live")` 在正常路径上够不到；
直接调它证明那不是死分支。

### 遗留项

1. **签名通道仍无周期敞口守卫** —— `EngineSafetySupervisor` 在 `_daemon.py` 未接线，
   `_build_runner_safety_boundary_factory` 硬性要求 `owner_policy`。属 Plan 19 范围。
2. **逐单 `RunnerNotionalCap` 全生态无 hook** —— 两条通道都没有下单前的拦截点，需要引擎
   侧先给出这个 seam。
3. **`ZombieWatchdog` 零接线** —— 同样两条通道皆未接。
4. **未在真 NT 引擎上跑过 tick** —— `get_engine_status` / `flatten_positions` 两个 host 都
   实现了（`engines/nautilus/host.py:170,186,690,751`，已 grep 实证），但本 plan 的验证止于
   fake engine；真跑要 Docker + NATS，与 Plan 21 遗留项 1 同一道门。
5. **重启后既不部署也不守卫，却报 healthy** —— 见上文红线表 0.3 的说明。修法不是"重启就
   watch"：那个 instance 在新进程里并不存在，`get_engine_status` 会抛错 → fail closed →
   对着空气 flatten 并锁死，比不 watch 更糟。真正要决定的是**重启后该不该重新部署**
   （即 Plan 21 那条 no-redeploy 设计是否仍然正确），那是一个独立的设计问题，不是本轮
   fix 能顺手带过的。
