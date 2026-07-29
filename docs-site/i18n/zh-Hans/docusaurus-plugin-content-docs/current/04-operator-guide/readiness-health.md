---
title: "就绪与健康探针"
sidebar_position: 3
---

# 就绪与健康探针

```bash
arx-runner health          # 就绪时退出码 0
arx-runner health --json   # 完整状态文档
```

就绪**不等于**「进程起来了」。守护进程可以正在运行、已连接，却仍然不就绪 —— 而在那个状态下，编排器把它移出服务是**正确的**。

## 探针

`arx-runner health` 读取一个状态文件、求值就绪判据，然后以 `0` 或 `1` 退出。它不发起任何网络调用，因此足够便宜到可以高频运行，也不会因为上游慢而自己失败。

| 情况 | 退出码 | `--json` 输出 |
|---|---|---|
| 就绪 | `0` | 完整状态文档 |
| 未就绪 | `1` | 完整状态文档 |
| 状态文件不存在 | `1` | `{"ready": false, "path": "…"}` |

文件默认在 `~/.arx/state/runner-ready.json`，可用 `--ready-file` 覆盖。它的权限是 `0600`、位于 `0700` 目录下，且**原子写入** —— 先写唯一命名的临时文件、fsync、再 rename ——
所以探针永远读不到写了一半的文档。

## 「就绪」到底断言了什么

八项条件必须同时成立：

1. runner 自己标记了就绪；
2. 传输已连接；
3. 凭据状态为 `active`；
4. 凭据绑定有效；
5. 凭据未过期；
6. 每个已启用的传输模式都在线；
7. 本地数据库通过 SQLite `quick_check`；
8. 无效传输授权数为零。

其中两项值得多说。条件 7 意味着**本地存储损坏会让 runner 变为未就绪**，而不是让它继续接受它可能记录不下来的工作。条件 8 意味着一个验证失败的传输授权会把 runner 移出服务，而不是被跳过。

机器凭据过期时就绪会被直接拒绝 —— 且此时状态文件被**删除**，而不是改写为未就绪。文件缺失与文件未就绪都过不了探针，所以删掉它不丢任何信息，而且不会留下一份声称着已失效身份的陈旧文档。

## 状态文档

```json
{
  "ready": true,
  "tenant_id": "acme",
  "runner_id": "018f8b5f-…",
  "credential_id": "b0e4a8f2-…",
  "credential_version": 2,
  "credential_valid_until": "2026-12-31T23:59:59Z",
  "machine_key_id": "ed25519-7f3a1c",
  "credential_state": "active",
  "credential_binding_valid": true,
  "strategy_id": null,
  "nats_connected": true,
  "deployment_subscription": true,
  "transport_modes": {"sandbox": true},
  "runtime_metrics": { … }
}
```

这里全部是公开元数据。没有凭据、没有密钥材料、没有策略参数 —— 这个文件可以放心挂载、抓取与记录。

## 运行时指标

`runtime_metrics` 携带三十个字段，覆盖四个方面。它们是运维判断 runner 是否跟得上的视角，无需访问任何上游。

| 方面 | 字段示例 |
|---|---|
| 本地存储 | `database_bytes`、`wal_bytes`、`disk_free_bytes`、`sqlite_quick_check` |
| 事实投递 | `pending_fact_batches`、`oldest_pending_fact_age_seconds`、`fact_publish_attempts`、`published_fact_batches`、`last_fact_puback_age_seconds` |
| 部署收敛 | `desired_deployments`、`desired_applied_drift`、`oldest_desired_applied_drift_age_seconds`、`quarantined_deployments`、`restart_count_total`、`in_progress_commands`、`overdue_in_progress_commands`、`command_outcomes`、`terminal_command_outcomes` |
| 授权过期 | `policy_heads`、`expired_policy_heads`、`next_policy_expiry_seconds`、`transport_authorities`、`invalid_transport_authorities`、`next_transport_expiry_seconds` |

最值得优先告警的三个：

- **`oldest_pending_fact_age_seconds` 持续上升** —— 事实正在本地堆积，因为投递不出去。执行不受影响，但你对执行的**视图**正在变陈旧。
- **`desired_applied_drift` 非零且不下降** —— runner 接受了一个它尚未收敛到的期望状态。
- **`next_transport_expiry_seconds` 变小** —— 某个授权临近过期，而过期会让 runner 变为未就绪。

注意第一项**刻意不是**就绪失败。一台暂时投递不出事实的 runner 仍在正确交易、仍受本地保护；因为上报积压就把它移出服务，等于把一个可观测性问题变成一个执行问题。见[失联不等于停止](/zh-Hans/trust-model/safety-survives-disconnect)。

## 接入方式

**Docker Compose**

```yaml
healthcheck:
  test: ["CMD", "arx-runner", "health"]
  interval: 30s
  timeout: 5s
  retries: 3
  start_period: 60s
```

务必给 `start_period`。启动时要做完整的授权验证，所以最初几秒未就绪是合理的。

**systemd**

用 timer 或监管单元来跑探针，而不是 `ExecStartPost` —— 就绪是一个**持续条件**，不是一次性的启动结果。

**Kubernetes**

把 `arx-runner health` 用作 readiness probe，liveness probe 则建议只检查进程本身。因为「变为未就绪」就重启 runner，会丢掉正是那份未就绪在提示你去看的本地状态。

## 报告未就绪时

| 先看 | 含义 |
|---|---|
| 文件完全不存在 | 守护进程从未到达就绪；去读它的启动输出 |
| `credential_state` 非 `active` | 已在上游轮换或吊销 —— 跑 `arx-runner credential verify` |
| `credential_valid_until` 已过 | 已过期；轮换它 |
| `nats_connected` 为 false | 传输不可达，或授权验证失败 |
| `sqlite_quick_check` 非 `ok` | 本地存储受损；**先读**[排障](/zh-Hans/operator-guide/troubleshooting)再删任何东西 |
| `invalid_transport_authorities` 非零 | 有授权未通过验证 |

状态文档会指明是哪一项没过 —— 所以退出码是信号，JSON 是诊断。
