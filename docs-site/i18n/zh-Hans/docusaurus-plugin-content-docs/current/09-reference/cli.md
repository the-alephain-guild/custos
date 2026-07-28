---
title: "CLI 参考"
sidebar_position: 1
---

# CLI 参考

`arx-runner` 是这个 runner 的**唯一**接口。没有 HTTP 管理 API，没有配置文件驱动模式，
也没有第二个入口 —— `python -m custos` 会以非零码退出并指回这里。

```text
arx-runner {enroll,credential,vault,nats-transport,publish-capability,start,health}
```

每个子命令都支持 `--help`。本页是地图，`--help` 才是权威 —— 它由你实际运行的那个
parser 生成。

标注**必填**的 flag 没有默认值。凡是选择交易模式或指定权威来源的参数都刻意如此：
默认值等于一个没有任何人做过的选择。

## enroll

取得一个 runner 能够证明的机器身份。

```bash
arx-runner enroll \
  --token '<one-time-token>' \
  --backend https://arx.example.com \
  --tenant-id acme \
  --runner-id 018f8b5f-6f7d-7e23-8c31-bd34ab9d0d41
```

| Flag | 必填 | 含义 |
|---|---|---|
| `--token` | ✅ | ARX 签发的一次性注册令牌 |
| `--backend` | ✅ | 注册目标端点 |
| `--tenant-id` | ✅ | 所属租户 |
| `--runner-id` | ✅ | 本 runner 的 UUID |
| `--agent-version` | | 上报的 agent 版本 |
| `--runner-toml` | | 覆盖元数据路径 |
| `--machine-vault` | | 覆盖加密金库路径 |
| `--age-recipient` | | age 公钥接收者；默认取 `SOPS_AGE_RECIPIENT` |

它把公开绑定元数据写入 `runner.toml`，并把凭据与 Ed25519 私钥留在加密的机器金库里。
私钥在本地生成，**从不被传输**。见[注册](/zh-Hans/getting-started/enrollment)。

## credential

管理注册产出的机器凭据。

```bash
arx-runner credential verify
arx-runner credential rotate --reason "scheduled rotation"
arx-runner credential revoke --reason "host decommissioned"
```

| 子命令 | 必填 | 另接受 |
|---|---|---|
| `verify` | — | `--runner-toml` |
| `rotate` | `--reason` | `--runner-toml`、`--age-recipient` |
| `revoke` | `--reason` | `--runner-toml`、`--authority-path`、`--ready-file` |

两个破坏性操作都**必填** `--reason`，且会被记录。一次没有说明的轮换，与攻击者轮换他刚
偷到的密钥无法区分。

轮换用**旧密钥**签名的证明发送新公钥，且只在权威方接受之后才写本地。吊销在确认已吊销
状态后擦除本地金库与元数据。

## vault

管理场所凭据。一把密钥一个加密文件。

```bash
printf '%s\n' '<api-secret>' | arx-runner vault put \
  --key-id binance-testnet \
  --tenant-id acme \
  --api-key '<api-key>' \
  --api-secret-stdin \
  --scope-digest '<lowercase-sha256>' \
  --age-recipient "$SOPS_AGE_RECIPIENT" \
  --permission-scope trade_no_withdraw

arx-runner vault verify --key-id binance-testnet --tenant-id acme
arx-runner vault list
```

### `vault put`

| Flag | 必填 | 含义 |
|---|---|---|
| `--key-id` | ✅ | 金库条目名；同时是文件名，故须匹配 `^[a-zA-Z0-9_-]{1,64}$` |
| `--tenant-id` | ✅ | 所属租户 |
| `--api-key` | ✅ | 场所 API key（非密文） |
| `--scope-digest` | ✅ | DeploymentSpec 绑定为该凭据 scope 的小写 SHA-256 |
| `--api-secret-stdin` / `--api-secret-env` / `--api-secret` | ✅（三选一） | 密文的提供方式 |
| `--age-recipient` | | age 公钥接收者 |
| `--permission-scope` | | 仅 `trade_no_withdraw`，同时是默认值 |
| `--vault-dir` | | 覆盖金库目录 |

优先用 `--api-secret-stdin`。通过 `--api-secret` 传入的密文会出现在 `ps` 输出与 shell
历史里。

### `vault verify`

必填 `--key-id` 与 `--tenant-id`；另接受 `--vault-dir` 与 `--age-key-file`。它跑真实解密
路径 —— sops 解密、payload 解析、文件权限、权限范围。**这是验收面**；手工调 `sops` 测的
是另一回事。

### `vault list`

列出现有 key id，并对 group / world 可读的文件在 stderr 告警。接受 `--vault-dir`。

## nats-transport

签发与管理 runner 的传输授权。五个子命令接受同一组 flag。

```bash
arx-runner nats-transport verify \
  --trading-mode sandbox \
  --nats-url tls://nats.example.com:4222 \
  --nats-server-name nats.example.com
```

| Flag | 必填 |
|---|---|
| `--trading-mode` `{sandbox,testnet,live}` | ✅ |
| `--nats-url` | ✅ |
| `--nats-server-name` | ✅ |
| `--nats-ca`、`--runner-toml`、`--machine-vault`、`--transport-vault-dir`、`--verification-timeout-secs` | |

子命令：`enroll`、`rotate`、`revoke`、`resume`、`verify`。

## publish-capability

用已注册的机器密钥签名并发布下一个能力修订。

| Flag | 必填 |
|---|---|
| `--manifest` | ✅ |
| `--runner-toml`、`--authority-path`、`--idempotency-key`、`--capability-version-id`、`--capability-version` | |

## start

运行守护进程。它只在机器授权通过 fail-closed 验证之后才启动。

```bash
arx-runner start \
  --enabled-mode sandbox \
  --engine sandbox-sim
```

**`--enabled-mode {sandbox,testnet,live}` 必填。** 一个进程一个模式。

### 选择引擎

| Flag | 默认 | 效果 |
|---|---|---|
| `--engine nautilus` | 默认 | 三种模式下的真实执行 |
| `--engine sandbox-sim` | | 完整本地生命周期、不连场所；只声明 `sandbox` |

### 传输

sandbox 与 testnet 走**模拟**传输，只有 `live` 走 live 传输。所以一台 sandbox runner 用
`--nats-sim-*` 系列配置。

| 分组 | Flag |
|---|---|
| 模拟 | `--nats-sim-url`、`--nats-sim-ca`、`--nats-sim-server-name`、`--nats-sim-issuer-public-key` |
| Live | `--nats-live-url`、`--nats-live-ca`、`--nats-live-server-name`、`--nats-live-issuer-public-key` |
| 本地回环开发 | `--development-local-nats-url` —— 仅 sandbox、不可提升 |

### 指令验证

| Flag | 含义 |
|---|---|
| `--crucible-domain-public-key` | 用于验证签名期望状态指令的公钥 |
| `--crucible-domain-key-id` | 期望的签名 key id |
| `--reconcile` | 启用 reconcile 循环 |

这两个 flag 名保留了历史拼写。它们是 parser 实际接受的字符串，因此**原样照录而不做美化**
—— 文档里被改名的 flag，等于一条跑不起来的命令。
<!-- disclosure-ok: exact CLI flag an operator types; renaming it here would document a command argparse rejects -->

### 路径

| Flag | 默认 |
|---|---|
| `--runner-toml` | `~/.arx/runner.toml` |
| `--machine-vault` | 覆盖值；必须等于 `runner.toml` 中的值 |
| `--vault-dir` | `~/.arx/vault` |
| `--ready-file` | `~/.arx/state/runner-ready.json` |
| `--runner-capability` | `~/.arx/runner-capability.json` |
| `--runner-fact-outbox` | `~/.arx/state/runner-fact-outbox.db` |
| `--nats-transport-vault-dir` | `~/.arx/vault/runner-nats-transport` |

### Artifact 处理

`--artifact-quarantine-dir`、`--artifact-activation-dir`、`--artifact-cache-dir`、
`--artifact-registry`、`--artifact-registry-username`、
`--artifact-release-policy-envelope`、`--artifact-release-policy-key-id`、
`--artifact-release-policy-public-key`、`--artifact-sigstore-trusted-root`、
`--development-artifact-root`。

验证是 fail closed 的 —— 见[策略工具包](/zh-Hans/toolkit/overview)。

### 事实节奏

`--runner-fact-snapshot-interval-secs`（默认 `10.0`）、
`--runner-fact-period-secs`（默认 `86400`）、
`--runner-fact-period-retry-secs`（默认 `30.0`）。

## health

```bash
arx-runner health
arx-runner health --json
```

接受 `--ready-file`。授权缺失、过期、已吊销或不匹配时以非零码退出。就绪**不等于**
「进程起来了」—— 见[就绪与健康](/zh-Hans/operator-guide/readiness-health)。

## 退出码

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 操作失败 —— 消息会指明是哪一项检查没过 |
| `2` | 用法错误，或使用了已退休的 `python -m custos` 入口 |

## 没有哪些命令

**不存在**创建、批准或发布 DeploymentSpec 的命令，也不存在让 runner 给自己授权的命令。
这些按设计属于 ARX —— 见[信任模型](/zh-Hans/introduction/trust-model)。
