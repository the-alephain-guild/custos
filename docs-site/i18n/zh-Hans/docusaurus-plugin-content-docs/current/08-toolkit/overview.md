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

Nautilus 包要求 base toolkit 版本精确匹配。固定 fork wheel 和平台要求见[安装指南](/getting-started/installation)。

## 执行 ABI

entry-point group 为 `alephain.strategy_runtime.v1`。适配器接收验证后的 `StrategyExecutionContext`，以 `deployment_instance_id` 寻址运行实例。spec id、摘要和 generation 记录来源与顺序。

有效配置使用有限 `Decimal` 数字解析，拒绝重复键，递归冻结容器并重算 `effective_config_digest`。适配器必须使用传入配置，不能另读默认值或修改配置。

`sha256-canonical-json-v1` 使用紧凑 UTF-8、递归排序的对象键、原序数组和有限 Decimal 值。实现其他编码器时应使用契约测试向量。

## 产物边界

`StrategyArtifactRefV1` 描述签名前的可执行文件、manifest、运行产物、SBOM 和契约 schema。独立 attestation、审批、发布选择和部署状态不属于该引用。

runner 通过认证后的发布解析接口取得完整 release BOM，验证所有成员和独立证据，将下载内容隔离，原子激活不可变目录，完成验证后再导入。产物元数据不能选择自己的信任根。详见[签名验证](/toolkit/artifact-signing)与[物化](/toolkit/artifact-materialization)。

## 类型检查与提取证据

历史提取清单记录 241 个文件：36 个平台无关文件、55 个 Nautilus 文件和 150 个私有 vendor 文件。它描述提取时的 revision，不是当前源码文件总数。

历史 75/289 个类型错误已完成收敛。当前 `make toolkit-typecheck` 对 base 和 Nautilus 包运行全包 strict 检查，并校验 typing closure 证据。私有第三方 vendor 代码不在 mypy 范围内，另有行为一致性和提取检查。

```bash
make check-toolkit-extraction
make toolkit-typecheck
make check-authority
```

## 交接与运行状态

当前登记的 Nautilus 2 契约交接已完成，并记录 toolkit RC7。这些属于契约和候选版本证据，不代表生产就绪。live 执行仍未启用，已部署运行时验收仍开放。详见按 revision 记录的[发布状态](/release-governance/release-status)。

V1 是当前首个生产契约。持续演进的源码和内部契约由 Git review 与 CI 管理。历史收据证明其记录的 revision；有意修改契约时同步 schema 和 fixture，不重写旧验收证据。
