---
title: "策略工具包"
sidebar_position: 1
---

工具包定义策略执行 ABI 及 Custos 消费的产物元数据。ARX 提供发布选择和部署意图，Custos 在导入代码前验证材料。

## 软件包

<!-- generated:packages -->

| 包 | 源码版本 | Python |
|---|---|---|
| `custos-strategy-toolkit` | `0.1.0` | `>=3.11` |
| `custos-strategy-toolkit-nautilus` | `0.1.0` | `>=3.12,<3.13` |

<!-- /generated:packages -->

Nautilus 包要求 base toolkit 版本精确匹配。支持平台与依赖安装方式见[安装指南](/getting-started/installation)。

## 执行 ABI

entry-point group 为 `alephain.strategy_runtime.v1`。适配器接收验证后的 `StrategyExecutionContext`，以 `deployment_instance_id` 寻址运行实例。spec id、摘要和 generation 记录来源与顺序。

有效配置使用有限 `Decimal` 数字解析，拒绝重复键，递归冻结容器并重算 `effective_config_digest`。适配器必须使用传入配置，不能另读默认值或修改配置。

`sha256-canonical-json-v1` 使用紧凑 UTF-8、递归排序的对象键、原序数组和有限 Decimal 值。实现其他编码器时应使用契约测试向量。

## 产物边界

`StrategyArtifactRefV1` 描述签名前的可执行文件、manifest、运行产物、SBOM 和契约 schema。独立 attestation、审批、发布选择和部署状态不属于该引用。

runner 通过认证后的发布解析接口取得完整 release BOM，验证所有成员和独立证据，将下载内容隔离，原子激活不可变目录，完成验证后再导入。产物元数据不能选择自己的信任根。详见[签名验证](/toolkit/artifact-signing)与[物化](/toolkit/artifact-materialization)。

## 开发检查

`make toolkit-typecheck` 检查 base 和 Nautilus 包。修改策略集成时运行相关契约测试，并使用匹配版本的工具包。

当前尚未开放生产使用。支持流程与限制见[发布状态](/release-governance/release-status)。
