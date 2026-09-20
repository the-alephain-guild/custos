# 06 - strategy-state-and-order-protection-fixes

> **Status**: ✅ Completed
> **Completed**: 2026-09-20
> **Created**: 2026-09-20
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-strategy-deep-review.md`（Codex deep review，8 findings）
> **基线**: HEAD `45ff6f1`；8 项探针在该 HEAD 上全部复现成立，定向测试 162 passed。

## 修复来源

- 功能审计：`.forge/reviews/2026-09-20-custos-strategy-deep-review.md`
- 复现脚本：`.forge/reviews/2026-09-20-custos-strategy-deep-repro.py`（8 探针，断言的是**缺陷现状**）
- 分诊：ST-2/3/4/5 → P1（保护或限额执行错误）；ST-1/6/7/8 → P2（退出数量或内部状态错误）
- 实施顺序按审查方 Suggestions：保护链（ST-3→4→2）→ 资金状态（ST-5→6）→ 数量与生命周期（ST-1→7）→ 分配权重（ST-8）

## 贯穿本轮的单一根因

审查方 Risk Assessment 的原话是「意图已经产生」被当成「动作已经成功」。八项里有六项是同一形状：

| 意图 | 被当成 | finding |
|---|---|---|
| 达到止盈价格 | 止盈已成交 | ST-2 |
| 发出撤单请求 | 订单已消失 | ST-4 |
| 记录预留资金 | 分配已获批 | ST-5 |
| 提交入场委托 | 持仓已建立 | ST-6 |
| 创建保本止损 | 保护已归属 | ST-7 |
| 调用 check() 读行情 | 可以顺带推进层级 | ST-2(b) |

因此修复的共同形状是：**把"已请求"与"已确认"分成两个状态，只有执行回报能推进后者**。

## 修复任务

### Fix 1: 修复路径不得清空可用 ATR [ST-3 / P1]

**Root Cause**: `order_reconciler.ensure_exchange_sl_exists` 用合成 pending signal 覆盖入场上下文，且固定传 `entry_atr=None`；`sltp.submit_stop_loss` 随后从 `position_tracker.pending_entry_atr` 读到 None，`OrderPriceCalculator` 在 ATR 缺失时返回 None，于是一笔止损都发不出。

**Files**: `coordinators/order_reconciler.py`、`coordinators/sltp.py`

1. 写失败测试：ATR=2、倍数 2、入场价 100 时，跨三次修复冷却窗口必须补出止损（当前发 0 笔）。
2. `submit_stop_loss` / `submit_safety_stop_loss` 接受显式 `atr` 参数，与 pending signal 状态解耦。
3. 修复路径按优先级取 ATR：现存 `pending_entry_atr` → 当前 `ctx.indicators["atr"]`；**不再调用 `set_pending_signal`**，部分入场期间的原 pending signal/ATR 保持不变。
4. ATR 确实不可得时记 error 级日志显式暴露未受保护状态，不静默返回。

### Fix 2: 撤单请求不等于订单消失 [ST-4 / P1]

**Root Cause 一**: `handle_order_cancel_rejected` 把撤单失败无条件解释成「订单已不存在」并清掉 `entry_order_id`；该单仍可成交，而 `handle_order_filled` 的 tracked-entry 判定随即失败、提前 return，成交拿不到任何保护。
**Root Cause 二**: `execute_entry_for_pair` 发出旧单撤销请求后立即创建新单，单一 `entry_order_id` 被覆盖，旧单仍 open。

**Files**: `coordinators/order_reconciler.py`、`coordinators/signal_execution.py`

1. 写失败测试：撤单失败后该单成交，必须建立保护；旧单仍 open 时不得提交覆盖身份的新单。
2. 撤单失败时核实缓存终态：仅当订单不在缓存或已 `is_closed` 才释放归属；仍 open 则保留 `entry_order_id` 与信号链接并记 warning。
3. `_cancel_pending_entry_order` 返回撤单是否仍在途；在途时本 bar 跳过新入场，等确认后的下一个 bar 再入场（信号每 bar 重评估，不丢机会）。

### Fix 3: 止盈层级依执行回报推进 [ST-2 / P1]

**Root Cause**: `TickMonitorManager._check_scaled_tp` 在返回动作**之前**就把层级置为已命中，派发失败、本地拒绝或未成交都没有恢复路径；恢复函数为读行情调用 `check()` 又丢弃返回值，同样消费层级；分批 TP 订单从未加入 `tracker.tp_order_ids`，被拒时落进「普通减仓平仓拒绝」分支，连带撤销有效的安全止损。

**Files**: `tick_monitor.py`、`coordinators/execution.py`、`coordinators/order_reconciler.py`、`coordinators/trade_event_handler.py`

1. 写失败测试：本地拒绝后同价重试、重启恢复不消费层级、拒绝的分批 TP 不得撤销独立有效的 SL。
2. 层级状态从 `bool` 升为三态 `ARMED / PENDING / COMPLETED`；`check()` 只置 PENDING，`confirm_level` 由成交推进到 COMPLETED，`release_level` 由拒绝/取消/本地拒绝退回 ARMED。
3. 新增 `observe(price)`：只更新 trailing peak 等非消费状态，供恢复路径读取行情，不返回动作、不推进层级。
4. 分批 TP 提交后 `add_tp_order` 登记归属，使其命中既有的 take-profit 拒绝分支（保留止损），不再进入全平路径。
5. `submit_order` 返回 False（本地拒绝）时立即 release 层级。

### Fix 4: 分配失败必须阻断提交 [ST-5 / P1]

**Root Cause**: `execute_entry_for_pair` 忽略 `CapitalAllocator.allocate()` 的返回值，且在调用它之前就已累加 `ctx.allocated_capital` 并 `record_entry`，随后照常 `submit_order`。

**Files**: `coordinators/signal_execution.py`

1. 写失败测试：币对额度 100、请求 200 时必须零提交，且 allocator 与 context 状态不被污染。
2. 分配前置于任何状态写入：`allocate` 失败即 warning 并 return，不 `record_entry`、不累加 `allocated_capital`、不提交订单。
3. 币对额度与总可用资金两类拒绝走同一条阻断路径。

### Fix 5: 零成交终结订单必须完整回滚 [ST-6 / P2]

**Root Cause**: 入场在发送前即记录 position entry 并占用资金；完全未成交就 canceled/rejected 时只清订单 ID 和 pending signal。资金释放与 `PositionTracker.reset` 都挂在 `PositionClosed` 上，而这类订单从未形成持仓，该事件永不到来。

**Files**: `orders.py`、`coordinators/entry_reservation.py`（新建）、`coordinators/signal_execution.py`、`coordinators/trade_event_handler.py`、`coordinators/order_reconciler.py`

1. 写失败测试：资本 1000、请求 400，完全未成交的 cancel 与 reject 两条路径都必须回到可用 1000、entry_count=0。
2. `OrderTracker.set_entry_order` 记录本笔预留资金与下单总量；新增未成交比例查询。
3. 入场单终结时按未成交比例返还预留（allocator + `ctx.allocated_capital`）；零成交且无真实持仓时 `position_tracker.reset()`。
4. 部分成交后撤单只释放未成交部分，不擦除已形成的持仓。

### Fix 6: 分批止盈比例基数跨模式统一 [ST-1 / P2]

**Root Cause**: Tick 路径用触发当时的剩余仓位乘 `exit_pct`，交易所路径（`orders.create_scaled_orders`）用初始总量乘 `exit_pct`；同一配置因模式或成交时序产生不同的退出总量（50%+50% 在 tick 下只退出 75%）。

**Files**: `tick_monitor.py`、`coordinators/execution.py`、`sltp_mode.py`、`coordinators/order_reconciler.py`

1. 写失败测试：50%+50% 与 33%+33%+34% 在逐档成交下的退出总量必须与交易所模式一致。
2. `init_position` 记录初始计划数量作为唯一比例基数。
3. 退出数量由 monitor 按基数计算；最后一档吸收舍入余量，使总计划退出量完整。

### Fix 7: TICK 保本止损必须可追踪 [ST-7 / P2]

**Root Cause**: TICK 模式允许 `move_stop_to_break_even`，代码创建并发送了交易所止损，但只在 EXCHANGE/HYBRID 分支登记订单。完整 Tick 退出看到缓存中存在 reduce-only 单便等待撤单，而撤单函数只遍历 tracker，找不到这笔单——退出被无限阻塞，孤儿扫描又因方向正确而保留它。

**Files**: `coordinators/sltp.py`

1. 写失败测试：TICK 模式保本后，超过止盈价格必须完成退出，不得无限等待一个从未发出的撤单请求。
2. TICK 模式的保本止损同样登记进 `order_tracker`，与它实际创建的交易所订单归属一致。

### Fix 8: equal 分配与注册顺序无关 [ST-8 / P2]

**Root Cause**: `register_pair` 给首个无显式 tier 的币对赋比率 1.0、第二个赋 0.5，且不回头调整已注册的币对，也从不读 `config.mode`。资本 200 的两个币对因此得到 200/100 的额度，首个币对可占全部资金。

**Files**: `capital_allocator.py`

1. 写失败测试：资本 200、两个币对必须各得 100，且交换注册顺序结果不变。
2. 区分显式 tier 与隐式均分；每次注册后在**完整币对集合**上重算隐式权重。
3. 隐式权重分摊显式 tier 之外的剩余比例，总和不超过 1。

## 验证清单

- [x] 8 项探针的缺陷断言全部翻转（改写为正式回归测试）
- [x] 既有 162 项定向测试仍通过
- [x] 每个 Fix 的失败测试先红后绿
- [x] `make verify` 通过（exit 0）
- [x] `make verify-nt` 通过（exit 0）
- [x] 无真实账户、下单、容器或生产操作

## 偏离与改进日志


无。八项修复均按审查方给出的位置与验收实施，未偏离计划范围。

执行中登记两项计划外改动，均为修复的直接后果而非方向变更：

### DEVIATION: 部分成交的止盈层级仍欠其余量（自省发现）
- **等级**: 低（在 ST-2 既定验收范围内）
- **原因**: 层级三态初版在任何成交上关闭层级，而 IOC 分批止盈可能只成交一部分。ST-2 验收原文点名「部分成交」，初版不满足。
- **影响**: `tick_monitor.py`、`coordinators/trade_event_handler.py`
- **决定**: 层级以「还欠多少」结算——成交按数量入账，重试只要余量，取消时仍有欠量即退回 ARMED。
- **发现方式**: Step 3.5 自省 Round 1，非外部审查。

### DEVIATION: 结算逻辑落在新建的共享模块
- **等级**: 低
- **原因**: 取消与拒单是两个不同协调器上的两个回调（`TradeEventHandler.handle_order_canceled` 与 `OrderReconciler.handle_order_rejected`），而它们终结一笔入场的方式完全相同。把结算塞进任一方，另一方就得跨协调器 import；各写一份则是两个会各自漂移的副本。
- **影响**: 新建 `coordinators/entry_reservation.py`（53 行，单函数）
- **决定**: 独立模块承载 `release_unfilled_entry()`，两处共用一个地址。
- **登记时点**: 实施时漏登，由 fix 06 的代码审计 C1 抓出，随 fix 07 Fix 1 补记。

### DEVIATION: strict mypy 要求改写保护量的传递方式
- **等级**: 低
- **原因**: `submit_stop_loss` 新增 `atr` 参数后，`on_entry_filled` 里的 `**quantity_kwargs` 展开会加宽到提交函数的任意关键字参数，strict mypy 拒绝。
- **影响**: `sltp_mode.py`、`runtime_types.py`、`coordinators/signal_execution.py`、`coordinators/order_reconciler.py`
- **决定**: 保护量显式传参（每个提交函数本就把 None 读作「用持仓量」，行为等价）；ATR 指标读取改走 `runtime_types.Indicator` 协议，顺带消除 `signal_execution` 里的私有副本。
- **更新的文档**: 无（内部类型，不触及契约）

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-20
- **总 Task 数**: 8（ST-1 至 ST-8）
- **偏离数**: 2（均为低风险，见上）
- **验证结果**: 全部通过
- **实施 commit 范围**: `1151889`..`de47a3a`
- **契约影响**: 无。改动全部在 toolkit 策略适配层，不触及 RunnerFact、gateway-contract 或 authority 资产。`make check-authority` 通过。
- **红线守护**: 四条 non-custodial 红线全数守住。ST-8 的额度计算由 float 比率改为 Decimal 算术，是红线 0.4（money math 用 Decimal）的正向收紧；其余改动不触及凭据、引擎启动门或失联降级。
- **失败模式覆盖**: 见下表；本轮新增的 37 项全部为失败模式或状态一致性用例（撤单被拒、本地拒绝、venue 拒单、部分成交、重启恢复、额度拒绝、零成交终结）。

### 测试计数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/toolkit/test_strategy_state_and_order_protection.py` | 37 |
| `tests/toolkit/test_tick_monitor.py` | 54 |
| `tests/toolkit/test_sltp_mode.py` | 31 |
| `tests/test_plan_closeout_counts.py` | 33 |

上表合计 155 条。中间两个文件是既有文件，本轮修改了其中的用例（层级状态表示、保护量传参形式），按 `progress-management.md` 的规则重新计数认领。

最后一行需要解释，否则下一个读者会以为是误列：本轮没有编辑 `test_plan_closeout_counts.py` 一个字节，但它**按带测试计数表的 plan 份数参数化**——本 plan 一落盘，它自己的收集数就从 31 变成 33。认领它的判据是「谁让这个数字变了」，不是「谁编辑了这个文件」，所以这一行归本 plan。

### 扰动验证

八项修复各自的测试都经过反向扰动确认会转红——把修复改回缺陷形态，对应用例失败；还原后全绿。共 11 个扰动点，11/11 转红。首次对 ST-8 的扰动点选错（改了修复后已不再被读取的 `_tiers` 值），换到真正承载均分的那一行后确认转红；这条本身值得记住：**扰动验证必须打在真正承载行为的那行代码上，否则「仍然绿」说明的是扰动无效，不是测试无效。**

### 审查方原始探针的现状

`.forge/reviews/2026-09-20-custos-strategy-deep-repro.py` 现在会在 ST-2 处中止退出。这是预期的：该脚本的每个断言描述的都是**缺陷现状**，缺陷修好后断言自然不成立。该脚本是审查方的产物，本轮未修改它。其覆盖的行为已逐条改写为上表第一个文件中的正式回归测试。

### 功能验证（主路径）

1. 在 sandbox 或 testnet 配置一个用 scaled 止盈的策略（例如两档各 50%），让行情走过第一档目标价。
2. 预期：第一档发出的减仓单若被本地拒绝或被交易所拒单，同一价位下一个 tick 会重试，而不是这一档就此作废；被拒的止盈单不再导致既有止损被一并撤销。
3. 配置 `allocation.tiers` 给某币对一个小于所需下单额的额度，触发一次入场。预期：日志出现 `Capital allocation refused`，该笔入场没有订单发出，allocator 可用资金与上下文占用均无变化。
