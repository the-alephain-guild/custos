---
title: "OKX connector"
sidebar_position: 5
---

Use `okx` for cash spot and `okx_perpetual` for linear USDT/USDC perpetuals. A pair such as `BTC-USDT` maps to `BTC-USDT.OKX` or `BTC-USDT-SWAP.OKX` respectively. Inverse contracts and leveraged spot are refused.

Sandbox uses public prices and local execution. Remove inherited `OKX_API_KEY`, `OKX_API_SECRET` and `OKX_API_PASSPHRASE` variables from the runner environment before sandbox startup. Testnet uses the exchange demo environment; live requires the signed approval described in [production preparation](/operator-guide/production-preparation).

Store API key, secret and passphrase in the local vault. `vault put --api-passphrase-env OKX_PROVISIONING_PASSPHRASE` reads the passphrase from the named variable and encrypts it with the credential. Unset that variable after provisioning. Never put secret values into the deployment document.

The signed `nautilus_config.venue` object accepts `region` (`global`, `eea`, `us`) and `margin_mode` (`cross`, `isolated`). Select the region matching the account. Perpetual accounts must use net position mode and have the requested leverage configured before startup; the preflight reads and compares those settings. Spot uses cash execution and leverage 1.

Contract sizes are converted to base quantities for signed fill evidence. Fees retain their actual currency, and rebates retain their sign. Independent REST evidence uses paginated fills, closed positions and funding records. Ambiguous, unsupported or truncated evidence fails reconciliation. Validate full trading cycles on the actual demo account before live acceptance.
