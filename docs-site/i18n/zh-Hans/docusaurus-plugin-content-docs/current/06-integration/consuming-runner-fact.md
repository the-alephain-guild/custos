---
title: "消费签名观测"
sidebar_position: 3
---

签名通道发布 RunnerFact 批次和独立策略信号 envelope。每种接口都应使用已注册 runner 公钥及精确契约验证。离线部署状态不是这两类验证器可接受的输入。

## RunnerFact subject

```text
crucible.runner.fact.v1.{tenant_id}.{runner_id}.{trading_mode}
```

subject 只包含租户、runner 和模式；部署实例位于签名批次 header 中，不在 subject 里。序号流按租户 + 模式 + runner + 部署实例划分，因此同一 runner 的多个实例共用一个 subject，各自独立编号。spec 或 generation 改变不会重置序号。

## 批次签名

签名域为 `CRUCIBLE-RUNNER-FACT-BATCH-V1\0`，包含末尾 NUL。封闭 header 包含：

```text
schema_version, batch_id, tenant_id, trading_mode, runner_id,
deployment_instance_id, deployment_spec_id, deployment_spec_digest,
generation, strategy_id, capability_version_id, capability_version,
capability_manifest_digest, key_id, emitted_at, source_seq_start,
source_seq_end, payload_digest
```

```text
payload_digest = sha256(canonical_json(facts))
signed bytes = DOMAIN || canonical_json(header)
```

`facts` 和 `signature` 不在 header 中。规范 JSON 使用紧凑 UTF-8、排序对象键、原序数组、不作 ASCII 转义的 Unicode，且末尾无换行。拒绝二进制浮点数和非有限数字，十进制字符串需精确解析。

## 应用前验证

1. 匹配 subject、租户、模式、runner 和部署实例。
2. 重算 facts 摘要。
3. 用 `key_id` 对应的已注册公钥验签，并验证授权/能力绑定。
4. 对该实例流检查预期序号，按稳定事件/批次身份去重。
5. 拒绝未知事实 kind 和无效契约字段。

契约 golden 使用合成测试密钥，不能将其视为可信运行身份。

## 批次 kind

| 用途 | `facts[].kind` |
|---|---|
| 结算 | `fill`、`position_closed`、`fee`、`period_closed` |
| 风险 | `equity_snapshot`、`position_snapshot` |
| 健康 | `heartbeat`、`RunnerRuntimeLogFact.v1` |
| 对账 | `execution_fill`、`venue_ledger_snapshot_manifest`、`venue_ledger_snapshot_chunk`、`reconciliation_period_closed` |
| 部署生命周期 | `RunnerDeploymentLifecycleFact.v1` |

只有部署的引擎已就绪且状态可靠时，`heartbeat` 才报告 `online`。仍在启动中的部署，包括因连不上交易所而在重试的部署，报告 `degraded`。

生命周期事件 id 包含稳定指令/应用身份，不包含观测时间。同一应用的重投递因此保持幂等。

## 交易所账本快照

一份交易所账本快照由一个清单（manifest）和若干分块（chunk）组成。每个分块有五个部分：`balances`、`positions`、`fills`、`fees` 与 `cash_flows`。一个分块五部分合计最多 512 条，规范化后最多 262144 字节。

每条余额都写明所属钱包。`wallet_type` 是交易所自己的钱包名，写成小写标识，例如 `spot`。`sub_account` 为 null 表示交易所主账户，否则是非空的子账户名。清单带有快照自身的 `sub_account`，并为每个部分给出条数，其中包括 `cash_flows_count`。

资金变动记录资金进入、离开账户或在账户内部移动：

| `kind` | `from` | `to` | `counterparty_uid` | `destination` |
|---|---|---|---|---|
| `internal_transfer` | 钱包 | 同一子账户内的钱包 | null | null |
| `sub_account_transfer` | 钱包 | 另一子账户的钱包 | null | null |
| `uid_transfer_out` | 钱包 | null | 必填 | null |
| `uid_transfer_in` | null | 钱包 | 必填 | null |
| `deposit` | null | 钱包 | null | null |
| `withdrawal` | 钱包 | null | null | 对象或 null |

`amount` 大于零，`fee` 不为负；方向由 `kind` 表达，不用正负号。`destination` 记录收款 `address`，交易所提供时还有 `network` 与 `memo`。`external_reference` 是交易所提供的交易哈希或外部流水号。`cash_flow_id` 是交易所自己的记录编号，在同一快照内唯一。

`completeness` 按部分分别报告。`cash_flows_complete` 不影响其余四个标志。没有资金变动且 `cash_flows_complete: true` 的快照，表示所覆盖的时间窗内没有资金移动；同样是空列表但 `cash_flows_complete: false`，只表示资金变动没有采集。需要完整划转与提币记录的消费方，必须要求 `cash_flows_complete: true`。

余额按钱包、子账户、资产、币种排序，子账户为 null 的排在最前。资金变动按 `occurred_at` 排序，相同时再按 `cash_flow_id` 排序。对每个分块的规范化内容校验 `chunk_digest`，对快照身份、完整性、条数与各分块摘要校验清单的 `content_digest`。契约 golden 中有一份可用于核对这两种摘要的参考快照。

## 策略信号

Subject：`crucible.runner.strategy-signal.v1.{tenant_id}.{runner_id}.{trading_mode}`。 <!-- disclosure-ok: exact public strategy-signal subject -->

签名域：`CRUCIBLE-RUNNER-STRATEGY-SIGNAL-V1\0`。 <!-- disclosure-ok: exact strategy-signal signing domain -->

签名覆盖签名域字节与以下封闭载荷的规范 JSON，仅排除 `signature`：

```text
schema_version, fact_id, subject, tenant_id, trading_mode, runner_id,
deployment_instance_id, deployment_spec_id, deployment_spec_digest,
generation, strategy_id, capability_version_id, capability_version,
capability_manifest_digest, strategy_version, instrument, client_order_id,
timeframe, direction, occurred_at, source_sequence, input_digest, trace_id, key_id
```

`schema_version` 为 1；`direction` 为 `long`、`short` 或 `flat`；`client_order_id` 可为 null。校验精确 subject 及完整实例/能力范围，验证注册签名，按 `fact_id` 去重，并按租户/模式/runner/实例检查 `source_sequence`。多个实例共享信号 subject，但序号流独立。

信号具有独立持久化序号分配与 PubAck 跟踪，不是 RunnerFactBatch 的第十四种 kind，也不能与批次序号比较。策略信号不能证明订单已成交。

## 投递

outbox 在发布前持久化，记录 PubAck 后才完成本地投递。重试保持身份。消费者需保存自己的持久化游标和去重状态；历史重放可用性取决于 broker 保留策略及消费者配置。本地 outbox 持久化不等于下游拥有无限历史保留。
