---
title: "OKX 连接器"
sidebar_position: 5
---

`okx` 用于现金现货，`okx_perpetual` 用于以 USDT/USDC 结算的线性永续。例如 `BTC-USDT` 分别映射为 `BTC-USDT.OKX` 和 `BTC-USDT-SWAP.OKX`。反向合约和杠杆现货会被拒绝。

sandbox 使用公开行情及本地撮合。启动前移除 runner 环境中继承的 `OKX_API_KEY`、`OKX_API_SECRET` 和 `OKX_API_PASSPHRASE`。testnet 使用交易所模拟环境；live 须完成[生产准备](/operator-guide/production-preparation)中的签名批准验证。

API key、secret 和 passphrase 均存入本地金库。`vault put --api-passphrase-env OKX_PROVISIONING_PASSPHRASE` 从指定环境变量读取 passphrase，并与凭据一起加密。配置完成后清除该变量，不能把密钥值写入部署文件。

签名的 `nautilus_config.venue` 接受 `region`（`global`、`eea`、`us`）和 `margin_mode`（`cross`、`isolated`）。选择账户所属区域。永续账户须使用净持仓模式，并在启动前设置请求的杠杆；预检会读取并比较这些设置。现货使用现金执行，杠杆为 1。

签名成交证据会把合约张数换算为基础资产数量，保留手续费实际币种及返佣符号。独立 REST 证据分页读取成交、已平仓头寸和资金费用。数据含糊、不受支持或被截断时，核对失败。实盘验收前须在实际模拟账户验证完整交易周期。
