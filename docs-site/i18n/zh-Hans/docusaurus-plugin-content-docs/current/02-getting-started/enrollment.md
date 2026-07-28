---
title: "Enrollment"
sidebar_position: 2
---

# Enrollment

Enrollment 是 runner 取得一个"可自证"身份的过程。`arx-runner enroll` 是唯一受支持的
路径。不存在 NATS enrollment、本地未签名 bootstrap token、手写 `runner.toml`、默认
tenant，也不存在明文签名密钥的降级通路。

这份清单是刻意封闭的。上面每一项，都是一条"在权威没签发的情况下取得 runner 身份"的路。

## 各自负责什么

**ARX** 签发并拥有 enrollment token、恰好消费它一次，并拥有由此产生的机器凭据 —— 它的
有效期、版本、轮换、吊销与不可变公钥证据。它同时在端点上施加身份、tenant 与访问策略。

**Custos** 生成 Ed25519 密钥对、证明自己持有私钥、把返回的不透明凭据加密存储，并在权威
不可用时 fail closed。

私钥在本地生成，从不发送。ARX 从来看不到它 —— 这正是那份"持有证明"有意义的原因。

## 交换过程

1. 你从 ARX 取得一次性 enrollment token。
2. Custos 在内存中生成 Ed25519 密钥对和一个新的挑战 nonce。
3. Custos 签署一份 canonical 证明，绑定 token 摘要、声称的 tenant、runner UUID、nonce、
   机器 key id 与公钥摘要。
4. Custos 把 token、公钥、nonce、key id 与签名发往
   `POST /api/v1/runner-enrollments`。私钥留在你的机器上。
5. ARX 校验 token 与证明、消费 token 一次、持久化公开证据，并签发带 tenant 的不透明
   凭据，含 id、版本与有效期。
6. Custos 用 sops+age 把该凭据与私钥一起加密。只有非敏感的绑定元数据写入
   `runner.toml`。

证明是换行分隔的 UTF-8，且字段顺序严格如下 —— 顺序是契约的一部分，因为两边算不出同一份
canonical 形式时，产出的签名谁也验不了。

```text
crucible.runner.enrollment.pop.v1
tenant_id=<tenant>
runner_id=<uuid>
challenge_nonce=<uuid>
machine_key_id=<ed25519-key-id>
public_key_sha256=<lowercase-sha256>
enrollment_token_sha256=<lowercase-sha256>
```

## 落到磁盘上的东西

`~/.arx/vault/runner-machine.enc` 是一份 sops+age 文档，把不透明机器凭据与 Ed25519 私钥
放在一起。模式 `0600`；父目录与 age 身份目录为 `0700`。运行时解密需要
`SOPS_AGE_KEY_FILE`。

`~/.arx/runner.toml` 不含凭据、也不含密钥。它只记录 `tenant_id`、`runner_id`、
`backend_url`、`credential_id`、`credential_version`、`credential_valid_until`、
`machine_key_id`、`machine_vault_path` 与 `enrolled_at`。

这些字段与解密后的 vault 有任何不一致，都是启动错误，不是告警。字段参考见
[配置参考](/reference/configuration)。

## 实际执行

```bash
mkdir -p "$HOME/.arx/vault" "$HOME/.arx/state"
chmod 700 "$HOME/.arx" "$HOME/.arx/vault" "$HOME/.arx/state"
age-keygen -o "$HOME/.arx/age.key"
chmod 600 "$HOME/.arx/age.key"

export SOPS_AGE_KEY_FILE="$HOME/.arx/age.key"
export SOPS_AGE_RECIPIENT='age1...'

arx-runner enroll \
  --token '<一次性 token>' \
  --backend https://arx.internal:8000 \
  --tenant-id acme \
  --runner-id 018f8b5f-6f7d-7e23-8c31-bd34ab9d0d41

arx-runner credential verify
```

明文 HTTP 只在 loopback 开发场景被接受。重定向从不跟随 —— 把 enrollment token 或机器
凭据重定向出去，等于把它带过它本来要建立的那条信任边界。

## 轮换与吊销

```bash
arx-runner credential rotate
arx-runner credential revoke
```

**轮换**会生成新密钥对，并用**旧**密钥签署的 nonce 绑定证明把新公钥发出去 —— 身份的
连续性是被证明的，不是被声称的。Custos 只在收到接受响应之后，才原子替换加密 vault 与
公开元数据。轮换失败时，先前的凭据保持完好可用。

**吊销**用当前密钥发送 nonce 绑定证明。权威确认吊销状态后，Custos 立即删除加密 vault 与
`runner.toml`。执行循环无法用已吊销的主体启动，本地也没有把它复活的路径。

## 启动与就绪

在连接传输或构造执行 host 之前，启动要求：

- 加密机器 vault 与 age 身份；
- 一份未过期的凭据；
- tenant、runner、凭据 id、版本、有效期与 key-id 绑定逐项精确一致；
- 服务端确认该凭据仍然有效；
- 一份绑定到同一公钥的、已校验的能力回执。

就绪输出只重复公开的凭据元数据及其有效期。`arx-runner health` 在权威缺失、过期、被吊销
或不匹配时返回非零。

有一处不对称值得明说：中断不会停掉已经在跑的引擎，但新进程不会从"无法校验的权威"启动。
对已经被信任的东西保持连续性；这份连续性绝不外延到未经证明的东西上。
