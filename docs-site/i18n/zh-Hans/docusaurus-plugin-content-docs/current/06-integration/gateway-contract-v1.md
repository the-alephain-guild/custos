---
title: "网关契约 v1"
sidebar_position: 1
---

# 网关契约 v1

机器可读的契约位于仓库的 `docs/gateway-contract/v1/` 下。本页说明里面有什么 ——
以及同样有用的：**刻意没有什么**。

## Custos 为哪些东西发布 schema

| Schema | 覆盖 |
|---|---|
| `enrollment.schema.json` | 本地 provision 的机器注册材料 |
| `runner_fact_batch_v1.schema.json` | Custos 发出的签名事实批次 |
| `strategy_artifact_ref_v1.schema.json` | 签名前的产物引用 |
| `strategy_manifest_v1.schema.json` | 产物本地的兼容性元数据 |
| `strategy_artifact_pre_import_verification_receipt_v1.schema.json` | 本地验证回执 |
| `strategy_execution_context_v1.schema.json` | 交给适配器的冻结上下文 |
| `development_source_ref_v1.schema.json` | 仅 sandbox 的开发源引用 |

规律是：Custos 只为**它自己拥有**的东西发布 schema —— 自己的注册材料、自己的事实，以及它所定义的执行边界。

## 这里没有 DeploymentSpec schema

Custos 不发布它，而且它的缺席由**测试断言**，不是靠人记。

规范的 DeploymentSpec 归上游所有。Custos 手里的是一个狭窄的本地执行视图，且只在签名与
digest 验证通过**之后**才派生出来。为它发布 schema 等于邀请生产方把 runner 的本地投影当作契约，而真正的权威是那份签名的规范 payload。

部署发布同样不是 Custos 的操作。**不存在**创建、签名或发布 DeploymentSpec 的 CLI 命令 ——
见 [CLI 参考](/zh-Hans/reference/cli)。

## 指令契约实际在哪里

签名指令在代码中被定义为一个**严格的消费者**，而不是一份你可以宽松地据以生产的已发布
schema：

- 在解析任何字段**之前**先验证精确 subject 与精确事件字节；
- 字段集是精确的 —— 未知键被**拒绝**，不是被忽略；
- tenant、mode、runner、instance、generation 与 digest 必须在 subject、envelope 与 payload
  三处一致。

subject 形状、两种事件类型与一致性矩阵，见[参考实现](/zh-Hans/integration/reference-implementations)。

## 版本化

`v1` 是唯一有内容的版本。同级目录是占位，里面什么都没有。

新增**可选**字段属 MINOR，但仍需两侧都部署 —— 因为 schema 是 `additionalProperties: false`，未更新的消费方会拒绝这个新字段。新增**必填**字段属 MAJOR：不发送它的旧生产方会直接校验失败。见 [SemVer 与 LTS](/zh-Hans/release-governance/semver-lts)。

切出 `v2` 属 MAJOR 变更，且**不是**同时维持两份契约的办法 —— 首个生产契约的规则是 V1
就地演进，不留前代 parser、不留兼容别名。

## 对照它做验证

schema 既被代码消费，也被权威门消费：

```bash
make check-authority
```

该门同时断言上文所列「刻意缺席」的那些东西确实不存在 —— 因此未来某次悄悄重新引入
DeploymentSpec schema 的改动会**失败**，而不是无声通过。
