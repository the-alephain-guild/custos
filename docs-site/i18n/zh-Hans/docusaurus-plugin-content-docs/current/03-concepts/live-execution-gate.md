---
title: "执行准入"
sidebar_position: 3
---

签名部署在创建引擎前执行准入检查。被拒绝的部署产生类型化终止结果；可恢复运行故障使用独立的有界重试路径。

## 签名通道检查

| 检查 | 适用范围 |
|---|---|
| 已验证的产物运行能力 | 所有模式 |
| 运行模式与签名指令匹配 | 所有模式 |
| 宿主支持模式与 connector | 所有模式 |
| 凭据声明 `trade_no_withdraw` | Testnet/live |
| 运行组合已启用 live 执行 | Live |
| 存在正确绑定的签名晋升证据 | Live |

live 默认关闭。签名 daemon 会验证与当前镜像及源码版本匹配的运行环境批准材料。通过后仍须验证每次部署的签名晋升证据、产物和风险策略，详见[生产准备](/operator-guide/production-preparation)。

`sandbox-sim` 仅支持 sandbox。Nautilus 的 connector 声明按模式区分，详见自动生成的[支持表](/engines/nautilus-trader)。Binance、SoDEX 和 OKX 均提供现货及线性永续配置路径；账户与发布验收仍需分别完成。

## 离线准入

离线操作通过 `--reconcile-strategy-id` 显式选择。独立模式门在解析/发布 spec 或读取凭据前拒绝 live。它使用本地策略材料，不依赖签名发布能力，也不产生晋升证据。本地安全检查仍然运行。

## 核对实现

签名 supervisor 的准入检查位于 `src/custos/core/engine_lifecycle.py`，离线边界位于 `src/custos/offline/mode_guard.py`。相关测试包括 `tests/test_engine_lifecycle.py`、`tests/test_nautilus_host_capability.py`、`tests/test_nt_venue_wiring.py` 和 `tests/test_offline_mode_guard.py`。

准入确认执行资格；持续敞口和回撤控制见[断线时的安全控制](/trust-model/safety-survives-disconnect)。
