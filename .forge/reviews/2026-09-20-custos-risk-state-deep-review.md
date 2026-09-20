# Code Review: Custos 风控状态、合约数量与策略连续性

> **Depth**: deep
> **Date**: 2026-09-20
> **Reviewer**: Codex，单执行面
> **基线**: `45ff6f1`，下文源码行号按该提交标注；交付前检查至 `efd1efd` 及 Fix 06 对 `sltp.py` / `order_reconciler.py` 的未提交 ATR 修复，在包含它们的工作树上重跑全部 11 个探针，仍全部成立。并行计划、源码和测试不属于本次改动。
> **范围**: Toolkit 入场/保护/平仓事件、风控日切与重启、SuperTrend 指标、签名宿主熔断生命周期；交叉检查订单记账、场所配置和事实发布路径。

## Summary

新增 **9 项 P1，未确认 P0**。11 个离线探针复现这些问题，其中反向成交的事件顺序和 Tick 保护丢失通过真实 Nautilus BacktestEngine 验证。扩大后的现有测试 **1,419 passed**。

本轮没有修改业务源码，没有连接交易所，也没有推送或发布。仅新增本报告与同目录的 `2026-09-20-custos-risk-state-deep-repro.py`。复现脚本复用上一份审查脚本的 Harness，并明确区分受控缓存/回报与原生撮合引擎。

后续对指标状态、保护订单清理、原生数量换算、持久化风控及既有修复边界的补查，没有再确认新的独立 P0/P1。这只是本轮范围内的审查收敛；下列问题仍然存在，不能据此宣称项目已无严重 bug。修复后还需要复核实际改动与交互影响。

## Strengths

- 原生合约数量换算与 FixedRiskSizer 已能处理乘数；问题主要出在调用链另一端重新拼装反向名义金额。
- 签名边界在同一进程内替换实例时保留熔断器，避免了部分热更新重置问题。
- 保护修复可以在软暂停期间运行，正常独立止损拒单也不会直接撤销其他已接受保护单；但 Tick 回调的暂停范围过宽。
- 原生回测、纯协调器与库级算法均可离线运行，有条件把本轮组合用例纳入长期回归。

## Concerns

下文 `adapter/` 指 `packages/custos-strategy-toolkit-nautilus/src/custos_toolkit_nautilus/adapter/`。

### RS-1 [P1] 反向开仓漏乘合约乘数，可能放大目标仓位两个数量级

**位置**：`adapter/coordinators/signal_execution.py:132-136`；对照 `adapter/sizing.py:63-70`。

反向下单先用 `current_quantity * price` 把旧仓换算成报价币，再与目标金额相加；旧仓数量在永续合约中是合约单位，这里漏掉 multiplier。后面的 `quantity_from_notional()` 又正确除以 multiplier，结果旧仓平仓部分被重复放大。

**复现**：原生线性合约 multiplier=0.01、价格 100，旧空仓 10 张，目标新多仓名义金额 10。正确应买 20 张，实际构造并提交给下游 **1,010 张**，扣除旧空仓后对应 1,000 的新多仓名义金额。乘数为 1 的现货/部分永续不会暴露这个问题。

**影响边界**：独立 Runner 下单上限仍可能拒绝这笔错单；若订单在授权额度内，策略目标仓位就会错误放大。本探针验证订单构造与协调器派发，不声称已通过真实场所准入或成交。

**建议及验收**：统一原生数量与报价金额的转换；测试 multiplier=0.01、1、10 的双向反转，按最终净仓数量验收，不能只测单次入场。

### RS-2 [P1] 本地拒绝的止损被永久计入已保护数量，自愈不再补单

**位置**：`adapter/coordinators/sltp.py:314-316`（同类路径 `:204-206`、`:379-381`）；`adapter/coordinators/order_reconciler.py:320-336`。

保护订单在 submit_order 前写入 tracker，返回 False 没有回滚。RunnerSafetyOrderGate 的本地拒绝发生在原生缓存注册之前，也没有 OrderRejected 回调。修复器只移除缓存中已终结的订单，把缓存缺失的订单无限期当作在途保护。

**复现**：真实 RunnerSafetyOrderGate 配合实际 OKX ID 校验函数，拒绝带连字符的保护单 ID；没有任何订单进入提交端或缓存，但 tracker 显示覆盖数量 1。跨过三次修复冷却周期，仍然没有补单尝试，也未因保护缺失暂停。

**建议及验收**：注册保护必须与派发结果一致；本地拒绝立即撤销覆盖计数并进入可观察的未保护状态。未知在途订单要有超时和查询确认。覆盖 EXCHANGE/HYBRID/NATIVE_TRAILING 及保本换单的 False/异常路径。

与 ST-2 的分批止盈层级消费不同：这里影响的是缺失止损的永久虚假覆盖，修复一条路径不会自动修好另一条。

### RS-3 [P1] 交易所止损锚定信号 K 线价格，而不是实际入场价

**位置**：`adapter/coordinators/signal_execution.py:193`；`adapter/coordinators/sltp.py:187-202`；`adapter/coordinators/trade_event_handler.py:88-112`。

入场前把 bar.close 写成 first_entry_price；成交后没有用实际成交价格校正该值，EXCHANGE 止损继续读取它。Tick 初始化则使用 last_px，导致两种模式对同一笔成交使用不同基准。

**复现**：bar.close=100，合法的买入限价偏移 10%，实际在 90 成交，配置固定 2% 止损。真实价格计算器产生的卖出止损是 **98**，而按成交价应为 **88.2**。98 已处在当前价格上方，会造成场所立即触发或拒绝的风险；本轮未验证场所的具体响应。

**建议及验收**：区分信号参考价、委托价和实际持仓成本，保护以明确定义的成交基准计算；测试限价偏移、价格改善、滑点与多批成交。不要只用 signal/bar/fill 三者相同的样例。

### RS-4 [P1] 策略回撤门不更新行情期间的权益峰值

**位置**：`adapter/coordinators/risk_control.py:51-59`；峰值更新仅见初始化 `:49` 与 `adapter/coordinators/trade_event_handler.py:83-86`。

check_risk_limits 读取最新权益却不更新 peak_equity。没有成交的持仓期间，行情形成的新峰值不会进入策略回撤基准，即使该高权益已被风险门读取过。

**复现**：compound 模式，最大回撤 5%，权益先后为 1000、1100、1030。后两次风险检查均允许入场，峰值仍是 1000；实际从 1100 回撤约 **6.36%**。

**影响边界**：独立宿主熔断器也追踪权益，但其 10% 阈值不能替代策略配置的 5% 阈值。不能把此结论扩展成所有宿主风控都失效。

**建议及验收**：在稳定的行情/风险评估节拍更新可靠权益峰值，再计算回撤，且不能依赖后续成交来采样。覆盖持仓上涨后回落、候选入场被过滤、无新成交等路径。

### RS-5 [P1] 日切在入场检查时执行，会擦掉新交易日已经发生的亏损

**位置**：`packages/custos-strategy-toolkit/src/custos_toolkit/risk/controller.py:96-108`、`:128-129`、`:182-190`；调用点 `adapter/coordinators/trade_event_handler.py:137`。

record_trade 不接受成交时间，也不先推进交易日。日切直到下次 check_limits 才执行；如果午夜后先发生止损成交、之后才出现下一次入场检查，新日亏损就被混入旧计数，然后整体清零。

**复现**：23:59 检查过风险；00:01 止损亏损 60，权益从 1000 降至 940；00:02 首次新入场检查把 session_pnl 从 -60 清成 0，日亏损阈值 5% 未阻止入场。为隔离此条件，关闭策略回撤和连续亏损限制；6% 亏损也低于独立宿主 10% 熔断线。

**建议及验收**：使用执行事件时间确定 PnL 所属交易日；在记账前推进日界，并处理迟到事件。测试午夜两侧成交，以及连续多天没有入场检查但仍有退出成交。

### RS-6 [P1] 进程重启清除熔断锁定，策略日亏损状态也没有进入快照

**位置**：`src/custos/cli/_daemon.py:416-424`；`src/custos/core/fallback_breaker.py:85-89`；`adapter/coordinators/snapshot.py:58-65`；`adapter/trading_strategy.py:345-359`。

签名安全边界只从内存 registry 复用 breaker。daemon 重启后 registry 为空，重新构造的 breaker 把 frozen 和 peak_equity 清空；宿主 peak 字典也仅存在内存中。该行为违反熔断器“冻结直到人工干预”的既有契约。

另一个同类缺口是 Toolkit 内部 RiskController 的日损益、暂停截止时间和峰值没有被基础快照保存。正常 on_save/on_load 也无法恢复它们。

**复现 A**：真实签名 boundary factory 创建 breaker，在 1000→800 的权益变化后冻结。同进程重新 build 仍冻结；模拟 daemon 重启，新 registry 复用同一 store/resolver 后，新 breaker 在权益仍为 800 时允许新订单，且构造路径没有读取任何持久化熔断状态。

**复现 B**：策略已因日亏损禁止交易，执行实际 SnapshotCoordinator 保存/加载，在同日恢复后又允许交易。

**建议及验收**：由明确所有者持久化锁定状态及其身份/解除条件；恢复准入前加载风控状态。策略风控也要独立于“加速指标预热”配置持久化。验证崩溃恢复、正常重启、热更新、跨日恢复和显式解除的区别。

### RS-7 [P1] 平仓清理抹掉仍存续的新仓保护或未完成入场归属

**位置**：`adapter/coordinators/trade_event_handler.py:158-201`；`adapter/coordinators/sltp.py:150-153`。

反向成交路径虽然特意保留新仓 SL/TP，却仍无条件 reset position_tracker 和 tick_monitor。普通部分入场仓位被止损平掉时，非 reversal 的剩余入场单也被 clear() 清掉归属，但不会被撤销。

**原生引擎复现**：先买 1，再卖 2 完成净额反向。真实回调顺序为 OrderFilled（缓存已有新空仓）→旧 PositionClosed→新 PositionOpened。前一个回调把 Tick 保护初始化为新空仓，旧平仓回调紧接着清空它。缓存最终仍持有空仓 1，价格跌到应止盈的 90 时 monitor 返回 None。没有通用 PositionOpened 回调重建该状态。

**部分成交复现**：入场单成交一半，其已成仓位先被 SL 平掉；剩余入场单还 open，却已丢失 tracker ID/pending signal。随后剩余部分成交，实际 handler 不创建任何保护。

**建议及验收**：按仓位生命周期及订单归属清理；旧仓终结不得 reset 新仓，也不能遗忘仍可能成交的入场单。覆盖一笔跨零反转、多笔部分反转、部分入场后先止损及迟到回报。与 ST-4 的撤单失败归属丢失是不同触发路径。

### RS-8 [P1] 保护单拒绝触发的暂停同时关闭 Tick 退出，修复后也不恢复

**位置**：`adapter/coordinators/order_reconciler.py:461-474`；`adapter/strategy_core.py:350-375`。

止损拒单调用 pause()，日志称暂停新风险；但核心 on_trade/on_quote 在 paused 时完全不转发，因此 HYBRID 的本地追踪止盈/退出也停了。bar 风险卫生确实会补安全止损，但成功补单后不会改变暂停状态。其他币对的 Tick 退出也受同一个策略级开关影响。

**复现**：HYBRID 追踪已激活，峰值 120；一次安全止损拒绝使策略暂停，修复器随后补出新安全止损。价格回落到 115，monitor 本应给出追踪退出，但真实 core callback 直接返回，退出处理完全不被调用。

**建议及验收**：区分停止新增风险、继续保护退出和停止进程；保护路径不应被普通软暂停拦截。测试拒单暂停、修复后仍暂停及多币对场景；避免简单自动 resume 意外解除另一来源的暂停。

### RS-9 [P1] SuperTrend 截断历史后重新初始化递推状态，会凭空产生反向信号

**位置**：`adapter/indicators/supertrend.py:64-68`、`:178-190`。

每个 bar 都对最近 length+50 根重新调用 pandas-ta.supertrend，没有延续递推的前一方向、上下轨和 ATR。SuperTrend 有路径依赖，固定窗口重算不能等价替代延续状态。

**复现**：length=10、multiplier=3；15 根价格 200，随后每根下降 5 到 100，再横盘 100 根（每根 high=close+1、low=close-1）。实际类在索引 80 从空头翻成多头，而这一时段价格一直是 100。用仓库同一个 pandas-ta 实现在完整历史上计算，索引 80 和末尾均为空头；这不是版本或浮点容差比较造成的差异。

**影响边界**：使用本 Toolkit SuperTrend 类的策略可能产生虚假平空/开多信号。外部策略若使用另一实现，需要另验其导入路径；不能据此断言所有名为 Supertrend 的策略都有这个缺陷。

**建议及验收**：采用与目标算法一致的持续递推状态，持久化所需 ATR/轨道/方向；不要仅扩大窗口。以完整历史为参考，比较每根方向，覆盖长时间单边、横盘、窗口移出拐点和重启延续。

## Verification

可重放入口：

```sh
uv run --extra dev --extra nautilus python .forge/reviews/2026-09-20-custos-risk-state-deep-repro.py
```

本次实际使用已安装环境 `.venv/bin/python`，避免并行修复时 uv 重同步影响环境。结果为 **11 个探针通过（9 个问题，RS-6/RS-7 各两个触发用例）**。

- RS-7a 使用真实 BacktestEngine、真实市场单与成交/仓位事件，实际打开净空仓。
- RS-1/RS-3 使用真实原生 instrument 和实际 Custos 订单构造/协调器；缓存及提交回报受控。
- RS-2 使用真实安全门与 OKX ID 校验；边界 store 为未调用的桩，不涉及生产授权。
- RS-4/5/6/8 使用实际风控/快照/回调函数，控制时间、权益和运输结果。
- RS-9 使用实际 SuperTrend 与同一安装库的完整历史算法做对照。

既有测试：

```sh
.venv/bin/python -m pytest tests/toolkit tests/test_order_reservation.py tests/core/test_fallback_breaker.py tests/test_portfolio_snapshot.py tests/test_multivenue_money.py tests/engines/nautilus/test_runner_safety_execution_boundary.py -q
```

结果：**1,419 passed in 3.76s**，这是并行 ATR 修改前的既有测试基线；之后仅重跑本轮 11 个缺陷探针，不将修复方正在编写的测试算作本次验收。复现脚本通过 Ruff check/format 检查。

## File-by-File Summary

| 文件/调用链 | 结论 |
| --- | --- |
| signal_execution / execution / sizing | RS-1、RS-3；原生 FixedRiskSizer 对 0.01/1/10 乘数的独立检查未发现另一项严重问题 |
| sltp / order_reconciler / orders | RS-2、RS-8；并与 ST-2/3/4/7 区分触发路径 |
| trade_event_handler / sltp_mode / strategy_core | RS-7、RS-8；通过原生反向成交核对真实事件顺序 |
| risk_control / shared risk controller | RS-4、RS-5；不将宿主独立熔断器误判为同样失效 |
| snapshot / state_persistence / daemon / fallback_breaker | RS-6；热替换与真正进程重启结果不同 |
| supertrend / ATR / MACD / Tick trailing | RS-9；相邻指标的窗口重算存在同类连续性风险，本轮未另确认独立严重问题 |
| venue configuration / settlement / cash inventory / portfolio snapshot | 本轮交叉检查未新增独立 P0/P1；不是场所联调验收 |
| reservation boundary / FIFO fill accounting / fact production and publication | 结合前轮发现复查，未新增独立 P0/P1；不重复列 RR/RD 尚待验证的缺陷 |

## Suggestions

先修 RS-1/2/7/9 的数量、保护和信号问题，再修 RS-6 的重启锁定，以及 RS-3/4/5/8 的保护基准和风控事件时序。把同一场景沿“信号→订单→原生成交→保护→风险状态→重启”完整跑通，避免只新增验证函数是否存在的测试。

## Risk Assessment

**当前仍不具备“未发现严重问题”的结论。** 本轮新增问题均可能改变下单目标、保护有效性或配置的风险限制，应作为生产前阻断项处理。既有 1,419 项测试通过只说明它们覆盖的条件成立；修复后需要把本轮探针转换为预期正确行为的回归，再重新审查受影响链路。
