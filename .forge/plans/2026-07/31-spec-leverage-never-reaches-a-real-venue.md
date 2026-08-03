# 31 — spec 里的 leverage 到不了真实交易所，只到 sandbox

> **Status**: ⏳ In Progress —— 接线已落地并**真机验完**（custos `9b3af5f`）：调用到达交易所
> （`Set default leverage BTCUSDT 3X`，改动前 46 小时日志 0 次），保证金比由 1.0 变为 **3.00**。
> 余下只有第 4 项：sandbox 那条路径的类型不匹配是否让它静默失效，**未查**。
> `futures_margin_types` 有意不设，见 §进度 2
> **Created**: 2026-08-01
> **Project**: custos (`tesseract-trading/custos/`)
> **Depends on**: 无 —— 现有代码即可复现，且已在真机观察到
> **Blocks**: 策略的「止损 vs 强平距离」启动校验（它信 spec 的 leverage，而没有东西让交易所与之一致）
> **For Claude**: `/forge:execute`
> **multi_session_scope**: false

## 观察到的事实

2026-08-01 testnet，spec 声明 `leverage: 3`，交易所回报：

```
持仓          -0.0071 BTC
标记价        ~63066
名义          ~447.77 USDT
initial margin 447.77144 USDT
```

保证金 = 全额名义，即**实际 1x**，不是 3x。

## 不是策略的问题

toolkit 里 `leverage` 只被用来**推理**，从不设置：`adapter/trading_config.py:145-190` 拿它估算强平
距离（`liq_distance = 1.0 / leverage`）来校验固定止损是否可能永远触发不了。这是对的分工 ——
设置交易所杠杆不是策略的事，是执行客户端的事。

## 是 custos 的 venue builder 少接了一根线

`engines/nautilus/venue_binance.py:138` 的 `build_futures_leverages(spec)` 把 leverage 算出来了，
但它**只**被 `build_exec_client_config_sandbox`（`:206-217`）消费。testnet 与 live 走的
`_build_binance_exec_config`（`:240-250`）从头到尾没碰过它，于是交易所账户的默认杠杆生效。

而 NT 的 `BinanceExecClientConfig` **有**这个字段：

```
futures_leverages   : dict[BinanceSymbol, int] | None
futures_margin_types: dict[BinanceSymbol, BinanceFuturesMarginType] | None
```

留空 → `None` → 不下发 → 用账户默认。

`build_futures_leverages` 的 docstring 自己写着「(and any future Binance live exec config)」——
作者知道当时没接。

## 接的时候有个类型坑

三处类型互不相同：

| 来源 | 类型 |
|---|---|
| `build_futures_leverages` 返回 | `dict[InstrumentId, Decimal]` |
| `SandboxExecutionClientConfig.leverages` 声明 | `dict[str, float] \| None` |
| `BinanceExecClientConfig.futures_leverages` 声明 | `dict[BinanceSymbol, int] \| None` |

所以那句「drops straight into」对 Binance 侧是错的：**键和值都不是**。需要单独一个 builder 做
`InstrumentId → BinanceSymbol`、`Decimal → int` 的转换。

**顺带的疑问，本 plan 未核实**：sandbox 声明的是 `dict[str, float]`，实际拿到的是
`dict[InstrumentId, Decimal]`。msgspec 直接构造不做类型校验（PS lesson #36 就是这条），
所以这个不匹配是静默通过的。sandbox 的杠杆到底有没有生效，取决于 `SimulatedExchange` 怎么用这些
键 —— **本次没有查证**，接线时要一起验。

## 为什么现在无害，但方向是错的

眼下 spec 说 3x、交易所是 1x，策略按 3x 校验止损（假设强平距离 33%），实际强平距离 ~100% ——
**校验比现实严格**，偏安全。

危险的是反过来：交易所账户若被设成 20x 而 spec 写 3，校验会按 33% 的强平距离放行一个
5% 的止损，而真实强平距离只有 5% —— 止损可能永远来不及触发。**spec 的 leverage 是一个校验所
信任、却没有任何东西去兑现的假设**，这跟 PS lesson #21（比例字段无单一真理源）是同一类问题。

## 进度

1. **已做** —— `build_binance_futures_leverages` 新增，testnet/live 的 exec config 接上
   `futures_leverages`（custos `9b3af5f`）。转换是必需的而不是顺手：sandbox 那份键 `InstrumentId`
   值 `Decimal`，Binance 这份键 `BinanceSymbol` 值 `int`，且 `BinanceSymbol` 会把 `-PERP` 去掉。
   测试断言的是**键与值的类型**而不只是相等 —— 因为 msgspec 直接构造不校验，把 sandbox 那份塞进来
   会被照单全收然后什么也不做。用「故意塞 sandbox 那份」证伪过，三条测试全红。
2. **不做，且写明理由** —— `futures_margin_types` 保持不设。isolated 与 cross 的强平距离不同，
   而 spec 无从表达选哪个；在这里挑一个等于**在实现层发明策略**，而不是搬运声明。要做得先给 spec
   加字段，那是契约变更。
3. **已验** —— 调用到达 + 保证金比 3.00，见 §真机读回。
4. **待验** —— sandbox 那条路径的类型不匹配（声明 `dict[str, float]`、实际收 `dict[InstrumentId,
   Decimal]`）有没有让它静默失效。本次没查 `SimulatedExchange` 拿这些键做什么，**不要当作已确认可用**。

## 怎么算验过（第 3 项）

「配置传了」不等于「交易所改了」。判据是**读回**：重启后从运行时确认 BTCUSDT 的杠杆是 3 而不是账户
默认，或等价地看保证金比 —— 名义 447 的仓位，3x 下初始保证金应在 149 附近而不是 447。后者不需要额外
凭据，账户状态日志里就有，是本 plan 当初据以发现问题的同一个量。

## 真机读回（2026-08-03，镜像 `9b3af5f`）

重建镜像重启后，Binance exec client 的日志里出现了这一行：

```
04:20:00  ExecClient-BINANCE: Set default leverage BTCUSDT 3X
```

**它是新的**，不是一直都在：改动前 46 小时的日志 0 次、停机测试那份 0 次、现在 1 次。也就是说
`POST /fapi/v1/leverage` 这个调用**以前从来没发生过**，现在发生了 —— spec 的声明第一次真的到达
交易所。

### 保证金也验了：3.00

先踩了一个坑值得记下来。改动后那条 `locked=447.04 / initial=446.58` 看着像"没生效"，其实它的时间戳是
`04:20:00.825`，**比杠杆调用（`04:20:00.993`）早 168 毫秒** —— 是改动前的快照。而它之后的每一条
AccountState 都是 `margins=[]`、`locked=0`：那些是 WS 增量更新，只带 USDT 钱包余额，**根本不携带
保证金**。也就是说改动后的日志里既没有"生效"的证据，也没有"没生效"的证据 —— 差点把"读不到"当成
"是零"。

完整快照只在启动对账时来一次。所以趁着 `05:19` 开的那笔仓还在，重启读它：

```
net_position = -0.0071                 名义 ≈ 445.8
locked       = 148.58947299 USDT
initial      = 148.59164298 USDT       445.8 / 148.59 = 3.00
```

**对照改动前**：同样一笔 ~447 名义的仓位，initial margin 是 447.77 —— 比值 1.0。现在是 3.00。

第 3 项验完：**调用到达交易所，且杠杆确实生效**。
