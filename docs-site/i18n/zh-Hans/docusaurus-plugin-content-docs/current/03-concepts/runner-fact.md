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

批次 kind 的清单和精确签名规则见[消费参考](/integration/consuming-runner-fact)。未知 kind 属于终止性契约错误。wire 使用整数或规范十进制字符串，持久化前拒绝二进制浮点数。

## 结算与对账

`period_closed` 是 period 为 `YYYY-MM` 的日历结算观测，由结算生命周期发出。对账间隔不会自动产生结算关闭；交易所账本证据使用 `reconciliation_period_closed`。独立账本证据不可用时，该路径不发出关闭事实。

## 订单与成交更正

有两种 kind 是可选的，各有一个能力标志位。部署声明 `reconciliation.order_lineage = "v1"` 才会收到 `RunnerOrderEventFact.v1`，声明 `settlement.fill_correction = "v1"` 才会收到 `RunnerFillCorrectionFact.v1`。两个标志位相互独立，只能与终止估值、估值检查点两个标志位一起声明。默认的能力模板两个都不声明，由操作者按部署打开。

`RunnerOrderEventFact.v1` 记录一条订单事件：`initialized`、`submitted`、`updated`、`rejected`、`denied`、`refused`、`canceled`、`expired`、`modify_refused`、`modify_rejected`、`cancel_rejected` 或 `fill_voided`。每种事件带哪些字段由 schema 规定。订单状态是签名事件能确定的粗状态（`initialized`、`working`、`partially_filled`、`filled`、`denied`、`rejected`、`refused`、`canceled`、`expired`、`voided`），不是引擎跟踪的全部状态。

数量按两种单位签名。原生数量用交易所自己的数量单位；基础数量用基础资产单位，严格等于原生数量乘以 `quantity_multiplier`，乘数一并签入。

| 事实 | 原生数量 | 基础数量 |
|---|---|---|
| `execution_fill` | 无 | `quantity` |
| `RunnerOrderEventFact.v1` | `quantity`、`requested_quantity`、`voided_quantity`、`leaves_quantity_after` | `base_quantity`、`requested_base_quantity`、`voided_base_quantity`、`leaves_base_quantity_after` |
| `RunnerFillCorrectionFact.v1` | `voided_quantity` | `voided_base_quantity` |

被拒的改单或撤单（`modify_refused`、`modify_rejected`、`cancel_rejected`）从不改变订单的确认数量与确认价格。`requested_values_known` 表示被拒的请求值是否已知；为 true 时，缺席的请求值表示这次请求没有改这一项。

成交作废对它指向的那笔成交是累计的。同一笔成交的两次作废，只有作废数量、费用冲销额和重开标记三者都相同才算同一版本；重复投递的事实按 `event_id` 识别。作废数量变小、或费用冲销额的绝对值变小的版本不予应用。被作废的成交带有手续费时，前一版给出过冲销额之后，不带冲销额的版本不予应用。作废数量超过这笔成交数量的版本同样不予应用。`order_status_after` 是引擎应用这次作废之后的订单状态，订单状态以它为准。

| 声明的标志位 | 本实例签过这笔成交 | 本实例没有签过这笔成交 |
|---|---|---|
| 都没有 | 都不签 | 都不签 |
| 只有 `order_lineage` | `fill_voided`（结算未更正） | `fill_voided` |
| 只有 `fill_correction` | 成交更正 | 都不签 |
| 两个都有 | 两条一起，同一批次、同一个 `source_event_id` | `fill_voided` |

没有本地成交的作废（例如对账时交易所认定的作废）带合成的成交编号，只有订单事件。实例流已被终止估值封闭之后才观察到的作废，以及作废的是更早的 runner 签出的成交，都不更正，差额在交易所账本里体现。

## 其他观测通道

策略信号使用独立签名 envelope，有自己的序号和发布跟踪，不是额外批次 kind。本地离线状态未签名，不能解释为 RunnerFact 证据。

## 投递失败

PubAck 处理完成前，签名批次保留在持久化 outbox。某个流发布失败时，本轮 drain 不再发送该流后续批次。应监控队列年龄和存储容量。上游投递失败本身不会关闭本地风控。

运行日志使用显式构造、脱敏后的 `RunnerRuntimeLogFact.v1`，不转发原始 stdout。详见[可观测性](/operator-guide/runtime-log-observability)。
