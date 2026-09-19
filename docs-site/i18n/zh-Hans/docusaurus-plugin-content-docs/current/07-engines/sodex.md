---
title: "SoDEX connector"
sidebar_position: 4
---

Custos 提供独立的 SoDEX 现货和永续 connector。Custos 将 `sodex` 映射到 `SODEX_SPOT`，将 `sodex_perpetual` 映射到 `SODEX_PERPS`。

## 支持边界

| 路径 | 当前行为 |
|---|---|
| Sandbox 适配器 | 读取正式行情，在本地撮合 |
| Testnet 适配器 | 账户字段齐全时可构建测试网执行客户端 |
| 离线 CLI testnet | 尚不完整：spec 无法携带 SoDEX 要求的账户字段 |
| Live | 宿主声明和执行配置构建器均拒绝 |

离线 spec 拒绝未知字段，目前没有 `wallet_address` 或 `sodex_account_id`。把这两个字段直接加入 JSON 会导致校验失败。离线 CLI 当前无法提供 SoDEX testnet 要求的完整输入，因此本页不提供该路径的启动命令。

## Sandbox 配置

使用带 `config.yaml` 的兼容策略目录，并选择 `--engine nautilus`。connector、交易所原生交易对和初始余额需要匹配：

| Connector | 交易对示例 | 余额示例 | 账户类型 |
|---|---|---|---|
| `sodex` | `vBTC_vUSDC` | `10000 vUSDC` | 现金 |
| `sodex_perpetual` | `BTC-USD` | `10000 USD` | 保证金 |

这些示例用于说明命名格式，实际标的需与行情端支持的集合核对。Custos 原样使用 pair，再追加 `.SODEX_SPOT` 或 `.SODEX_PERPS`，不会转换 Binance 风格的交易对。Sandbox 行情不要求交易所密钥，但离线 runner 仍会解析声明的金库条目；本地演示条目仅用于 sandbox 模拟。

[独立 sandbox](/getting-started/standalone-sandbox)中的目录仅用于生命周期练习，不是交易策略。选择 Nautilus 前需替换为兼容的策略代码。

## Testnet 账户输入

适配器要求金库中的 `api_key`（密钥名称）和 `api_secret`（签名私钥），以及 spec 中的 `wallet_address` 和数值型 `sodex_account_id`。缺失时在创建客户端前失败，不会退回读取环境中的交易凭据。现货与永续身份必须匹配各自账户。

## 启动排错

标的集合为空时，先核对 pair 的精确名称与网络选择。`portfolio_prices_missing:<instrument>` 指明估值缺少的标的，应检查错误中对应的标记价格或计价资产。行情客户端已连接不代表权益估值所需的价格全部可用。

确认上游支持并验证完整输入路径后，才能把 testnet 或生产执行列为可用能力。
