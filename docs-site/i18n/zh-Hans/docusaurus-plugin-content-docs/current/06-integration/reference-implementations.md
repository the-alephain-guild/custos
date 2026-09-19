---
title: "验证参考"
sidebar_position: 5
---

本页说明可对照源码核查的签名指令行为。正式部署指令由 ARX 签发，操作者自有离线输入使用另一份契约。

## 指令绑定

签名指令 subject 为 `<provisioned-prefix>.{tenant_id}.{runner_id}.{mode}`。事件类型和部署实例位于签名事件材料中，不追加到 broker subject。

两种目标状态事件为 `DeploymentSpecReadyForRunner` 和 `DeploymentInstanceDesiredStateChanged`，都包含完整正式载荷、显式 generation 和生命周期状态。Custos 在解释载荷前验证事件原始字节及 subject，再检查传输模式、租户、runner、实例和摘要的一致性。

正式 spec 摘要按 `sha256-canonical-json-v1` 对契约规定的载荷字段计算，不包括指令 envelope 和摘要字段本身。应使用匹配的生产者/消费者 golden 向量，不能以通用 JSON 序列化替代契约。

## 持久化处理

| 结果 | 投递动作 |
|---|---|
| 无效签名/subject/契约 | 持久化拒绝后 TERM |
| 精确重投递 | 重放持久化结果 |
| 冲突或过时 generation | 持久化终止结果后 TERM |
| 引擎应用成功 | 提交已应用状态和生命周期事实后 ACK |
| 可恢复引擎/依赖失败 | NAK 等待重试 |

签名验证器、指令消费者和生命周期 supervisor 职责不同。测试应覆盖解析及引擎动作前的拒绝，以及跨持久化提交边界的重放。

## 观测消费者

RunnerFact 批次与策略信号详见[消费参考](/integration/consuming-runner-fact)。消费者先验证签名、授权、范围和序号，再修改自身状态。本地离线状态用于操作诊断，不具有签名权威。
