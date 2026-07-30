# 27 — 启动期的守卫问了一个还没准备好的组合（equity 币种未声明 + flatten 在持仓到达前空转）

> **Status**: 🔲 Not started
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

有资金的多币种 testnet 账户上启动，不因币种未声明而误跳闸；且任何一次 fail-closed 的 flatten，
要么真的作用在持仓上，要么在记录里说清它没有。

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

## 偏离与改进日志 (Deviations & Improvements)

- 若实施中发现 `portfolio.equity(venue)` 的条目集其实**不**随账户币种数变化（即我对多条目来源的判断
  有误），记在这里 —— 那会推翻发现 A 的根因，须重新实证再动手。
- B 的选型（B1/B2/B3）定下来后记在这里，连同对"就绪"如何定义的判断依据。

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
