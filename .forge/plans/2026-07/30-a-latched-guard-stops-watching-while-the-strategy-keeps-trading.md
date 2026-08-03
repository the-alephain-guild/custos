# 30 — 熔断闩住后守卫就不看了，可策略还在交易

> **Status**: ⏳ In Progress —— **三者中排第一**（2026-08-03 定，见 §三者顺序）：28 主体已验、29 的候选
> 原因未复现，只有本 plan 的缺陷仍完整存在且可按需复现。**层级已定：A + C**（owner 2026-08-03，
> 见 §决策已定）；Foundation Scan 改了原代价表的两格，见 §Foundation Scan
> **Created**: 2026-08-01
> **Project**: custos (`tesseract-trading/custos/`)
> **Depends on**: 无 —— 现有代码即可复现，且已在真机复现
> **Blocks**: 无人值守长跑；也让 Plan 22 的「本地敞口守卫」在闩住之后名存实亡
> **For Claude**: `/forge:execute`；**先定 §决策 的修复层级再动手**
> **multi_session_scope**: false

## 上下文 (Context)

### 真机时间线，2026-08-01

在测 `max_total_notional` 该设多少的时候撞见的。当时 spec 还没有 `risk_config`，守卫用的是
最严默认 $200：

```
02:44:57.127  notional_breach            对账接管来的 353 USDT 空仓 > 200
02:44:57.127  offline_exposure_guard_latched
03:25:00      ENTRY  size=449.24
03:36:00      ENTRY  size=449.17
04:21:00      ENTRY  size=449.11
05:24:00      ENTRY  size=449.12
```

闩住之后的 **2 小时 40 分**里，策略自己开了 4 笔仓，**每一笔都是那条把守卫闩住的上限的两倍多**，
而守卫一次都没有再看过。

### 为什么会这样，两处代码合起来看

`safety.py:299` 的 `run()` 在全部 watched 都闩住时 **return**：

```python
if self._watched and all(watched.latched for watched in self._watched.values()):
    _log.error("offline_exposure_guard_latched", spec_ids=sorted(self._watched))
    return
```

`evaluate_once()` 也会 `if watched.latched: continue` 跳过。而 `engine_safety.py:57-58` 的 flatten
**只在判定跳闸的那一次**发生：

```python
if verdict.tripped:
    await self._engine.flatten_positions(instance_id, verdict.reason or "fail_closed")
```

所以闩住 = 平一次 + 不再接受新 generation + 守卫循环退出。**策略进程没有被碰过**，它在引擎里继续
跑自己的信号。

### 缺的是那句推理，不是那段代码

`safety.py` 模块 docstring 把设计意图写得很清楚：

> A trip latches: the position is flattened once and no further generation is admitted,
> because flattening alone would be undone by the next generation the operator publishes.

这句话里藏着一个前提：**敞口只会经由「操作者发布新 generation」回来**。真机证据说这个前提是错的
—— 敞口是策略自己按信号开回来的，全程不需要任何 generation。于是「拒绝新 generation」这道闸门
拦的是一条当时没人走的路，而真正在放敞口进来的那条路没有闸门。

代码忠实实现了 docstring 说的东西；错的是 docstring 那句 *because*。

### 为什么这比「操作不便」严重

「latched」这个词读起来像**已控制住**。`make status` 也会给出 unhealthy，看上去像停了。实际是：
仓位在开、敞口在涨、守卫已经不在了 —— 一个**看起来比裸奔更安全**的裸奔。这正是 PS lesson #15
说的那种事：为消除盲区而生的保护机制，自身的失效路径复制了它要消除的那个盲区。

## 决策 (Decision) —— 先定层级再动手

三条路，代价和语义都不同：

| # | 做法 | 换来什么 | 代价 |
|---|---|---|---|
| A | 跳闸时把策略停掉（不只是平一次） | 敞口真的不再回来 | 「停策略」这个动作目前不在 `EngineSafetyPort` 上；且停掉 = 挂单也没人管了，与 Plan 29 的孤儿单问题耦合 |
| B | 闩住后守卫继续跑，敞口每次回到上限之上就再平一次 | 守卫名副其实；不需要新原语 | 变成平仓/开仓拉锯，可能反复吃手续费；要有节流（PS lesson #13 的 rate guard）|
| C | 闩住后至少继续**看**并持续告警，不再动手 | 最小改动，消除「静默」 | 敞口照样在涨，只是这次有人喊 —— 无人值守时等于没修 |

**倾向 B + C**：守卫的职责是「让敞口不超过上限」，跳闸后停止观察是把职责交还给了没人。B 需要
节流与「平不掉怎么办」的处理，那部分与 Plan 28 的 escape hatch、Plan 29 的确认回路是同一片地。
**但这三份 plan 会互相踩，顺序要先定。**

A 看起来最彻底，但它把「停策略」这件事引进安全面，而这条通道现在连撤单确认都还没有
（Plan 29），停下来留一地挂单未必比 B 安全。

## Foundation Scan（2026-08-03 实测）—— 改了上面代价表的两格

起草时的代价表有两处是推的，不是量的。实测结果如下，**上面那张表按本节读**。

### 缺陷仍在，但行号已挪

| 计划原文 | 实测位置 | 内容 |
|---|---|---|
| `safety.py:299` return | `safety.py:304-309` | `if self._watched and all(latched): _log.error(...); return` |
| `evaluate_once` 跳过 | `safety.py:201-202` | `if watched.latched: continue` |
| `engine_safety.py:57` | `engine_safety.py:57-58` | `if verdict.tripped: await flatten` —— 未变 |

### 更正 1：A 的代价被高估了

原文写「『停策略』这个动作目前不在 `EngineSafetyPort` 上」——这半句对，
`engine_safety.py:12-15` 确实只声明 `get_engine_status` 与 `flatten_positions`。但它推出的
结论（A 需要引入新原语）不成立：

- `daemon.py:124` 与 `:151` 把**同一个 engine 对象**分别交给守卫和 reconciler；
- 该对象满足 `OfflineEngine`（`reconciler.py:63-80`），其中已有
  `async def stop(deployment_instance_id)` 与 `def attached(deployment_instance_id)`；
- 两个 host 都实现了这两个方法（`engines/nautilus/host.py:147`/`:154` 与 `:551`/`:583`）。

所以 A 要的是**加宽守卫自己的 Protocol**，不是新原语、不是改对象图。

### 更正 2：B 无需改 breaker，但也没有收敛点

`FallbackBreaker.evaluate()` 算 `tripped` 时不看 `_frozen`——`fallback_breaker.py:142` 的
`not self._frozen` 只挡了那条日志，verdict 照常返回 `tripped=True`。于是只要
`evaluate_once` 不再跳过已闩住的条目，`EngineSafetySupervisor` 每个 tick 都会再平一次，
而全仓没有任何节流（`TICK_SECS = 5.0`）。B 能把「敞口超限的持续时间」压到一个 tick，
压不住敞口本身——策略会照自己的信号再开回来。

### 爆炸半径为零

`EngineSafetySupervisor` 全仓只有一个消费者，就是这个离线守卫（grep 实证：
`src/` 内除自身定义外仅 `offline/safety.py` 引用）。签名通道现在不用它。

### 一条降级了的顾虑

原文担心「停下来留一地挂单未必比 B 安全」，依据是 Plan 29 的孤儿单。Plan 29 那 46 小时
连跑的结论是 19/19 撤单确认、重启后交易所零 resting 单——这个顾虑比起草时弱。

## 决策已定（owner，2026-08-03）：A + C

**跳闸时先平一次（保持现状），随即停掉该部署；守卫循环不退出，继续确认这一停有没有真的
生效。** 选它的理由是三条里只有它收敛：策略停了就不会再开仓，而 B 是一场守卫与策略在同一
进程里没有终点的拉锯。

C 在 A 之下的含义随之收窄，也更准：闩住之后要继续看的不再是敞口数字，而是**这一停有没有
落地**。停完再去问引擎的组合，只会因为实例已经不在而 fail closed，每个 tick 重新平一次
空气——那正是 B 的病。所以「继续看」问的是 `attached()`，即 C12 那句话：**要回答「它还
活着吗」，就去问那个活着的东西本身，不要问记录它的那个字典。**

不做的事，写清楚以免下一个读者以为是漏了：

- **不 `release()` 已闩住的条目。** `allows_new_generations()` 靠 `_watched` 里还有闩住的
  条目才返回 False（`safety.py:147-150`）；一旦 release，`_watched` 空了，代次闸门会**反向
  解闩**。停掉部署与继续拒绝新代次是两件事，都要成立。
- **不改 `core/engine_safety.py` 的语义。** 它叫「评估一份快照并就地遏制一次跳闸」，A 的
  「跳闸即结束这个部署」是离线通道自己的判断，落在离线通道自己的 wrapper 里。
- **不改本通道的退出语义。** 守卫停完所有部署后照常 tick，lane 不因此退出。

## Tasks

### Task 1 — 先写会红的测试（A 与 C 各一条）

1. 闩住之后 `run()` 不退出，且守卫会去 `stop()` 那个部署；
2. 停不掉（引擎仍 `attached`）时**每个 tick** 都要喊，不是喊一次。

两条都必须在实现前跑红。

### Task 2 — 改写两条把缺陷当契约的既有测试

`tests/test_offline_lane_safety.py:295` 的
`test_the_tick_ends_once_every_watched_deployment_is_latched` 与 `:352` 的
`test_the_tick_ends_against_an_engine_that_never_answers`，断言的正是
「全闩住 → `run()` 返回」——即本 plan 要修的行为。它们不是碍事的测试，是**上一版设计的
留痕**，所以改写而不是删除，并在改写处写明它们原本断言的是什么。

同型前例：Plan 26 也撞见过一条把症状写成契约的既有测试。

### Task 3 — 落 A：闩住即停掉部署

- `offline/safety.py` 新增 `OfflineSafetyEngine` Protocol = `EngineSafetyPort` + `stop` +
  `attached`，`OfflineExposureGuard` 收它；
- `evaluate_once` 对已闩住的条目走遏制分支，不再 `continue`；
- `run()` 删掉「全闩住即 return」那一段，连同 `offline_exposure_guard_latched` 那条日志
  ——它的字面意思是「守卫就此停手」，改完之后这句话不再为真，留着就是假话。

### Task 4 — 落 C：停机没确认就一直喊

- `attached()` 仍为真 → 每 tick 记 ERROR 并**重试** stop（不是喊一次就算）；
- stop 超时 → 记 `..._unconfirmed`，不假装已停，也不停下 tick；
- stop 抛异常 → **传播**，与既有「平仓失败不吞」同一契约
  （`tests/test_offline_lane_safety.py:362`）。

### Task 5 — 两个 host 都要被测到

沿用 Plan 26/27/28 的惯例：断言 `NtTradingNodeHost` 与 `SandboxSimulationHost` 都满足加宽
后的 Protocol，避免只有 NT 那条路被覆盖。

### Task 6 — 不变量：停了也不许解闩

守卫停掉部署之后，`allows_new_generations()` 必须仍为 False。

## 失败模式覆盖契约

| # | 场景 | 期望 |
|---|---|---|
| FM1 | `stop()` 超时不返回 | 记 unconfirmed，tick 继续，不假装已停 |
| FM2 | `stop()` 抛异常 | 传播，不吞（同 flatten 既有契约） |
| FM3 | 停机后引擎仍 `attached` | 每 tick 告警 + 重试 stop |
| FM4 | 闩住的部署从未 attach 过 | 不停、不喊（不制造噪声） |
| FM5 | 守卫停掉部署之后 | `allows_new_generations()` 仍为 False |
| FM6 | wedged engine 走 fail_closed 闩住 | 同样触发停机路径 |

## 复现 (Reproduce)

不需要特殊构造，把上限设到当前仓位之下即可：

```yaml
# philosophers-stone/deploy/custos/conf/supertrend/spec-override.yaml
risk_config:
  max_total_notional: 100        # 低于 10% 仓位
```

`make -C deploy/custos start-detached MODE=testnet`，等 `fallback_breaker_tripped`（跳闸那一刻），
然后放着。下一次趋势翻转时策略会照常开仓，日志里不会再有任何守卫的声音。

> **改完之后这条 repro 的判据变了**：跳闸后应当紧跟一条
> `offline_exposure_latched_deployment_stopping`，此后策略不再开仓。原来这里写的是等
> `offline_exposure_guard_latched`——那条日志随 Task 3 一并删除，因为它的字面意思
> （守卫就此停手）在修完之后不再为真。

## 三者顺序已定（2026-08-03）：本 plan 排第一

Plan 28 / 29 / 30 改同一条平仓路径，当初记的是「先定顺序再开第一份」。46 小时真机证据出来后，
顺序是清楚的：

| plan | 缺陷现在还在不在 | 结论 |
|---|---|---|
| 28 逃生口 | 主体**已验**（10/10 真机触发），剩下的是一次性上限与 flatten 路径 —— 两者都**要不可强求的条件**才触发 | 不动，等条件 |
| 29 撤单确认 | 三个候选原因 46 小时**一个都没复现**；查出来的重复撤单已修 | Task 2-4 不开，留判据 |
| **30 闩住后停摆** | **仍然完整存在**，且可按需复现 | **排第一** |

判据很朴素：28 和 29 现在都缺"缺陷还在"的证据，而 30 的缺陷是这轮**顺手就撞见的**，代码路径也已
读实（`safety.py:299` return + `evaluate_once` 跳过 + `engine_safety.py:57` 只在跳闸那一 tick 平一次）。
先修看得见的那个。

另外它和另外两份的耦合比原先估计的小：29 的 Task 2-4 不做了，28 待的是外部条件，本 plan 要动的
`safety.py` / `engine_safety.py` **不在**它们俩改的那条策略侧平仓路径上。原先「会互相覆盖」的顾虑
在 29 撤回 Task 2-4 之后基本消失。

~~**仍未定的是 §决策 里的 A / B / C。**~~ **已定：A + C**（owner，2026-08-03，见 §决策已定）。
原文那句「B 需要节流，而节流所依赖的『平仓确认』正是 29 选项 2 撤回的那部分」仍然成立——它正是
不选 B 的理由之一：B 没有收敛点，而它唯一可能的收敛机制在 29 已经撤回。

## 完成情况（2026-08-03）

- **完成日期**: 代码侧 2026-08-03；**真机未验**
- **总 Task 数**: 6（全部落地）
- **实施 commit**: `c2413cd`（决策 + 任务）· `b291124`（实现 + 测试）
- **验证结果**: `uv run pytest tests/` = **2297 passed / 25 skipped / 1 xfailed / 1 failed**；
  `ruff check src/ tests/ scripts/ packages/` 全绿

**那 1 条 failed 与本次改动无关，已实证**：
`test_runner_deployment_command_golden.py::test_golden_hash_matches_snapshot_sidecar_and_optional_sibling`
比对的 `SIBLING` 指向**另一个仓库**（`../crucible-rust/docs/authority/runner-deployment-command-golden-v1.json`），
两边字节在 index 3332 处差一个换行。把本次改的三个文件 `git stash` 回 HEAD 后重跑，该条**照样红**
——所以是 workspace 内的跨仓 golden 漂移，不是本次引入。它在独立 clone 下不触发
（`if SIBLING.is_file()`）。修它要跨仓协同，不属本 plan 范围，见 §遗留项 4。

**`make check` 在主干上本来就是红的**，与本次无关：`ruff format --check` 报
`src/custos/core/runner_fact.py` 与 2 个 pinned integration test 需要重排——这三个文件被
`docs/authority/**` 的资产索引按字节 pin 住，跑 `make fmt` 会修好 gate 但毁掉证据链
（`.forge/README.md` §Deferred 1 副作用 + `historical-lessons.md` C6）。本次只对**自己改过的
三个文件**跑了 `ruff format --check`，全部已格式化。

### 改了什么

| 位置 | 改动 |
|---|---|
| `offline/safety.py` | 新增 `OfflineSafetyEngine` Protocol（`EngineSafetyPort` + `stop` + `attached`） |
| `offline/safety.py` | `evaluate_once` 对已闩住的条目走 `_end_deployment`，不再 `continue` |
| `offline/safety.py` | `_end_deployment`：`attached()` 为真则告警 + 停机（有界）；超时记 unconfirmed；异常传播 |
| `offline/safety.py` | `run()` 删除「全闩住即 return」，连同 `offline_exposure_guard_latched` 日志 |
| `offline/safety.py` | 类 docstring 改写——原文那句 *because* 是本 plan 判定为错的那句 |

`core/engine_safety.py` 与 `core/fallback_breaker.py` **未改**，签名通道语义不变。

### 测试条数（取自 `pytest --collect-only`，非手写）

| 测试文件 | 条数 |
|---|---|
| `tests/test_offline_lane_safety.py` | 46 |
| `tests/engines/nautilus/test_nautilus_host_implements_engine_protocol.py` | 4 |
| `tests/test_plan_closeout_counts.py` | 15 |

上表合计 65 条。这是三个文件的**全量**条数，不是本 plan 的增量：本 plan 新写 12 条、
改写 2 条（那 2 条见下）。

`tests/test_offline_lane_safety.py` 上一次被 plan 22 数为 38，本次重数为 46 ——
按 `progress-management.md` §"数字类声明必须来自实跑"，重数写在这里，**不去改 plan 22 那一行**。
`tests/test_plan_closeout_counts.py` 正是靠这条规矩红的，红得对。

第三行需要解释，否则下一个读者会以为是凑数：**本 plan 并没有往那个探针文件里写测试**，
但它有两个 `parametrize` 跑在「带计数表的 plan」这份清单上，而本 plan 恰好新增了一张计数表
——于是每个参数化各多一个参数，13 变 15。它被 plan 26 数过，现在由本 plan 重数。
再加行不会继续涨：清单看的是"这份 plan 有没有表"，不是"表里有几行"。

### 两条被改写的既有测试

`tests/test_offline_lane_safety.py` 里的
`test_the_tick_ends_once_every_watched_deployment_is_latched` 与
`test_the_tick_ends_against_an_engine_that_never_answers` 断言的是「全闩住 → `run()` 返回」
——也就是本 plan 要修的行为本身。改写为断言相反的事，并在 docstring 里写明它们原本断言的是什么。
同型前例是 Plan 26（一条既有测试把症状写成了契约）。

### 失败模式覆盖

| # | 场景 | 测试 |
|---|---|---|
| FM1 | `stop()` 超时不返回 | `test_a_stop_that_never_returns_is_recorded_rather_than_assumed` |
| FM2 | `stop()` 抛异常 | `test_the_tick_does_not_swallow_a_failure_to_stop` |
| FM3 | 停机后引擎仍 `attached` | `test_a_deployment_that_will_not_stop_is_reported_on_every_tick` |
| FM4 | 闩住的部署从未 attach | `test_a_latched_deployment_the_engine_never_held_is_left_alone` |
| FM5 | 停机不得解闩 | `test_stopping_the_deployment_does_not_unlatch_the_generation_gate` |
| FM6 | wedged engine 走 fail_closed | `test_an_engine_that_never_answers_is_stopped_too` |

**FM3 配了证伪搭档** `test_a_deployment_that_did_stop_is_not_stopped_again`：不配的话，
FM3 的「每 tick 都喊」可能绿在「守卫无条件重复」上，而不是绿在「引擎确实还握着它」上。

**两处探针都实跑证伪过**：把 `_end_deployment` 里的 `attached()` 早返回删掉，FM4 与它的证伪
搭档双双转红；`isinstance` 那两条对只有旧两个方法的引擎判 False（实跑三个类分别为
False / False / True）。

### 红线 gate 满足度

按 lesson #40：红线名是设计意图，兑现声明是能力实现，两者不混。

| 红线 | code test 覆盖 | runtime wire | 真机 | defer |
|---|---|---|---|---|
| 0.3 失联≠停止 | ✅ 12 条新测试覆盖停机/告警/不解闩/超时/异常五条路径 | ✅ 守卫由 `daemon.py:124` 组合，engine 与 reconciler 同一对象，无需新接线 | ❌ **未验** | 无 |
| 0.1 Key/KEK 不出进程 | 不适用 | 未触碰 | — | — |
| 0.2 G6 host gate | 不适用 | 未触碰 | — | — |
| 0.4 Decimal money | 不适用 | 未触碰（未新增任何 money 运算） | — | — |

**0.3 这一行只兑现到「闩住之后守卫不再停摆」**。它不声称敞口从此不会超限——策略在跳闸到停机
之间开的仓仍然会发生，本 plan 收窄的是「此后无人看管」那一段。

### 遗留项

1. **真机未验**。判据写在 §复现：跳闸后应紧跟
   `offline_exposure_latched_deployment_stopping`，此后策略不再开仓。按 Plan 25 的先例，
   真机证据到手前本 plan 不标 ✅。
2. **停机之后交易所可能留 resting 单**。Plan 29 的 46 小时连跑给的是「正常关停」路径的证据
   （19/19 撤单确认），**不是**「守卫强制停机」路径的。这条要在真机复验时一并看。
3. **告警不节流**。`attached()` 一直为真时每个 tick（5s）一条 ERROR。这是有意的——「喊一次
   然后沉默」正是本 plan 要消除的形状——但真机上若确实出现停不掉的引擎，日志量要复看一次。
4. **跨仓 golden 漂移**（本 plan 顺手撞见，不属本 plan 范围）：custos 与 crucible-rust 两侧的
   `runner-deployment-command-golden-v1.json` 字节不一致。它只在 workspace checkout 下红，
   独立 clone 不触发，所以 CI 大概率一直是绿的——与 C7「陈旧产物互相印证出的假绿」同族，值得
   单独起一条。
