---
title: "SoDEX connector"
sidebar_position: 4
---

Custos 提供独立的 SoDEX 现货和永续 connector。Custos 将 `sodex` 映射到 `SODEX_SPOT`，将 `sodex_perpetual` 映射到 `SODEX_PERPS`。

## 配置与模式

sandbox 使用生产行情和本地撮合。testnet 使用测试网执行客户端；live 使用主网，并要求运行环境和签名部署批准。

在 `nautilus_config.venue` 中配置 `wallet_address`、正整数 `sodex_account_id`、`settlement_currency` 和 `margin_mode`（`cross` 或 `isolated`）。这些字段属于签名引擎配置，不能放在部署文档顶层。现货杠杆须为 1；永续账户杠杆和保证金模式须与签名配置一致，启动前会读取交易所状态进行核实。

金库的 `api_key` 是密钥名称，`api_secret` 是签名私钥。现货与永续使用各自账户及密钥。钱包地址和账户编号须对应交易账户，不能用 API 密钥地址代替。

使用交易所原生交易对，例如现货 `vBTC_vUSDC`、永续 `BTC-USD`。从交易所产品元数据确认结算币种，不能从 `USD` 字样推断为美元。`vUSDC` 与 `USDC` 是不同资产；金库、初始余额和签名风险策略须保留实际币种。

独立账户证据读取余额、持仓、成交、资金费用和已平仓历史；查询截断、账户不匹配或不一致的区块快照都会拒绝核对。清算与额外 builder 费用保持拒绝，直到相应经济事件已有完整核对支持。

## 订单类型与止损模式

SoDEX 的现货和永续都只接受市价单与限价单，交易所不能挂止损单。因此 `sodex` 或 `sodex_perpetual` 上的策略须把 `risk.trade.sl_tp_mode` 设为 `tick`；其他模式在启动时以 `[STARTUP]` 错误拒绝运行。tick 模式没有止损：它按行情监控止盈与移动止损出场，并以市价单平仓。敞口需要用别的方式封顶，例如 `max_total_notional` 上限（见[断线时的安全控制](/trust-model/safety-survives-disconnect)）。

现货账户是现金账户，交易所拒绝 reduce-only 订单。在现金账户上，策略 toolkit 发出的出场单与保护单都不带 `reduce_only`。现货出场最多卖出可用的基础币余额，并扣除策略启动时账户里已有的、不足一个数量步长的部分；低于交易对最小数量或最小名义金额的出场单不会发出，策略会在日志中写明原因。

部署步骤见[生产准备](/operator-guide/production-preparation)。本地配置测试不代表交易所账户已通过测试网或生产验收。
