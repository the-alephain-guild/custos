---
title: "注册"
sidebar_position: 2
---

签名通道要求向 ARX 注册身份。无需 ARX 的本地 sandbox/testnet 使用[独立身份](/getting-started/standalone-sandbox)；独立身份不能启动签名通道。

## 前提

从 ARX 获取一次性注册 token、tenant id、runner UUID 和 backend URL。安装 `sops` 与 `age`，为每个 runner 使用独立状态目录。

```bash
umask 077
mkdir -p "$HOME/.arx/vault" "$HOME/.arx/state"
chmod 700 "$HOME/.arx" "$HOME/.arx/vault" "$HOME/.arx/state"
# Generate only if this runner has no age identity yet.
age-keygen -o "$HOME/.arx/age.key"
export SOPS_AGE_KEY_FILE="$HOME/.arx/age.key"
export SOPS_AGE_RECIPIENT="$(age-keygen -y "$SOPS_AGE_KEY_FILE")"
```

将 token 保存到权限为 `0600` 的文件，并将 `ENROLLMENT_TOKEN_FILE`、`ARX_BACKEND_URL`、`TENANT_ID` 和 `RUNNER_ID` 设为取得的值。不要通过命令参数传递 token。

```bash
uv run arx-runner enroll \
  --token-file "$ENROLLMENT_TOKEN_FILE" \
  --backend "$ARX_BACKEND_URL" \
  --tenant-id "$TENANT_ID" \
  --runner-id "$RUNNER_ID"
uv run arx-runner credential verify
```

注册成功后删除已消费的 token 文件。明文 HTTP 仅用于 loopback 开发，客户端不跟随重定向。

## 身份证明与本地文件

Custos 在本地生成 Ed25519 密钥对，签名证明绑定 token 摘要、租户、runner UUID、nonce、key id 和公钥摘要。发送到 `POST /api/v1/runner-enrollments` 的内容包括公开材料和证明，不包含私钥。

签名前像使用按以下顺序排列、以换行分隔的 UTF-8：

```text
crucible.runner.enrollment.pop.v1
tenant_id=<tenant>
runner_id=<uuid>
challenge_nonce=<uuid>
machine_key_id=<ed25519-key-id>
public_key_sha256=<lowercase-sha256>
enrollment_token_sha256=<lowercase-sha256>
```
<!-- disclosure-ok: exact enrollment signing domain required for verification -->

| 文件 | 内容 |
|---|---|
| `~/.arx/runner.toml` | 公开身份、backend、凭据有效期/版本及金库路径 |
| `~/.arx/vault/runner-machine.enc` | 加密的机器凭据和签名私钥 |
| `~/.arx/age.key` | 解密金库所需的本地 age identity |

文件权限为 `0600`，私有目录为 `0700`。启动时比较元数据与解密后的身份并检查有效期；签名通道还会向 ARX 验证授权，并要求绑定同一公钥的能力收据。详见[配置参考](/reference/configuration)。

## 轮换与撤销

```bash
uv run arx-runner credential rotate --reason "scheduled rotation"
uv run arx-runner credential revoke --reason "host decommissioned"
```

轮换使用旧密钥证明身份连续性，上游接受后才写入替换内容。撤销确认后移除本地机器金库和元数据。这些操作要求已注册身份；独立身份没有远端凭据生命周期。

注册本身不会配置传输或授权部署。下一步见[签名 sandbox](/getting-started/first-sandbox-run)。
