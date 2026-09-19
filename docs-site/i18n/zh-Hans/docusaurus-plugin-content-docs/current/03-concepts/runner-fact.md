---
title: "RunnerFact"
sidebar_position: 5
---

RunnerFact 是已注册 runner 的签名观测。ARX 验证后再用于正式业务记录。

```text
引擎 / watchdog / 熔断器 -> 类型化事实 -> SQLite outbox -> 签名批次 -> ARX
```

## 身份与序号

部署流由 `tenant_id + trading_mode + runner_id + deployment_instance_id` 标识。spec id、spec 摘要和 generation 是签名来源记录及约束，不拆分流或重置序号。

outbox 在持久化批次的同一事务中分配序号。生命周期应用与生命周期事实原子提交，报告重试不会重复已提交的引擎操作。

批次包含十三种 kind，精确签名规则和清单见[消费参考](/integration/consuming-runner-fact)。未知 kind 属于终止性契约错误。wire 使用整数或规范十进制字符串，持久化前拒绝二进制浮点数。

## 结算与对账

`period_closed` 是 period 为 `YYYY-MM` 的日历结算观测，由结算生命周期发出。对账间隔不会自动产生结算关闭；交易所账本证据使用 `reconciliation_period_closed`。独立账本证据不可用时，该路径不发出关闭事实。

## 其他观测通道

策略信号使用独立签名 envelope，有自己的序号和发布跟踪，不是额外批次 kind。本地离线状态未签名，不能解释为 RunnerFact 证据。

## 投递失败

PubAck 处理完成前，签名批次保留在持久化 outbox。某个流发布失败时，本轮 drain 不再发送该流后续批次。应监控队列年龄和存储容量。上游投递失败本身不会关闭本地风控。

运行日志使用显式构造、脱敏后的 `RunnerRuntimeLogFact.v1`，不转发原始 stdout。详见[可观测性](/operator-guide/runtime-log-observability)。
