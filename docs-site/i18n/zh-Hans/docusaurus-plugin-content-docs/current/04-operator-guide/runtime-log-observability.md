---
title: "运行时日志与可观测性"
sidebar_position: 4
---

# 运行时日志与可观测性

可观测性分两条独立通道，这个分离本身就是整个设计。

**本地**，runner 把结构化 JSON 事件写到 stdout。这条是你的：它留在你的机器上，由你决定
用什么收集，详尽程度取决于代码。

**上游**，runner 发出 `RunnerRuntimeLogFact.v1` —— 一条签名事实，与它产出的其他事实走
同一个流。这条通道可验证，且刻意做得很窄。

两条通道之间没有桥。runner 从不 tail 自己的 stdout 再上传，也从不回落到发送原始异常文本。

## 为什么不上传 stdout

日志行天生是非结构化的。任何东西都可能落进去 —— 异常 repr 里的凭据、debug dump 里的
签名 payload、故障时被回显的 API 响应。

上传 stdout 意味着凭据保证要依赖「任何人、任何地方、永远不会记错东西」。那不是保证，
那是一个在出事那天之前一直运气不错的期望。

所以上游通道只接受显式构造的事件，且每一条在写入任何持久位置之前都要过脱敏。

## 一条运行时日志事实包含什么

```json
{
  "kind": "RunnerRuntimeLogFact.v1",
  "event_id": "<deterministic uuidv5>",
  "occurred_at": "<RFC3339 UTC>",
  "level": "INFO",
  "component": "local_cap",
  "message": "…",
  "structured_fields": {},
  "correlation_id": "<uuid>",
  "causation_id": null
}
```

`level` 取 `DEBUG` / `INFO` / `WARN` / `ERROR` —— 闭合集合。`component` 由发出事件的代码
提供，必须匹配 `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`。

外层批次携带完整流身份：tenant、mode、runner、deployment instance、spec id 与 digest、
generation、strategy、capability、key id、一段连续序号区间、payload digest 与签名。
spec / digest / generation 是签名栅栏 —— 它们从不改变 subject，也从不重置序号。

它走普通的事实 subject，验证方式与一条成交或结算事实完全相同。见
[消费 RunnerFact](/zh-Hans/integration/consuming-runner-fact)。

## 脱敏是拒绝，不是擦洗

在事实进入本地队列之前，脱敏器递归遍历 message 与每一个结构化字段：

- 看起来敏感的**键** —— API key 与 secret、密码、token、凭据、authorization、私钥、
  age key、KEK —— 被匿名化；
- 可识别的秘密**形状**无论出现在哪里都被替换：`Bearer` token、`rkc1` 凭据、
  `AGE-SECRET-KEY-…`、PEM 私钥、形如赋值的片段，以及高熵字符串；
- 经过以上处理后**仍然**可识别为秘密材料的，**整条事实被拒绝**，且发生在它接触 SQLite
  之前。

最后一点值得记牢。脱敏器不会「尽力而为，剩下的照过」。一条它无法保证安全的事实不会变成
一条部分脱敏的事实 —— 它根本不存在。丢一条可观测性事件是可恢复的，发布一条凭据不是。

它无法表示的形状同理：不支持的对象、非有限浮点与二进制浮点被递归拒绝。数值字段以 JSON
整数或规范十进制字符串传输，理由和金额一样。

## 限制

拒绝同时也是尺寸的执行方式，所以这些是硬边界而非截断点：

| 限制 | 取值 |
|---|---|
| message 长度 | 4 KiB |
| `structured_fields` 总量 | 32 KiB |
| 嵌套深度 | 有界 |
| 键数量 | 有界 |
| 键长度 | 仅短字符串 |

超出任何一项的事件被整条拒收。改成截断会有从秘密中间切断的风险，从而让形状匹配器失效。

## 关联与幂等

`event_id` 是确定性 UUIDv5。它的前像包含 tenant、mode、runner、deployment instance、
correlation id，以及**脱敏后**内容的 digest —— 在脱敏之后计算，因此一个事件的身份永远
不依赖已被移除的材料。

由此得出两个结果。同一流内相同内容是幂等的，重试不会产生重复。不同 tenant / mode /
runner / instance 流中的相同内容不会在全局去重表里碰撞，因此一个租户的事件永远不会被
误认成另一个租户的。

## 投递

运行时日志事实与其他事实共用投递路径：本地队列先提交再发布，批次删除前必须拿到 PubAck，
两者之间崩溃会重放同一个 `batch_id` —— 消费者去重让这一点是安全的。

发布失败的流会在本轮 drain 中挡住来自同一流的后续批次。序号连续性被保住，而不是为吞吐
牺牲掉，因为一个看到缺口的消费者分不清「丢了」和「还没到」。

当发布本身失败时，失败日志只包含结构化事件身份与异常类型。事件自身的内容绝不作为兜底
诊断输出 —— 那会把这套设计要避免的非结构化路径重新引进来。

## 它不提供什么

它不是日志聚合产品。没有查询 API，没有你在这里配置的保留策略，也没有向 runner 索取历史
事件的办法。

在主机上排障请用你已有的工具读 stdout 上的本地 JSON。签名通道回答的是另一个问题 ——
不是给你看的「发生了什么」，而是给一个不在现场的消费者的「可证明地发生了什么」。
