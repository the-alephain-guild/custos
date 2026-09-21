# 29 - containment-must-look-before-it-claims

> **Status**: ✅ Completed
> **Created**: 2026-09-21
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-fix03-recheck-review.md` RR-8（验收未覆盖的那一半）

fix 26 关掉了 RR-8 的「撤销增险挂单 + 保留减仓保护」，并**显式声明**「有确认、有重试」本轮不做，
理由是「确认需要一条可信的 venue 状态读取回路，属于独立的工作面」。

## 先撤回那条理由

那条回路**已经在仓里**，而且正在生产路径上跑：

- `host.py:1159 _preserve_and_confirm_shutdown` —— 按 deadline 轮询 `_open_venue_state`，
  确认增险挂单归零才返回，超时抛错。
- `host.py:1200 _flatten_and_confirm_shutdown` —— 取消、平仓、要求**连续 N 次**读到空的 venue
  cache（`_shutdown_stable_polls`）才算确认，并且给刚发出的平仓留一个 ack 窗口
  （`_shutdown_close_retry_secs`）再重发，避免对场所刷请求。

两者读的都是 `runtime.cache`，也就是 NT 按场所执行回报维护的那份视图 —— 它正是「场所状态」在
本进程里的可信表示。所以 RR-8 缺的不是基础设施，是**熔断这条路没有用上关停那条路已有的确认**。

这是 C29 的一个反向实例：上一轮的 defer 理由到今天不一定还成立，**接着做之前先核一遍那条理由**，
而不是照抄。

## 根因

`host.py:1586 flatten_positions` 是一次性的：撤增险单、对每个 instrument 发 close，然后记
`positions_flattened` 就返回。**没有任何一步回头看**。报告的原话是「不能把提交平仓请求当作零风险
确认」，而现在的代码恰恰如此 —— 唯一的例外是 `nt_flatten_containment_unconfirmed`，它守的是
「一个都没看见」的情形，不是「发过请求之后到底成没成」。

第二个缺陷藏在同一行里：`instrument_ids` 为空时记 error `nt_flatten_containment_unconfirmed`。
这个分支把两种完全不同的状态合成了一个：

| 实际状态 | 现在记成 |
|---|---|
| 刚才的收缴成功了，现在确实没有持仓 | `nt_flatten_containment_unconfirmed`（error） |
| 启动期对账还没把账户已有持仓送过来，我们什么都看不见 | 同上 |

前者每个 tick 都会再报一次 error，而它其实是**成功**。这让那条日志作为信号失效 —— 一条永远在响
的警报等于没有警报（生态 #50 的形态）。

## Fix 1: 收缴发出请求之后必须回头看 [P1]

**Files**: `src/custos/engines/nautilus/host.py`、
`tests/test_containment_must_look_before_it_claims.py`（新建）

1. `flatten_positions` 在发出撤单与平仓之后，进入一个**有期限**的确认循环，判据是
   「没有持仓 **且** 没有增险挂单」。减仓保护单可以留着，它们不是失败 —— 这与 fix 26 的取舍一致。
2. 循环内重发：仍在的增险挂单再撤一次，仍在的持仓再 close 一次，重发之间留一个 ack 窗口
   （复用 `_shutdown_close_retry_secs` 的做法），不对场所刷请求。
3. 期限用新常量 `_containment_confirm_secs = 5.0`，轮询复用 `_shutdown_poll_secs`。
   监督 tick 默认 10 秒（`subcommands/start.py:188`），5 秒的确认稳稳落在一个 tick 内，
   不会让两次 tick 叠在一起。
4. **超时不抛，记 error 返回**。抛出会沿 `EngineSafetySupervisor._tick` 的 tripped 分支一路
   传到监督任务，而 daemon 对「长跑任务意外退出」是 fail 整个进程的 —— 一个场所慢会掀掉所有
   实例的监督。记 `nt_containment_not_confirmed` 并带上剩余数量；breaker 仍冻结（新单进不来），
   下一个 tick 若仍触发会再收缴一次。
5. `_open_venue_state` 抛错**不是确认**（C9）。按 fix 26 的同一纪律记
   `nt_containment_state_unreadable` 并退出循环 —— 不能因为读不到就当没事，也不能因为读不到
   就卡在这里。

## Fix 2: 把「成功了」和「什么都没看见」分开 [P1]

**Files**: 同上

`nt_flatten_containment_unconfirmed` 只应在**没有发出过收缴**、且当前什么都看不见时出现。
收缴之后确认到位，记 `nt_containment_confirmed`（info）。

判据不能凭数据推 —— 「现在没有持仓」这一件事在两种状态下长得一模一样。要分开，得知道
**这一轮有没有发出过请求**，这是本次调用自己的事实，不需要跨 tick 保存任何状态。

## 明确不做

- **跨 tick 的收缴账本**（记下「上次要求收缴 N 个，这次看还剩几个」）。原因：breaker 的
  `tripped` 是每 tick 按**当前**数字重算的（`fallback_breaker.py:201`
  `tripped=reason is not None`），notional 超限被平掉之后就不再 tripped，`flatten_positions`
  也就不再被调用 —— 跨 tick 的账本在最常见的成功路径上根本不会被读第二次。确认放在调用内
  才对得上生命周期。
- **RR-8 报告里「受控停止流程」那半句**。报告根因段提到「也没有调用受控停止流程」，但验收段
  要的是「撤销增险挂单 + 保留减仓保护 + 有确认有重试」。熔断后是否该顺带停机是一个策略决定，
  不是缺陷 —— 现有设计是冻结 + 平仓、保留节点，让运维决定停不停。本 plan 不改这个决定。

## 验证清单

- [x] 失败测试先红后绿（7 条先红 1 条先绿，后 2 条二次先红）
- [x] 平仓请求发出后**确实回头读了** venue 状态（R1 撤掉确认循环，7 条红）
- [x] 持仓不消失时会重发，且在期限内结束、不挂死（R2）
- [x] 超时记 error 且**不抛**（R3）
- [x] 读不到 venue 状态不算确认，且不卡住（R5）
- [x] 减仓保护单在确认判据里不算残留（R4）
- [x] 收缴成功不再记 `nt_flatten_containment_unconfirmed`（R1/R6）
- [x] 扰动验证各用独立 `PYTHONPYCACHEPREFIX`（C15）
- [x] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 收缴后确认与重试 | P1 | ✅ | 2026-09-21 | RR-8 验收剩余半 |
| 2 分开成功与看不见 | P1 | ✅ | 2026-09-21 | 同一行里的第二个缺陷 |

## 偏离与改进日志

### DEVIATION: 还是加了一点跨 tick 状态，一个 bool
- **等级**: 低
- **原因**: 计划的「明确不做」里写着不做跨 tick 的收缴账本，理由是 notional 超限被平掉之后
  breaker 不再 tripped、`flatten_positions` 不会被调第二次，账本读不到第二次。**那条推理对
  notional 成立，对 drawdown 不成立** —— 权益不会因为平了仓就回来，
  `fallback_breaker.py:201` 每个 tick 按当前数字重算，于是 drawdown 触发的熔断在成功收缴
  之后仍然每 tick 调一次 `flatten_positions`，每次都看到空、每次都记一条 error 说「未确认」。
  一条永远在响的警报等于没有警报（生态 #50）。
- **决定**: 加 `_containment_confirmed: set[str]`，每实例一个 bit。看不见东西时：确认过 →
  `nt_containment_still_clear`（info）；没确认过 → 保留原来的 error。这不是计划里拒掉的
  「上次还剩几个」账本，但它确实是跨 tick 状态，所以按偏离记。
- **影响**: `host.py` 两处 `_active_nodes.pop` 旁边各加一次 `discard`。实例 id 活得比节点长，
  不清掉会让新 generation 拿上一个节点的证据回答自己的问题 —— R6 扰动（去掉两处 discard）
  会让 `test_a_redeployed_instance_does_not_inherit_the_old_confirmation` 转红。

### DEVIATION: fix 26 那条 defer 理由今天不成立，先撤回再动手
- **等级**: 低（方法论，不是代码）
- **原因**: fix 26 写的是「确认需要一条可信的 venue 状态读取回路，属于独立的工作面」。
  动手前按 C29 的纪律核了一遍：那条回路早就在仓里，而且在生产路径上跑
  —— `_preserve_and_confirm_shutdown`（`host.py:1159`）与 `_flatten_and_confirm_shutdown`
  （`:1200`），两者读的都是同一个 `runtime.cache`。
- **决定**: 本 plan 开头显式撤回那条理由，再复用同一套形状（deadline + 轮询 + 重发 + ack 窗口）。
- **教训**: 上一轮的 defer 理由是当时的判断，不是永久事实。接着做之前先核那条理由，
  而不是照抄它去论证「所以还是做不了」。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-21
- **总 Fix 数**: 2
- **偏离数**: 2（均为低风险，见上）
- **验证结果**: 全部通过
- **实施 commit 范围**: `53d59c8`（plan）..HEAD
- **契约影响**: 无。新增三条日志事件（`nt_containment_confirmed` /
  `nt_containment_not_confirmed` / `nt_containment_state_unreadable` / `nt_containment_still_clear`），
  wire 契约、schema、authority 资产均未动。
- **红线守护**: 四条全数守住。本 fix 直接服务红线 0.3 —— 熔断是失联时本地继续守护的最后一步，
  「发出请求」与「确认收缴」之间那段差距正是这条红线以前没有覆盖的地方。

### 测试条数（`pytest --collect-only` 实跑）

| 测试文件 | 条数 |
|---|---|
| `tests/test_containment_must_look_before_it_claims.py` | 9 |
| `tests/test_plan_closeout_counts.py` | 79 |

### 验收条款逐句对照（C27）

RR-8 验收原文：「制定有确认、有重试的熔断控制流程，撤销所属实例的风险增加挂单并保留必要减仓
保护；不能把提交平仓请求当作零风险确认。」

| 验收分句 | 落点 | 覆盖它的测试 |
|---|---|---|
| 撤销所属实例的风险增加挂单 | fix 26 | `test_a_resting_risk_increasing_order_is_cancelled_by_containment` |
| 保留必要减仓保护 | fix 26 + 本轮确认判据 | `test_protective_orders_survive_containment`（26）+ `test_protective_orders_do_not_block_confirmation`（本轮） |
| 有确认 | 本轮 | `test_containment_reads_the_venue_back_before_it_claims`、`test_a_risk_increasing_order_that_survives_blocks_confirmation` |
| 有重试 | 本轮 | `test_a_position_that_will_not_close_is_asked_again` |
| 不把提交请求当作零风险确认 | 本轮 | `test_a_position_that_never_closes_ends_loudly_and_on_time`、`test_an_unreadable_venue_is_not_confirmation_and_does_not_wedge` |

至此 RR-8 的验收五个分句全部有测试对应，fix 26 那条「本轮不做」的声明可以收回。

**报告根因段提到的「也没有调用受控停止流程」不在验收里，本轮也不做**：熔断之后是否顺带停机
是策略决定而非缺陷。现有设计是冻结 + 平仓 + 保留节点，让运维决定停不停；本 plan 不推翻它。

### 扰动验证

六处，各用独立 `PYTHONPYCACHEPREFIX`（C15），还原从 scratchpad 备份拷回（C15 续编）。

| # | 把修复改回什么 | 转红的测试 |
|---|---|---|
| R1 | 发完请求就返回，不确认 | 7 条 |
| R2 | 仍在的持仓/挂单不再重发 | `..._is_asked_again` |
| R3 | 超时改成抛异常 | 2 条 |
| R4 | 减仓保护单也算残留 | `..._do_not_block_confirmation` |
| R5 | 读不到 venue 状态当成确认 | `..._is_not_confirmation_and_does_not_wedge` |
| R6 | 节点消失后不清确认位 | `..._does_not_inherit_the_old_confirmation` |

R6 第一次写的锚点在文件里匹配 0 次，替换器按 C10 的规矩直接拒绝、没有去猜；那一轮随后跑出的
9 passed 是**未扰动**的结果，不算证据，换成正确锚点（两处 `discard`）重跑才转红。这一条留在
这里，是因为「扰动脚本没跑成」与「扰动不咬」在输出上长得很像（生态 #50 的形态）。

### 功能验证（主路径）

1. 在 testnet 上部署一个策略，挂一张增险单并持有一个仓位，然后把 runner safety policy 的
   `max_notional` 调到当前敞口以下，让熔断在下一个监督 tick 触发。
2. 日志里应当先看到 `nt_containment_cancelled_risk_increasing_orders` 与
   `positions_flattened`（这两条是「已请求」），随后看到 `nt_containment_confirmed`
   （这条才是「已确认」，带 `protective_order_count`）。
3. 若场所拒绝平仓，应当在约 5 秒内看到 `nt_containment_not_confirmed`，带上剩余的
   `position_count` 与 `risk_increasing_order_count`；监督循环**不应**因此退出，
   daemon 继续运行，下一个 tick 若仍触发会再收缴一次。
4. 收缴成功之后若熔断仍因回撤保持触发，后续 tick 应当记 `nt_containment_still_clear`（info），
   而不是反复报 `nt_flatten_containment_unconfirmed`（error）。
