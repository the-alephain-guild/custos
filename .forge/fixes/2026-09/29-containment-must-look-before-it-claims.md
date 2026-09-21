# 29 - containment-must-look-before-it-claims

> **Status**: 🔲 Not started
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

- [ ] 失败测试先红后绿
- [ ] 平仓请求发出后**确实回头读了** venue 状态（撤掉确认循环会红）
- [ ] 持仓不消失时会重发，且在期限内结束、不挂死
- [ ] 超时记 error 且**不抛**（抛出会掀掉监督任务）
- [ ] 读不到 venue 状态不算确认，且不卡住
- [ ] 减仓保护单在确认判据里不算残留（fix 26 的取舍不被本轮推翻）
- [ ] 收缴成功不再记 `nt_flatten_containment_unconfirmed`
- [ ] 扰动验证各用独立 `PYTHONPYCACHEPREFIX`（C15）
- [ ] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 收缴后确认与重试 | P1 | 🔲 | | RR-8 验收剩余半 |
| 2 分开成功与看不见 | P1 | 🔲 | | 同一行里的第二个缺陷 |

## 偏离与改进日志
