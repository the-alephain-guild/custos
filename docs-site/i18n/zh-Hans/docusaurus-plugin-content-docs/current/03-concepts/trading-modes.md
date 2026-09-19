---
title: "交易模式"
sidebar_position: 2
---

Custos 接受三种交易模式。

| 模式 | 行情 | 执行 | 资金 |
|---|---|---|---|
| `sandbox` | Nautilus 读取实时行情；`sandbox-sim` 不连接行情 | 本地模拟 | 模拟资金 |
| `testnet` | 交易所测试网 | 测试网订单 | 测试资金 |
| `live` | 正式交易所 | 正式订单 | 真实资金；当前 daemon 准入仍阻止执行 |

## 选择通道和进程模式

签名通道要求至少一个 `--enabled-mode`。重复该参数可建立按模式隔离的传输会话；每条签名指令的模式必须属于已配置集合。

```bash
arx-runner start --enabled-mode sandbox --enabled-mode testnet --reconcile
```

这段命令只展示模式选择。身份、传输、签名公钥和产物前提见[部署指南](/operator-guide/deployment)。

离线通道通过 `--reconcile-strategy-id` 选择，从每份 `OfflineDeploymentSpec` 读取模式，仅接受 sandbox/testnet，不要求 `--enabled-mode`。`deployment validate/publish --mode` 可额外断言 spec 应使用的模式，不能覆盖 spec 中的值。

Nautilus 2 宿主在同一 event loop 上只允许一个活动 node。启用多个模式不代表同一进程能并发运行多个部署。

## 执行准入

签名指令需要通过模式绑定、宿主与 connector 支持、产物能力及凭据范围检查。live 还要求启用执行能力并携带签名晋升证据。live 默认关闭，匹配的运行环境批准材料通过密码学验证后才具备准入条件。

离线输入使用独立契约和准入路径，不要求签名部署审批，但仍拒绝 live，并保留本地凭据和安全检查。离线结果及 sandbox 开发产物都不能在本地晋升为生产。

## 传输

签名 sandbox/testnet 会话使用 `--nats-sim-*`，签名 live 传输使用 `--nats-live-*`。传输连接成功不会启用 live 执行。离线通道使用 `--nats-url` 指定的自有 broker。

选择交易所和模式前，请查看[connector 支持情况](/engines/nautilus-trader)。
