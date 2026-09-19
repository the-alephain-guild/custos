---
title: "引擎规划"
sidebar_position: 3
---

Custos 当前提供 NautilusTrader 宿主和 sandbox 模拟宿主。`hummingbot`、`freqtrade` 等预留 extra 不会安装可运行的引擎集成。

## 当前接口

引擎适配器必须实现 `ExecutionEngineProtocol` 中的生命周期、能力、就绪、终止事件、风险和连接契约，并保留本地凭据处理、执行准入、风险控制及精确金额表示。

当前 Nautilus 宿主按 connector 声明 sandbox/testnet/live 能力，但 daemon 仍禁用 live。实际限制见[NautilusTrader](/engines/nautilus-trader)，验收情况见[发布状态](/release-governance/release-status)。

## 候选方向

| 候选 | 主要集成工作 |
|---|---|
| Hummingbot | 将独立 bot 配置与生命周期适配到 runner 监督；策略需要单独集成 |
| Freqtrade | 映射策略/配置模型与生命周期，不对外暴露 runner 管理代理 |
| 原生引擎绑定 | 有实测性能需求时，再评估进程内扩展或受监督进程边界 |

这些是设计候选，不是已排期发布。新适配器需要验证就绪、停止和失败行为，并完整接入安全与观测；只实现少数生命周期方法不够。
