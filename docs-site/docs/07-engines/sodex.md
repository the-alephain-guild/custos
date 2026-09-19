---
title: "SoDEX connector"
sidebar_position: 4
---

Custos provides separate SoDEX spot and perpetual connectors. Custos wires `sodex` to `SODEX_SPOT` and `sodex_perpetual` to `SODEX_PERPS`.

## Configuration and modes

Sandbox uses production prices and local matching. Testnet uses the test network execution client. Live uses mainnet and requires runtime and signed deployment approval.

Set `wallet_address`, positive integer `sodex_account_id`, `settlement_currency` and `margin_mode` (`cross` or `isolated`) inside `nautilus_config.venue`. These are signed engine settings, not top-level deployment fields. Spot leverage must be 1. Perpetual leverage and margin mode must match the account; startup compares them with independent exchange state.

Vault `api_key` is the key name and `api_secret` is the signing private key. Spot and perpetuals use their own accounts and keys. The wallet/account must identify the trading account; the API key address is not a substitute.

Use venue-native symbols, such as `vBTC_vUSDC` for spot or `BTC-USD` for perpetuals. Read settlement currency from exchange instrument metadata instead of inferring USD from the symbol. `vUSDC` and `USDC` are distinct assets; preserve the actual asset in balances and signed risk policy.

Independent evidence reads balances, positions, trades, funding and closed-position history. Truncated queries, mismatched accounts or inconsistent block snapshots refuse reconciliation. Liquidation and additional builder fees remain refused until their economic events have complete reconciliation support.

See [production preparation](/operator-guide/production-preparation). Local configuration tests do not establish acceptance of an exchange account on testnet or production.
