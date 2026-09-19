---
title: "配置参考"
sidebar_position: 2
---

每个 runner 应使用独立身份和状态目录。默认根为 `~/.arx`，可用显式 CLI 参数修改各路径。

## 身份元数据

`runner.toml` 由 `enroll` 或 `identity standalone` 写入，精确字段集如下：

| 字段 | 含义 / 校验 |
|---|---|
| `tenant_id` | 非空、不含空白的租户 |
| `runner_id` | 非 nil UUID |
| `backend_url` | 绝对 backend URL；独立身份为 `http://standalone.invalid` |
| `credential_id` | 非 nil 凭据 UUID |
| `credential_version` | 正整数 |
| `credential_valid_until` | 带时区的时间戳 |
| `machine_key_id` | 以 `ed25519-` 开头的标识 |
| `machine_vault_path` | 加密机器金库的绝对路径 |
| `enrolled_at` | 带时区的时间戳 |

字段缺失或存在未知字段会导致加载失败。该文件是公开元数据，opaque credential 和私钥加密保存在引用的金库中。不要通过手工修改绑定切换身份。签名命令拒绝 backend 为 `.invalid` 的独立身份。

## 状态与产物路径

| 参数 | 默认值 / 用途 |
|---|---|
| `--runner-toml` | `~/.arx/runner.toml` |
| `--machine-vault` | 可选覆盖，必须匹配元数据绑定 |
| `--vault-dir` | `~/.arx/vault` |
| `--ready-file` | `~/.arx/state/runner-ready.json` |
| `--runner-capability` | `~/.arx/runner-capability.json`，用于签名通道 |
| `--runner-fact-outbox` | `~/.arx/state/runner-fact-outbox.db`，签名持久化状态/事实 |
| `--offline-state` | `~/.arx/state/offline-lane.db`，离线已应用 generation |
| `--production-state-root` | 可选的签名通道可变状态根，不能选择离线通道 |

产物缓存、隔离、激活、开发源码和传输金库各有独立参数，详见自动生成的[CLI 参考](/reference/cli)。自定义 production root 必须是真实目录，禁止组或其他用户写入，绑定状态路径必须位于该根下。

身份和凭据文件权限为 `0600`，私有目录为 `0700`。容器中需要持久化数据库和金库。`runner-ready.json` 是派生健康文档，不能替代数据库。

## 环境输入

| 变量 | 用途 |
|---|---|
| `SOPS_AGE_KEY_FILE` | 解密本地金库的 age identity |
| `SOPS_AGE_RECIPIENT` | 配置凭据时的默认公钥 recipient |
| `CUSTOS_ARTIFACT_RELEASE_POLICY_ENVELOPE` | 签名发布策略文件 |
| `CUSTOS_ARTIFACT_RELEASE_POLICY_PUBLIC_KEY` / `CUSTOS_ARTIFACT_RELEASE_POLICY_KEY_ID` | 策略 authority 绑定 |
| `CUSTOS_ARTIFACT_SIGSTORE_TRUSTED_ROOT` | Sigstore 可信根 |
| `CUSTOS_ARTIFACT_REGISTRY` / `CUSTOS_ARTIFACT_CACHE_DIR` | Registry 与缓存默认值 |
| `CUSTOS_ARTIFACT_REGISTRY_USERNAME` / `CUSTOS_ARTIFACT_REGISTRY_TOKEN` | 私有 registry 认证，需同时提供 |
| `CUSTOS_DEVELOPMENT_ARTIFACT_ROOT` | 显式 sandbox 开发源码位置 |
| `CUSTOS_DEVELOPMENT_LOCAL_NATS_URL` | 签名开发路径中显式的 loopback sandbox 传输例外 |

运行时从金库解密交易所秘密，不继承环境中的交易凭据。`vault put --api-secret-env` 仅用于配置输入，优先使用 stdin。策略配置见[部署指南](/operator-guide/deployment)，通道选择见[交易模式](/concepts/trading-modes)。
