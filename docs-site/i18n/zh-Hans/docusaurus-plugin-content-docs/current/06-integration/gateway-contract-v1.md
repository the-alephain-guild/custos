---
title: "Gateway 契约 V1"
sidebar_position: 1
---

Custos 为自身观测和执行边界发布 schema，当前清单见[JSON Schema 参考](/reference/json-schema)。

## 签名部署输入

ARX 拥有正式 DeploymentSpec 和指令签发权。Custos 的严格消费者先验证原始字节/subject、字段集、身份、generation 和摘要，再派生本地执行视图。Custos 不发布另一份正式 DeploymentSpec schema，也没有签发该指令的 CLI。

签名指令客户端绑定已有授权 durable，详见[NATS subject](/reference/nats-subjects)和[验证参考](/integration/reference-implementations)。

## 离线输入

`offline_deployment_spec.schema.json` 描述独立的操作者契约。`deployment validate/publish` 仅在 sandbox/testnet 校验和发布该输入，不能产生正式签名指令或晋升证据。

## 观测输出

RunnerFact 批次包含封闭的 13 种 kind，以及签名身份、摘要和序号字段。策略信号使用独立签名 envelope。离线状态未签名，签名事实消费者不得接受它。

## 校验与兼容性

JSON Schema 校验结构。签名、授权、跨字段绑定和序号约束需要契约验证器及测试。两侧应使用匹配的 revision，并运行：

```bash
make check-authority
```

严格 V1 字段集的修改方式与历史证据处理见[契约版本](/integration/contract-versioning)。
