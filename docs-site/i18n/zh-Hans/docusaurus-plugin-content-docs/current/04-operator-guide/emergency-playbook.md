---
title: "应急手册"
sidebar_position: 5
---

# 应急手册

平台不可达、runner 不健康、或需要恢复进程时的处置。针对具体故障的诊断见
[排障](./troubleshooting)。

## 连接中断

runner 的订阅不可用时，就绪状态会被清除。**不会**发生的事和会发生的事一样重要：

- 本地安全防护继续工作。敞口上限、fallback 熔断器与 zombie watchdog 都不依赖平台可达。
- runner 以有界退避重试订阅。
- 它绝不会静默切到未签名的 topic 或另一个权威源。

已应用的观测结果留在签名事实 outbox 里。连接恢复后，发布者继续推进，事实身份与序号
归属都不变 —— 不丢，也不重排。

连接中断既不是停止交易的理由，也不是无防护交易的许可。见
[失联时安全防护依然生效](/trust-model/safety-survives-disconnect)。

## 健康检查

```bash
arx-runner health
arx-runner health --json | jq .
du -h ~/.arx/state/runner-fact-outbox.db
```

JSON 形式是唯一的运行时健康投影。它从事实数据库原子刷新，报告每个已启用的传输模式、
SQLite quick-check 状态、数据库/WAL/磁盘字节数、指令结果与在途租约、期望与已应用之间的
漂移、重启与隔离计数、待发事实与 ack 的年龄、签名策略到期时间、制品缓存与激活字节数，
以及传输权威的到期或吊销状态。

它是投影，不是第二本账 —— 从不持有业务状态。

## 告警阈值

**立即 page**，当满足任一条件：

- `sqlite_quick_check != "ok"`
- `overdue_in_progress_commands > 0`
- `quarantined_deployments > 0`
- `invalid_transport_authorities > 0` —— 本地过期、被吊销，或 broker 拒绝授权
- `quarantined_artifacts > 0`

**告警**，持续则升级：

| 信号 | 告警阈值 | page 阈值 |
|---|---|---|
| `oldest_desired_applied_drift_age_seconds` | 30 | 120 |
| `oldest_pending_fact_age_seconds` | 30 | 120 |
| `disk_free_bytes` | 低于 2 GiB | 低于 1 GiB 时停止新增风险敞口的动作 |
| `next_policy_expiry_seconds` | 低于 900 | 已过期的 testnet/live 策略保持 fail closed |
| `next_transport_expiry_seconds` | 低于 900 | — |

`transport_modes` 缺任何一项都会使整份文档失效。某项为 false 时诊断信息仍保留，但
`ready=false` —— 一个失败的模式不会躲在健康的模式后面。

## 恢复前先保全

先保全事实数据库及其 `-wal` / `-shm` 兄弟文件。

SQLite quick check 失败、磁盘耗尽或 WAL 陈旧，都属于需要人工介入的恢复事件。
**不要为了让健康检查变绿而删库** —— 那会丢掉 runner 实际做过什么的记录，而这恰恰是
唯一无法从上游重建的东西。

## 进程恢复

1. 查看 `journalctl -u custos -n 200` 或容器日志。
2. 保全 `.arx/runner.toml`、机器 vault、交易所 vault、domain-event 公钥与事实 outbox。
3. 重启服务。
4. 确认 `arx-runner health` 成功。
5. 检查 `arx-runner health --json` 的漂移、隔离、策略到期与待 ack 年龄。
6. 确认上游收到了预期的生命周期事实 generation。

runner 从 enroll 得到的机器身份与上游期望状态恢复。`runner.toml` 里没有长期凭证，也
没有任何本地文件是部署生命周期的 canonical 记录 —— 所以干净重启是收敛，而不是猜测。

## 停止执行

要停止交易，请在上游改期望状态。那条路径有签名、可审计、可回退。

如果必须**立刻**停、等不了平台，就停掉 runner 进程。进程退出不影响本地持仓 —— 交易所
不知道也不关心某个进程结束了。你失去的是原本盯着这些持仓的本地安全防护，所以进程级
停止是最后手段，不是第一选择。
