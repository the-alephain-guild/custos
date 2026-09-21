# 30 - answers-outliving-their-question

> **Status**: ✅ Completed
> **Created**: 2026-09-21
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-21-custos-fix-recheck-review.md` FR-1 … FR-7（7 项 P1）
> **复现**: `.forge/reviews/2026-09-21-custos-fix-recheck-repro.py`（本机实跑，7 条探针全部成立）

复核报告的七项在代码上各不相干，但形状是同一个：**一个在某个时刻、对某个范围成立的判断，
被拿去回答另一个时刻、另一个范围的问题。**

| # | 当时被问的是 | 拿去回答的是 |
|---|---|---|
| FR-1 | 缓存里那张**改单之前**的委托是不是减仓 | 改单**之后**那张会不会开出反向仓位 |
| FR-2 | 这张入场单**累计**保护过多少 | 这一笔成交是不是**新持仓**的第一笔 |
| FR-3 | 现在有没有持仓 | 有没有东西能把敞口**重新**打开 |
| FR-4 | 这个 instance **第一个**节点起好了没有 | **现在这个**节点起好了没有 |
| FR-5 | **sandbox** 的额度是多少 | **testnet** 这条边界该用什么额度 |
| FR-6 | **第一次**落盘成没成 | 冻结**到底**有没有写下来 |
| FR-7 | 有入场候选时权益是多少 | 这段行情的**峰值**是多少 |

FR-5 严格说是范围越界而不是时间越界，但同样是「答案跑出了它被提问的那个格子」，
所以放在一起处理。

七项**都不是上一轮修错了**，而是上一轮修的那条路修对了、旁边那条路没被覆盖。报告的原话是
「部分是旧问题的遗漏输入，部分是修复之间的新交互」。所以本 plan 的每一处都必须保留原场景
的测试（C29：修复之后原探针还成立不等于没修好，但原回归转红一定是修坏了）。

---

## Fix 1: 改单按它将要留下的那张委托判定 [P1]

**Files**: `src/custos/core/order_reservation_boundary.py`、
`src/custos/engines/nautilus/runner_safety.py`、
`tests/engines/nautilus/test_an_amendment_is_judged_by_what_it_would_leave.py`（新建）

### 现状

`order_reservation_boundary.py:190 before_modify_order` 取缓存里那张委托来判风险方向：

```python
order = self.cached_order(command.client_order_id)
if order is not None and semantics.order_is_risk_reducing(order):
    return _Modification(client_order_id=client_order_id, prior_reserved_notional=None)
```

`order_is_risk_reducing`（`runner_safety.py:196`）读的是 `order.quantity` ——
**改单之前**的数量。于是「持多 1 → 挂 SELL 1 普通平仓（合法）→ 改成 SELL 2」这条路上，
改单请求带的 `quantity=2` 根本没进入判断：函数看到的仍是那张 SELL 1，判定为减仓，直接放行，
既不过冻结检查也不占任何预留。原生撮合之后账上是 **SHORT 1**，而 breaker 一直是 frozen。

fix 16（EE-1）加的 `_unsettled_reductions` 记的是「已被接受的平仓单认领了多少可平量」，
改单路径**完全不碰它**，所以改大之后那张单的认领量还停在旧值。

### 改法

判定的对象从「缓存里那张」换成「改完会是哪张」，两本账（可平量认领 / 金额预留）一起原子调整。

1. `NautilusCachedOrderSemantics` 把风险方向的判断抽成一个按「委托 + 数量」工作的私有函数，
   `order_is_risk_reducing` 与新增的 `modified_order_is_risk_reducing(intent, already_reducing)`
   共用它；后者的数量取自 intent（`intent.quantity` 为 None 时才回落到缓存值，与
   `modified_order_notional` 同一条规则）。另加 `modified_order_quantity(intent)` 供记账。
   两个方法同步登记进 `OrderSemantics` Protocol。
2. `before_modify_order` 重写为：
   - 缓存里没有这张委托 → 直接抛。今天也是抛（`modified_order_notional` 会抛），
     但那是**走到后面才撞上**的；改成一开始就说清楚。
   - 算这张单**自己之外**其他平仓单认领掉的可平量（否则它会把自己的认领算两遍）。
   - 判定改完之后是不是仍然只减仓：
     - **是** → 刷新它的认领量；若它此前持有金额预留，把预留释放掉（它不再增加敞口）。
     - **否** → 先过 `_require_risk_increasing_allowed()`（冻结即拒），再按有没有既存预留
       决定 `replace_order_reservation_sync` 还是 `reserve_order_notional_sync`，
       然后把它移出减仓账本。
   - 内存两本账的改动一律放在 store 调用**之后**，store 抛错即拒，不留半改状态。
3. `_Modification` 多记两件事：改之前的认领量、改完之后落在哪本账上。
   `rollback_modify` 据此把两本账都还原 —— 包括「新建的预留要释放」和
   「释放掉的预留要补回」这两种新出现的方向。
4. 每条路径都登记 `_pending_modifications`，这样 `OrderModifyRejected` 在减仓路径上也能回滚
   （今天只有增险路径登记，减仓路径被拒时什么都不回滚）。

### 为什么「已预留 → 变成减仓」要释放预留

不释放的话这张单会同时待在两本账上：`event_is_risk_reducing` 在成交时按缓存里的新数量判定，
会走 FIFO 减仓路径，那笔预留就永远挂着直到撤单；而 `_unsettled_reductions` 里没有它的认领，
于是**另一张**平仓单会以为可平量还是满的 —— EE-1 那个缺陷从改单这扇门原样回来。
释放用的 `reason="canceled"`：被取消的是那笔**预留**（这张单不再增加敞口），不是委托本身。

### 验收（对照报告原文逐句）

| 验收分句 | 覆盖它的测试 |
|---|---|
| 比较修改后的净敞口 | 改大成反向 → 拒；改大但仍在可平量内 → 放行 |
| 原子调整在途可平数量 | 改小之后另一张平仓单拿得到让出来的量 |
| 原子调整金额预留 | 减仓→增险新建预留；增险→减仓释放预留 |
| 拒绝/撤销修改时回滚 | `OrderModifyRejected` 之后两本账都回到改之前 |
| 同时存在其他退出订单 | 已有一张半仓平仓单时，改单只按剩下那半判定 |
| 不能只对 reduce_only 订单的改单做测试 | 全部用普通平仓单，另加一条 reduce_only 对照 |

---

## Fix 2: 持仓结束时，入场单的保护基数要跟着归零 [P1]

**Files**: `packages/custos-strategy-toolkit-nautilus/src/custos_toolkit_nautilus/adapter/orders.py`、
同包 `adapter/coordinators/trade_event_handler.py`、
`tests/toolkit/test_strategy_state_and_order_protection.py`（扩 `TestCloseCleanupRespectsWhatSurvives`）

### 现状

`orders.py:191` 的返回值决定下游走哪条分支：

```python
return protection_delta, previous_protected == 0 and protection_delta > 0
```

第二个值就是 `initialize_position`。它只在这张入场单**第一次**产生正敞口时为 True。

fix 11（RS-7）让半仓被止损之后入场单的归属留了下来（`trade_event_handler.py:242`
`if not surviving_positions and not entry_may_still_fill: ctx.position_tracker.reset()`），
这是对的；但同一段里 `:256` 把 `tick_monitor` reset 了，而 `order_tracker` 的
`_entry_protected_quantity` 原样保留。于是剩下那半成交时 `initialize_position=False`，
`sltp_mode.py:107/115` 只走 `_extend_tick_base` —— 新持仓的 entry price、方向、trailing
状态一个都没设，`monitor._entry_price` 停在 None，价格怎么走 `check()` 都返回 None。

交易所安全止损仍然会提交（那条路不看 `initialize_position`），所以**不是完全没有保护**；
失效的是这个新持仓的 tick / trailing 退出。TICK 模式走的是同一个分支。

### 改法

「这张单累计成交了多少」和「当前这个持仓的生命周期」是两套状态，让它们各自记账。

1. `OrderTracker` 加 `rebase_entry_protection()`：把 `_entry_exposure_offset_quantity`
   推到当前 `_entry_filled_quantity`，并把 `_entry_protected_quantity` 归零。
   语义上与它原本的用途一致 —— 这个 offset 一直表示「这张单里**不**为当前持仓开敞口的那部分」，
   反向单用它扣掉先平旧仓的那段，这里用它扣掉已经开过又平掉的那段。
2. `handle_position_closed` 在「持仓真的归零、而入场单还会继续成交」时调用它，
   也就是 `not surviving_positions and entry_may_still_fill`。
   反向单那条路（`surviving_positions` 非空）不动 —— 替代持仓的保护是开出它的那笔成交种的。

### 验收

- 半仓成交 → 被止损平掉 → 余量成交：新持仓的 monitor 有 entry price，trailing 会触发退出。
- 保护数量不被放大：余量 0.5 成交只保护 0.5，不是累计的 1.0。
- 同一持仓内的连续两次入场成交仍然只初始化一次（原行为，不能回退）。
- TICK 与 HYBRID 两个模式都覆盖。

---

## Fix 3: 撤增险挂单与有没有持仓无关 [P1]

**Files**: `src/custos/engines/nautilus/host.py`、
`tests/test_containment_must_look_before_it_claims.py`（扩）

### 现状

`host.py:1610` 先从持仓构造 `instrument_ids`，`:1611` 为空就 return，而
fix 26 加的 `_cancel_risk_increasing_orders` 在 `:1635` —— 在那个 return 的**后面**。
于是「仓位是空的、但挂着一张非 reduce-only 的入场限价单」这一状态下，熔断一次都不会撤它。
冻结只挡新提交，这张单是场所已经接受的，下一个 tick 就能成交，敞口原样回来。

fix 29 加的 `_containment_confirmed` 短路（`:1617`）让这件事更糟一档：确认过的实例
后续直接记 `nt_containment_still_clear` 返回，连挂单都不看。

### 改法

把撤单提到 `instrument_ids` 判空之前，让「要不要撤单」只由挂单决定：

1. `_cancel_risk_increasing_orders` 改成返回它看到的增险挂单数量，
   `None` 表示**读不到**（`_open_venue_state` 抛错，C9：读不到不是确认）。
2. `flatten_positions` 先撤单，再按 `(持仓, 增险挂单)` 两个数字分流：
   - 有持仓 → 照旧 close + `_confirm_containment`。
   - 无持仓但有增险挂单 → 不 close（没东西可平），仍走 `_confirm_containment`
     确认撤单被场所受理。
   - 两个都是 0 → 走原来的 `still_clear` / `unconfirmed` 分支。
   - 读不到挂单（`None`）→ 不走 `still_clear`，按 unconfirmed 记。
     「读不到」和「确实没有」不能合成一个答案。

### 验收

- 空仓 + 一张增险挂单 → 撤单被调用；确认之后记 `nt_containment_confirmed`。
- 同一实例此前确认过，再来一张增险挂单 → 仍然撤（`still_clear` 不再短路掉挂单检查）。
- 空仓 + 只有 reduce-only 挂单 → 不撤（保护单是持仓自己的，fix 26 的取舍不变）。
- 空仓 + 无挂单 + 未确认过 → 仍记 `nt_flatten_containment_unconfirmed`（启动期语义不变）。
- 空仓 + 无挂单 + 确认过 → 仍记 `nt_containment_still_clear`（fix 29 的行为不变）。

---

## Fix 4: readiness 是现在的事实，不是一次性闸门 [P1]

**Files**: `src/custos/cli/_daemon.py`、`tests/test_startup_is_not_a_breach.py`（扩）

### 现状

`_daemon.py:542`：

```python
if deployment_instance_id in self._evaluating:
    return True
```

`_evaluating` 一旦加入就不再移除，也不再探测。而 `deployment_instance_id` 标识的是
**部署**不是**这一次运行** —— 节点自动重启、同代替换，id 都不变。于是第二个节点继承了
第一个节点的「已就绪」判定，启动期那份还没拿到账户状态的 unreliable snapshot 被
`EngineSafetySupervisor` 读成「运行中失联」，冻结 + 收缴。这正是 fix 26 要防的那件事
从重启这扇门原样回来。

### 改法

每一轮都问 `deployment_ready`，不缓存判定；缓存的只是**日志去重**和**等待窗口的起点**。

1. `_evaluating` 换成 `_ready`：表示「上一轮看到它是就绪的」，仅用于识别状态翻转。
2. 探到 not-ready 且 `_ready` 里有它 → 判定这是一次重启：清掉 ready/timeout 标记，
   把等待窗口的起点推到现在，记一条 `signed_supervision_restart_awaiting_readiness`。
3. 超时分支加 `_timed_out` 标记，保证「过了上界照常评估」的行为不变，同时不再每轮刷一条 error。
4. 没有 `deployment_ready` 的 host 用独立的 `_probeless` 集合去重（原来与 `_announced`
   共用一个集合，两种事件混在一起）。
5. 加 `retain(active_ids)`，`_run_signed_safety_supervision` 每轮调一次，
   清掉已经消失的部署 —— 否则字典只增不减，而且同一个 id 再回来时会继承上一次的窗口。

「等待不是豁免」这条不变：过了上界仍然照常评估，照常 fail closed。

### 验收

- 同一 instance 从 ready 翻回 not-ready：第二轮**又探了一次**，且被挡住不评估。
- 挡住期间 `get_engine_status` 一次都没被调用（重启窗口内不冻结）。
- 重启之后恢复 ready：正常评估（等待没有变成永不看）。
- 重启之后始终不 ready：过了上界照常评估、照常冻结，且 timeout 只记一条。
- 部署消失后 `retain` 清掉它的状态；同 id 再出现时拿到的是一个新窗口。

---

## Fix 5: 策略换版只到它自己那个模式的边界 [P1]

**Files**: `src/custos/core/order_reservation_boundary.py`、`src/custos/cli/_daemon.py`、
`tests/test_a_policy_renewal_reaches_the_live_boundary.py`（扩）、
以及全部 `RunnerReservationBoundary(...)` 构造点（7 个文件、11 处）

### 现状

`_daemon.py:429`：

```python
for instance_id, boundary in tuple(boundaries.items()):
    boundary.adopt_policy(limits.policy_id, limits.breaker)
```

`limits` 是按收到的 `trading_mode` 解析出来的，循环却遍历**全部**边界。runner 同时启用多个
模式时（`args.enabled_modes`），一条 sandbox 的换版消息会把 testnet 边界的 policy id 和
breaker 配置一起换掉。报告实测：testnet 上限 10,000 被换成 sandbox 的 100，原本合法的
敞口 500 随即 `notional_breach`，熔断触发收缴。

### 改法

两层，按 lesson #22/#28：外层选择，内层拒绝。

1. `RunnerReservationBoundary` 增加必填的 `trading_mode`。这不是登记表上的一个字段：
   边界属于一个部署，部署跑在一个模式里，而签名策略是按模式签发的 —— 让边界自己说得出
   它属于哪个模式，才不会有第二本注册表与它对不上。
2. `adopt_policy` 增加 `trading_mode` 关键字参数，与自己的模式不符即抛。
   即使将来有人忘了在外层过滤，边界自己也会拒。
3. 通知器只对模式相符的边界调用，并在日志里带上 `trading_mode`；
   一条换版没有命中任何边界时记一条 info（不是错误：这个模式此刻没有部署是正常的）。

### 验收

- sandbox 换版：sandbox 边界采纳，testnet 边界的 policy id 与 breaker 配置**逐字不变**，
  原本合法的敞口仍然合法。
- testnet 换版：testnet 边界采纳（fix 27 的原场景保留）。
- **relaxed double**（lesson #28）：绕过通知器直接对 testnet 边界
  `adopt_policy(..., trading_mode="sandbox")` → 抛。证明内层不是 dead branch。
- 换版仍然保住冻结与高水位（fix 27 的行为不变）。

---

## Fix 6: 没写下去的冻结要接着写 [P1]

**Files**: `src/custos/core/fallback_breaker.py`、
`tests/test_a_freeze_must_outlive_the_process.py`（扩）

### 现状

`fallback_breaker.py:126-134` 捕获落盘异常、记 error、返回，**不留任何痕迹**。
而后续的发布都有前置条件：`:160` `if not already_frozen`、`:199` `if newly_frozen or peak_advanced`。
于是一次瞬时写失败之后，冻结就只活在内存里 —— 存储恢复了也没人再试，
下一次重启按那行未冻结的记录重建，breaker 放行新单，而没有任何人解除过那次锁定。

fix 20 把「冻结跨重启」做成了红线级承诺，这条异常路径是那个承诺的缺口。

### 改法

1. 记下「有一次状态没写成功」以及它当时带的 reason code。
2. `fail_closed` 与 `evaluate` 在各自的发布条件之外，**多一个条件**：有待写状态就再写一次。
   重试写的是**当前**状态（峰值可能已经前进），reason code 用当时那次的 ——
   这一 tick 没有冻结任何东西，不该用这一 tick 的理由去解释那次冻结。
3. 重试失败照样记 error 并带上尝试次数：一条永远在响的警报要能看出它是第几次
   （生态 #50）。磁盘持续不可用时不承诺写成功，只承诺不再沉默。
4. `restore` 不再让一行陈旧记录解除内存里的冻结（`self._frozen or frozen`），
   峰值取两者较大。今天两个调用点都是对全新 breaker 调用，所以这是把一个脚坑关掉，
   不改变现有行为。

### 验收

- 冻结落盘失败一次、存储随后恢复：下一次 evaluate/fail_closed 重试成功，
  durable 行变成 frozen=True，按它重建的 breaker **不**放行新单。
- 连续失败：每次都重试、每次都记 error，内存冻结始终有效，durable 行始终是旧值。
- reason code 不被重试那一 tick 改写。
- `restore` 拿到一行 frozen=False 不会解除已经冻结的 breaker。
- 正常路径的发布次数不变（fix 20 的既有断言不回退）。

---

## Fix 7: 权益峰值按 bar 节拍采样，不按入场决策 [P1]

**Files**:
`packages/custos-strategy-toolkit-nautilus/src/custos_toolkit_nautilus/adapter/trading_strategy.py`、
同包 `adapter/coordinators/risk_control.py`、
`tests/toolkit/test_risk_gate_semantics.py`（改写 RS-4 那组）

### 现状

fix 13 把采样放进 `risk_control.py:86 check_risk_limits`，而这个函数唯一的调用点是
`trading_strategy.py:651 _entry_gates_pass` —— 只有出现入场候选、且方向被允许时才会走到。
持仓期间的 NEUTRAL bar 一次都不采样。报告实测：权益从 1000 升到 1100 期间没有采样，
下一根 1030 且有入场信号时把峰值更新成 1030，6.36% 的真实回撤被当成 0。

fix 13 的测试直接调 `check_risk_limits` 三次，所以看不到这个调用节拍的缺口
（C27：验收分句被根因修复整体盖过；这里是「测试的调用方式不是生产的调用方式」）。

### 改法

把「更新风险状态」和「准入检查」分开：

1. 采样搬到 `_on_bar_risk_hygiene`（`trading_strategy.py:551`）。这个钩子在
   `strategy_core.py:354` 被调用，位置在软暂停短路**之前**、`on_core_bar` 之前，
   所以每一根 bar 都到，暂停期间也到 —— 正是报告要求的「观察节拍独立于入场决策」。
2. 只在权益**可靠**时采样。`update_peak_equity` 只会抬高峰值，一个不可靠的偏高读数
   会把峰值永久抬起来，之后每一次检查都在量一个不存在的回撤。
   今天 `check_risk_limits` 的采样反而没有这个保护（可靠性检查排在它后面）。
3. 采样放在 pair context 查找**之前**：权益是账户级的，不该由「哪个品种恰好来了 bar」决定。
4. `check_risk_limits` 里那次 `update_peak_equity` 删掉，让它变成纯粹的准入检查。
   留着它既冗余又会绕开第 2 条的可靠性保护。

### 验收（必须走真实 bar 流程，不是单独调风险函数）

- 持有期间的 NEUTRAL bar 把峰值抬到 1100；下一根 1030 带入场信号时被拒。
- 软暂停期间仍然采样（`_on_bar_risk_hygiene` 在暂停短路之前）。
- 权益不可靠时不采样（峰值不被脏数据抬起来）。
- 新高本身不是回撤（RS-4 原语义保留）。
- 成交时的采样（`trade_event_handler.py:95`，compound 模式）不受影响。

---

## 明确不做

- **`modify_orders` 批量改单**：fix 25 已经把它整体拒掉，本 plan 不改这个决定。
  Fix 1 只覆盖单张改单这条真正会放行的路径。
- **熔断之后是否顺带停机**：fix 29 的同一句话仍然成立，是策略决定不是缺陷。
- **重启后 durable 行说未冻结、而上一个进程的冻结从未写成功**这个跨进程情形：
  Fix 6 让写能落下去，但磁盘持续不可用时仍然可能丢。这不是本轮能关掉的，
  报告自己也写了「没有要求在磁盘持续不可用时保证写入」。
- **`RunnerSafetyLimits` 带上 trading_mode**：Fix 5 让边界自己知道模式即可，
  不动 resolver 的返回契约。

## 验证清单

- [x] 七处各自先写失败测试、先红后绿
- [x] 报告的 7 条探针逐条复跑，并在 close-out 里写明每条是「中止在哪一行」还是
      「为什么还绿」（C29）
- [x] 每处扰动验证用独立 `PYTHONPYCACHEPREFIX`（C15），扰动后先与 scratchpad 备份
      逐字节比对确认真的改到了（C31），还原从同一份备份拷回（C15 续编）
- [x] 原场景回归全部保留且仍绿（fix 11/13/16/20/26/27/29）
- [x] `make verify` 与 `make verify-nt` 均 exit 0
- [x] close-out 的测试条数来自 `pytest --collect-only` 实跑，逐文件列表

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 改单按改后判定 | P1 | ✅ | 2026-09-21 | FR-1，报告列为最优先 |
| 3 空仓也要撤增险挂单 | P1 | ✅ | 2026-09-21 | FR-3，报告列为最优先 |
| 5 换版只到本模式 | P1 | ✅ | 2026-09-21 | FR-5，报告列为最优先 |
| 2 新持仓重新初始化保护 | P1 | ✅ | 2026-09-21 | FR-2 |
| 4 readiness 每轮重问 | P1 | ✅ | 2026-09-21 | FR-4 |
| 6 未写下的冻结要重试 | P1 | ✅ | 2026-09-21 | FR-6 |
| 7 峰值按 bar 采样 | P1 | ✅ | 2026-09-21 | FR-7 |

## 偏离与改进日志

### DEVIATION: `trading_mode` 变成 `RunnerReservationBoundary` 的必填构造参数
- **等级**: 中（跨 11 处构造点的内部契约变更，无 wire 影响）
- **原因**: FR-5 要按模式选择换版对象。可选的做法有三种：边界注册表旁边再维护一张
  `{instance_id: mode}` 表、让通知器去问 host、或让边界自己带着模式。前两种都制造了
  「两份关于同一件事的记录」，而这正是本轮在修的病。
- **决定**: 边界带 `trading_mode`（部署跑在一个模式里，策略按模式签发，两者本来就是
  一对一），并让 `adopt_policy` 在模式不符时抛错。外层选择 + 内层拒绝是两层，按
  lesson #22/#28 各自可测（扰动 R4/R5 分别只让对方那层的测试转红）。
- **影响**: `src/custos/cli/_daemon.py` 一处构造 + 6 个测试文件共 10 处构造点，
  `adopt_policy` 4 处调用点。`_run_signed_safety_supervision` 的签名不变。

### DEVIATION: 「已预留的委托被改小成减仓单」时释放预留，reason 用 `canceled`
- **等级**: 低
- **原因**: 存储的 release 只接受 `rejected` / `canceled` 两个理由，两个都不是
  「这张单不再增加敞口了」的精确说法。
- **决定**: 用 `canceled`，并在代码注释里写明被取消的是**那笔预留**而不是委托本身。
  不释放的代价更大：那张单会同时待在两本账上——成交时按缓存里的新数量判成减仓、走 FIFO
  路径，预留就一直挂着；而 `_unsettled_reductions` 里没有它的认领，于是另一张平仓单
  会以为可平量还是满的，EE-1 的缺陷从改单这扇门原样回来。

### DEVIATION: 用正则批量给测试加构造参数，改坏了 5 个文件（C10 dogfood）
- **等级**: 低（未提交，当场恢复）
- **原因**: 给 11 处 `RunnerReservationBoundary(...)` 补 `trading_mode=` 时图快写了一条
  正则，作用域是「`fallback_breaker=` 那一行之后」。而其中几处的
  `fallback_breaker=FallbackBreaker(` 是跨多行的，参数被插进了构造调用的中间。
- **决定**: 按 C10 的规矩重做——逐处手写替换、每处断言**整行唯一匹配**、改完
  `ast.parse` 验证。恢复用 `git show HEAD:<path>`（这几个文件在 HEAD 上是干净的，
  所以 HEAD 与备份等价；若已改过则必须用 scratchpad 备份，见 C15 续编）。
- **教训**: C10 原文写的是「作用域必须是语法结构而不是行」。这次我没用清理性正则，
  只用了「锚点行 + 追加一行」，仍然中招——**「行」在多行调用里根本不是一个可锚定的
  位置**。`git checkout --` 被护栏拦下是对的，它逼我去想恢复源头取哪一份。

### DEVIATION: FR-7 的复现探针中止在一行「与缺陷无关」的断言上（C29）
- **等级**: 低（证据链，不是代码）
- **原因**: 报告的探针把 `_on_bar_risk_hygiene` 绑到 harness 上，但本轮的修复让那个方法
  去调一个兄弟方法 `_sample_drawdown_baseline`。harness 没有它，于是 `on_bar` 的顶层
  `except` 把 AttributeError 吞成 `_log_error`，探针中止在 `assert not h._log_error.called`
  ——**看起来像修好了，实际是那个 harness 里的策略根本没有这条修复**。
- **决定**: 在 scratchpad 的副本里补上那一行绑定再跑，探针这才中止在真正的缺陷断言
  （`assert h._risk_controller.peak_equity == 1000`）。原报告文件一字未动。
  本仓的回归测试两个方法都绑真实实现，钉的是生产形态。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-21
- **总 Fix 数**: 7（全部 P1）
- **偏离数**: 4（1 中 / 3 低，见上）
- **验证结果**: 全部通过。`make verify` exit 0；`make verify-nt` exit 0，
  **2960 passed / 29 skipped / 1 xfailed**
- **实施 commit 范围**: `9afb68b`（plan）`3c7012b` · `b921498` · `5607c2e` ·
  `c96faa7` · `fe9e0c3`
- **契约影响**: 无 wire / schema / authority 资产变更。内部契约两处：
  `RunnerReservationBoundary` 的构造与 `adopt_policy` 各多一个 `trading_mode`；
  `OrderSemantics` 协议新增 `modified_order_quantity` 与
  `modified_order_is_risk_reducing`。新增日志事件两条
  （`runner_policy_renewal_no_boundary` · `signed_supervision_restart_awaiting_readiness`）；
  `fallback_breaker_state_persist_failed` 多一个 `attempt` 字段，
  `runner_policy_renewal_adopted` 多一个 `trading_mode` 字段。预留事件 id 新增两个前缀
  （`modify_to_reducing` · `modify_to_increasing`），它们只是 `_event_id` 的幂等键，
  不进 wire。

### 红线 gate 满足度（C/lesson #40 模板）

| 红线 | code test 覆盖 | runtime wire | defer | 说明 |
|---|---|---|---|---|
| 0.1 Key/KEK 不出进程 | 未触及 | 未触及 | 无 | 本轮不碰凭据路径 |
| 0.2 引擎启动七道门不绕过 | 未触及 | 未触及 | 无 | 本轮不碰启动门 |
| 0.3 失联 ≠ 停止 | ✅ FR-3/4/6 三处新增回归 | ✅ 全部在生产接线上 | 无 | 见下 |
| 0.4 Money 用 Decimal | ✅ 现有 money contract 测试仍绿 | 未触及 | 无 | 本轮新增代码无 float |

红线 0.3 是本轮的主要服务对象，三处各自是它的一个缺口：熔断收缴对空仓书视而不见
（FR-3）、重启窗口内被误判为失联而冻结（FR-4）、冻结落盘失败一次就再也不写、重启等于
一次无人署名的解除（FR-6）。三处的修复都在生产接线上，不是只有单测。

### 测试条数（`pytest --collect-only` 实跑）

| 测试文件 | 条数 |
|---|---|
| `tests/engines/nautilus/test_an_amendment_is_judged_by_what_it_would_leave.py` | 11 |
| `tests/test_a_policy_renewal_reaches_the_live_boundary.py` | 13 |
| `tests/test_a_freeze_must_outlive_the_process.py` | 20 |
| `tests/test_containment_must_look_before_it_claims.py` | 13 |
| `tests/test_plan_closeout_counts.py` | 81 |
| `tests/test_startup_is_not_a_breach.py` | 10 |
| `tests/toolkit/test_risk_gate_semantics.py` | 14 |
| `tests/toolkit/test_strategy_state_and_order_protection.py` | 76 |

### 报告探针逐条复跑（C29）

在 scratchpad 的副本上跑（原报告文件一字未动），两处按 API 变化做了最小适配：
`RunnerReservationBoundary(...)` 补 `trading_mode=`，FR-7 的 harness 补一行
`_sample_drawdown_baseline` 绑定（理由见偏离日志第四条）。

| 探针 | 结果 |
|---|---|
| CONTROL（EE-1 双平仓） | 仍然通过 —— 上一轮的修复没有被本轮破坏 |
| FR-1 | 中止在 `assert len(positions) == 1 and positions[0].is_short ...`：改单被拒，撮合后没有反向仓位 |
| FR-2 | 中止在 `assert monitor._entry_price is None`：新持仓拿到了 100 |
| FR-3 | 中止在 `assert not strategy.cancel_order.called`：空仓书上的入场单被撤了 |
| FR-4 | 中止在第二次 `assert await startups.may_evaluate(...)`：替换节点被挡住 |
| FR-5 | 中止在 `assert active.policy_id == other_policy ...`：testnet 边界一个字段都没动 |
| FR-6 | 中止在 `assert calls == 2 and durable["frozen"] is False ...`：第三次 tick 重试写成功 |
| FR-7 | 中止在 `assert h._risk_controller.peak_equity == 1000`：NEUTRAL bar 已经把峰值抬到 1100 |

七条全部中止在各自的缺陷断言上。**没有一条是「还绿但输入形态不同」**——本轮不需要用到
C29 的那半边解释。

### 验收条款逐句对照（C27）

| finding | 验收分句 | 覆盖它的测试 |
|---|---|---|
| FR-1 | 比较修改后的净敞口 | `test_enlarging_a_plain_close_past_the_position_is_refused` · `test_enlarging_within_the_position_is_still_a_close` |
| FR-1 | 原子调整在途可平数量 | `test_shrinking_a_close_hands_back_the_room_it_held` |
| FR-1 | 原子调整金额预留 | `test_an_amendment_into_risk_reserves_what_it_would_add` · `test_an_order_that_stops_adding_risk_releases_its_reservation` |
| FR-1 | 拒绝/撤销修改时回滚 | `TestARejectedAmendmentPutsBothBooksBack` 三条 |
| FR-1 | 同时存在其他退出订单 | `test_another_exit_order_narrows_what_an_amendment_may_claim` |
| FR-1 | 不能只对 reduce_only 改单做测试 | 全部用普通平仓单；`test_a_reduce_only_amendment_is_never_refused` 是对照 |
| FR-2 | 归零后同一订单继续开仓要重新初始化新仓保护 | `test_the_new_position_knows_what_it_paid`（hybrid/tick 各一） |
| FR-2 | 同时保留尚未成交的订单身份 | fix 11 的 `test_a_partly_filled_entry_keeps_its_ownership_through_a_stop_out` 仍绿 |
| FR-2 | 测「成交→平仓→余量成交」，不能只验同一仓位的两次连续入场 | `test_the_new_position_can_still_trail_out` + 对照 `test_a_second_lot_of_the_same_position_still_only_extends` |
| FR-3 | 是否撤单独立于是否存在持仓 | `test_a_working_entry_is_cancelled_even_with_no_position_open` |
| FR-3 | 每次确认都检查当前挂单，不依赖历史成功标记 | `test_a_previous_confirmation_does_not_excuse_a_new_entry` |
| FR-3 | 覆盖初始空仓 / 平仓后残单 / 先前确认后新发现的订单 | 上两条 + `test_an_unreadable_order_book_is_not_a_clear_one` |
| FR-4 | 启动等待属于每一次引擎启动 | `test_a_restart_under_the_same_id_is_asked_again` |
| FR-4 | 清理离开的运行状态 | `test_a_deployment_that_goes_away_leaves_no_spent_window_behind` · `test_the_supervision_loop_forgets_deployments_that_left` |
| FR-4 | 保留有界等待 | `test_a_restart_that_never_finishes_is_still_guarded` |
| FR-4 | 测首次启动 / 同代自动重启 / 换代重启 / 重启超时 | fix 26 的四条仍绿 + 上述三条（自动重启与换代重启在 runner 侧同形：同一 instance id、新节点） |
| FR-5 | 按 tenant/runner/mode 精确归属选择更新对象 | `test_a_sandbox_renewal_leaves_a_testnet_boundary_alone` |
| FR-5 | 同步更新 ID 与配置 | `test_the_daemon_hands_a_renewal_to_the_boundary_of_that_mode`（fix 27 原场景） |
| FR-5 | 混合注册表：目标模式更新、其他模式完全不变 | 上面第一条 + `test_exposure_that_was_legitimate_stays_legitimate` |
| FR-5 | 验证过期和非当前 policy 的处理 | `test_a_renewal_for_a_mode_with_no_deployment_is_recorded_not_lost` + `test_the_boundary_refuses_a_policy_from_another_mode_on_its_own` |
| FR-6 | 跟踪未确认的状态写入并有界重试 | `test_the_next_evaluation_writes_what_the_failed_one_could_not` |
| FR-6 | 恢复时不能把未确认锁定误当作正常解锁 | `test_a_stale_durable_row_cannot_lift_a_freeze_that_is_already_held` |
| FR-6 | 一次失败后恢复 | `test_a_restart_after_the_retry_still_refuses_orders` |
| FR-6 | 连续失败 | `test_a_sink_that_never_works_keeps_trying_and_keeps_saying_so` |
| FR-6 | 旧 durable row 仍为未冻结 | `test_a_restart_after_the_retry_still_refuses_orders` 走的正是这条路 |
| FR-7 | 观察节拍独立于入场决策 | `test_a_high_printed_while_holding_enters_the_baseline` |
| FR-7 | 把「更新风险状态」与「准入检查」分开 | `test_admission_no_longer_moves_the_baseline_itself` |
| FR-7 | 从真实 bar 流程测上涨持有 | `test_a_high_printed_while_holding_enters_the_baseline` |
| FR-7 | 回落后入场 | `test_the_give_back_after_that_high_is_refused` |
| FR-7 | 软暂停期间的峰值 | `test_a_paused_strategy_still_samples_the_baseline` |

**两处验收分句没有直接对应的新测试，各自说明**：

1. FR-6 的「显式人工解除与待写锁定的顺序」。本仓没有「内存里解除冻结」的 API——解除
   走 `arx-runner breaker clear`，它改的是 durable 行，进程要重启才读到（fix 20）。
   所以「先解除还是先补写」这个顺序在当前设计里不存在两种可能：待写的锁定只会写回
   frozen=True，而操作员的解除写的是另一条记录、由下一次 `restore` 读取，而 `restore`
   现在拒绝用一行 frozen=False 去解除内存里的冻结。**不为一个不存在的路径编测试**。
2. FR-4 的「换代重启」。runner 这一侧看不到 generation：`_SupervisionStartups` 只拿到
   `deployment_instance_id`，同代自动重启与换代替换在它眼里完全同形（同一个 id、一个新
   节点）。所以一条测试覆盖两种情形，而不是假装分开测了。

### 扰动验证

18 处，各用独立 `PYTHONPYCACHEPREFIX`（C15）；每次施加后先与 scratchpad 备份逐字节
比对确认真的改到了（C31），还原从同一份备份拷回（C15 续编）。

| # | 把修复改回什么 | 转红的测试 |
|---|---|---|
| R1 | 改单仍按缓存里那张判定 | 6 条 |
| R2 | 减仓改单不刷新可平量认领 | 2 条 |
| R3 | 回滚不还原可平量认领 | 2 条 |
| R4 | 换版通知不按模式筛选 | 3 条（内层抛错，证明两层独立） |
| R5 | `adopt_policy` 不校验模式 | `..._refuses_a_policy_from_another_mode_on_its_own` |
| R6 | 撤单挪回持仓判空之后 | 3 条 |
| R7 | `still_clear` 短路不看挂单数 | `..._an_unreadable_order_book_is_not_a_clear_one` |
| R8 | readiness 判定重新加闩 | 2 条 |
| R9 | 监督循环不调 `retain` | `..._loop_forgets_deployments_that_left` |
| R10 | `retain` 不清等待窗口与超时标记 | `..._leaves_no_spent_window_behind` |
| R11 | evaluate 不重试待写状态 | 3 条 |
| R12 | 重试用本 tick 的 reason | `..._carries_the_reason_the_freeze_was_reached_under` |
| R13 | `restore` 直接覆盖 frozen/peak | `..._cannot_lift_a_freeze_that_is_already_held` |
| R14 | `fail_closed` 不重试待写状态 | `..._a_restart_after_the_retry_still_refuses_orders` |
| R15 | 平仓时不 rebase 入场保护基数 | 4 条 |
| R16 | rebase 只清 protected、不推 offset | `..._only_the_quantity_that_reopened_is_protected` |
| R17 | 不在 bar hygiene 里采样峰值 | 4 条 |
| R18 | 采样不看权益可靠性 | `..._an_unreliable_equity_is_not_a_new_high` |

R9 第一次不咬：当时只有一条直接调 `retain` 的测试，删掉循环里的调用它照样绿。补了
`test_the_supervision_loop_forgets_deployments_that_left`（走真实监督循环）之后才转红。
留在这里是因为「方法有测试」和「有人调它」是两件事——前者绿不代表后者接上了。

### 功能验证（主路径，需真实环境）

1. **FR-1**：testnet 上挂一张合法的普通平仓单，触发熔断，然后把它改大到超过当前持仓。
   应当在日志里看到 `runner_order_refused`，`reason_code` 为
   `custos_runner_fallback_breaker_frozen`；场所侧的委托数量不变。
2. **FR-3**：持仓为空但挂着一张入场限价单时触发熔断。应当先看到
   `nt_containment_cancelled_risk_increasing_orders`，再看到 `nt_containment_confirmed`，
   **不应**看到 `nt_containment_still_clear`。
3. **FR-4**：让 NT 节点在同一部署下重启。重启窗口内不应出现 `fallback_breaker_fail_closed`；
   应当看到 `signed_supervision_restart_awaiting_readiness`，随后是第二条
   `signed_supervision_evaluating`。
4. **FR-5**：同时启用 sandbox 与 testnet，只给 sandbox 推一条策略换版。日志里
   `runner_policy_renewal_adopted` 的 `trading_mode` 应当只有 sandbox；testnet 上原本
   合法的敞口不应触发 `fallback_breaker_tripped`。
5. **FR-6**：冻结发生时让状态库短暂不可写（如临时改权限）。应当看到
   `fallback_breaker_state_persist_failed` 带 `attempt=1`，恢复可写后下一个监督 tick
   不再报错；随后重启 daemon，`arx-runner breaker status` 仍应显示 frozen。
6. **FR-2 / FR-7**：在 testnet 跑一个 HYBRID 策略，制造「入场单半仓成交 → 被止损 →
   余量成交」，确认新持仓的追踪止损会随回撤触发；并确认持仓期间的行情新高会进入回撤基准
   （下一次入场在回撤超限时被拒）。
