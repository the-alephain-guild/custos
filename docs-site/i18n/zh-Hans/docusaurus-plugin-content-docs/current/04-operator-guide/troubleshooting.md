---
title: "排错"
sidebar_position: 6
---

先记录所选通道、源码/镜像 revision、CLI 参数和本地 JSON 日志。诊断输出中不得包含凭据材料。

| 症状 | 检查 | 处理 |
|---|---|---|
| Health 通过却没有签名部署 | `deployment_subscription` 与 `--reconcile` | 启用协调，并配置签发方提供的指令信任输入 |
| `Runner startup authority check failed` | `runner.toml`、金库路径、age key、有效期及身份绑定 | 修正路径，或通过支持的生命周期更新身份 |
| 签名命令拒绝独立身份 | `backend_url` 为 `.invalid` | 使用离线通道，或另向 ARX 注册身份 |
| 本地目标状态未送达 | Broker URL、bootstrap、租户及 strategy id | 使 bootstrap、publish 和 start 参数匹配 |
| `stream ... is not owned` | 已有 broker 拓扑 | 使用独立 broker/租户，不覆盖其他应用的 stream |
| 发布前拒绝 spec | 模式、精确 schema 字段、源码摘要 | 执行 `deployment validate`；离线通道不能使用 live |
| 凭据解密/范围失败 | Key id、租户、金库路径、age identity | 执行 `vault verify`；签名范围需匹配部署 |
| `credential scope already has an active ...` | 使用同一范围的活动 testnet 部署 | 先停止并检查已有实例，再复用凭据范围 |
| `already holds this runner's event loop` | 已有 Nautilus node | 停止该 node 或使用独立进程和状态目录 |
| `strategy discovery is already pointed at ...` | 挂载策略目录 | 每个离线进程只使用一个策略目录 |
| `strategy config ... does not set trading.<key>` | spec 中 `strategy_path` 下的 `config.yaml` | 在该文件中设置 `trading.connector`、`trading.pairs` 和 `trading.leverage`；不会使用内置默认值 |
| 签名部署以 `strategy_trading_scope_mismatch` 被隔离 | 拒绝信息中策略一侧与部署一侧的 connector、品种和杠杆 | 按策略 release 声明的交易范围创建部署；runner 不会在部署未授权的品种或杠杆上运行策略 |
| 以 `strategy_trading_scope_undeclared` 或 `signed_strategy_config_overrides_trading` 被隔离 | 策略配置，以及签名的 strategy config | 策略须在配置中声明 `trading`；签名的 strategy config 不得包含 `trading` 段 |
| `portfolio_prices_missing:<instrument>` | 缺失标的及其标记/计价资产 | 检查名称、网络和行情可用性，不以零代替 |
| `portfolio_equity_missing:<currency>` | 账户与结算币种 | 检查账户余额和估值所需价格 |
| 熔断后拒绝离线 generation | 熔断锁存与近期风险控制日志 | 核对持仓/订单，处理原因后再主动重启 |
| SoDEX testnet 无法提供账户字段 | 离线 spec 契约 | 使用支持的 sandbox 路径，详见[SoDEX](/engines/sodex) |

## 签名指令失败

检查签名/key id、subject 身份、精确摘要、generation、产物绑定、能力和凭据范围。无效输入终止处理，可恢复的本地依赖失败会重试。应在 ARX 修正目标状态，不向签名订阅注入未签名输入。

## 金库检查

```bash
uv run arx-runner vault verify --key-id "$KEY_ID" --tenant-id "$TENANT_ID"
```

自定义路径时添加 `--vault-dir`。该命令使用与 runner 相同的显式 sops JSON 解密路径。手工 sops 解密成功不能证明租户与范围检查通过。

## 保留证据

记录实例/spec id、generation、revision、时间、类型化错误原因及非秘密健康字段。恢复前保留 SQLite 状态及其 WAL/SHM 文件。离线状态通道尽力发布，发布失败时检查本地日志。详见[应急恢复](/operator-guide/emergency-playbook)。
