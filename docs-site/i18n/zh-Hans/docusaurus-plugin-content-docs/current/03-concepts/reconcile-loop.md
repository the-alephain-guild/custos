---
title: "Reconcile 循环"
sidebar_position: 4
---

# Reconcile 循环

Custos 不以命令式接受指令。它收到一份签名的"**应该**在跑什么"的声明，与"**实际**在跑什么"比对，然后弥合差距。这个区别在出问题时才显出价值：一条消息丢了、一个进程崩了，期望状态仍然完好，所以 runner 重启后自行收敛，不需要谁把指令重发一遍。

## 输入

期望状态以签名 domain 事件送达。校验器在解析载荷**之前**先认证精确 subject 与精确序列化字节 —— 未经校验的消息里没有任何东西可信，包括那些"告诉你该不该信"的字段。

载荷携带不可变部署 spec、精确部署实例、期望生命周期状态与 generation。subject 形状见
[NATS Subject 参考](/reference/nats-subjects)。

## 状态

Reconcile 权威是持久化的，就在承载事实 outbox 的那个 SQLite 数据库里：

```text
desired_deployments[deployment_instance_id]
applied_deployments[deployment_instance_id]
command_in_progress_lease[deployment_instance_id]
command_outcomes[outcome_id]
runner_fact_outbox[batch_id]
```

一切以 `deployment_instance_id` 为键，绝不用 spec id。这既允许同一份不可变 spec 有多个实例，也阻止一次重试作用到错误的进程上 —— 是同一个选择的两个后果。

不存在内存态 reconciler、不存在以 spec 为键的水位线、不存在兼容 fallback。无法进入该权威的指令一律 fail-closed 拒绝；运行时绝不从本地文件或更老的载荷形状重建权威。

## 算法

1. 在解析载荷字段之前，先校验签名信封与精确 subject 绑定。
2. 校验 tenant、mode、runner、实例、spec digest、release 绑定与 generation。
3. 持久化精确指令，并与该实例已接受的 generation 比对。
4. 载入精确的持久期望记录。
5. 通过带认证的 resolver 解析策略发布物料。
6. 在不可变激活根下校验并激活精确制品。精确重投会重新载入持久激活，绝不导入可变源码路径。
7. 本地解析签名凭证 scope，经引擎生命周期 supervisor 应用，并把已校验制品作为必需输入传入。
8. 等待类型化的七项就绪回执。任务被创建**不等于**就绪 —— 一个刚启动就失败的任务，和一个启动后正常工作的任务，看起来是一样的。
9. 在一个事务里原子提交已应用状态与生命周期事实。
10. 只在该事务之后 ack。匹配的重启或重投会探测就绪状态，而不是再部署一次。

生命周期事件 id 由流权威、spec id 与 digest、generation、生命周期状态、稳定指令指纹与结果推导。观测时间留在载荷里，从不参与身份 —— 没有任何时间戳、本地文件或重建的载荷能替代签名指纹。

## 投递处置

每条消息恰好落到下表之一，且该选择是持久的：

| 结果 | 处置 |
|---|---|
| 签名错误、subject 不匹配、契约非法 | 持久化不可信拒绝，然后 TERM |
| generation 相同且字节完全相同 | 重放先前的持久处置 |
| generation 相同但字节不同；已过期；重试耗尽 | 原子写终态结果与事实，然后 TERM |
| 成功应用 | 原子写已应用状态与事实，然后 ACK |
| 引擎或本地依赖的瞬时失败 | NAK 等待重投 |

这张表推出两条性质，两条都是刻意的。毒指令不会造成无限重投循环 —— 它会终止。瞬时失败绝不会被 ack 成成功 —— 它会被重试。

## 监管

zombie 检测、熔断器状态、峰值权益跟踪与引擎任务完成，全部以
`deployment_instance_id` 为键。凭证按签名凭证 scope 索引，每次使用都绑定到精确实例。事实保留 `deployment_spec_id`，仅用于记录跑的是哪份不可变配置。

就绪超时、可重试终态事件与 zombie 断连共享**同一份**带指数退避的持久重启预算。不可重试的终态事件或预算耗尽，会原子地隔离该部署并入队终态生命周期事实。

任何长时任务意外退出，daemon 都视为致命：取消兄弟任务、停止部署、冲刷事实 outbox，然后关闭传输。顺序如此 —— 事实先冲刷完，再拆掉本该承载它们的传输。

## 熔断器的估值边界

fallback 熔断器每个实例每 tick 恰好读一次引擎状态，由 portfolio 快照提供者派生。因此未平仓名义敞口与实际权益来自同一个估值边界，而不是两个可能互相矛盾的边界。

探测异常或类型化的"不可靠"状态会立即冻结熔断器并请求平仓。缺失的 mark 或权益永远不能跳过一个 tick，也不能被当作零风险 —— 未知敞口按敞口处理，不按"没有"处理。

这是本地执行证据。它不替代提供聚合上限的签名 runner 策略，部署自己的 `risk_config`
也不能定义或覆盖该上限。见[失联时安全防护依然生效](/trust-model/safety-survives-disconnect)。
