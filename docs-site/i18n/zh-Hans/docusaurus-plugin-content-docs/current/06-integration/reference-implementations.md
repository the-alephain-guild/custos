---
title: "参考实现"
sidebar_position: 5
---

# 参考实现

一个生产方必须**精确做对**哪些事，Custos runner 才会接受它的指令；以及它能期待收到什么。

## 入站：指令 subject

subject 是一个固定前缀，后接 tenant、runner、mode 三个组件，顺序如下：

```text
<固定前缀>.{tenant_id}.{runner_id}.{mode}
```

注意里面**没有**什么：部署实例与事件类型。两者都在 payload 里；把它们编进 subject 的指令
会验证失败。

:::note 这不是第三方集成面
只有 ARX 向 runner 发布指令，subject 前缀属于那份封闭契约的一部分。如果第三方能发布指令，
信任模型本身就已经被击穿了。

本页记录的是 runner 在行动之前所做的**验证** —— 那才是审计者需要的。若你是在对接 Custos，
你消费的面是事实流，见[消费 RunnerFact](/zh-Hans/integration/consuming-runner-fact)。
:::

Custos 使用持久的、按 runner 划分的 JetStream consumer，并手动 ACK/NAK。

## 两种事件类型

```text
DeploymentSpecReadyForRunner
DeploymentInstanceDesiredStateChanged
```

事件类型是 payload 字段，形如 `{type}.{runner_id}.{deployment_instance_id}`。两者都携带
完整的规范 DeploymentSpec，外加显式的 `generation` 与 `lifecycle_state`。

缺值即非法。Custos **从不**为签名期望状态指令的任何字段填默认值 —— 填默认值等于依据一个
没有人签过的值行动。

## 哪些必须一致

验证把精确 subject 与精确事件字节绑定到已 provision 的 Ed25519 密钥，随后要求三处一致：

| 字段 | Subject | Envelope | Payload |
|---|---|---|---|
| tenant | ✅ | ✅ | ✅ |
| runner | ✅ | ✅ | ✅ |
| mode | ✅ | | ✅ |
| 部署实例 | | ✅ | ✅ |
| generation | | ✅ | ✅ |

任何不一致都是**终态拒绝**，不是重试。签名在解析**任何** payload 字段**之前**校验 ——
未经验证的消息里没有任何内容可信，包括那些本该告诉你「是否该信任它」的字段。

## 规范 digest

`sha256-canonical-json-v1` 只对规范 spec payload 求哈希。指令 envelope 与 digest 字段本身
被排除在外。

字段集精确、对象键递归排序、数组保序、对紧凑 UTF-8 JSON 字节求哈希。算法的任何变更都必须
附带跨语言 golden fixture —— 两个在**描述**上一致却在**字节**上不一致的实现，会各自确信
对方是对的。

## 出站：事实

Custos 把带类型的事实写入持久本地 outbox；另有一个发布器负责签名并发布批次。subject、
签名前像与校验清单见[消费 RunnerFact](/zh-Hans/integration/consuming-runner-fact)。

指令客户端**没有**出站业务发布 API。runner 无法发布指令，**包括发给它自己**。

## 生产方不能指望什么

- **没有未签名路径。** 不存在兼容 topic，也不存在兜底 schema。
- **没有默认值。** 缺失的必填字段被拒绝，而不是被补齐。
- **没有本地发布。** Custos 不会转发、重发或合成一条指令。
- **除 generation 外没有顺序假设。** generation 是排序输入；投递顺序不是。

## 拒绝与重试的分界

| 生产方错误 | runner 行为 |
|---|---|
| 签名错误、subject 不符、契约非法 | 持久化拒绝，然后 TERM |
| 同 generation、字节完全相同 | 重放此前的处置结果 |
| 同 generation、字节不同 | 终态结果，然后 TERM |
| 过期的 generation | 终态结果，然后 TERM |
| 瞬时本地故障 | NAK 等待重投 |

重发**字节完全相同**材料的生产方是安全的：runner 会重放它先前的决定，而不是执行两次。
而在同一 generation 下重发**不同字节**的生产方，是在提出一个自相矛盾的主张 ——
那是终态的。
