# 13 - risk-gate-semantics

> **Status**: ✅ Completed
> **Completed**: 2026-09-20
> **Created**: 2026-09-20
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-risk-state-deep-review.md` RS-8、RS-4、RS-5

## 三项的共同形状

风控的每个开关都有一个**范围**，而这三处的范围都比它们的名字宽或窄了一格：

- RS-8：「暂停新风险」的开关把保护退出也关了——范围太宽。
- RS-4：回撤基准只在成交时采样，行情走出的新高进不去——范围太窄。
- RS-5：日界只在入场检查时推进，跨日的成交先记在旧账上再被整体清零——范围错位。

## 修复任务

### Fix 1: 软暂停不得关闭保护退出 [P1 / RS-8]

**Root Cause**: 实现错误。`pause()` 只有两个调用点，都是拒单后「停止新增风险」。但
`strategy_core.on_trade` / `on_quote` 在 `_paused` 时整个 return，而 tick 路径**只做退出**——
HYBRID 的追踪止盈、tick 止损全停了。修复器随后补出新的安全止损，暂停状态也不会改变，于是仓位
带着保护单却退不出来。策略级开关还会连带停掉其他币对的 tick 退出。

**复现**（审查方）：HYBRID 追踪已激活、峰值 120；一次安全止损拒绝触发暂停，修复器补出新止损；
价格回落到 115 本应追踪退出，真实 core callback 直接返回，退出处理完全没被调用。

**Files**: `strategy_core.py`、测试

1. 先写失败测试：软暂停期间 tick 退出仍须执行；`prepare_shutdown` 期间仍须停止。
2. `on_trade` / `on_quote` 改判 `_shutdown_position_policy`（进程要停）而非 `_paused`（软暂停）。
   `on_bar` 保持按 `_paused` 短路——那是新入场的路径，正是要停的东西。
3. 不做自动 resume：审查方点名「避免简单自动 resume 意外解除另一来源的暂停」。

**验收**（报告原文）：区分停止新增风险、继续保护退出和停止进程；保护路径不应被普通软暂停拦截。

### Fix 2: 回撤基准要在每次风险评估时采样 [P1 / RS-4]

**Root Cause**: 实现错误。`check_risk_limits` 读了最新权益却不更新峰值，峰值只在初始化与成交后
更新。持仓期间行情走出的新高不进基准，于是从那个高点回撤时门不响。

**复现**（审查方）：compound、最大回撤 5%，权益 1000 → 1100 → 1030。后两次检查都放行，峰值仍是
1000，而从 1100 实际回撤 6.36%。

**Files**: `coordinators/risk_control.py`、测试

1. 先写失败测试：1000 → 1100 → 1030 的三次检查，第三次必须拒绝。
2. `check_risk_limits` 在判定前用当前权益更新峰值。`update_peak_equity` 只在创新高时抬升，
   所以「先更新再判」不会把回撤抹平——新高的回撤本来就是 0。

**验收**（报告原文）：在稳定的行情/风险评估节拍更新可靠权益峰值，再计算回撤，且不能依赖后续成交
来采样。覆盖持仓上涨后回落、候选入场被过滤、无新成交等路径。

### Fix 3: 成交按自己的时间归日 [P1 / RS-5]

**Root Cause**: 实现错误。`record_trade` 不接受成交时间，也不推进日界；日切只在下一次
`check_limits` 发生。午夜后先来一笔止损成交、之后才有入场检查时，新一天的亏损先被记进旧账，
然后连同旧账一起清零——当天的亏损额度凭空恢复。

**复现**（审查方）：23:59 检查过；00:01 止损亏 60（权益 1000 → 940）；00:02 首次新入场检查把
session_pnl 从 -60 清成 0，5% 日亏损阈值没有拦住入场。

**Files**: `packages/custos-strategy-toolkit/src/custos_toolkit/risk/controller.py`、
`coordinators/trade_event_handler.py`、测试

1. 先写失败测试：午夜两侧的成交必须各归各日；跨日的退出成交不得被后来的检查清掉。
2. `record_trade` 接受成交时间，记账**前**先推进日界。
3. 调用点传入事件时间。时间缺失时保持现有行为（不推进），不要用当前时钟冒充成交时间。

**验收**（报告原文）：使用执行事件时间确定 PnL 所属交易日；在记账前推进日界，并处理迟到事件。
测试午夜两侧成交，以及连续多天没有入场检查但仍有退出成交。

## 验证清单

- [x] 三项失败测试先红后绿，经扰动验证（7 红 → 10 绿）
- [x] 审查方探针：RS-4 不再成立；RS-5 / RS-8 见下方「两个探针为何仍断言成立」
- [x] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 软暂停不关保护退出 | P1 | ✅ | 2026-09-20 | RS-8 |
| 2 回撤基准每次采样 | P1 | ✅ | 2026-09-20 | RS-4 |
| 3 成交按事件时间归日 | P1 | ✅ | 2026-09-20 | RS-5 |

## 偏离与改进日志

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-20
- **总 Task 数**: 3
- **偏离数**: 0
- **验证结果**: 全部通过
- **实施 commit 范围**: `d8675a8`（plan 自身 `53b053b`，早于实施）
- **契约影响**: `RiskController.record_trade` 新增可选参数 `current_ts`（默认 0，行为不变）。
  唯一调用点已更新。`make check-authority` 通过。
- **红线守护**: 四条红线全数守住。三项都在恢复风控的有效范围。

### 两个探针为何仍断言成立（重要，不是没修好）

审查方的 11 个探针里，RS-4 的已经翻转。RS-5 与 RS-8 的仍然断言成立，原因都**不是**缺陷还在，
而是它们调用的不是生产路径。两条都实测过，不是推断：

**RS-8**：探针用 `NautilusStrategyCore.on_trade(h, ...)`，其中 `h` 是审查脚本自己的 Harness。
修复后 `on_trade` 读 `self._shutdown_position_policy`，而那个 Harness 没有这个属性——
`AttributeError` 被回调自身的 `except Exception` 吞掉，`on_core_trade_tick` 因此没被调用，
探针的断言照旧成立。实测：

```
缺属性时: on_core_trade_tick 被调用 False，_log_error 记录
          "on_trade: AttributeError: 'Harness' object has no attribute '_shutdown_position_policy'"
补上属性: 软暂停下 on_core_trade_tick 被调用 True   <- 修复生效
          shutdown 下 on_core_trade_tick 被调用 False <- 仍然停止
```

真实的 core 在 `strategy_core.py:299` 的 `__init__` 里就设了这个属性，所以生产上不存在这条路径。
`test_the_core_always_has_the_shutdown_flag` 把这一点钉住了。

**RS-5**：探针直接调 `h._risk_controller.record_trade(D("-60"))`，不传时间——那是修复前的签名。
本 plan 有意保留「时间未知时不推进日界」（不用墙钟冒充成交时间，见 Fix 3 第 3 条），所以这条调用
的行为不变，探针照旧成立。生产路径是 `TradeEventHandler` → `record_trade(pnl, event.ts_event)`，
实测：

```
成交后 session_pnl: -60
次日首次检查 allowed: False | reason: Daily loss limit (5.0%) reached
```

`test_the_close_handler_books_a_fill_on_its_own_day` 走的正是这条真实路径。

> 这两条值得单独记下来：**探针红不等于缺陷在，探针绿也不等于缺陷不在**。判断依据只能是生产路径
> 上的实测。写「探针仍成立所以没修好」和写「探针不算数所以修好了」都是偷懒——要给出路径差异的
> 证据。

### 测试计数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/toolkit/test_risk_gate_semantics.py` | 10 |
| `tests/test_plan_closeout_counts.py` | 47 |

上表合计 57 条。第一行是本轮新建；第二行由本 plan 的表格从 45 推到 47。

### 扰动验证

三处修复同时回退（tick 回调改回判 `_paused`、去掉峰值采样、去掉日界推进）：10 项中 5 项转红。
还原后全绿。

### 功能验证（主路径）

1. HYBRID 模式持仓并让追踪止盈激活，制造一次安全止损被拒（触发软暂停）。预期：追踪退出仍然工作，
   价格回落到追踪线时仍会平仓。修复前 tick 回调整个停转，仓位退不出来。
2. compound 模式、最大回撤 5%，持仓期间让权益从 1000 涨到 1100 再回落到 1030（期间无成交）。
   预期：下一次入场检查被拒，日志 `Max drawdown`。修复前峰值停在 1000，6.36% 的回撤不被识别。
3. 让一笔止损在 UTC 午夜后几分钟成交，随后触发入场检查。预期：当日亏损计入当日，超过日亏损阈值
   时阻止入场。修复前这笔亏损被算进前一日并随日切清零。
