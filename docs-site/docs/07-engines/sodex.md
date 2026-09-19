---
title: "SoDEX connector"
sidebar_position: 4
---

Custos provides separate SoDEX spot and perpetual connectors. Custos wires `sodex` to `SODEX_SPOT` and `sodex_perpetual` to `SODEX_PERPS`.

## Support boundaries

| Path | Current behavior |
|---|---|
| Sandbox adapter | Production market data with local matching |
| Testnet adapter | Builds a testnet execution client when all required account fields are supplied |
| Offline CLI testnet | Incomplete: its spec does not carry the required SoDEX account fields |
| Live | Refused by both the host declaration and execution config builder |

The offline spec rejects unknown fields and currently has no `wallet_address` or `sodex_account_id`. Adding these keys to a JSON example will fail validation. The offline CLI cannot currently supply the complete input required for SoDEX testnet, so no launch recipe is provided for that path.

## Sandbox configuration

Use a compatible strategy directory with `config.yaml` and select `--engine nautilus`. Set the connector, venue-native pair and starting balance together:

| Connector | Example pair | Example balance | Account type |
|---|---|---|---|
| `sodex` | `vBTC_vUSDC` | `10000 vUSDC` | Cash |
| `sodex_perpetual` | `BTC-USD` | `10000 USD` | Margin |

These illustrate symbol spelling; check the feed's available instruments. Custos uses the pair verbatim and appends `.SODEX_SPOT` or `.SODEX_PERPS`. It does not translate Binance-style symbols. Sandbox data requires no venue secret, but the offline runner still resolves its declared vault entry; use a local demo entry only for sandbox simulation.

The lifecycle-only directory in [standalone sandbox](/getting-started/standalone-sandbox) is not a trading strategy. Replace it with compatible strategy code before selecting Nautilus.

## Testnet account inputs

The adapter requires vault `api_key` (key name) and `api_secret` (signing private key), plus spec `wallet_address` and numeric `sodex_account_id`. Missing values fail before client construction; environment credentials are not a fallback. Spot and perpetual identities must match their own venue accounts.

## Diagnosing startup

An empty instrument set usually requires checking the exact pair and selected network. `portfolio_prices_missing:<instrument>` identifies the missing valuation input. Check the mark/quote asset named by the error; a connected data client can still lack a price needed for equity valuation.

Track upstream support and complete input-path verification before treating testnet or production execution as available.
