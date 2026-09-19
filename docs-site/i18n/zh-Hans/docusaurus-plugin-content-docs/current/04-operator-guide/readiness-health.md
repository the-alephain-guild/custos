---
title: "就绪与健康检查"
sidebar_position: 3
---

```bash
uv run arx-runner health --json
```

探针在本地读取 `~/.arx/state/runner-ready.json`，退出码为 0 或 1，不发起网络请求。daemon 使用其他路径时，探针也需指定 `--ready-file`。

## 三种检查

| 检查 | 能证明什么 | 查看内容 |
|---|---|---|
| Daemon 健康 | 已落盘的就绪文档满足判定条件 | `ready`、凭据有效期/绑定、传输模式、SQLite 检查 |
| 部署应用 | 目标 generation 到达本地应用边界 | 签名生命周期事实或离线 `observed_generation` |
| 引擎就绪 | node、连接、对账和投资组合条件成立 | 类型化引擎就绪收据或本地引擎诊断 |

签名 daemon 未带 `--reconcile` 时，可能健康但 `deployment_subscription: false`。需要接收部署时应检查此字段。离线 readiness 在订阅建立时写入，不是持续刷新的投资组合或传输监控；还需检查进程、近期状态和本地日志。

## 就绪判定

文档必须声明 ready、传输已连接、凭据有效且绑定正确、所有已启用传输模式连接、`sqlite_quick_check: ok`、无无效传输授权。文档缺失或格式错误时失败，凭据过期也会使探针失败。

文件包含公开身份/有效期、订阅状态和 `runtime_metrics`，不包含密钥或策略参数，以原子方式写入 `0700` 目录，文件权限为 `0600`。离线组合使用同一指标结构，但没有签名 outbox 或策略授权；相应计数为零不能证明签名投递成功。

## 引擎就绪

当前引擎检查八项条件：node 任务存活、行情连接、执行连接、投资组合初始化、可靠估值、对账初始化、策略可接收生命周期操作、必要能力已启用。

缺少标记价格或权益值时估值不可靠。检查 `portfolio_prices_missing:<instrument>` 等错误及其中的标的。投资组合已初始化，仍可能缺少计算权益所需的价格。

## 告警与监督

签名通道应监控 `oldest_pending_fact_age_seconds`、目标/已应用状态差异、隔离状态、策略/传输有效期及磁盘空间。报告积压本身不构成重启引擎的理由。

```yaml
healthcheck:
  test: ["CMD", "arx-runner", "health"]
  interval: 30s
  timeout: 5s
  retries: 3
  start_period: 60s
```

容器探针需使用匹配的 ready-file 路径。将进程存活与就绪检查分开，在配置自动重启前先调查未就绪原因。详见[排错指南](/operator-guide/troubleshooting)。
