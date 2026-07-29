---
title: "RunnerFact"
sidebar_position: 5
---

# RunnerFact

RunnerFact 是一份关于"本地发生了什么"的签名声明。它是执行事实离开你机器的唯一途径。

方向很重要。Custos 接收指令、发出观测；两者是分开的通道，谁也不能当作对方使用。runner
说发生了什么，由 runner 签名，因此可以被核验，而不是被相信。

```text
引擎 / watchdog / 熔断器
  -> 类型化本地事实适配器
  -> 事实 outbox（本地持久）
  -> 签名批次
  -> ARX
```

## 各自拥有什么

Custos 观测本地引擎并对观测结果签名。ARX 拥有由这些观测构建出的 canonical 业务与生命周期记录。

Custos 不判定一条事实对业务**意味着什么** —— 它报告引擎做了什么。这层分离正是"被攻破的
runner 无法改写历史"的原因：它只能产出签名声明，而与自身既有序列冲突的声明是可检出的。

不存在通用的未签名遥测通路。引擎观测必须先映射到一个显式版本化的事实类型，才可能进入
outbox。

## 身份

每个部署范围的批次都携带 tenant、mode、runner、`deployment_instance_id`、
`deployment_spec_id`、`deployment_spec_digest`、generation、策略与能力来源、事件时间、事件 id 与类型化载荷。

`deployment_instance_id` 是运行时身份。策略、spec、generation 与进程标识都不能替代它 ——
它们描述的是**配置了什么**，不是**哪个正在跑的东西**做了什么。

流身份跨 spec 与 generation 变更保持稳定：

```text
tenant_id + mode + runner_id + deployment_instance_id
```

`deployment_spec_id`、`deployment_spec_digest` 与 `generation` 是批次内的签名围栏。它们从不切分流，也从不重置源序号。配置变更是围栏，不是新流 —— 否则消费方会看到像"丢事实"
一样的空洞。

## 序号

序号由 outbox 独占分配，且在持久化签名批次的同一个事务里完成。

类型化事实构造器不得预填序号，outbox 会拒绝这种输入。调用方提供的序号等于第二个分配器，而两个分配器迟早会撞。

## 封闭联合

共 13 种事实 kind。未知 kind 是终态契约违反 —— 它不能降级到未签名日志，因为无法用联合表达的事实，正是消费方无法校验的事实。

| 消费方 | 接受的 `facts[].kind` |
|---|---|
| settlement | `fill`、`position_closed`、`fee`、`period_closed` |
| risk | `equity_snapshot`、`position_snapshot` |
| health | `heartbeat`、`RunnerRuntimeLogFact.v1` |
| reconciliation | `execution_fill`、`venue_ledger_snapshot_manifest`、`venue_ledger_snapshot_chunk`、`reconciliation_period_closed` |
| deployment lifecycle | `RunnerDeploymentLifecycleFact.v1` |

`period_closed` 是日历结算事实，其 `period` 恰好是 `YYYY-MM`，只由持久结算生命周期发出。
reconciliation 循环可以发 venue-ledger 证据与 `reconciliation_period_closed`，但绝不能把任意重试或快照间隔翻译成一次结算收口。

若独立的 venue ledger 不可用，该循环**不发**收口事实，并在本地记录该能力不可用。没有经过独立佐证的结算收口，是披着证据外衣的断言。

## 数值

载荷数值是 JSON 整数或 canonical decimal 字符串。Python 二进制浮点在持久化前被递归拒绝，因此签名不会依赖某种语言碰巧如何渲染浮点数。

见[金额运算精确](/trust-model/exact-money-arithmetic)。

## 生命周期事实

`RunnerDeploymentLifecycleFact.v1` 记录一次已应用的期望 generation：tenant 与 mode、
runner、部署实例、spec id 与 digest、generation 与生命周期状态、指令指纹与终态结果、
`observed_at`，以及由 outbox 分配的 `seq`。

发出它需要针对同一 mode、实例、spec digest 与策略的精确 `deployment_lifecycle` 能力绑定。只有 health 权限的授权无法发出生命周期事实。

生命周期事件 id **排除** `observed_at`。它的 UUIDv5 前像包含流身份、spec id 与 digest、
generation、生命周期状态、稳定指令指纹与结果。因此同一次 apply 的重试或重启保持同一个事件 id，而任何稳定分量变化都会产出不同的 id —— 这让重投对**消费方**幂等，而不仅仅对
runner 幂等。

## 失败语义

进入 outbox 的入队成功，是上报的持久化边界。Reconcile 维护相互独立的"已应用"与"已上报"
水位线。

入队失败时指令不会被 ack。重投重试的是**事实**，不会重复已经成功的引擎动作 —— 引擎工作与对它的上报是各自可恢复的。

ARX 不可用期间本地安全防护继续工作，且事实绝不会为了投递成功而降级到未签名 topic。投不出去的事实会等待；它不会变成一条更弱的事实。

subject、签名前像与校验步骤见[消费 RunnerFact](/integration/consuming-runner-fact)。
