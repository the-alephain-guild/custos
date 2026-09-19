---
title: "什么是 Custos？"
sidebar_position: 1
---

Custos 是非托管执行 runner，运行在你自己的基础设施上，在本地保存交易所凭据，并通过 NautilusTrader 执行和监督策略。

## 选择部署通道

| 通道 | 输入 | 身份 | 输出 | 模式 |
|---|---|---|---|---|
| 签名通道 | ARX 签发的目标状态 | 注册后的机器身份 | 签名 RunnerFact 和策略信号 | 可执行 sandbox/testnet；当前未启用 live 执行准入 |
| 离线通道 | 操作者发布的 `OfflineDeploymentSpec` | 本地独立身份或已注册身份 | 未签名的本地部署状态 | 仅 sandbox/testnet |

离线通道用于不依赖 ARX 后端的本地策略开发，仍然使用 NATS，也可能连接行情或测试网。它需要显式选择，不能运行 live，不产生晋升证据，也不会在签名通道失败后自动接管。

验证本地部署流程可从[独立 sandbox](/getting-started/standalone-sandbox)开始；连接 ARX 时先完成[注册](/getting-started/enrollment)。

## 职责

Custos 保存机器和交易所凭据、验证签名输入、应用目标状态、监督引擎并执行本地安全检查。签名通道中的授权、部署审批、产物选择和业务记录由 ARX 管理。runner 的签名观测用于上游处理，本身不构成审批。

## 术语

| 术语 | 含义 |
|---|---|
| `DeploymentSpec` | 签名通道的不可变配置；标识和摘要用于记录配置来源 |
| `DeploymentInstance` | 以 `deployment_instance_id` 寻址的运行实例；多个实例可以引用同一 spec |
| Generation | 单调递增的目标状态版本；同一版本的重投递不会创建新实例 |
| Engine handle | 与部署实例关联的本地引擎资源 |
| `RunnerFact` | 已注册 runner 发出的签名观测 |
| `OfflineDeploymentSpec` | 操作者在 sandbox/testnet 使用的独立未签名契约 |

签名运行时先验证指令原始字节和 subject，再解释载荷。租户、模式、runner 和实例身份必须一致。详见[协调循环](/concepts/reconcile-loop)。

## 当前支持情况

引擎能力声明、软件包发布和生产验收是不同状态。选择产物前请查看[发布状态](/release-governance/release-status)。当前 daemon 组合未启用 live 执行。
