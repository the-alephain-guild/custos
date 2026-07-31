# 28 — reduce-only 平仓被拒时没有出路（demo 撮合引擎的已知缺陷，lesson #14 的手工绕法待固化）

> **Status**: ⏳ In Progress —— 常规出场路径已落地并有真机证据（`9f38691`）；**Task 1 已补**
> （`6f0c0e1`，起因是跳过它当天就在生产里出事，见 §2026-07-31 事故）。
> 未做: Task 4 的另外两条平仓路径（熔断 flatten / `emergency_close`）、Task 5（sandbox host 断言）。
> `6f0c0e1` 的两处修复**尚未真机复验**
> **Created**: 2026-07-30
> **Project**: custos (`tesseract-trading/custos/`)
> **Depends on**: 无 —— 现有代码即可复现（需 demo 环境处于 reduce-only 损坏态）
> **Blocks**: 在 `demo-fapi` 上的平仓可靠性；lesson #14 记录的那次「平不掉」若重演，仍只能手工介入
> **For Claude**: `/forge:execute`；**先定 §决策 的门禁选型再动手**
> **multi_session_scope**: false

## 上下文 (Context)

owner 指示（2026-07-30）：**reduce-only 平单失败时，应当可以改用直接反方向下单。**

### 这条指示的实据来自本仓自己的教训

PS lesson #14（2026-06-03/04）记录过一次真实的"平不掉"：Binance **Demo** 上真实持有
`-0.0070 BTC`，`positionRisk` 有仓、`openOrders` **count=0**、one-way 模式 —— 此时 reduce-only 平仓
本该成功，却被 `-2022 ReduceOnly Order is rejected` 拒掉，策略与网页端**都**拒。最后用**非
reduce-only 市价 BUY 0.007** 一次成交净平。结论是 **Demo 撮合引擎 reduce-only 状态不一致**：普通下单
路径正常、reduce-only 路径损坏。

### 当前环境就是那个环境（实测，2026-07-30）

三轮 testnet 实跑的日志里，端点分布是：

| 端点 | 命中 | 用途 |
|---|---|---|
| `https://demo-fapi.binance.com` | 6 | **REST / 下单路径** |
| `wss://stream.binancefuture.com` | 12 | 行情流 |
| `wss://testnet.binancefuture.com` | 3 | WS |

**下单走的正是 `demo-fapi`** —— 与 lesson #14 同一个服务。行情来自 testnet 流，是个混合配置。

⚠️ 这三轮里 reduce-only **没有**失败：9 张单全部 `OrderAccepted`，`-2022` 零次。**这不构成"环境可靠"
的证据** —— lesson #14 记的是 demo 的 reduce-only 路径**间歇性**损坏，所以"那几分钟没坏"和"不会坏"是
两件事。起 plan 时我一度由"testnet ≠ demo"推断这个风险不适用于本账户，被 owner 纠正、并由上表证伪。

### 现有的两条平仓路径都硬编码 reduce_only

| 路径 | 位置 | 形态 |
|---|---|---|
| 出场单 | `packages/custos-strategy-toolkit-nautilus/…/adapter/execution.py:214-219` | market + `TimeInForce.IOC` + `reduce_only=True` |
| 紧急全平 | `…/adapter/strategy_core.py:327-368`（`close_position` 在 `:366`） | 逐仓 `cancel_all_orders` → `close_position(reduce_only=True, IOC)` |

### 但实际上是三条，且可达性不同（起 plan 时的 Foundation Scan 发现）

owner 指示把逻辑放 `strategy_core.py`。落点归属没问题，但**只放那里覆盖不到会真正开单的路径**：

| 路径 | 代码 | reduce_only | 离线通道可达？ |
|---|---|---|---|
| 常规出场 | toolkit `execution.py:214-219` | 硬编码 `True` | **可达** —— 出场信号（反转 / SL / TP）触发 |
| `emergency_close` | toolkit `strategy_core.py:327-368` | `True` | **不可达** —— 见下 |
| 熔断 flatten | **custos** `host.py:728` → NT `close_all_positions` | NT 默认 `True`（镜像内实测签名） | 可达，但**不是 toolkit 代码** |

两条实证：

1. **`emergency_close` 在本通道里没有调用方。** 它被声明为命令（`strategy_core.py:242`
   `_BASE_COMMANDS`），但 `supported_commands` 全仓**零消费方**，而 `src/custos/offline/daemon.py:203`
   的 docstring 自己写着「The lane publishes no facts, **holds no commands**, resolves no policies」——
   离线通道按设计不持有命令。它的 docstring 也自称「Crucible graceful-close layer」，是 sidecar 时代的
   遗留面。**fallback 只加在这里，在本通道里就是死代码。**
2. **熔断那条 flatten 不经过 toolkit。** `host.flatten_positions`（`:711-733`）调的是 NT 自带的
   `strategy.close_all_positions(instrument_id)`（`:728`，唯一调用点），其 `reduce_only` 默认 `True`。
   所以它**同样吃 -2022**，但改 `strategy_core.py` 碰不到它。

因此 fallback 的判定逻辑应当收敛成**一个共享 helper**（放 `strategy_core.py` 符合 owner 指示），由
**会真正开单的两条路径**各自调用：常规出场（`execution.py`）与熔断 flatten（需要 host 侧改为走 toolkit
的平仓路径，或在 host 侧复用同一个 helper）。`emergency_close` 一并接上，但要清楚它在本通道当前不可达 ——
接它是为了不留下两套语义，不是因为它现在会跑。

**代码已经知道 -2022 的存在。** `emergency_close` 的 docstring 明写先撤挂单是为了「不让 resting
reduce_only 占容量而引发 -2022 rejection」（`strategy_core.py:330-333`）。也就是说现有实现已经处理了
**容量型** -2022；本 plan 要处理的是**撤干净之后仍被拒**的那种 —— 引擎自身状态不一致，撤单救不了。

## 决策 (Decisions)

### 这个改动移除的正是一层有意的保护，所以门禁就是全部设计

`create_exit_order` 的 docstring 把 `reduce_only` 的用途写得很直白（`execution.py:177-180`）：

> reduce_only protects the money path: if an exit is re-submitted (e.g. after the close gate's
> in-flight window) while the local cache still lags a fill that already closed the position, the
> venue rejects the duplicate instead of opening a reverse position.

**换成普通单就等于把这层保护拆掉。** 若 fallback 写成「平单失败 → 转普通反向单」，那么在
「持仓其实已经平掉、只是本地 cache 滞后」的情形下，它会**开出一个反向仓** —— 正是 lesson #46 点名的
危险。所以本 plan 的难点不在"怎么下那张单"，而在**凭什么断定现在可以下**。

### 可行性约束：lesson #14 的权威判据，策略层现在拿不到

lesson #14 的判据是「查 `positionRisk` + `openOrders` 两个 REST 端点，看到真实持仓 + 零挂单 +
reduce-only 被拒」。但 **策略层没有任何 REST 客户端**（grep 实证：
`packages/custos-strategy-toolkit-nautilus/src/` 下无 `httpx` / `requests` / `fapi/v1` /
`positionRisk`；唯一命中是 `filter_manager.py:138` docstring 里的 "subscription requests"）。策略只能
看 NT 对账后的视图。

> **以下 A / B / C 已被 owner 的指定算法取代（见下一节），保留为背景说明，不要照它们实施。**

于是门禁有三种可能：

- **A —— 只用现有材料构造门禁。** 触发条件全部来自 NT 侧：① 拒单原因被分类为「reduce-only 被拒」
  （非保证金、非精度）；② 已经完成过一轮 `cancel_all_orders` 且确认无 resting 挂单；③ NT 仍报该仓
  open；④ 一次性 + 冷却，绝不每 tick 重试（lesson #13）。数量取 NT 报的仓位量。
  **代价**：③ 依赖的正是可能滞后的那个 cache —— 门禁最弱的一环恰好是它要防的东西。
- **B —— 给策略层加权威 venue 查询**，照 lesson #14 原样实现（`positionRisk` + `openOrders`）。
  门禁最强，但把 HTTP 与交易所凭据引入策略层，与当前分层（凭据在 runner/vault、策略只见 NT）冲突，
  范围明显更大。
- **C —— 不在策略层做，放到 host / 引擎侧**（那里已经握着 venue 适配器与凭据）。可能拿得到权威状态而
  不污染策略层。**未验证**：NT Binance 适配器是否把这类查询干净地暴露出来，本 plan 起草时没有实证，
  选 C 前必须先 grep 清楚。

### ⚠️ 一个曾被提出、随后被实证推翻的解释：容量被占满

> **2026-07-31 更新：本节的假说已被同一次长跑的对照组推翻，保留原文以记录推理链。**
> `08:40` 平掉本 runner 自己开的多仓时，保护性止损**仍挂着**而 `reduce_only=True` 平仓单**被接受**
> （时序见 §完成情况）。若全额 resting 止损足以占满额度，那一单必被拒。故容量不是 `-2022` 的成因；
> 两次的差别在**持仓本身**（对账接管的旧仓 vs 本 runner 自开的新仓）。下面的推理据此作废。

2026-07-30 的实测查询发现，启动时交易所侧**同时挂着 4 张** `0.0070` 的 reduce-only 止损单（`17fb6e05`
/ `5dbb46ff` / `4ccc9459` / `a39769df`，分别来自前四轮），而持仓只有 `0.0070`。

本仓 lesson #13 记的机制正是这个：**resting reduce-only 单的累计数量不得超过持仓，超出即 `-2022`**。
4 × 0.0070 = 0.028 远超 0.0070，所以那时**任何**新的 reduce-only 平仓请求都必被拒 —— 包括交易所网页端
那个平仓按钮。清扫撤掉那 4 张后又挂了 1 张新的，容量**仍然**被占满（1 × 0.0070 = 持仓全额）。

**所以"网页端也平不掉"这条证据，不足以区分「引擎坏了」和「容量被自己的止损单占满」。** 两者都会表现为
reduce-only 被拒。可立即验证的判据：**先撤掉那张 resting 止损，再平仓** —— 若这样就能平掉，则本案属容量
问题，与 lesson #14 那次（零挂单仍被拒）不是同一回事。

这不推翻下面要做的 fallback（lesson #14 证明过零挂单仍被拒的情形真实存在），但它改变**优先级**：如果
真实场景多数是容量问题，那么"让撤单真正生效"（见 §Follow-up 的孤儿撤单问题）比 fallback 更能解决问题，
且不需要拆掉 reduce-only 这层保护。

### owner 指定的算法（2026-07-30，最终）

> 第一次用 `reduce_only=True`；**如果失败，第二次直接下方向相反、数量一致的订单**；不要反复重试
> `reduce_only=True`。

按此实现。「一次 reduce-only → 一次普通反向单 → 停」，**不做 reduce-only 重试循环**（反复重试正是
lesson #13 那次每 ~150ms 刷一单的形状）。

### 我先前的顾虑已被实证解决，记录在此以免下一个读者重走

我起 plan 时反对无条件 fallback，理由是 `-2022 ReduceOnly Order is rejected` 有**两种**互斥含义：
① demo 引擎坏了（真有仓仍被拒）；② **确实没有仓可减了**（本地视图过期）。②是这个错误码的正常含义，
误判成①时下的普通反向单会**开出一个新仓**。

owner 2026-07-30 补充的实测把本案的歧义消掉了：**在交易所网页端手动点平仓同样失败**。网页端的平仓走的
也是 reduce-only，所以这是一条独立于我们代码的证据。三方互证仓位真实存在：

| 证据 | 值 |
|---|---|
| 交易所报的保证金占用 | `locked = 459.60442000 USDT` |
| NT 报的持仓 | `net_position = -0.0070` |
| 交易所网页端平仓 | **也失败** |

即①成立、②被排除。这与 lesson #14 的判据形状一致（真有仓 + 平不掉 = 引擎侧问题），只是当时用 REST
查证，这次用网页端行为查证。

### 残余风险与它的天然边界（不改变上面的算法，只说明）

「其实已经平了但本地视图过期」这种情形依然存在，只是它的窗口被两件事天然收窄：常规出场路径**只有在
策略认为自己持仓时才会触发**，而 NT 在启动时对账、运行中靠 websocket 收成交。所以要落进危险区，需要
「NT 认为有仓 + 交易所其实没仓 + 恰好此刻出场」三者同时成立。

因此本 plan **不**为此加一道 venue 查询（会与 owner 指定的"第二次直接下单"冲突，也把范围放大）。改为
把它当成一个**必须被测到的失败模式**（见 Task 2 第 2 条）与一条**必须被记清的日志**：那张普通反向单
发出前，日志要写明「依据是什么、reduce-only 被拒的原文是什么、数量取自哪里」，事后可复盘。

> 备选（若将来发现误开真的发生过）：NautilusTrader 自己能问交易所 ——
> `LiveExecutionClient.generate_position_status_reports`（镜像内实测存在），对账用的就是它，凭据已在
> 那一层，不需要把 REST 搬进策略层。届时在下普通单前插一次查询即可，算法其余部分不变。

### 分类器要先能分辨「reduce-only 被拒」

`classify_rejection_reason`（`packages/custos-strategy-toolkit/src/custos_toolkit/risk/exchange_errors.py:42`）
当前把 `-2022 ReduceOnly rejected` 与 `-2019 margin` **一起**归进 `"logic"` 桶（见该文件 `:10` 的
docstring）。下游因此无法只对"reduce-only 被拒"反应。

**这是本 plan 的第一步，也是安全前提**：若不区分，fallback 会在**保证金不足**时也去开反向单 —— 那是把
一次拒单变成一个新仓位。消费方 `…/adapter/coordinators/order_reconciler.py` 已经在用这个分类器，是
自然的接入点。

## 目标 (Goal)

reduce-only 平仓在撤干净挂单后仍被引擎拒绝时，有一条**受严格门禁**的出路把仓位真正平掉；且这条出路在
"仓位其实已经没了"的情形下**不会**开出反向仓。

## 非目标 (Non-goals)

- **不改 `reduce_only` 作为默认**。它是正确的默认，本 plan 只加一条例外路径。
- **不给"所有平仓失败"兜底**。只针对「撤净后仍被拒 + 分类为 reduce-only 被拒」这一种。
  超时、断连、保证金不足、精度错误一律不触发。
- **不做重试循环**。一次性 + 冷却（lesson #13）；反复下普通单是刷单事故的形状。
- **不顺手改 demo/testnet 端点配置**。下单走 demo-fapi 这件事值得单独判断（lesson #14 的预防条款写的是
  「下单/平仓可靠性验证不依赖 demo-fapi」），但那是配置决策，不在本 plan。见 §Follow-up。

## 实现任务 (Tasks)

### Task 1 — 分类器分出「reduce-only 被拒」

给 `classify_rejection_reason` 增加一个与 `-2019` 等**分开**的类别，只覆盖 reduce-only 被拒。
先写会红的测试：喂 `-2022` 与 `-2019` 两条真实 reason 文本，断言它们**不再**落进同一桶。

真实 reason 文本从 lesson #14 的记录取（`-2022 ReduceOnly Order is rejected`），不要自己编造格式 ——
分类器吃的是交易所原文。

### Task 2 — 先写会红的失败模式测试（门禁的两个方向）

两条都必须在实现前是红的：

1. **该触发时触发**：撤净挂单后 reduce-only 仍被 `-2022` 拒 + 仓位仍 open → 下一张**非 reduce-only**
   反向单，数量等于仓位量。
2. **不该触发时绝不触发**（更重要）：仓位实际已平、本地 cache 滞后仍报 open 的情形下 → **不得**下普通
   反向单。这条就是 lesson #46 的危险，也是选项 A 最弱的一环，必须有测试钉住。

另外各写一条：`-2019` 保证金不足**不触发**；同一仓位在冷却窗口内**不重复**下单。

### Task 3 — 落 owner 指定的算法

「第一次 `reduce_only=True` → 失败则第二次下同数量反向普通单 → 停」。这张 fallback 单必须：

- **数量与持仓一致**（owner 指定），取自 NT 报的持仓量，不是信号里的 size；
- **严格一次**：per-position 一次性 + 冷却，复用现有 close-guard 机制（lesson #13）不新造；
  **不得**对 reduce-only 做重试循环，也不得对普通反向单做重试；
- 下单前**先撤净该 instrument 的 resting 挂单**（现有 `emergency_close` 已是此顺序）—— 这一步同时
  处理 §决策 里那个容量成因，且能避免自己的止损单与平仓单互相占额度；
- 日志写清「reduce-only 被拒的原文、判定依据、数量取自哪里」，事后可复盘。

### Task 4 — 三条平仓路径都要覆盖，其中一条在 toolkit 之外

判定逻辑收敛为 `strategy_core.py` 里的**一个共享 helper**（owner 指定的落点），三处接入：

1. **`create_exit_order`（`execution.py:214-219`）** —— 本通道唯一**当前会真正触发**的平仓路径。
2. **熔断 flatten（`host.py:728`）** —— 在 custos 侧、调 NT 的 `close_all_positions`。要么改为走 toolkit
   的平仓路径，要么在 host 侧复用同一 helper。**不接它，则熔断在 demo 损坏态下依旧平不掉** ——
   而那正是最需要它成功的时刻。
3. **`emergency_close`（`strategy_core.py:327-368`）** —— 一并接上以免留下两套语义，但 close-out 必须
   **诚实标注它在本通道当前不可达**（无命令通道，见 §上下文），不得写成"已覆盖三条路径"就完事。

`emergency_close` 还有额外约束：它 **never propagates** 且不得延误关停（见其 docstring 与 lesson #34），
所以 fallback 在那条路上必须同样受总预算约束、不能把 best-effort 层变成会挂住的层。

### Task 5 — 两个 host 都要被测到

`--engine nautilus` 与 `--engine sandbox-sim` 各自断言。sandbox 不连交易所、拒单形态无法自然产生，
所以它的断言是**不回归**（现有平仓行为不变），不是"也走 fallback"。
（同 Plan 26 / 27 Task 5 的要求。）

## 验证清单 (Verification)

- [ ] `make verify` 全绿（注意 C6 记录的既有 `fmt-check` 恒红：3 个被 `docs/authority/**` 按字节 pin
      住的文件；实施前先测基线，只对照增量）
- [ ] Task 1 / Task 2 的四条测试修复前红、修复后绿（两侧输出都记进 close-out）
- [ ] **`-2019` 保证金不足不触发 fallback** —— 单独列项，因为这条错了就是把拒单变成新仓位
- [ ] **cache 滞后误报 open 时不下普通单** —— 单独列项，这是选项 A 的已知弱点
- [ ] 三条平仓路径各有覆盖，且 close-out **分别标注可达性**：`create_exit_order`（当前唯一会真正触发）、
      熔断 flatten（`host.py:728`，toolkit 之外）、`emergency_close`（本通道**不可达**，接它只为语义统一）。
      把不可达的那条写成"已验证"就是 C7 的自洽假绿
- [ ] `emergency_close` 路径上的 fallback 不延误关停（沿用 lesson #34 的 `wait_for(总预算)` + 兜底放
      `finally` 的判据）
- [ ] **真机证据 —— 不可强求，须诚实标注。** 本 plan 的触发条件依赖 demo 引擎处于**损坏态**，而它是
      间歇性的、无法按需复现。因此：**不得**因为"跑了一轮没触发"就宣布已验证。可接受的做法是记录
      「fallback 未被触发，因为环境当时正常」，并保留一条人工验证路径（若再次遇到 lesson #14 那种
      平不掉，按本 plan 的日志核对门禁是否如期放行）。**把未触发写成已验证，就是 C7 的自洽假绿。**

## 完成情况 (Partial close-out, 2026-07-31)

### 已落地：常规出场路径的 fallback（commit `9f38691`）

`create_exit_order` 增 `reduce_only: bool = True` 参数；`signal_execution.py` 按
`ctx.order_tracker.close_reject_count == 0` 决定传什么，并在拆掉保护时打 warning 写明依据与数量来源。
未新增状态 —— 复用既有的连续逻辑拒单计数（`order_reconciler.py:419` 只在 logic tier 递增，
`trade_event_handler.py:224` 确认平仓后清零）。

测试 4 条（`tests/toolkit/test_close_reduce_only_fallback.py`），其中 3 条实现前红（`KeyError:
'reduce_only'`）、实现后绿；第 4 条**实现前即绿**并被有意保留 —— 它断言「fallback 已武装但仓位已平时
不得下普通单」，而拆掉 `reduce_only` 之后，现有的 `positions_open` 早退是唯一挡住"开出反向仓"的东西，
必须有测试钉住它。`tests/toolkit` 1319 passed；全仓 2206 passed / 25 skipped / 1 xfailed。

> 全仓首次跑出 3 个 error（`test_toolkit_release_candidate_build.py`），实证为**工作区未提交**所致
> （"toolkit package sources must exactly match the clean source commit"），提交后自行消失 —— 是可复现
> 构建守卫在起作用，不是缺陷。

### 真机证据（这条 plan 的验证清单原本写明"不可强求"，但它真的发生了）

镜像 `9f38691`（进容器核验过 `create_exit_order` 带 `reduce_only`、默认 `True`、判断与日志都在）。
2026-07-31 testnet 实跑，**`-2022` 真实触发**，完整链条：

| 时刻 (UTC) | 事件 |
|---|---|
| `08:30:00` | 趋势跳变 `prev_trend=-1 → trend=1` → `Signal: EXIT_SHORT, action=close_on_reversal` |
| `08:30:01` | `reduce_only=True` 平仓单 `e8c775dd…` 被拒：`{'code': -2022, 'msg': 'ReduceOnly Order is rejected.'}` |
| `08:30:01` | 处理器撤净该 instrument 挂单（含止损 `c6ba006a…`）+ 2s 退避；拒单计数 → 1 |
| `08:31:00` | `Closing without reduce_only after 1 logical refusal(s) …; size=0.0070 taken from the open position` |
| `08:31:07` | `PositionClosed` — `side=FLAT`, `quantity=0.0000`, closing_order_id `e726ccb2…` |
| `08:32:00` | `Status: trend=BULL | pos=FLAT`，随后按设计开新仓 |

平仓单实测形态：`BUY MARKET 0.0070 IOC **reduce_only=False**` —— 方向相反、数量等于持仓。

> ⚠️ **2026-07-31 更正**：原文此处写「只发一次」，**是错的**。那一轮只发了一张，是因为普通单立即成交、
> 确认平仓把计数清零 —— 不是代码保证的。当时实现是 `count >= 1` 就一直用普通单，下一根 bar 会再发。
> 该性质由运气兑现，不由设计兑现。见 §2026-07-31 事故。

### 同一次长跑给出了对照组，**容量假说被推翻**

`08:40:00` 又一次跳变（`prev_trend=1 → trend=-1`）平掉本 runner 自己开的多仓 `0.0056`。这一次
`reduce_only=True` **直接被接受**，没有 `-2022`、没有走 fallback，`08:40:08` `PositionClosed`。

关键在时序 —— 这一单被接受时，**保护性止损还挂着**：

```
08:40:00.18   OrderInitialized   ← 平仓单 MARKET reduce_only=True
08:40:08.38   OrderAccepted      ← 交易所接受，此刻 SL 仍在
08:40:08.385  CancelOrder …      ← 之后才撤 SL（"Cancelled exchange SL order"）
08:40:14.5    OrderCanceled
```

若「一张全额 resting reduce-only 止损占满额度」足以造成 `-2022`，这一单必被拒 —— 它没有。所以
**§决策 里那个容量假说不成立，本 plan 不再以它为主要解释**（起 plan 时我据 4 张孤儿单提出它，后来实证
那 4 张多为对账重放；这次的对照组把它彻底排除）。

两次的差别不在挂单，而在**持仓本身**：`08:30` 平的是启动前就存在、经对账接管的空仓；`08:40` 平的是本
runner 自己开的多仓。这与 owner 报告的「交易所网页端也平不掉那个空仓」一致 —— 问题绑在那个特定持仓
上，正是 lesson #14 的形状。**因此本 plan 的 fallback 不是备胎，而是当时唯一能平掉它的手段。**

## 2026-07-31 事故：跳过 Task 1 的代价，几小时后在生产里兑现（fix commit `6f0c0e1`）

### 发生了什么

同一条 lane 继续跑到 `10:06`，趋势再次翻转触发平仓。交易所此时**后端不健康**，连续给出：

| 时刻 | 拒单 | 分级 | 处置 |
|---|---|---|---|
| `10:06:05` | `-1007 Timeout … execution status unknown` | server | 60s 熔断退避，**不武装** ✅ |
| `10:08:02` | 一整个 HTML 错误页 | server | 60s 熔断退避，**不武装** ✅ |
| `10:10:41` | **`logic: UNKNOWN`（事件没带 reason）** | logic | **武装了 fallback** ❌ |
| `10:11 / 10:12 / 10:13` | — | — | **每根 bar 各发一张普通反向单** ❌ |

### 两个缺口，都是本 plan 实施时我引入的

1. **武装依据用了 logic tier，而那是"原因无法识别"的默认桶。** `exchange_errors.py` 的 docstring 自己
   写着 `"logic"` 是「business rejections **and unrecognized reasons**」的默认。这个默认对「该退避多久」
   是安全的；**是本 plan 让它额外意味着「拆掉 reduce_only」**，于是默认桶变成危险桶。同一个信号回答两个
   问题，安全性不同 —— 我复用它时没查还有什么会落进来。
2. **「严格一次」没有实现。** §Task 3 写了「严格一次」，close-out 也这么声明，实际是 `count >= 1` 就
   一直用普通单。不是措辞问题，是我没有验证自己声明的性质。

### 为什么这次特别危险

`-1007` 的原文就是 **execution status unknown** —— 那张平仓单**可能已经成交**。若已成交而 NT 仍显示持仓，
一张普通反向单就是**开一个反向仓**。而 `UNKNOWN` 拒单出现在这种上下文里，很可能又是一次执行状态未知。

**实况：没有开出反向仓，只因为交易所把那些普通单也拒了。是故障挡住的，不是设计挡住的** —— 这种侥幸
不算通过（C7 自洽假绿的近亲：结果对了，机制没对）。

### 修法（`6f0c0e1`）

- 新增 `is_reduce_only_refusal(reason)`：只认明确的 reduce-only 拒绝标记；`-2019` 与**原因未知**一律不算。
  没有把 `classify_rejection_reason` 的返回值改动 —— 它另有消费者，且它回答的是另一个问题。
- `OrderTracker` 加 `_reduce_only_refused` 与 `_plain_close_submitted`，与那个"有东西被拒"的计数**分开**，
  两者都在确认平仓时清零。
- 普通单**每持仓一次**；第二次改为记录「唯一一次已发出，不再重发；仓若还在，该看交易所侧状态而不是再下单」。
- 测试 5 条新增，其中 3 条是纯安全方向（`None` / `""` / `"UNKNOWN"` 不武装；只有通用计数不武装；`-2019`
  不武装）+ 1 条一次性。另有 3 条旧测试被改正，其中一条原本断言「之后一直用普通单」—— **那正是要否掉的
  行为**，原设计在该点是错的。

### 副产品：三级分流在真机上各走了一遍

`-2022` → 逻辑层 → 武装 + 普通单平掉；`-1007` → 服务器层 → 60s 退避、保护不动；HTML 错误页 → 同上。
**这组对照证明分级不是纸面洁癖**：若按遗留项原样把桶合在一起，后两次都会错误地下普通反向单。

## 未做（不要按"已完成"读本 plan）

1. **Task 4 只覆盖了三条路径中的一条。** 熔断 flatten（`host.py:728` → NT `close_all_positions`）与
   `emergency_close`（`strategy_core.py:327`）仍硬编码 reduce-only，遇到同样的拒单依旧平不掉。
2. **Task 5 —— sandbox host 的"不回归"断言未加。**
3. **`6f0c0e1` 的两处修复尚未真机复验。** 需要在含它的镜像上再遇到一次真实的 `-2022` 才能确认武装依据与
   一次性都按预期工作 —— 而 demo 引擎的损坏是间歇的，**不得因为"跑了一轮没触发"就宣布验证通过**
   （与 §验证清单 末项同一条纪律）。

## 偏离与改进日志 (Deviations & Improvements)

- 门禁选型（A / B / C）定下来后记在这里，连同对"选项 A 的弱点是否可接受"的判断依据。
- 若选 C，先记下 NT Binance 适配器是否真的暴露了权威查询的 grep 实证 —— 起 plan 时未验证。
- 起 plan 时我曾由「testnet ≠ demo」推断 lesson #14 的风险不适用于当前账户，被 owner 纠正，并由端点
  实测证伪（下单走 `demo-fapi`）。**教训：环境属性要从运行时实际连的端点判定，不要从 mode 名字推断。**

## Follow-up hooks（不属于本 plan scope，登记以防遗漏）

- **下单为什么走 `demo-fapi` 值得单独判断。** lesson #14 的预防条款写的是「下单/平仓可靠性验证用
  Speculum 回测或稳定环境，**不依赖 demo-fapi**」，而当前 `MODE=testnet` 的 REST 正指向它，行情却来自
  testnet 流。这个混合配置是有意的还是历史残留，本 plan 没有判断。若改成真 testnet REST，本 plan 的
  触发条件可能永远不会出现 —— 那也是一种解法，但要先确认真 testnet 的 reduce-only 是健康的。
- **testnet 上仍有一个未平的空仓**（`-0.0070 BTC` + 三张 resting reduce-only stop），见 Plan 27
  §Follow-up。它当前平不掉的原因**不是**本 plan 这个缺陷：熔断那条 flatten 被 Plan 27 的两个发现废掉，
  `emergency_close` 在本通道不可达，而策略层没有 REST、无法照 lesson #14 手工净平。**本 plan 落地也不会
  自动平掉它** —— 本通道唯一会真正触发的是常规出场路径，它要等一个出场信号（趋势反转，或价格触及那张
  挂在 `65275.10` 的 stop）。若希望"按需平仓"成为一种能力，那需要给本通道一个命令通道，是独立的决策。
- **撤单没有真正生效，孤儿止损单在累积（可能是本 plan 那个 `-2022` 的主因）。** 2026-07-30 实测：启动
  时交易所侧同时挂着 4 张来自前四轮的 reduce-only 止损单。前几轮日志里都有对应的 `OrderCanceled`，但它们
  显然没在交易所侧生效 —— `runtime/` 下只有遥测 WAL 与 generation 记录两个库、**没有 NT 缓存库**，所以
  NT 每轮空缓存起步，那 4 张是交易所报回来的，即确实还挂着。这与 PS lesson #17（批量撤单
  fire-and-forget 无对账 → 孤儿 SL）同形。**这条值得独立起 plan**：它既是资金路径问题（占满 reduce-only
  额度导致平不掉），也让现有代码里"先撤单再平仓"这个 -2022 缓解措施实际失效。
- **`emergency_close` 是一个当前不可达的安全面。** 它被声明为命令却零消费方，而离线通道按设计不持有
  命令。要么给通道加命令投递，要么明确它只服务签名通道 —— 现状是"看起来有、实际调不到"，比没有更容易
  误导下一个读者（与 Plan 26 修的那类"把代表过去的东西当现在的答案"同源）。
