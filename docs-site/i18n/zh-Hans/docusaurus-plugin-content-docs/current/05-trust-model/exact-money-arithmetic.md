---
title: "金额运算精确"
sidebar_position: 5
---

# 金额运算精确

Custos 里每一个价格、数量与名义敞口都是 `Decimal`。在 wire 上它们是字符串。runner 里
没有任何金额值经过浮点。

## 它防的是什么

浮点误差不会自己报警。

在二进制浮点里 `0.1 + 0.2` 不等于 `0.3`。在名义敞口计算中，这点差异会产出一个几乎正确
的数 —— 它经得起肉眼检查，也能通过写得松的测试，而且朝着舍入的方向错。累积到一次持仓
限额检查上，"几乎正确"就是"上限守住了"和"多放过一单"之间的差别。

decimal 运算是消除这一类误差，而不是给它设个界。

## 两条规则

**从字符串构造**。用 `Decimal(str(x))`，绝不用 `Decimal(x)`（当 `x` 是 float 时）。
后者会静默继承你本想避开的二进制误差：`Decimal(0.1)` 是
`0.1000000000000000055511151231257827021181583404541015625`，而 `Decimal("0.1")`
恰好是 `0.1`。

这条最容易漏，因为两种写法扫一眼看不出差别，而且都产出一个 `Decimal`。

**序列化为字符串**。金额在 wire 上是 `"100.00"` 而不是 `100.0`。对多数解析器来说
JSON number 就是 float，所以把 `Decimal` 序列化成数字，等于把问题甩给读它的人。scale
按写入时保留；消费方需要时自行 quantize。

## 应用在哪

金额路径就是"数错了就会变成下错单或算错限额"的那些路径：

| 路径 | 模块 |
|---|---|
| 聚合敞口上限 | `src/custos/core/local_cap.py` |
| fallback 熔断器（回撤、名义敞口） | `src/custos/core/fallback_breaker.py` |
| 订单预留边界 | `src/custos/core/order_reservation_boundary.py` |
| 引擎协议面 | `src/custos/core/engine_protocol.py` |
| 签名事实产出 | `src/custos/core/runner_fact.py`、`runner_fact_producer.py` |
| 交易所适配器 | `src/custos/engines/nautilus/venue_binance.py` |

<!-- disclosure-ok: auditable source location -->

## 如何验证

```bash
grep -rnE 'float\(.*price|float\(.*amount|float\(.*notional' src/
```

在干净的树上没有输出。

这条 grep 抓的是显式转换。更隐蔽的情形是 `Decimal(x)` 而 `x` 本来就是 float —— 所以
构造规则那条值得用读的方式检查，而不是靠模式匹配。

`tests/test_nt_risk_engine.py` 覆盖熔断器与上限的运算；
`tests/test_runner_fact_store.py` 覆盖持久化与 wire 表示。
<!-- disclosure-ok: auditable source location -->

完整流程见[审计清单](./audit-checklist)。

## 这条保证不覆盖什么

运算精确意味着 Custos 算出来的数就是它想算的那个数。它不保证那个数是个好主意 —— 一个
策略完全可以以完美的精度亏钱。
