---
title: "NATS subject"
sidebar_position: 4
---

签名与离线流量使用不同契约和初始化路径，必须显式选择通道。

## 签名控制输入

ARX 配置 runner-control durable，并提供精确的指令和安全策略 filter。Custos 验证并绑定已有 durable，不创建签名通道拓扑。

指令 subject 结构为：

```text
<provisioned-command-prefix>.{tenant_id}.{runner_id}.{mode}
```

部署实例和事件类型位于签名事件材料中，不追加到此 subject。`DeploymentSpecReadyForRunner` 与 `DeploymentInstanceDesiredStateChanged` 都携带完整目标状态、显式 generation 和生命周期状态。安全策略 filter 同样由传输授权提供。

解释载荷前先验证精确 subject 和事件字节。传输会话模式必须匹配签名指令或策略。持久化处理结果决定确认方式：成功为 ACK，可恢复失败为 NAK，终止拒绝为 TERM。

ARX 是签发目标意图并消费签名观测的上游产品。身份注册是独立接口；指令/事实通过已配置传输投递，需要单独检查可用性。

## 签名观测输出

RunnerFact 批次：

```text
crucible.runner_fact.{trading_mode}.{tenant_id}.{runner_id}.{deployment_instance_id}
```

策略信号使用独立 envelope 和 subject：

`crucible.runner.strategy-signal.v1.{tenant_id}.{runner_id}.{trading_mode}` <!-- disclosure-ok: exact public strategy-signal subject required by consumers -->

两者都使用本地持久化发布和 PubAck 处理。信号序号与批次序号独立，详见[消费者验证](/integration/consuming-runner-fact)。

## 离线流量

| 方向 | Subject |
|---|---|
| 操作者目标状态 | `arx.<tenant>.deployment_spec.<strategy-id>` |
| Runner 观测状态 | `arx.<tenant>.deployment_status.<runner-label>.<spec-id>` |
| Runner 遥测快照 | `arx.<tenant>.telemetry.<runner-label>.<spec-id>.snapshot` |
| Runner 遥测成交 | `arx.<tenant>.telemetry.<runner-label>.<spec-id>.fill` |
| Runner 遥测平仓 | `arx.<tenant>.telemetry.<runner-label>.<spec-id>.position_closed` |

`nats bootstrap --profile standalone` 创建自有 deployment/observed stream。目标状态为每个 subject 保留最新消息；观测状态每个 subject 最多保留 10,000 条。observed stream 还预留 heartbeat subject，离线 daemon 不发出这类消息。

遥测沿用观测状态的信封（`envelope_version`、`event_id`、`tenant_id`、`occurred_at`、`payload_schema_version`、`payload`），`payload.kind` 取值为 `snapshot`、`fill` 或 `position_closed`。每个运行中的部署每 10 秒发布一份快照，包含引擎状态、未平持仓和未成交挂单；金额是十进制字符串。状态不可靠时附上原因，并省略持仓。遥测未签名、尽力发布，不是 RunnerFact 证据，runner 自身也从不读取它。

离线 daemon 收到 SIGTERM 或 SIGINT 时，会通过引擎停下每个运行中的部署，把其状态以 `stopped` 阶段发布；若有部署未能在 75 秒内确认停止，进程以非零退出码退出。

离线输入和状态未签名，应限制在操作者自有基础设施中。仅绑定 loopback 的演示 broker 不适合共享部署。离线状态尽力发布，不经过签名事实 outbox。
