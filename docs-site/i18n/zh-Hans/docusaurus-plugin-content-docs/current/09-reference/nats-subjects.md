---
title: "NATS Subject 参考"
sidebar_position: 4
---

# NATS Subject 参考

Custos 只有两条传输关系：消费签名的期望状态，发布签名的事实。没有第三条通道，除下述之外也没有任何入站控制路径。

## 入站 —— 期望状态

两种事件类型承载期望状态，一种用于创建，一种用于后续变更。它们的 subject 精确限定到具体 runner 与部署实例：

```text
<domain-prefix>.<tenant>.<mode>.deployment.
  DeploymentSpecReadyForRunner.<runner_id>.<deployment_instance_id>

<domain-prefix>.<tenant>.<mode>.deployment.
  DeploymentInstanceDesiredStateChanged.<runner_id>.<deployment_instance_id>
```

前缀随 ARX enrollment 一并下发；runner 启动时绑定它，并且不接受来自任何其他 subject
的事件。

**这不是一个集成点**。ARX 是期望状态的唯一发布方；来自其他地方的事件无论投到哪个
subject 都过不了签名校验。这里写出来，是为了让你能推理 runner 订阅了什么，而不是为了让你往里写。

Custos 用 durable、runner-scoped 的 JetStream consumer 订阅，手动 ACK/NAK。

校验把**精确的 subject 与精确的事件字节**绑定到已下发的 Ed25519 密钥。tenant、mode、
runner、实例、canonical spec id 与 canonical digest 必须在 subject、事件与载荷之间逐项一致 —— 任何一项不匹配都是拒绝，而不是告警。

两种事件都携带完整的 canonical 部署载荷，外加显式的 `generation` 与 `lifecycle_state`。缺值即非法：Custos 从不为签名指令补默认值，因为补出来的字段没有任何人签过名。

## Canonical digest

摘要算法是 `sha256-canonical-json-v1`。它**只**哈希 canonical 部署载荷 —— 指令信封与
digest 字段本身被排除，因为摘要无法覆盖它自己。

规则：

- 字段集合精确；
- 对象键递归排序；
- 数组保持顺序；
- 哈希紧凑 UTF-8 JSON 字节。

对该算法的任何改动都必须附带跨语言 golden fixture。两个实现只要差一个字节就会产出不同摘要，而故障表现为一次莫名其妙的签名拒绝。

## 出站 —— 签名事实

指令客户端**没有**出站业务发布 API。这是刻意的不对称：接收指令的那条路径不能被用来发送任何东西。

Custos 把类型化事实写入本地持久 outbox，由独立的发布者签名并批量发布给上游接收。序号由
outbox 分配，因此无法持久入队的事实会阻塞指令 ack，而不是被静默丢弃。

事实的 schema 与 subject 见[消费 RunnerFact](/integration/consuming-runner-fact)。

## 什么不是通道

ARX 授权只在 enrollment 时下发一次。此后它不在投递路径上：既不发布也不中转部署指令，也不是事实的目的地。它是否可用，不影响指令投递与事实发布。

这一点在运维上很重要 —— 它意味着授权侧的中断既不能停掉正在运行的部署，也不能被用来注入一个部署。
