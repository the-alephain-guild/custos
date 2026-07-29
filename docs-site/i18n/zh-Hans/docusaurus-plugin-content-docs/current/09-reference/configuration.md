---
title: "配置参考"
sidebar_position: 2
---

# 配置参考

Custos 把配置分放在 `~/.arx` 下的两处，按敏感度切分：

| 路径 | 内容 | 权限 |
|---|---|---|
| `~/.arx/runner.toml` | 注册时写入的非机密绑定元数据 | `0600`，目录 `0700` |
| `~/.arx/vault/<key-id>.enc` | 加密后的凭据与 runner 签名私钥 | `0600`，目录 `0700` |

`runner.toml` 里不存放任何机密。不透明的机器凭据与 Ed25519 私钥一起，存放在
`machine_vault_path` 指向的加密 vault 中。

只要这两处路径中任何一处对同组或其他用户可访问，Custos 就拒绝启动。

## `runner.toml`

该文件由 `arx-runner enroll` 写入，不应手工编辑。这里记录它，是为了让操作者能审计
runner 到底持久化了什么。

| 字段 | 类型 | 含义 |
|---|---|---|
| `tenant_id` | string | 所属租户。非空，不含空白字符。 |
| `runner_id` | UUID | 本 runner 的身份。不得为 nil UUID。 |
| `backend_url` | 绝对 URL | 本 runner 注册所对的控制面端点。 |
| `credential_id` | UUID | 已签发机器凭据的标识。不得为 nil。 |
| `credential_version` | 整数 ≥ 1 | 机器凭据的代次；轮换时递增。 |
| `credential_valid_until` | RFC 3339 时间戳 | 当前凭据代次的到期时间。 |
| `machine_key_id` | string | 签名密钥标识。必须以 `ed25519-` 开头。 |
| `machine_vault_path` | 绝对路径 | 加密机器 vault 的位置。 |
| `enrolled_at` | RFC 3339 时间戳 | 注册完成的时间。 |

每个字段在加载时都会校验。格式错误是**启动失败**，不是警告 —— 一个无法证明自身身份的
runner 不应该抵达任何交易所。

具体来说：`runner_id` 与 `credential_id` 必须能解析为 UUID 且不得为 nil；
`credential_version` 必须是正整数；两个时间戳都必须是 RFC 3339 **且带时区**；
`backend_url` 必须有 scheme 和 host；`machine_vault_path` 必须是绝对路径。

字段集合必须完全一致。缺少键和多出键属于同一类失败，错误信息会把两者都列出来 ——
多出一个字段意味着这份文件是由某个"对 runner 身份的理解与本 runner 不一致"的东西写的。

文件是原子写入的 —— 先写同目录下的临时文件、fsync、chmod 到 `0600`，再 rename 覆盖目标。写到一半崩溃留下的是原来那份完好的文件，而不是半截文件；这一点很要紧，因为每次启动都要读它。

### 示例

```toml
tenant_id = "acme"
runner_id = "6f1c8a30-6a5f-4a1e-9f0f-2a1d0f7a55c1"
backend_url = "https://control.example.com"
credential_id = "b0e4a8f2-9a11-4d3e-8f77-1c2b3d4e5f60"
credential_version = 2
credential_valid_until = "2026-12-31T23:59:59Z"
machine_key_id = "ed25519-7f3a1c"
machine_vault_path = "/home/operator/.arx/vault/runner-machine.enc"
enrolled_at = "2026-07-01T09:14:22Z"
```

## Vault 布局

凭据按"一个 key 一个加密文件"存放，用 sops 与 age 在进程内解密。配置、轮换与验证见[凭据 vault](/zh-Hans/operator-guide/credential-vault)。

```
~/.arx/vault/
├── runner-machine.enc       # 机器凭据 + Ed25519 签名私钥
└── <venue-key-id>.enc       # 每个交易所 API 凭据一个文件
```

用于解密这些文件的 age 身份通过 `SOPS_AGE_KEY_FILE` 提供。它不离开本机，也从不被传输。

## 命令行选项

`arx-runner start` 接受：

| 选项 | 默认 | 效果 |
|---|---|---|
| `--engine nautilus` | 默认 | 真实执行。sandbox、testnet 与 live 都可用，但受[执行门](/zh-Hans/concepts/live-execution-gate)约束。 |
| `--engine sandbox-sim` | — | 模拟宿主：完整的本地生命周期，不连接交易所。只声明 `sandbox`，因此 testnet 与 live 部署会被拒绝。 |
| `--runner-fact-outbox <path>` | `~/.arx/state/runner-fact-outbox.db` | 支撑可靠事实投递的 SQLite 数据库。 |
| `--vault-dir <path>` | `~/.arx/vault` | 存放按 key 加密凭据的目录。 |
| `--ready-file <path>` | `~/.arx/state/runner-ready.json` | 供 `arx-runner health` 消费的就绪标记。 |
| `--runner-capability <path>` | `~/.arx/runner-capability.json` | 已校验、且绑定到 runner 密钥的能力回执。 |

需要跨重启存活的状态位于 `~/.arx/state/` 下。它**不是**缓存：删掉 outbox 数据库，等于丢弃
runner 已经承诺要投递的事实。

注册与 vault 管理是各自独立的子命令：

```bash
arx-runner enroll \
  --token <enrollment-token> \
  --backend https://arx.example.com \
  --tenant-id <tenant> \
  --runner-id <uuid>

arx-runner vault put \
  --key-id <key-id> \
  --tenant-id <tenant> \
  --api-key <api-key> \
  --api-secret-stdin \
  --scope-digest <lowercase-sha256>
arx-runner vault verify --key-id <key-id> --tenant-id <tenant>
arx-runner vault list
arx-runner start --enabled-mode sandbox --engine nautilus
```

## 环境变量

| 变量 | 必需 | 用途 |
|---|---|---|
| `SOPS_AGE_KEY_FILE` | 是 | 解密 vault 所用 age 身份的路径。 |

运行中的 daemon 不从环境变量读取任何交易所凭据。密钥进入进程的唯一途径是解密一个 vault
文件，因此无论是进程列表还是被继承的环境，都泄漏不出密钥。

配置阶段是唯一可能由环境变量携带机密的地方：`vault put --api-secret-env` 会从指定的变量读取，作为 `--api-secret-stdin` 的替代。**优先用 stdin。**两者都能让机密不出现在命令行上，但环境变量会被这个 shell 随后启动的任何进程继承。

## 在 Docker 中运行

镜像要求把上述两处宿主路径挂载进来，并在运行时提供 age 身份。`~/.arx` 需要以读写方式挂载
—— runner 需要持久化凭据轮换的结果。完整调用见[部署](/zh-Hans/operator-guide/deployment)；镜像的构建方式见[安装](/zh-Hans/getting-started/installation) —— 目前这也是获得镜像的唯一途径。
