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

从策略的离线 spec 开始，它必须声明 `"spec_version": 2`（`arx-runner deployment schema` 会输出 runner 接受的版本）；将 `trading_mode` 设为 `testnet`，移除 `sandbox` 对象，并设置明确限额。spec 不携带 connector、pairs 和 leverage：runner 从 `strategy_path` 下 `config.yaml` 的 `trading` 段读取这三项，与策略读取的是同一份值；spec 中仍写有这三项时会被拒绝。发布前请确认它们适用于你的测试网账户。`risk_config` 仅接受 `max_total_notional` 和 `max_drawdown_pct`，值为正的十进制字符串或整数，按部署生效。

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

## 交易所拒绝撤单时

风险控制与停止会逐张撤单，每次被拒后先看拒绝原因，再决定是否重发：

| 拒绝 | runner 的处理 |
|---|---|
| 订单已在等待撤单回应 | 不再发撤单；15 秒内没有回应时向交易所查询该订单 |
| 交易所查不到该订单 | 停止撤单，改为向交易所查询三次：立即一次，之后隔 10 秒、再隔 20 秒各一次。最后一次查询 40 秒后本地仍显示挂单的，交给对账处理，不再为它发任何请求 |
| 触发限流 | 暂停该账户的全部安全路径请求（撤单、平仓、查询）60 秒，每次翻倍，最长 600 秒；不会改为一次撤销全部挂单 |
| 连接失败 | 暂停该账户 5 秒，每次翻倍，最长 120 秒；重试 10 次后放弃该订单 |
| 其他拒绝 | 2 秒后重试该订单，每次翻倍，最长 60 秒；重试 5 次后放弃 |

目前只对 SoDEX 的拒绝文本分类；其他交易所的拒绝一律按「其他拒绝」处理。在 SoDEX 上，安全路径的请求还按账户限额：先允许 10 个，之后每秒 1 个，账户其余的下单额度留给同一账户上的其他交易。

runner 放弃的订单并不等于已撤：停止或风险控制仍报告它未确认。停止的错误日志与 `stop_summary.residual_orders` 按原因列出剩余订单数，例如 `in_flight`、`not_found`、`venue_state_contradicted`、`cancel_abandoned`、`paused` 或 `backing_off`。结束监督前，应在交易所核对这些订单。

## 状态与停止报告中的尘埃

尘埃是低于交易对最小可下单量的持有：数量低于最小下单量，或者在已知价格时，价值低于最小名义额。任何订单都卖不出、平不掉它，所以风险控制与平仓停止把它视为已平，不为它发平仓单，也不等待它消失。现货手续费以基础币扣除时，常会留下这样的余量。

尘埃出现在引擎状态与遥测快照的 `dust` 列表中；离线通道停止时，也出现在 `stop_summary.dust` 与 `offline_shutdown_summary` 中。每一条写明交易对、资产、数量及其来自持仓还是账户余额、它低于的最小值及原因（`quantity` 或 `notional`），以及已知时的价格。`position_count` 包含尘埃持仓。

没有价格的尘埃不计入守卫评估的权益与敞口，因此不会触发熔断。数量达到或超过最小下单量的持有不在此列：没有价格时状态不可靠，守卫失败即停。OKX 上一笔最小订单可能价值数十美元，那里的尘埃与其他持有一样估值，缺价时同样失败即停。

## 限制容器日志大小

运行 runner 容器时开启日志轮转，例如为 Docker 的 `json-file` 日志驱动设置 `max-size` 与 `max-file`，避免一次长时间或日志繁多的运行占满磁盘。先读取停止的退出状态，再导出日志：导出很大的日志可能耗尽你为停止预留的时间。

测试网结果和离线状态不能授权 live 执行，也不能作为生产晋升证据。
