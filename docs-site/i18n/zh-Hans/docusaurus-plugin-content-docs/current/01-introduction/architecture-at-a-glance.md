---
title: "架构概览"
sidebar_position: 3
---

Custos 的签名通道和离线通道分别组装运行流程，共用引擎与凭据层。

```text
ARX 签名指令 -> 验证 -> 持久化目标状态 -> 产物激活
                                         |
                                         v
本地金库 -----------------------------> 引擎宿主 -> 交易所
                                         |
签名 outbox <------------------------- 运行观测
     |
     +-----> ARX

操作者 -> 本地 NATS -> 离线协调器 -> 策略目录 -> 引擎宿主
                           |
                           +-----> 本地部署状态
```

本文将 ARX 作为一个产品介绍。身份授权与指令、事实接口具有不同的可用性要求；注册端点不可用不等于消息传输不可用。

## 组件

| 组件 | 职责 |
|---|---|
| 身份与金库 | 加密本地密钥、绑定机器元数据、解析交易所凭据 |
| 签名指令接入 | 验证原始字节和 subject，持久化目标状态 |
| 产物运行时 | 解析、验证、隔离并激活签名通道的发布产物 |
| 离线协调器 | 从策略目录执行操作者的 sandbox/testnet spec |
| 引擎宿主 | 创建和监督 node，报告连接与投资组合状态 |
| 本地安全检查 | 独立于传输检查敞口、回撤并执行风险控制 |
| 观测投递 | 签名通道使用持久化 outbox；离线通道发布本地状态 |

## 运行边界

签名运行时以 `deployment_instance_id` 操作实例，spec id、摘要和 generation 记录配置来源与顺序。离线运行标识由 `spec_id` 确定性派生，不是 ARX 的正式业务身份。

supervisor 可通过重复 `--enabled-mode` 为签名通道启用多个传输会话。Nautilus 2 宿主在同一 event loop 上只允许一个活动 node。并发运行多个 node 需要独立 runner 进程和状态目录；启用多个模式不会解除这一限制。

交易所凭据保留在宿主机上。金额计算使用 `Decimal`，wire 使用整数或规范十进制字符串。当前 daemon 组合未启用 live 执行准入。

## 阅读路径

- [独立 sandbox](/getting-started/standalone-sandbox)：验证本地部署流程。
- [签名 sandbox](/getting-started/first-sandbox-run)：连接已注册 runner。
- [NautilusTrader](/engines/nautilus-trader)：引擎与 connector 限制。
- [断线时的安全控制](/trust-model/safety-survives-disconnect)：各通道的执行边界。
