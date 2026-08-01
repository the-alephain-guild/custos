# 27 — 启动期的守卫问了一个还没准备好的组合（equity 币种未声明 + flatten 在持仓到达前空转 + 定价来源从未被订阅）

> **Status**: ⏳ In Progress —— 发现 A 已修并**已真机验证**（`40d94e6`）+ 发现 B 的 B2 已修、**真机只
> 验到未改动的那一支**（`1073d36`）+ **发现 C 已修、未真机验证**（`675c0fd`，见下）。
> **B1 查证后不能按原文实现**（离线通道不等就绪 / readiness 里的对账字段是占位符 / 改 latch 被本 plan
> 非目标排除）；且 **B1 的必要性已降级** —— 起初把它标为"必需"依据的是一个后来被实证推翻的结论，
> 见 §发现 C。可行形态与遗留项见 §完成情况
> **Created**: 2026-07-30
> **Project**: custos (`tesseract-trading/custos/`)
> **Depends on**: 无 —— 现有代码即可复现
> **Blocks**: 离线通道在**有资金的 testnet 账户**上正常运行（现状：启动即熔断跳闸并 latch）
> **For Claude**: `/forge:execute`；单 session（两个独立根因，可拆两 plan，见 §决策）
> **multi_session_scope**: false

## 上下文 (Context)

2026-07-30 philosophers-stone 首次在**重建过的镜像**（含 Plan 25 的 `-4015` 修复）上跑 testnet
实盘通道，34 分钟真机运行（`08:55` → `11:08`）暴露两个**互相独立**的问题。两者都发生在启动期，
共同点是**守卫在组合还没准备好的时候就去问它**。

`-4015` 已确认修好（0 次出现、0 次 `OrderRejected`、我方 client order id `a39769df…` = 32 字符
< 36 上限），本 plan 与它无关。

### 发现 A：equity 币种未声明 → 有资金的多币种账户结构性不可靠

真机账户状态（Binance USDT-futures testnet，`base_currency=None`）：

```
AccountBalance(total=4_488.58845941 USDT, locked=451.94572000, free=4_036.64273941)
AccountBalance(total=5_000.00000000 USDC, free=5_000.00000000)
AccountBalance(total=0.01000000 BTC,      free=0.01000000)
```

`_resolve_equity` 在**没有指定币种**时只有一条路走得通（`portfolio_snapshot.py:207`）：

```python
if len(entries) != 1:
    return None, None
```

三个币种同时非零 → 条目数 ≠ 1 → 返回 `None, None` → `portfolio_equity_ambiguous`
（`:129`）→ snapshot 不可靠 → 守卫 fail closed。

**四个调用点里只有一个声明了币种：**

| 调用点 | 方法 | 是否传 currency |
|---|---|---|
| `host.py:647` | `runner_fact_risk_snapshot(…, currency: str)` | **是** —— 签名里就是必填参数 |
| `host.py:690` | `get_open_notional`（`:684`） | 否 |
| `host.py:742` | `get_positions`（`:736`） | 否 |
| `host.py:792` | `get_engine_status`（`:772`） | 否 |

所以在**任何**多币种账户上，这三个守卫按构造必然不可靠。注意方向：**往账户里多充一种币会让它更糟，
不是更好** —— 起 plan 前我一度以为"充值后就好了"，恰好反了。

`SUPPORTED_CURRENCIES`（`core/runner_fact.py:74`）= `{USD, USDT, USDC, BTC, ETH}`，账户这三种全在
白名单内，所以没有任何一层拒绝它们；模糊纯粹来自"条目多于一个"。

**为什么 sandbox 一路全绿。** 这三个方法各有两份实现：`SandboxSimulationHost` 的
（`:166` / `:185` / `:192`）返回常量 `Decimal("0")` / `[]` / 恒 healthy，**从不查组合**；只有
`NtTradingNodeHost` 的（`:684` / `:736` / `:772`）才去查 snapshot。离线 sandbox 通道因此永远碰不到
这条路 —— 与 Plan 25 的 `DEV-25-TASK-4-PREMISE-CORRECTED`（sandbox 有一条根本不建 NT 节点的路径）
同一个成因。**只有 testnet/live 会咬。**

### 发现 B：flatten 在 reconciliation 交付持仓之前跑，空转，而记录读起来像已经兜住

时间线（同一次实跑，毫秒级）：

| 时刻 | 事件 |
|---|---|
| `08:55:49.307` | `Updated AccountState(...)` —— 余额到达 |
| `08:55:49.353323` | `fallback_breaker_fail_closed` reason=`portfolio_equity_ambiguous`（发现 A 触发）|
| `08:55:49.353411` | `positions_flattened` **`instrument_count: 0`** —— 什么都没平 |
| `08:55:49.353443` | `offline_exposure_guard_latched` |
| `08:55:51.893` | 账户**既有空仓**才经 reconciliation 进来：`OrderFilled SELL 0.0070 @ 63995.10`，`position_id=…-EXTERNAL` |
| `08:55:51.901` | 策略自己给它挂上 reduce-only `STOP_MARKET BUY 0.0070 @ 65275.10` |

全程只有 **1 次** `positions_flattened`。守卫已 latch，按其设计不重启不会再评估。结果：testnet 上一个
活的空仓，**只由策略自己那张 SL 兜着**，而 runner 的安全熔断已经跳完闸退场。

这条**正是 C9 预防条款的字面实例**。C9 写的是：「超时按『读不出』处理并 fail closed，但记录必须说清
**containment 没有被确认**，不能写得像已经兜住了」。而 `positions_flattened` 这个事件名 + warning 级别，
读起来就是「已兜住」—— 实际 `instrument_count: 0` 意味着**它没有机会兜任何东西**。

同时它是 **C12 同一个错误的第三个尺度**。C12 说：把**代表过去的东西**当成**现在的答案**。
- Plan 26：跨一个进程边界后仍信旧记录（`container_id`）
- C12：跨一次 loop 调度后仍信旧 registry（`membership ≠ liveness`）
- **本条**：跨 reconciliation 窗口后，把「组合此刻说 0 个 instrument」当成「没有东西要平」

三次都是同一个形状。组合在 `08:55:49` 说 0，不是因为账户是空的，而是因为**它还没被告知**。

### 发现 C（2026-07-31 追加）：守卫的定价来源全仓没有人订阅

修好发现 A 之后，同一条路上换了一个原因继续 fail closed：`mark_price_unavailable`。我第一反应是"行情
还没到，等一会就好"，据此还一度把 B1（等就绪）说成必需。**查证后这个说法是错的，两条实证都指向别处。**

`snapshot()` 给每个持仓定价的顺序是（`portfolio_snapshot.py:135-141`）：

```python
mark = cache.mark_price(instrument_id)                   # 首选
if mark is None and self._price_type_mid is not None:
    mark = cache.price(instrument_id, self._price_type_mid)   # 退路：MID
if mark is None:
    return ...unreliable(f"mark_price_unavailable:{instrument_id}")
```

1. **首选那条从来不可能有值。** `git grep subscribe_mark_prices 675c0fd~1` 在 `src/` 与 `packages/`
   **零命中** —— 全仓没有任何地方订阅过 mark price，所以 `cache.mark_price()` 恒为 `None`。
2. **退路那条也拿不到。** 在 runner 镜像里实测：cache 里只有 trade tick 时，`LAST` 有值而
   `MID` / `BID` / `ASK` 全是 `None`；补一条 quote tick 后 `MID` 才有值。本部署订阅的是 bar + trade，
   **不订阅 quote**。

两条合起来：**只要有持仓，snapshot 就不可能可靠，熔断除了 fail closed 没有别的可能** —— 与等多久无关。
这也解释了为什么它在此前的所有观察里都藏着：`_resolve_equity` 在 `:124-130` 就先返回了，代码**根本走不到**
定价这一段。发现 A 一直在给发现 C 打掩护。

**这与发现 B 是不同的病。** B 是"问早了"（等一会就有答案），C 是"问了一个没人回答的问题"（等多久都没有）。
把 C 误当成 B 的一部分，正是我先前判断 B1 必需的由来。

**修法（`675c0fd`）**：`PairContextCoordinator.subscribe_mark_prices()` 给每个 pair 订阅 mark price，
在 `on_start` 里无条件调用。三点理由记在方法的 docstring 与
`tests/toolkit/test_mark_price_subscription.py` 的模块 docstring 里：

- **用 mark price 而不是 last trade**，因为永续合约的未实现盈亏与强平就是按它计价的，不是权宜之计；
- **无条件订阅**，因为 tick 订阅挂在出场模式与 tick 监控配置下，而"守卫需要一个价格"与这两件事毫无关系。
  **把它挂在那份配置下，正是熔断依赖了没人订阅的数据这件事的成因** —— 所以这里刻意不加任何条件，并有一条
  测试钉住"tick 监控关掉时订阅照样发生"；
- 第三条测试断言 `on_start` 里**确实调用**了它 —— 一个"存在但没接线"的订阅，就是事故当时的状态。

## 决策 (Decisions)

### 发现 A 的修法：从 spec 的 pairs 推导，不新增 spec 字段

仓里**已有**这个模式：`sandbox_runner_fact_host.py:150 _settlement_currency(spec)` 从
`spec["pairs"]` 取 quote 币种（`BTC-USDT` → `USDT`），并要求**恰好一个**，否则 raise
`RunnerFactContractError`。

`OfflineDeploymentSpec.pairs`（`offline/spec.py:72`）是 `Field(min_length=1)` 必填非空，所以离线通道
一定拿得到。**推荐复用这个推导，把结果传给那三个调用点。**

关键理由：**这个推导在零持仓时依然成立**。若改从持仓的 `settlement_currency` 推（`:148-149` 已有
这个动作：`getattr(position, "settlement_currency", resolved_currency)`），恰好在最需要它的时刻
（启动期、还没有持仓）拿不到 —— 而那正是本次跳闸的时刻。注意它的 fallback 还是 `resolved_currency`，
也就是说它**依赖** equity 已经解析成功，救不了本 plan 的场景。

备选是给 spec 加一个显式币种字段。不推荐：pairs 已经隐含了这个信息，加字段等于让 PS 侧 `render_spec`
也跟着改，且两处可能不一致。

实施要注意两点边界：
1. `_settlement_currency` 的「恰好一个」要求意味着**跨 quote 币种的多 pair 部署会 raise**。这是对的
   （equity 本来就无从单一表达），但错误必须说清是"部署跨了结算币种"，不要复用 `ambiguous` 这个
   已经指向另一回事的词。
2. 白名单是 `runner_fact` 的常量。给三个通用守卫复用它之前，先确认这个耦合是想要的；不想要就把
   校验分离。

### 发现 B 的修法：需要 owner 定，三选

- **B1 —— 让启动期评估等 reconciliation 就绪。** 加 readiness gate，在组合被 NT 填充完之前不评估。
  治根，但要定义"就绪"在 NT 语义下是什么（存在可靠信号吗？还是只能超时？），范围最大。
- **B2 —— 记录先诚实。** `instrument_count == 0` 时不得记成 `positions_flattened`，要明确写
  containment **未被确认**；并在持仓到达后重新评估一次。这是 C9 已经写下、但没在这条路上落地的要求。
- **B3 —— 两者都做。**

**推荐 B3；B2 是底线**，因为一条读起来像"已兜住"的日志比没有日志更危险 —— 它会让人（和下一个
审查者）停止追问。

⚠️ 发现 A 与发现 B **相互独立**，任一可单独修：修好 A 后熔断不会再因币种模糊跳闸，但换任何别的原因
跳闸时 flatten 照样会空转；修好 B 后 flatten 不再空转，但账户仍是多币种、A 照旧不可靠。因此本 plan
可按需拆成两份，**但不要只修 A 就宣布这条通道安全** —— 那只是让 B 更难被发现。

## 目标 (Goal)

有资金的多币种 testnet 账户上启动，不因币种未声明而误跳闸；守卫给持仓定价所依赖的数据**有人订阅**；
且任何一次 fail-closed 的 flatten，要么真的作用在持仓上，要么在记录里说清它没有。

## 非目标 (Non-goals)

- **不动 exposure guard 的 latch 语义** —— 「清除需要重启」是有意设计（已实证）。
- **不动 fail-closed 的方向** —— equity 读不出时拒绝交易是对的，问题在"读不出"本可避免。
- **不改 `SandboxSimulationHost` 返回常量这件事** —— 它是显式的本地模拟边界。
- **不在本 plan 处理 testnet 上那个遗留空仓** —— 见 §Follow-up。

## 实现任务 (Tasks)

### Task 1 — 先写会红的测试（A）

用真实 `NautilusPortfolioSnapshotProvider` + fake node，令 `portfolio.equity(venue)` 返回**多个**
币种条目，断言：

1. 不传 currency 时**当前**返回 `portfolio_equity_ambiguous`（锁住现状，证明测试真的踩在这条路上）；
2. 三个守卫路径（`get_open_notional` / `get_positions` / `get_engine_status`）在多币种账户下
   **修复后**可靠。

第 2 条必须在修复前是红的，红的输出记进 close-out。不要在测试里复刻 `_resolve_equity` 的判断
（手抄等于测自己的假设 —— C4 / C7）。

### Task 2 — 把推导出的币种接到三个调用点（A）

复用 `_settlement_currency(spec)` 的推导逻辑（`sandbox_runner_fact_host.py:150`），把结果传给
`host.py:690` / `:742` / `:792`。`:647` 已有必填参数，不动。

跨 quote 币种的多 pair 部署按 §决策 报一个**语义不同**的错误，不要复用 `ambiguous`。

### Task 3 — 先写会红的测试（B）

构造「熔断在组合为空时跳闸，随后持仓到达」的时序，断言：

1. `instrument_count == 0` 的那次**不得**产生读起来像 containment 已完成的记录；
2. 按选定的修法（B1 / B3）断言持仓到达后守卫会再看一次，或（B2）断言记录明确标注未确认。

时序类测试不要靠 `sleep` 碰运气 —— 用可控的时钟或显式的事件顺序，否则它会变成一条偶发红。

### Task 4 — 落 B 的修法并把教训指回 C9

按 owner 选定的 B1/B2/B3 实施。C9 已经写下「记录必须说清 containment 没有被确认」，本条是它在
另一条路上的复发 —— 实施后在 C9 条目下补一行 dogfood 记账（不新建教训，它已经存在）。

### Task 5 — 两个 host 都要被测到

`--engine nautilus` 与 `--engine sandbox-sim` 各自断言。sandbox 返回常量、**不该**被本 plan 改动，
但要有测试**钉住**这一点，否则未来有人"顺手统一"两边就会把那个显式模拟边界弄没了。
（参考 Plan 26 Task 4 的同样要求。）

## 验证清单 (Verification)

- [ ] `make verify` 全绿（注意 C6 记录的既有 `fmt-check` 恒红：3 个被 `docs/authority/**` 按字节
      pin 住的文件；实施前先实测基线，只对照增量）
- [ ] Task 1 / Task 3 的测试修复前红、修复后绿（两侧输出都记进 close-out）
- [ ] 跨 quote 币种多 pair 部署的错误信息**不含** `ambiguous` 字样，且说清是跨结算币种
- [ ] sandbox 常量路径有测试钉住，且本 plan 未改动它
- [ ] **真机证据（A）**：在同一个多币种资金账户（USDT + USDC + BTC 同时非零、`base_currency=None`）
      上启动 testnet，日志中 `portfolio_equity_ambiguous` **0 次**。单测证不了这条 —— 它证的是
      解析逻辑，不是真实账户的币种分布
- [ ] **真机证据（B）**：构造一次启动期 fail-closed（可临时把某个阈值调到必然触发），确认记录不把
      `instrument_count == 0` 写成已兜住
- [x] **发现 C 的静态判据**：`git grep subscribe_mark_prices <fix>~1` 在 `src/` + `packages/` 零命中
      （证明"从未被订阅"而非"订阅了但没到"）；三条测试覆盖逐 pair 订阅 / 与 tick 配置无关 / 已接进
      `on_start`
- [ ] **真机证据（C）**：在**行情在流且有持仓**的情况下，`mark_price_unavailable` 0 次。两个条件缺一
      不可 —— 只有持仓而无行情，或只有行情而无持仓，都碰不到这条路，**不得**据此宣布已验证

## 完成情况 (Partial close-out, 2026-07-31)

### 发现 A 已修（commit `40d94e6`）

币种在 deploy 时由 pairs 推导并与 node 存在一起（`host.py:267` 字典 / `:528` 访问器），三个守卫路径
（`:719` / `:785` / 第三处同形）改为声明币种。推导收在新模块
`src/custos/engines/nautilus/settlement.py`，sandbox 的 RunnerFact host 改为委托它 —— 同一条规则只有
一份实现（lesson #12）。跨 quote 币种的部署报 `SettlementCurrencyError`，措辞不复用 `ambiguous`。
未注册币种时退回修复前行为（安全）但打 `settlement_currency_unregistered` 警告，不静默（lesson #15）。

测试 5 条新增，其中 2 条把成因钉住（不声明币种 → `portfolio_equity_ambiguous`；声明了 → 解析出
`4488.58845941`）。**旧测试 `assert provider.calls == [None, None, None, "USDT"]` 原本把缺陷钉死**
——它明确断言那三处传 `None`；现已改为四处都声明币种，断言的是生产行为而非退化路径。

### 发现 B 的 B2 已修（commit `1073d36`）

`flatten_positions` 在 `instrument_ids` 为空时改记 `nt_flatten_containment_unconfirmed`（error 级，
`host.py:763`），不再写 `positions_flattened`；真的平掉东西时保留原事件。**这不是自创写法** —— 本通道的
超时路径早已如此（`offline/safety.py` 的 `offline_exposure_containment_unconfirmed`，其 docstring 明写
"nothing was flattened, and the record says so rather than implying containment happened"）。C9 那条在
超时路径兑现过，只有「够到引擎却发现什么都没有」这条没有，现在补齐。

`make verify` 相关：`tests` 全套 2213 passed / 25 skipped / 1 xfailed，ruff format + check 干净。

### 发现 C 已修（commit `675c0fd`）

见 §发现 C。新增 3 条测试（`tests/toolkit/test_mark_price_subscription.py`）：逐 pair 订阅、
tick 监控关掉时照样订阅、`on_start` 确实接线。镜像重建后进容器核验了四项 —— 方法存在、**接进
`on_start`**、实现里**没有**任何条件分支、NT 侧 `Strategy.subscribe_mark_prices` 可用。第三项是反向查：
确认新方法**不带**条件，而不只是确认它存在，因为本事故的形状就是"守卫的数据需求被挂在了不相关的可选
配置下"。

### 发现 C 的真机首跑：订阅生效了，然后暴露出下一个（`69e153e`，2026-08-01）

含 `675c0fd` 的镜像首次在有持仓的 testnet 上跑。结果：

```
portfolio_equity_ambiguous     0     ← 发现 A 再次确认
mark_price_unavailable         0     ← 订阅生效了，取到了 mark price
fallback_breaker_fail_closed   1     ← 但换了个原因跳闸
positions_flattened            1     （instrument_count: 1，确有持仓）
```

新原因：**`portfolio_snapshot_invalid:TypeError`**。`mark_price_unavailable` 为 0 说明价格**取到了** ——
炸在拿到之后用它的时候。

**根因：cache 的两个价格来源返回的不是同一种东西。**

| 调用 | 返回 |
|---|---|
| `cache.mark_price(id)` | **`MarkPriceUpdate`** —— 一个带时间戳的事件，价格在它的 `.value` 里 |
| `cache.price(id, MID)` | **`Price`** —— 价格本身 |

`portfolio_snapshot.py` 把两者当同一种东西，于是把 wrapper 交给了 `Position.unrealized_pnl()`，
而它只收 `Price`。修法是 `mark = getattr(mark, "value", mark)`。

**这条分支在今天之前从未跑过。** 没人订阅 mark price → 第一支永远 `None` → 永远走返回 `Price` 的退路。
`675c0fd` 让它第一次活过来，类型不匹配随即现形。**这是本 plan 内第三次同一形状**：A 挡住 C，C 挡住这个。

**为什么单测抓不到，且不是"测试写少了"。** 旧 fake 的 `mark_price()` 返回 `_DecimalValue("100")` ——
一个**长得像代码的假设、而不像 NautilusTrader** 的对象。它通过多少次都不可能发现这件事，因为它验证的
是我们的假设自洽，不是与框架一致（C4 / C7 的形状）。新测试**用真的 `MarkPriceUpdate`**，修复前红，
报的正是"传进去的是 update 不是 price"。**当一个类型本身就是 bug 时，fake 必须是真类型。**

### 第二跑（`69e153e`）：TypeError 消失，B2 的那一支首次真机命中，然后 B1 被实证需要

```
portfolio_equity_ambiguous          0    ← A 稳定
mark_price_unavailable              0    ← C 的订阅稳定
portfolio_snapshot_invalid          0    ← 类型修复生效
nt_flatten_containment_unconfirmed  1    ← B2 改的那一支，首次真机命中
positions_flattened                 0
fallback_breaker_fail_closed        1    ← 第三个原因
```

**B2 真机确认。** 上一跑把持仓平掉了，这一跑无仓可平，于是 flatten 正确记成
`nt_flatten_containment_unconfirmed` 而不是 `positions_flattened`。**这正是 B2 改动的那半边**，
之前一直只验到未改动的那一支，现在两支都验过了。

**新原因 `portfolio_equity_missing:USDT`，时间线精确到毫秒：**

| 时刻 (UTC) | 事件 |
|---|---|
| `02:30:03.556` | NT 配置 `reconciliation_startup_delay_secs=**10.0**` |
| `02:30:03.570` | ExecEngine：**"Awaiting startup reconciliation completion"** |
| **`02:30:05.846`** | **熔断 fail closed `portfolio_equity_missing:USDT`**（启动后 2.3s）|
| `02:30:05.847` | guard latched |
| `02:30:05.962` | Portfolio: **Updated AccountState** —— 余额到达，**晚了 116 毫秒** |
| `02:30:08.460` | "Execution state reconciled" / "Startup reconciliation completed" |

**差 116 毫秒。** 而且这不是偶然：NT **按设计**要等 —— 它自己声明了 10 秒启动延迟，自己打日志说
"正在等启动对账完成"，实际 4.9 秒后才报完成。守卫在 2.3 秒去问一个 NT 明说还没准备好的组合。

### 这推翻了我当天早些时候给出的建议

我在 §B1 剩下的可行形态 里写过：「发现 A 与发现 C 修好后…宽限窗口推迟的是**尚未出现过的**某种
unreliable…建议等真机上再出现一次再决定」。

**真机出现了，就在同一天。** 而且它不是偶发扰动，是**结构性**的：账户余额只能等对账送来，NT 明码
标价要等 10 秒，任何"把 snapshot 算得更对"的修复都改变不了这件事 —— 数据当时确实不存在。

所以 B1 的必要性**从"已降级"回升为"有实证需求"**。我先前的降级理由（"防的是假想成因"）被这条时间线
证伪，在此更正。

### 但现在有比宽限窗口更好的选项，而且是今天才有的

宽限窗口要选一个数字，而选数字总是在猜。**遗留项 3 修好之后，真正的信号已经可以问了**：
`EngineReadinessChecks` 现在读 `trader.is_running`，而按 `NautilusKernel.start_async` 的序列，
trader 启动 ⟹ 启动对账已完成。也就是说「对账是否完成」不再需要用时间估计，可以直接判定。

于是 B1 有两种形态，需 owner 定：

| | 做法 | 代价 |
|---|---|---|
| **B1-a 有界宽限窗口** | 首次评估在窗口内遇到 unreliable 只记录不跳闸 | 要挑一个数字；挑小了没用，挑大了平白推迟 fail-closed |
| **B1-b 等就绪再开始评估** | 离线通道在启动 breaker 前先等 readiness（现在它压根不调 `wait_ready`，见遗留项 1）| 范围更大，但**判据是真的**，不是估的 |

**推荐 B1-b**，理由是它不引入猜测的数字；B1-a 可作为 B1-b 的兜底上限（就绪迟迟不来也不能无限不设防）。
两者都改 fail-closed 的时机 = 安全语义，所以留给 owner 拍板，本 plan 不擅自实施。

### 真机验证到哪一步（2026-07-31，逐条）

| 修复 | 真机状态 | 依据 |
|---|---|---|
| A（equity 币种声明） | ✅ 已验 | 含 `40d94e6` 的镜像上启动，`portfolio_equity_ambiguous` **0 次** |
| B2（空 flatten 诚实记录） | ⚠️ **只验到未改动的那一支** | 观察到有持仓时仍正确记 `positions_flattened`（证明没改坏正常路径）；**`instrument_count == 0` 那一支没有在真机上出现过**，即本次改动的那半边未被真机触达 |
| C（mark price 订阅） | ⚠️ **订阅已验，整条路未通** | 2026-08-01 首次在「行情在流 + 有持仓」下跑到：`mark_price_unavailable` **0 次**，证明订阅生效、价格确实取到；但紧接着 `portfolio_snapshot_invalid:TypeError`，见上节。`69e153e` 修好类型后**尚未复跑** |

B2 这一行是刻意写细的：把"跑过了、没报错"当成"改的那一支验过了"，就是 C7 的自洽假绿。

### B1 未做、**不能按原文实现**，且**必要性已降级**

**先说降级。** 我先前把 B1 标为"必需"，依据是"启动期熔断因为行情还没到而误跳闸，所以必须等就绪"。
§发现 C 实证推翻了这个依据 —— 那次跳闸不是因为"还没到"，是因为**没有人订阅**，等多久都不会到。
`675c0fd` 修的是数据来源，不是时机。B1 现在防的是"其它原因导致的启动期跳闸 + flatten 空转"，与
发现 A 修好后的判断一致：比最初以为的弱得多。

**再说为什么不能按原文实现。** owner 选了 B3（B1 + B2）。B2 已落地；B1 查证后无法照「等对账就绪」
实现，三条实证：

1. **离线通道根本不等引擎就绪。** `wait_ready` 的调用方只有签名通道的
   `src/custos/core/engine_lifecycle.py`（`:156` / `:292` / `:374`）；离线 reconciler 在 `deploy` 返回后
   直接报 healthy。这也解释了实跑中观察到的「`wait-status` 绿的时点早于交易所对账完成」。
2. **那套 readiness 里唯一提到对账的字段是个占位符。** `host.py:616` 写的是
   `reconciliation_initialized=authority.trading_mode == "sandbox"`，而
   `EngineReadinessChecks.ready`（`core/engine_protocol.py:226-237`）要求全部字段为真 —— 所以该字段
   **在 testnet/live 恒为 False**，即便去调 `wait_ready` 也永不就绪。它名叫"对账已初始化"，却既不检查
   对账、在真实模式下也不可能通过。**这是一个"看起来在检查、其实没检查"的门**，与本 plan 修的
   「把一个只能回答局部的信号当成回答全部」同源，单列为遗留项 3。
3. **B1 的另一种实现被本 plan 自己的非目标排除。** 「跳闸后继续评估直到确认」需要改 latch 语义，而
   latch 即 `breaker.tripped`（`offline/safety.py:92`），而 §非目标 明写「不动 exposure guard 的 latch
   语义」。

**B1 剩下的可行形态**：给启动期一个**有界宽限窗口** —— 首次评估在窗口内遇到 unreliable 只记录不跳闸，
窗口过后恢复现行为。这是「等就绪 + 有截止时间」的实质，但它改的是 **fail-closed 的时机**，属安全语义，
留待 owner 决定是否做、窗口多长。

⚠️ **若要做，先确认它防的是什么。** 发现 A 与发现 C 修好后，已知会在启动期让 snapshot 不可靠的两个成因
都已消除。一个宽限窗口若在此时加进来，它推迟的是**尚未出现过的**某种 unreliable —— 而推迟 fail-closed
是有代价的。**建议：等真机上再出现一次启动期 unreliable、看清它的 reason 之后再决定**，不要为一个假想的
成因放宽安全时机。

### 遗留项

1. **B1（有界宽限窗口）** —— 需 owner 定，且必要性已降级，见上。
2. **真机复验** —— 发现 C 的订阅尚未在真机跑过（需「行情在流 + 有持仓」同时成立）；发现 B2 只验到
   未改动的那一支。逐条状态见上表。
3. ~~**`reconciliation_initialized` 是个不检查对账的占位门。**~~ **已修（`cf70afb`，2026-08-01）**，
   收口过程见下节 —— 查证后发现它不止"没检查"，而是**反的**；且同一个结构里另有两个字段同病。

## 遗留项 3 收口：readiness 问的是引擎，不再是它被要求跑的模式（`cf70afb`，2026-08-01）

### 查证推翻了原来的描述：它不是"没检查"，是**反的**

原式 `reconciliation_initialized = (trading_mode == "sandbox")`。实证 `_build_exec_plan`
（`host.py:411-438`）：**sandbox 是唯一关掉对账的模式**（本地撮合、没有交易所账户可对，返回 `False`），
testnet / live 才真的跑对账（返回 `True`）。所以这个字段**恰好在什么都没对账时为真，在真的对账了时为假**
—— 它让 `ready` 在唯一会碰到交易所的两个模式上永不可达。

### 同一个结构里另外两个字段也不是证据

`EngineReadinessChecks` 自称 "Evidence that a created task has crossed every mandatory ready
boundary"。七个字段里三个不是证据：

| 字段 | 原实现 | 实际检查了什么 |
|---|---|---|
| `reconciliation_initialized` | `trading_mode == "sandbox"` | 反的，见上 |
| `portfolio_initialized` | `getattr(kernel, "portfolio", None) is not None` | 常量 —— kernel 总有 portfolio |
| `strategy_accepting_lifecycle` | `not task.done()` | 与上面两行的 `node_task_alive` **逐字符相同** |

### 为什么三个一起修，而不是只修被点名的那个

**只修第一个会比三个都不修更糟。** `ready` 会从"永不为真"变成"靠两个常量和一个重复项为真" ——
永不通过的门至少是吵的，永远通过的门是静的。所以这次把三个一起换成引擎真能被问到的东西，
并在此显式声明**我扩大了范围**，理由如上。

### 让对账可判定的依据（NT 没有"对账完成"标志，这点先查清了）

`LiveExecutionEngine` 上没有任何 `reconciliation_complete` 之类的属性，只有
`reconciliation`（**是否会跑**，不是是否跑完）。可用的判据来自 kernel 自己的启动序列
（`NautilusKernel.start_async` 源码实证）：

```
_start_engines() → _connect_clients() → _await_engines_connected()
  → if exec_engine.reconciliation: _await_execution_reconciliation()   ← 失败即 return
  → _initialize_portfolio() → _await_portfolio_initialization()        ← 失败即 return
  → _trader.start()
```

对账失败时 `start_async` **直接返回，trader 永不启动**。所以 **trader 在跑 = 对账那一步已经过了**，
这是 NT 能提供的最接近完成信号的东西。

新实现：`trader_running and (reconciliation_enabled or trading_mode == "sandbox")`。
第二项不是多余的 —— trader 无论对账跑没跑都会启动，所以「在 testnet 上把对账关掉」这种配置错误
需要它自己那一条才拦得住。

### 验证

新增 9 条测试（`tests/engines/nautilus/test_readiness_checks_what_it_claims.py`）。
**注意其中 8 条在修复前就是绿的** —— 因为修复前 testnet 上一切都不 ready，它们全是空洞通过。
使它们变得有意义的是基线那条（`test_a_testnet_node_can_become_ready_at_all`，修复前红）：
基线证明完整节点确实 ready，其余每条只改一个字段并断言不 ready，于是逐条隔离成立。
**这组测试是自证的，但只在基线通过之后** —— 这一点值得记下来，因为"修复前就绿"很容易被读成"没用"。

全仓 2261 passed / 25 skipped / 1 xfailed；`ruff check` 干净；`scripts/check-authority-docs.py`
通过（它 pin 的是七个字段**名**，本次一个都没改名，只改了它们的含义 —— 所以证据链不动）。

### 仍未做

**真机未验。** 离线通道压根不调 `wait_ready`，所以这条路在本通道上跑不到；**签名通道调**，
真机验证要在签名通道上取。这条修的是签名通道的真问题，不是离线通道的。

## 偏离与改进日志 (Deviations & Improvements)

- 若实施中发现 `portfolio.equity(venue)` 的条目集其实**不**随账户币种数变化（即我对多条目来源的判断
  有误），记在这里 —— 那会推翻发现 A 的根因，须重新实证再动手。
- B 的选型（B1/B2/B3）定下来后记在这里，连同对"就绪"如何定义的判断依据。
- **DEV-27-C-MISDIAGNOSED-AS-TIMING（2026-07-31，我的两次错判，都已撤回）**：修好发现 A 后守卫改报
  `mark_price_unavailable`，我先说「熔断因为标记价格还没到而误平仓」，又据此说「B1 是必需的」。owner 要求
  先调查清楚，查证后**两句都被推翻**：那 11 分钟零 bar 到达，且 `mark_price` 与 `MID` 都是结构性拿不到
  （零订阅 + 不订阅 quote），与"等多久"无关。**成因**：`mark_price_unavailable` 这个 reason 读起来像
  "暂时没有"，我按字面把它当成时序问题，没有去查"这个数据本来由谁提供"。**教训**：`X_unavailable` 类
  reason 不区分"还没到"与"没人给"，判断是哪一种必须查供给侧（谁订阅 / 谁写入），不能从措辞推断 ——
  与 lesson #9/#11「不信推理信实证」同源，这次的具体形态是**把缺失当成延迟**。
- **发现 C 是被发现 A 挡住的。** `_resolve_equity` 在 `:124-130` 先返回，代码走不到 `:135-141` 的定价段。
  修一个缺陷让下一个缺陷现形，这不是"越修越糟"，而是同一条路上本来就串着两个 —— 但它意味着
  **A 的真机验证通过，不能推断这条路已经可用**。

## Follow-up hooks（不属于本 plan scope，登记以防遗漏）

- **testnet 上有一个遗留空仓，且交易所侧状态在逐轮累积。** `-0.0070 BTC`（`BTCUSDT-PERP`，均价
  `63995.10`）。首轮 runner 于 `11:08` 停止（容器 `exit 137`，SIGKILL —— 走了一段优雅关闭后被 compose
  超时杀掉），**关停没有撤 SL**。此后为取 Plan 25 / 26 的真机判据又跑了两轮，每轮 reconciliation 都把
  这个空仓重新捡进来、策略再挂一张 reduce-only `STOP_MARKET`，于是累计出三个我方 32 字符 id
  （`a39769df…` / `4ccc9459…` / `5dbb46ff…`，均 `OrderAccepted`）。**本 plan 的两个发现在这两轮里各自
  又复现一次**（`instrument_count: 0` 与 `portfolio_equity_ambiguous` 均一字不差），所以它们不是一次性
  巧合。持仓与这些挂单仍在、无人管理：要平掉需要有意识地操作，否则每次启动都多一层「账户既有持仓」，
  而**发现 B 恰好让启动期的 flatten 对它无效**。
- **PS 实跑取的是镜像里的 toolkit，不是仓里的源码。** 当前本地 `custos-runner:v0.3.0` 的
  `image.revision = b55c211`，而 custos HEAD 已到 `3353e74` —— **Plan 26 的修复不在这个镜像里**。
  Plan 26 那条 PS 侧真机判据需要**再重建一次镜像**才能取。
- **PS 侧的 `CUSTOS_IMAGE_MIN_REV` 是下限、不强制任何具体修复。** 当前门槛 `cec0f8a9` 比重建前那个
  无 `-4015` 修复的 `ea7e2bf4` 还老，所以旧镜像照样过检。要让"镜像含某个修复"成为机械要求，得抬这个
  常量 —— 属 PS 侧，且它散在 5 处（Makefile、一个测试常量、README、dev-guide、05-deployment）外加
  PS Plan 53 正文（PS 侧有守护测试断言该哈希出现在 active plan 里）。
