---
title: "消费 RunnerFact"
sidebar_position: 3
---

# 消费 RunnerFact

本页面向"要写一个消费方"的人：subject、精确签名前像，以及校验器必须检查什么。概念模型
—— 事实是什么、联合为何封闭 —— 见 [RunnerFact](/concepts/runner-fact)。

## Subject

```text
crucible.runner_fact.{mode}.{tenant_id}.{runner_id}.{deployment_instance_id}
```

subject 跨 spec 与 generation 变更保持稳定。配置变更不会搬走一条流，因此订阅了某个部署
实例的消费方会持续收到该实例的事实。

## 签名前像

签名域是 `CRUCIBLE-RUNNER-FACT-BATCH-V1\0` —— 注意结尾的 NUL，它是签名域的一部分。

被签名的 header 是一个**封闭的 18 字段对象**，顺序如下：

```text
schema_version, batch_id, tenant_id, trading_mode, runner_id,
deployment_instance_id, deployment_spec_id, deployment_spec_digest,
generation, strategy_id, capability_version_id, capability_version,
capability_manifest_digest, key_id, emitted_at, source_seq_start,
source_seq_end, payload_digest
```

`facts` 与 `signature` **不在** header 内。取而代之：

```text
payload_digest = sha256(canonical_json(facts))
被签名字节      = DOMAIN || canonical_json(header)
```

因此签名是通过摘要覆盖载荷，而不是逐值覆盖 —— 这让校验方无需缓冲任意大的批次即可校验
header。

### Canonical JSON

这一步做错，是"签名明明正确却验不过"最常见的原因，所以请按规则实现，而不是照搬现成的
序列化器：

- UTF-8、紧凑（无无意义空白）；
- 对象成员按 Unicode 码点升序排序；
- 数组顺序保持不变；
- 普通 Unicode **不**做 ASCII 转义；
- 拒绝 NaN 与二进制浮点；
- 结尾无换行。

V1 签名前像 golden 固定了精确字节、摘要、合成密钥与签名。请对照 golden 实现，而不是对照
你的语言默认 JSON 编码器 —— 多数默认编码器至少违反上面一条。

该 golden 里的合成密钥只是契约证据。它永远不是运行时身份证据，用它签名的批次绝不能被
当作真实批次接受。

## 校验器必须检查什么

1. subject 与批次的 tenant、mode、runner、部署实例一致。
2. `payload_digest` 等于收到的 `sha256(canonical_json(facts))`。
3. 签名可用该 `key_id` 对应的 runner enrolled 公钥，在
   `DOMAIN || canonical_json(header)` 上验证通过。
4. `source_seq_start` 与 `source_seq_end` 与你已接受的该流内容连续。
5. 每个 `facts[].kind` 都在封闭联合内。未知 kind 是终态契约违反，不是可以跳过的值。

**先查序号，再消费载荷内容**。一个密码学上验证通过、但序号跳跃的批次，意味着有事实丢失；
继续消费后面的内容等于静默接受了一段残缺历史。

## 接受的 kind

| 消费方 | `facts[].kind` |
|---|---|
| settlement | `fill`、`position_closed`、`fee`、`period_closed` |
| risk | `equity_snapshot`、`position_snapshot` |
| health | `heartbeat`、`RunnerRuntimeLogFact.v1` |
| reconciliation | `execution_fill`、`venue_ledger_snapshot_manifest`、`venue_ledger_snapshot_chunk`、`reconciliation_period_closed` |
| deployment lifecycle | `RunnerDeploymentLifecycleFact.v1` |

## 幂等

生命周期事件 id 排除 `observed_at`；它的 UUIDv5 前像由流身份、spec id 与 digest、
generation、生命周期状态、稳定指令指纹与结果构成。

因此同一次 apply 的重试或重启会产出**相同**的事件 id。请基于它去重 —— 它就是为此存在的。

## 数值

载荷数值以 JSON 整数或 canonical decimal 字符串到达，绝不是浮点。请把 decimal 字符串解析
成精确 decimal 类型。解析成 double 会把这套字符串表示本来要避免的误差重新引进来。

## 可用性

消费方不可达时，事实在 runner 的持久 outbox 中累积，恢复后发布，身份与序号不变。不存在
有损模式，也不存在未签名的降级 topic。

宕过一段时间的消费方不需要回补机制；它只需要从上次接受的序号继续。
