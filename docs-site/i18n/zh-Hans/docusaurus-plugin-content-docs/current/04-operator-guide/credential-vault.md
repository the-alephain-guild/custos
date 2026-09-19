---
title: "凭据金库操作"
sidebar_position: 2
---

Custos 使用独立的 sops+age 加密文件保存凭据。机器签名密钥与交易所密钥保留在 runner 宿主机上。

| 路径 | 内容 | 创建入口 |
|---|---|---|
| `~/.arx/vault/runner-machine.enc` | 机器凭据与 Ed25519 私钥 | 注册、轮换或独立身份创建 |
| `~/.arx/vault/<key-id>.enc` | 一份交易所凭据 | `vault put` |

目录权限使用 `0700`，文件使用 `0600`，通过 `SOPS_AGE_KEY_FILE` 指定 age identity。不同凭据路径的运行时会拒绝或报告不安全权限；不要仅依赖警告保护目录。

## 添加与验证

将 `KEY_ID`、`TENANT_ID`、`API_KEY` 和 `SCOPE_DIGEST` 设为目标凭据绑定值，从安全的本地来源通过 stdin 输入秘密：

```bash
uv run arx-runner vault put \
  --key-id "$KEY_ID" --tenant-id "$TENANT_ID" --api-key "$API_KEY" \
  --api-secret-stdin --scope-digest "$SCOPE_DIGEST" \
  --permission-scope trade_no_withdraw
uv run arx-runner vault verify --key-id "$KEY_ID" --tenant-id "$TENANT_ID"
uv run arx-runner vault list
```

`--age-recipient` 默认读取 `SOPS_AGE_RECIPIENT`。自定义目录时统一添加 `--vault-dir`。Key id 必须匹配 `^[a-zA-Z0-9_-]{1,64}$`。

CLI 和运行时使用相同的 JSON 解密命令，包含 `--input-type json --output-type json`；`.enc` 是文件命名约定，不是 sops 格式。`vault verify` 检查该路径和本地载荷/范围，不会向交易所 API 验证权限。

## 范围与通道差异

唯一接受的权限声明为 `trade_no_withdraw`。交易所上的实际密钥也必须禁用提现；本地声明不能改变交易所权限。

签名通道要求金库中的 scope digest 匹配部署绑定的范围。离线 spec 通过 `provenance_ref.credential_id` 指定凭据，运行时从该 id 派生宿主凭据范围。不要给离线 spec 添加 `credential_scope`，schema 会拒绝它。

两条通道都会解析真实本地金库条目，sandbox 模拟也一样。独立教程中的演示值仅用于不交易的 fixture。

## 轮换与恢复

已注册机器凭据使用 `credential rotate --reason`，远端接受后才写入替换内容。独立身份没有远端认证或轮换端点；替换时使用不同本地状态根，并保留恢复所需状态。

更换交易所条目时，主动通过 `vault put` 写入并验证，再协调部署的范围绑定。不要把解密载荷输出到日志或写入部署 spec。详见[应急恢复](/operator-guide/emergency-playbook)。
