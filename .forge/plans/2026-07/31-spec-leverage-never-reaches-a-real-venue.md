# 31 — spec 里的 leverage 到不了真实交易所，只到 sandbox

> **Status**: 🔲 Not started —— 只记录，不实施
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

## 该做什么

1. 给 testnet/live 的 exec config 接上 `futures_leverages`（含类型转换）
2. 同时考虑 `futures_margin_types`（isolated vs cross 同样影响强平距离，spec 目前完全没有表达）
3. 验证下发是否真的生效 —— 读回交易所侧的杠杆，而不是只看配置传了
4. 顺手核实 sandbox 那条路径的类型不匹配有没有让它静默失效
