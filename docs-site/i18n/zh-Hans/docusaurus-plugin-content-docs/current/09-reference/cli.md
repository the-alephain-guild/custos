---
title: "CLI 参考"
sidebar_position: 1
---

`arx-runner` 是 runner 的控制台接口。在源码仓库使用 `uv run arx-runner`，或在已激活环境中直接使用 `arx-runner`。`python -m custos` 已停用。runner 不提供 HTTP 管理 API。

## 命令索引

| 命令 | 用途 |
|---|---|
| `enroll` | 获取 ARX 认证的机器身份 |
| `credential verify/rotate/revoke` | 管理注册后的凭据 |
| `identity standalone` | 创建离线使用的本地未认证身份 |
| `vault put/verify/list` | 存储和检查本地交易所凭据 |
| `nats bootstrap` | 通过 `--profile standalone` 初始化自有离线 stream |
| `deployment validate/publish` | 校验或发布 `OfflineDeploymentSpec`，仅限 sandbox/testnet |
| `nats-transport enroll/rotate/revoke/resume/verify` | 管理签名通道传输授权 |
| `publish-capability` | 使用已注册身份发布签名能力版本 |
| `release-policy generate-development-authority/issue` | 创建本地发布信任策略材料；开发 authority 不构成生产批准 |
| `start` | 启动所选通道 |
| `health` | 读取本地就绪文档 |

## 启动 runner

签名部署消费要求 `--reconcile`、已注册身份、能力与信任输入，以及至少一个 `--enabled-mode`。重复该参数可增加签名传输会话。Nautilus 宿主仍在同一 event loop 上只允许一个活动 node。

离线操作要求 `--reconcile-strategy-id`、已有本地身份和 `--nats-url`。它从 spec 读取模式，用 `--offline-state` 保存独立数据库，并以 `--runner-label` 报告状态，默认 label 为 runner UUID。离线通道拒绝 live。`--production-state-root` 用于签名通道，不能选择离线组合。

详见[签名 sandbox](/getting-started/first-sandbox-run)、[独立 sandbox](/getting-started/standalone-sandbox)和[配置参考](/reference/configuration)。

## 配置要点

- `vault put` 要求选择一种秘密输入方式。优先使用 `--api-secret-stdin`，命令行秘密可能出现在进程列表和历史记录中。
- `--scope-digest` 绑定签名通道的凭据范围。离线 spec 引用本地 key id，并单独派生宿主范围。
- `deployment --mode` 断言 spec 模式，不转换模式。`--strategy-dir` 在内存中绑定或校验目录摘要，不改写 spec 文件。
- `credential rotate/revoke` 要求 `--reason`。独立身份不能使用这些远端生命周期操作。
- 发布信任策略签发需要匹配的 authority key 和 Sigstore 身份/根，详见[部署指南](/operator-guide/deployment)。

## 退出码

| 退出码 | 含义 |
|---|---|
| `0` | 命令成功；`health` 判定通过 |
| `1` | 操作失败或健康状态未就绪 |
| `2` | Parser 用法错误或使用了已停用入口 |

## Parser 参考

下表从实际 parser 生成，显示清除可选环境覆盖后的默认值。路径中的 `~` 表示当前用户主目录。运行时可能增加条件，例如签名通道要求 `--enabled-mode`；请结合具体命令的帮助和上方指南使用。

<!-- generated:cli -->

### credential

### credential verify

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--runner-toml` | 可选 | `~/.arx/runner.toml` | — |

### credential rotate

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--runner-toml` | 可选 | `~/.arx/runner.toml` | — |
| `--reason` | 必填 | — | — |
| `--age-recipient` | 可选 | — | — |

### credential revoke

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--runner-toml` | 可选 | `~/.arx/runner.toml` | — |
| `--reason` | 必填 | — | — |
| `--authority-path` | 可选 | `~/.arx/runner-capability.json` | — |
| `--ready-file` | 可选 | `~/.arx/state/runner-ready.json` | — |

### deployment

### deployment validate

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--spec-file` | 必填 | — | — |
| `--strategy-dir` | 可选 | — | — |
| `--mode` | 可选 | — | — |

### deployment publish

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--spec-file` | 必填 | — | — |
| `--tenant-id` | 必填 | — | — |
| `--strategy-id` | 必填 | — | — |
| `--nats-url` | 可选 | `nats://localhost:4222` | — |
| `--strategy-dir` | 可选 | — | — |
| `--mode` | 可选 | — | — |

### enroll

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--token-file` | 必填 | — | — |
| `--backend` | 必填 | — | — |
| `--tenant-id` | 必填 | — | — |
| `--runner-id` | 必填 | — | — |
| `--agent-version` | 可选 | — | — |
| `--runner-toml` | 可选 | `~/.arx/runner.toml` | — |
| `--machine-vault` | 可选 | `~/.arx/vault/runner-machine.enc` | — |
| `--age-recipient` | 可选 | — | — |

### publish-capability

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--manifest` | 必填 | — | — |
| `--runner-toml` | 可选 | `~/.arx/runner.toml` | — |
| `--authority-path` | 可选 | `~/.arx/runner-capability.json` | — |
| `--idempotency-key` | 可选 | — | — |
| `--capability-version-id` | 可选 | — | — |
| `--capability-version` | 可选 | — | — |

### release-policy

### release-policy generate-development-authority

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--private-key-output` | 必填 | — | — |
| `--public-key-output` | 必填 | — | — |
| `--receipt-output` | 必填 | — | — |

### release-policy issue

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--authority-private-key` | 必填 | — | — |
| `--authority-public-key` | 必填 | — | — |
| `--sigstore-trusted-root` | 必填 | — | — |
| `--policy-id` | 必填 | — | — |
| `--version` | 必填 | — | — |
| `--not-before` | 必填 | — | — |
| `--expires-at` | 必填 | — | — |
| `--issuer` | 必填 | — | — |
| `--workflow-identity` | 必填 | — | — |
| `--source-repository` | 必填 | — | — |
| `--envelope-output` | 必填 | — | — |
| `--receipt-output` | 必填 | — | — |
| `--environment-output` | 必填 | — | — |

### health

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--ready-file` | 可选 | `~/.arx/state/runner-ready.json` | — |
| `--json` | 可选 | `False` | — |

### identity

### identity standalone

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--tenant-id` | 必填 | — | — |
| `--runner-toml` | 可选 | `~/.arx/runner.toml` | — |
| `--machine-vault` | 可选 | `~/.arx/vault/runner-machine.enc` | — |
| `--age-recipient` | 可选 | — | — |
| `--valid-days` | 可选 | `365` | — |

### nats

### nats bootstrap

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--profile` | 必填 | — | `standalone` |
| `--nats-url` | 可选 | `nats://localhost:4222` | — |
| `--tenant-id` | 必填 | — | — |
| `--timeout-secs` | 可选 | `30.0` | — |

### nats-transport

### nats-transport enroll

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--runner-toml` | 可选 | `~/.arx/runner.toml` | — |
| `--machine-vault` | 可选 | — | — |
| `--transport-vault-dir` | 可选 | `~/.arx/vault/runner-nats-transport` | — |
| `--trading-mode` | 必填 | — | `sandbox`, `testnet`, `live` |
| `--nats-url` | 必填 | — | — |
| `--nats-ca` | 可选 | `~/.arx/certs/crucible-nats-ca.pem` | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--nats-server-name` | 必填 | — | — |
| `--verification-timeout-secs` | 可选 | `30.0` | — |
| `--issuer-public-key` | 可选 | — | — |
| `--crucible-url` | 必填 | — | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--authorization-intent-id` | 必填 | — | — |
| `--operation-timeout-secs` | 可选 | `300.0` | — |
| `--age-recipient` | 可选 | — | — |

### nats-transport rotate

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--runner-toml` | 可选 | `~/.arx/runner.toml` | — |
| `--machine-vault` | 可选 | — | — |
| `--transport-vault-dir` | 可选 | `~/.arx/vault/runner-nats-transport` | — |
| `--trading-mode` | 必填 | — | `sandbox`, `testnet`, `live` |
| `--nats-url` | 必填 | — | — |
| `--nats-ca` | 可选 | `~/.arx/certs/crucible-nats-ca.pem` | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--nats-server-name` | 必填 | — | — |
| `--verification-timeout-secs` | 可选 | `30.0` | — |
| `--issuer-public-key` | 可选 | — | — |
| `--crucible-url` | 必填 | — | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--authorization-intent-id` | 必填 | — | — |
| `--operation-timeout-secs` | 可选 | `300.0` | — |
| `--age-recipient` | 可选 | — | — |

### nats-transport revoke

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--runner-toml` | 可选 | `~/.arx/runner.toml` | — |
| `--machine-vault` | 可选 | — | — |
| `--transport-vault-dir` | 可选 | `~/.arx/vault/runner-nats-transport` | — |
| `--trading-mode` | 必填 | — | `sandbox`, `testnet`, `live` |
| `--nats-url` | 必填 | — | — |
| `--nats-ca` | 可选 | `~/.arx/certs/crucible-nats-ca.pem` | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--nats-server-name` | 必填 | — | — |
| `--verification-timeout-secs` | 可选 | `30.0` | — |
| `--issuer-public-key` | 可选 | — | — |
| `--crucible-url` | 必填 | — | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--authorization-intent-id` | 必填 | — | — |
| `--operation-timeout-secs` | 可选 | `300.0` | — |
| `--age-recipient` | 可选 | — | — |

### nats-transport resume

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--runner-toml` | 可选 | `~/.arx/runner.toml` | — |
| `--machine-vault` | 可选 | — | — |
| `--transport-vault-dir` | 可选 | `~/.arx/vault/runner-nats-transport` | — |
| `--trading-mode` | 必填 | — | `sandbox`, `testnet`, `live` |
| `--nats-url` | 必填 | — | — |
| `--nats-ca` | 可选 | `~/.arx/certs/crucible-nats-ca.pem` | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--nats-server-name` | 必填 | — | — |
| `--verification-timeout-secs` | 可选 | `30.0` | — |
| `--issuer-public-key` | 可选 | — | — |
| `--crucible-url` | 必填 | — | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--operation-timeout-secs` | 可选 | `300.0` | — |
| `--age-recipient` | 可选 | — | — |

### nats-transport verify

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--runner-toml` | 可选 | `~/.arx/runner.toml` | — |
| `--machine-vault` | 可选 | — | — |
| `--transport-vault-dir` | 可选 | `~/.arx/vault/runner-nats-transport` | — |
| `--trading-mode` | 必填 | — | `sandbox`, `testnet`, `live` |
| `--nats-url` | 必填 | — | — |
| `--nats-ca` | 可选 | `~/.arx/certs/crucible-nats-ca.pem` | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--nats-server-name` | 必填 | — | — |
| `--verification-timeout-secs` | 可选 | `30.0` | — |
| `--issuer-public-key` | 可选 | — | — |

### start

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--runner-toml` | 可选 | `~/.arx/runner.toml` | — |
| `--machine-vault` | 可选 | — | — |
| `--nats-transport-vault-dir` | 可选 | `~/.arx/vault/runner-nats-transport` | — |
| `--enabled-mode` | 可选 | — | `sandbox`, `testnet`, `live`; 可重复 |
| `--development-local-nats-url` | 可选 | — | — |
| `--nats-sim-url` | 可选 | — | — |
| `--nats-sim-ca` | 可选 | `~/.arx/certs/crucible-nats-ca.pem` | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--nats-sim-server-name` | 可选 | — | — |
| `--nats-sim-issuer-public-key` | 可选 | — | — |
| `--nats-live-url` | 可选 | — | — |
| `--nats-live-ca` | 可选 | `~/.arx/certs/crucible-nats-ca.pem` | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--nats-live-server-name` | 可选 | — | — |
| `--nats-live-issuer-public-key` | 可选 | — | — |
| `--vault-dir` | 可选 | `~/.arx/vault` | — |
| `--reconcile` | 可选 | `False` | — |
| `--reconcile-strategy-id` | 可选 | — | — |
| `--runner-label` | 可选 | — | — |
| `--nats-url` | 可选 | `nats://localhost:4222` | — |
| `--offline-state` | 可选 | `~/.arx/state/offline-lane.db` | — |
| `--crucible-domain-public-key` | 可选 | `~/.arx/crucible-domain-event.pub` | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--crucible-domain-key-id` | 可选 | — | — | <!-- disclosure-ok: exact public CLI flag or default path from argparse -->
| `--engine` | 可选 | `nautilus` | `nautilus`, `sandbox-sim` |
| `--ready-file` | 可选 | `~/.arx/state/runner-ready.json` | — |
| `--runner-capability` | 可选 | `~/.arx/runner-capability.json` | — |
| `--runner-fact-outbox` | 可选 | `~/.arx/state/runner-fact-outbox.db` | — |
| `--development-artifact-root` | 可选 | `~/.alephain/v1-team/strategy-artifacts` | — |
| `--artifact-quarantine-dir` | 可选 | `~/.arx/state/artifact-quarantine` | — |
| `--artifact-activation-dir` | 可选 | `~/.arx/state/artifact-activations` | — |
| `--artifact-cache-dir` | 可选 | `~/.arx/state/artifact-cache` | — |
| `--artifact-registry` | 可选 | `ghcr.io` | — |
| `--artifact-registry-username` | 可选 | — | — |
| `--artifact-release-policy-envelope` | 可选 | — | — |
| `--artifact-release-policy-key-id` | 可选 | — | — |
| `--artifact-release-policy-public-key` | 可选 | — | — |
| `--artifact-sigstore-trusted-root` | 可选 | — | — |
| `--runner-fact-snapshot-interval-secs` | 可选 | `10.0` | — |
| `--runner-fact-period-secs` | 可选 | `86400` | — |
| `--runner-fact-period-retry-secs` | 可选 | `30.0` | — |
| `--runtime-promotion-receipt` | 可选 | — | — |
| `--runtime-promotion-bundle` | 可选 | — | — |
| `--runtime-sigstore-trusted-root` | 可选 | — | — |
| `--runtime-image-digest` | 可选 | — | — |
| `--runtime-source-revision` | 可选 | — | — |
| `--production-state-root` | 可选 | — | — |

### vault

### vault put

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--key-id` | 必填 | — | — |
| `--tenant-id` | 必填 | — | — |
| `--api-key` | 必填 | — | — |
| `--api-passphrase-env` | 可选 | — | — |
| `--scope-digest` | 必填 | — | — |
| `--api-secret-stdin` | 同组选一 | `False` | — |
| `--api-secret-env` | 同组选一 | — | — |
| `--api-secret` | 同组选一 | — | — |
| `--age-recipient` | 可选 | — | — |
| `--permission-scope` | 可选 | `trade_no_withdraw` | `trade_no_withdraw` |
| `--vault-dir` | 可选 | `~/.arx/vault` | — |

### vault verify

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--key-id` | 必填 | — | — |
| `--tenant-id` | 必填 | — | — |
| `--vault-dir` | 可选 | `~/.arx/vault` | — |
| `--age-key-file` | 可选 | — | — |

### vault list

| 参数 | 要求 | 默认值 | 可选值 / 重复 |
|---|---|---|---|
| `--vault-dir` | 可选 | `~/.arx/vault` | — |

<!-- /generated:cli -->
