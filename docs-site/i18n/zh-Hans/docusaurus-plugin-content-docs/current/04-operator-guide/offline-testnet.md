---
title: "离线 testnet 操作"
sidebar_position: 7
---

这条路径在不依赖 ARX 的情况下，将兼容策略运行到交易所测试网。订单经过真实网络，使用测试资金。独立模拟宿主演示不会验证这条路径。

## 准备

1. 在 Python 3.12 环境安装 Nautilus extra。
2. 按[独立 sandbox](/getting-started/standalone-sandbox)创建独立身份和 broker 拓扑，使用单独的持久化状态目录。
3. 提供真实兼容的策略目录及注册名称，其 `config.yaml` 必须显式设置 `trading.connector`、`trading.pairs` 和 `trading.leverage`。不能使用模拟宿主 fixture。
4. 将测试网专用交易凭据写入本地金库，匹配租户和 `provenance_ref.credential_id`。
5. 选择测试网账户支持的 Binance connector。SoDEX 用户先阅读[输入限制](/engines/sodex)。

## 配置与校验

从策略的离线 spec 开始，将 `trading_mode` 设为 `testnet`，移除 `sandbox` 对象，并设置明确限额。spec 不携带 connector、pairs 和 leverage：runner 从 `strategy_path` 下 `config.yaml` 的 `trading` 段读取这三项，与策略读取的是同一份值；spec 中仍写有这三项时会被拒绝。发布前请确认它们适用于你的测试网账户。`risk_config` 仅接受 `max_total_notional` 和 `max_drawdown_pct`，值为正的十进制字符串或整数，按部署生效。

将 `SPEC_FILE`、`STRATEGY_DIR`、`STATE_ROOT`、`TENANT_ID`、`STRATEGY_ID`、`RUNNER_LABEL` 和 `NATS_URL` 设为本地配置，并通过 `SOPS_AGE_KEY_FILE` 导出 age identity 路径。

```bash
uv run arx-runner deployment validate --mode testnet \
  --spec-file "$SPEC_FILE" --strategy-dir "$STRATEGY_DIR"
uv run arx-runner deployment publish --mode testnet \
  --spec-file "$SPEC_FILE" --strategy-dir "$STRATEGY_DIR" \
  --tenant-id "$TENANT_ID" --strategy-id "$STRATEGY_ID" --nats-url "$NATS_URL"
uv run arx-runner start --engine nautilus \
  --runner-toml "$STATE_ROOT/runner.toml" --vault-dir "$STATE_ROOT/vault" \
  --ready-file "$STATE_ROOT/ready.json" --offline-state "$STATE_ROOT/offline.db" \
  --reconcile-strategy-id "$STRATEGY_ID" --runner-label "$RUNNER_LABEL" \
  --nats-url "$NATS_URL"
```

`--strategy-dir` 校验或绑定源码摘要；spec 的 `strategy_path` 必须在 runner 环境内指向同一个可读目录。部署运行期间应避免修改挂载代码。

离线桥接从 credential id 派生凭据范围，宿主用它避免活动部署冲突。离线 schema 没有可手工填写的 `credential_scope` 字段。

## 观察与停止

检查 `arx.<tenant>.deployment_status.<runner-label>.<spec-id>` 的本地状态，再核对引擎就绪、测试网订单和持仓。本地 `health: healthy` 表示已应用目标状态，不是签名验收收据。

停止时沿用 spec id，递增 generation 并设置 `lifecycle_state: stopped`。默认停止策略保留持仓，结束监督前应在交易所核对未完成订单和持仓。熔断触发有单独的风险控制行为，详见[应急恢复](/operator-guide/emergency-playbook)。

测试网结果和离线状态不能授权 live 执行，也不能作为生产晋升证据。
