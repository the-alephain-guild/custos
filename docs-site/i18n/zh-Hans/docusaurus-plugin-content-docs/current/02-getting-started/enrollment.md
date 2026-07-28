---
title: "注册 (Enrollment)"
sidebar_position: 2
---


# 注册 (Enrollment)

`arx-runner enroll` 是创建 runner 机器主体的**唯一**支持路径. 不存在 NATS 注册路径、
本地 unsigned 引导 token、手写 `runner.toml`、默认 tenant, 或明文 RunnerFact key fallback.

## 归属

- **ARX** 拥有 enrollment token 状态、一次性消费、Runner 机器凭据、过期、
  版本、轮换、撤销、不可变公钥证据以及健康投影.
- **ARX** 暴露公开 typed URL 并施加身份 / tenant / RBAC 策略. 它不持久化或重建 Runner
  的业务状态.
- **custos** 生成并保管 Ed25519 私钥,执行所有权证明 (PoP),存储返回的不透明机器凭据,
  在权威不可用时**失败关闭** (fail closed).

## Enrollment v2

1. Operator 从授权的ARX获取一次性 enrollment token.
2. custos 在内存中生成 Ed25519 密钥对和一个新鲜的挑战 nonce.
3. custos 对规范化 `arx.runner.enrollment.pop.v1` 证明签名. 证明绑定 token digest、
   声称的 tenant、Runner UUID、nonce、机器 key ID 以及公钥 digest.
4. custos 把一次性 token、公钥、nonce、key ID 和签名发给 ARX `POST /api/v1/enrollments`.
   **私钥永不外发**.
5. ARX 验证 token 权威与证明,一次性消费该 token,持久化不可变公钥证据,并签发
   带 tenant 的不透明 `rkc1` 凭据 (含 `credential_id`、版本、过期).
6. custos 用 sops+age 一起加密凭据与私钥. 只有非敏感的绑定元数据写入 `runner.toml`.

规范化证明是 newline 分隔的 UTF-8, 严格按此顺序:

```text
arx.runner.enrollment.pop.v1
tenant_id=<tenant>
runner_id=<uuid>
challenge_nonce=<uuid>
machine_key_id=<ed25519-key-id>
public_key_sha256=<lowercase-sha256>
enrollment_token_sha256=<lowercase-sha256>
```

## 本地权威文件

`~/.arx/vault/runner-machine.enc` 是 sops+age JSON 文档, 内含不透明机器凭据与 Ed25519
私钥. 必须为权限 `0600`;父目录与 age identity 目录必须为 `0700`. 运行时解密要求
`SOPS_AGE_KEY_FILE` 环境变量.

`~/.arx/runner.toml` **不含任何凭据或私钥**. 只记录以下字段:

- `tenant_id`
- `runner_id`
- `backend_url`
- `credential_id`
- `credential_version`
- `credential_valid_until`
- `machine_key_id`
- `machine_vault_path`
- `enrolled_at`

这些字段与解密后 vault 的任何不一致都是启动错误.

## Operator 流程

```bash
mkdir -p "$HOME/.arx/vault" "$HOME/.arx/state"
chmod 700 "$HOME/.arx" "$HOME/.arx/vault" "$HOME/.arx/state"
age-keygen -o "$HOME/.arx/age.key"
chmod 600 "$HOME/.arx/age.key"

export SOPS_AGE_KEY_FILE="$HOME/.arx/age.key"
export SOPS_AGE_RECIPIENT='age1...'

arx-runner enroll \
  --token '<one-time-token>' \
  --backend https://arx.internal:8000 \
  --tenant-id acme \
  --runner-id 018f8b5f-6f7d-7e23-8c31-bd34ab9d0d41

arx-runner credential verify
arx-runner onboard --manifest runner-capability.json
arx-runner start --nats-url nats://arx.internal:4222
```

HTTP 仅在本地回环开发中被接受. **重定向不被跟随**, 因为重定向 enrollment token 或
机器凭据会跨越预定的信任边界.

## 轮换与撤销

`arx-runner credential rotate` 生成一对新密钥,并用旧密钥签名一个 nonce 绑定的证明
把新公钥送去. 权威返回新的不透明凭据、递增的版本、过期以及新 key 绑定. custos 只在
接受响应后**原子替换**加密 vault 与公开元数据.

`arx-runner credential revoke` 发送一个由当前密钥签名的 nonce 绑定证明. 权威确认
`state=revoked` 后, custos 立即删除加密的机器 vault 与 `runner.toml`;执行循环
**无法**以已撤销的主体启动.

## 启动与就绪

在连接 NATS 或构造执行 host 之前, 启动要求:

- 加密的机器 vault 与 age identity 存在;
- 一个未过期的 `rkc1` 凭据;
- tenant、Runner、凭据 ID / 版本 / 过期以及 key-ID 绑定完全一致;
- 服务端验证凭据仍然有效;
- 一份经校验的、绑定同一公钥的 Runner capability 收据.

就绪 (readiness) 文件只重复公开凭据元数据与其过期. `arx-runner health` 对缺失、
过期、已撤销或权威不一致的情况返回非零. 云端 outage **不会**停止一个已在跑的本地引擎,
但一个新进程**不会**从不可验证的权威启动.

## 迁移顺序

ARX migration `0024` 必须先落地并被填充, 然后 ARX migration `0067` 才能
移除源表. 顺序是: 目标 migration → 语义抬升与退休 permit → 源表 drop. **永远不要**
先跑 `0067`.
