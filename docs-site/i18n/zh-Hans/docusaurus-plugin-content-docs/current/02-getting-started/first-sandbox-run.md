---
title: "首次运行签名 sandbox"
sidebar_position: 3
---

本指南将已注册 runner 接入 ARX，并启用部署协调。无需 ARX 的本地验证见[独立 sandbox](/getting-started/standalone-sandbox)。

## 启动前

先完成[注册](/getting-started/enrollment)，再向部署管理员取得：

- sandbox NATS 传输授权、TLS CA、server name 和固定的 issuer 公钥。
- 验证部署指令所需的 domain-event 公钥及 key id。
- 位于 `~/.arx/runner-capability.json` 的有效 runner 能力收据。
- 交易所凭据，以及签名部署绑定的精确 scope digest。
- 可访问的策略发布材料和 runner 本地发布信任配置，或通过受支持签名路径提供的显式 sandbox 开发产物。

另取得传输授权 URL 和已批准的授权 intent UUID，设为 `TRANSPORT_AUTHORITY_URL` 与 `TRANSPORT_INTENT_ID`。将 `NATS_SIM_URL`、`NATS_SIM_SERVER_NAME`、`NATS_SIM_ISSUER_PUBLIC_KEY`、`NATS_CA_FILE`、`DOMAIN_PUBLIC_KEY_FILE` 和 `DOMAIN_KEY_ID` 设为取得的值。`SOPS_AGE_KEY_FILE` 必须指向注册时使用的 age identity。

## 配置传输和交易所密钥

```bash
uv run arx-runner nats-transport enroll \
  --trading-mode sandbox \
  --authorization-intent-id "$TRANSPORT_INTENT_ID" \
  --nats-url "$NATS_SIM_URL" \
  --nats-server-name "$NATS_SIM_SERVER_NAME" \
  --nats-ca "$NATS_CA_FILE" \
  --crucible-url "$TRANSPORT_AUTHORITY_URL" # disclosure-ok: exact CLI flag accepted by the parser
```

按[金库操作](/operator-guide/credential-vault)配置凭据。`sandbox-sim` 也会解析本地金库。仅在已批准的 sandbox 部署明确使用演示密钥时使用演示值，不要自行编造签名 scope digest。

使用不可变发布产物时，先按[部署指南](/operator-guide/deployment)配置发布策略和信任根。

## 启动协调

```bash
uv run arx-runner start \
  --enabled-mode sandbox \
  --engine sandbox-sim \
  --reconcile \
  --nats-sim-url "$NATS_SIM_URL" \
  --nats-sim-ca "$NATS_CA_FILE" \
  --nats-sim-server-name "$NATS_SIM_SERVER_NAME" \
  --nats-sim-issuer-public-key "$NATS_SIM_ISSUER_PUBLIC_KEY" \
  --crucible-domain-public-key "$DOMAIN_PUBLIC_KEY_FILE" \
  --crucible-domain-key-id "$DOMAIN_KEY_ID"
```
<!-- disclosure-ok: exact CLI flags accepted by the runner -->

`--reconcile` 用于创建引擎并订阅部署指令。省略它时，进程仍可能通过健康检查，但没有部署消费者运行。

`sandbox-sim` 不连接交易所。运行兼容策略、读取实时行情并在本地模拟成交时，应安装 Nautilus extra，再选择 `--engine nautilus`。

## 验证结果

在另一个终端运行：

```bash
uv run arx-runner health --json
```

检查 `ready: true` 与 `deployment_subscription: true`。随后在 ARX 创建并批准 sandbox 部署，核对对应实例和 generation 的生命周期事实。引擎就绪还要求可靠的投资组合估值；daemon 健康不等于策略就绪。

启动失败时按[排错指南](/operator-guide/troubleshooting)检查错误中指明的身份、传输或产物条件。不要把切换离线通道作为签名部署失败的恢复方式。
