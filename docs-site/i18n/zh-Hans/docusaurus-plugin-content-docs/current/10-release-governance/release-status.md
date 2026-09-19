---
title: "发布状态"
sidebar_position: 1
---

Custos 支持开发和 sandbox/testnet 流程。live 默认关闭：只有匹配当前安装镜像的运行环境批准材料通过验证，并满足签名部署、凭据和风险策略要求，才具备执行准入。新增源码能力不表示已有发行镜像完成生产验收。

| 流程 | 当前支持 |
|---|---|
| 独立 sandbox | 使用 `sandbox-sim` 验证本地生命周期 |
| Nautilus sandbox | 兼容策略、行情与本地模拟成交 |
| 离线 testnet | 支持的交易所 connector 与测试网凭据；需查看各交易所限制 |
| 签名部署 | 要求注册、签发的授权和验证后的发布输入 |
| Live 交易 | 须验证运行环境批准材料及签名部署授权 |

## 安装与更新

源码和本地容器配置见[安装指南](/getting-started/installation)。更换运行时前阅读[升级指南](/release-governance/upgrade-paths)。使用分发产物时，以正式发布提供的版本及验证说明为准。

构建、健康探针或 sandbox 运行成功不能证明生产就绪。使用测试网账户前，确认所选 connector 的支持范围和限制。[SoDEX 指南](/engines/sodex)说明账户配置要求。

支持窗口见[SemVer 与 LTS](/release-governance/semver-lts)。本页说明产品可用范围，不代表新的稳定版本发布公告。
