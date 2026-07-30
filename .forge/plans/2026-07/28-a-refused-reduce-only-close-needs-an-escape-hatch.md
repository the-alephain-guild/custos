# 28 — reduce-only 平仓被拒时没有出路（demo 撮合引擎的已知缺陷，lesson #14 的手工绕法待固化）

> **Status**: 🔲 Not started
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

于是门禁有三种可能，**需 owner 定**：

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

**倾向 A + 严格一次性**，理由是范围可控且不改分层；但必须在 plan 里写明它的弱点，并把「反向仓被误开」
本身当成一个**必须被测到的失败模式**，而不是"应该不会发生"。若 owner 认为 demo 上的误开风险不可接受，
再走 B/C。

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

### Task 3 — 落 owner 选定的门禁

按 §决策 选 A（或 B/C）实现。无论哪个，这张 fallback 单必须：

- 数量取自**权威来源**（A 下是 NT 报的仓位量；B/C 下是交易所返回的 `positionAmt`），不是信号里的 size；
- 一次性 + per-position 冷却，且冷却状态与在途 guard 复用现有 close-guard 机制（lesson #13），不新造；
- 日志把「为什么允许拆掉 reduce_only」写清楚（哪条门禁过了、依据是什么），事后要能复盘。

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
- **`emergency_close` 是一个当前不可达的安全面。** 它被声明为命令却零消费方，而离线通道按设计不持有
  命令。要么给通道加命令投递，要么明确它只服务签名通道 —— 现状是"看起来有、实际调不到"，比没有更容易
  误导下一个读者（与 Plan 26 修的那类"把代表过去的东西当现在的答案"同源）。
