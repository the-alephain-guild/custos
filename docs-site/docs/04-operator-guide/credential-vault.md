---
title: "Credential Vault Operations"
sidebar_position: 2
---


# Credential Vault Operations

> Custos 六件套之一。源码：`src/custos/core/credential_vault.py`。**Key 本地金库承重墙**
> —— 生态 non-custodial 兑现载体核心。

## 模块职责

`credential_vault` 是 Custos 持有 operator 交易所凭证和 Runner machine principal 的本地金库。它把加密的凭证在
runner 本地解密成交易所 API key，交给 `nautilus_host` 下单——**KEK（age 私钥）永不
出主机，云端产品面 schema 永不持有 key**。这是「Key 与策略逻辑只在 runner 本地」这条保证的工程兑现点，也是 custos 必须开源的根本原因：operator 能审计这段代码，才
敢把 key 交给 daemon。

0.2.0+ 实现（clean-break，CEO 2026-07-10 directive）：

- **`MachineCredentialVault`（生产身份根）** —
  `src/custos/core/machine_credential_vault.py`：单一
  `~/.arx/vault/runner-machine.enc` 同时加密 Ed25519 私钥与 tenant-bearing opaque
  `rkc1` credential。`runner.toml` 只保存 credential ID/version/expiry/key ID 与 vault
  引用，不含 plaintext。enroll/rotate 是唯一写路径；startup/onboard/RunnerFact 是读路径。

- **`PerKeyVault`（生产 runtime reader）** — `src/custos/core/per_key_vault.py`：
  每个凭证独立一个 `~/.arx/vault/<key-id>.enc` 文件（sops+age 加密），reconciler
  runtime 通过 `sops --decrypt --input-type json --output-type json` shell out 读取，
  `SOPS_AGE_KEY_FILE` env 定位私钥。显式 type 参数避免 sops 3.13+ 把 `.enc` 后缀误判为
  binary store。
  继承 `_BaseVault` 保留 `_verify_permission_scope` + `_emit_decrypt_audit` 两条
  invariant，**永不日志 plaintext**。
- **`arx-runner vault put`（写入 CLI）** — `src/custos/cli/subcommands/vault.py`：
  一次一条 credential 写入 `.enc` 文件（`subprocess.run(input=...)` 直传 stdin，
  无 shell buffer 持 plaintext），emit `CredentialEncrypted` 审计事件。
- **`arx-runner vault verify` / `list`**：verify 独立跑一次 decrypt 路径 + scope
  invariant 复检；list 扫目录列 key-ids 并对 mode & 0o077 的 `.enc` 打 stderr 警告。
- **`CredentialVault`（Mock）**：test/dev harness 保留，返回占位凭证 dict + emit
  审计事件，**永不接入 runtime**（`_daemon._build_vault` unconditional
  `PerKeyVault`）。
- **`SopsAgeVault`（旧多 credential JSON 类）已删除**：0.2.0 breaking change。
  旧用户手工跑 `sops --decrypt --input-type json --output-type json <老文件>` 后逐条
  `arx-runner vault put` 迁移。
- **未来**：Hashicorp Vault provider（team tier）——Vault token 也只在 runner。

### JSON format contract

`vault put`、public `vault verify` 与 runtime `PerKeyVault.decrypt()` 三条路径共享同一
JSON format contract：encrypt/decrypt 一律显式传入 `--input-type json --output-type json`。
decrypt argv 由 `per_key_vault.sops_json_decrypt_command()` 单点构造，CLI 与 runtime 不得
各自复制 flags。`<key-id>.enc` 只是稳定的 storage naming contract，**不表示 SOPS binary
format**，也不得作为格式自动推断的输入。

`arx-runner vault verify` 是 operator acceptance surface：它同时验证真实 SOPS decrypt、
JSON payload、文件 mode 与 `trade_no_withdraw` permission scope。手工调用底层 `sops` 只能
作为诊断补充，不能替代 public CLI 的 put → verify roundtrip，也不能作为发布 gate 的唯一
证据。

## Removed in 0.2.0

- `SopsAgeVault(sops_file=..., age_key_file=...)` — 多 credential in one JSON 文件
  的旧模型。CEO clean-break directive (2026-07-10)：**无 fallback read path，无
  自动迁移命令**。旧用户升级路径：手工
  `sops --decrypt --input-type json --output-type json` 老 JSON → 逐个 `arx-runner vault put`
  建 per-key `.enc`。理由：旧的单文件 JSON 多凭证模型存在双写竞态，
  按 key 拆分文件后不再有该竞态。

## 关键接口

> **对外暴露口径**：本模块**绝不**对任何外部方暴露 key
> 或解密接口；`decrypt` 只被本地 `DeploymentReconciler` 调用。runner 出网只有遥测 +
> 状态，plaintext 永不上 NATS / HTTP。*This module's API surface is consumed
> exclusively by the arx coordination layer (audit signals only); no direct external
> client access, and credentials never leave the host.*

| 符号 | 签名 | 说明 |
|------|------|------|
| `CredentialVaultProtocol` | `decrypt(credential_id: str) -> dict` | 金库接口；实现必须在每次成功 decrypt 时 emit 审计事件 + 校验 `permission_scope` 非提币 |
| `_BaseVault` | `_verify_permission_scope` + `_emit_decrypt_audit` | 共享 invariant，`CredentialVault` / `PerKeyVault` 都继承 |
| `CredentialVault` | `decrypt(credential_id) -> dict` | Mock 金库（test/dev；runtime 不接入） |
| `PerKeyVault` | `__init__(*, vault_dir, tenant_id, initiator)` + `decrypt(...)` | 生产 runtime reader，读 `~/.arx/vault/<key-id>.enc` |
| `AuditEvent` | `enum`（`CREDENTIAL_DECRYPTED = "CredentialDecrypted"`, `CREDENTIAL_ENCRYPTED = "CredentialEncrypted"`） | 闭枚举，防 rename 静默破坏审计 writer 的 pattern match |

`_verify_permission_scope` 拒收 `permission_scope != "trade_no_withdraw"` 的凭证；
`_emit_decrypt_audit` 发 `CredentialDecrypted` 审计事件（只带 `credential_id` 引用，
**plaintext 永不进审计日志**）。

## Permission scope

`trade_no_withdraw` 是 custos 0.2.0+（包括 0.3.x）唯一合法的 credential permission scope。
`arx-runner vault put` 通过显式的
`--permission-scope {trade_no_withdraw}` flag 接收该值，并在省略 flag 时使用相同默认值；
写入的 encrypted payload 和 `CredentialEncrypted` audit event 都记录这一非敏感 metadata。
decrypt 时 `_verify_permission_scope` 再次执行同一安全边界，形成写入端与读取端的双层防御。

新增任何 scope 都是公开 CLI 与跨系统权限契约扩展，必须发布 custos minor version，并同步
更新 arx 的 producer/authorization contract、两侧 schema 与契约测试。在这些更新完成前，
不得通过绕过 argparse choices 或修改 encrypted payload 的方式引入新值。

`DeploymentSpec.provenance_ref.credential_id` 是 `~/.arx/vault/<key-id>.enc` 的文件名来源，
normative consumer model 因此要求它匹配 `^[a-zA-Z0-9_-]{1,64}$`。这与 vault CLI 的
safe-ID 规则一致，保证来自 NATS 的 desired state 不能通过路径分隔符、点号、控制字符或
非 ASCII 文本逃逸 per-key Vault 目录。

## 红线契约

- **KEK 不出本地**：age 私钥 / Vault token 永不离开 runner 主机；云端 schema 永不
  持有 key。
- **机器私钥单一来源**：enrollment PoP、machine HTTP/NATS auth、RunnerFact 与
  `RunnerRuntimeLogFact.v1` 复用同一 Ed25519 identity；禁止另建明文
  `runner-fact-key.json`。
- **无 unsigned bootstrap**：缺失、过期、revoked 或 binding mismatch 时 startup 与
  readiness fail closed；不存在 sandbox/testnet 手工 `runner.toml` 例外。
- **`permission_scope = trade_no_withdraw` 强制**：金库拒收任何允许提币的凭证
  （`_verify_permission_scope`），从 key 权限层堵死资金外流。
- **每次 decrypt 必发审计事件**：`CredentialDecrypted` 审计事件供下游 audit writer
  构建不可变链（audit 三件套）；「对账不静默」在凭证层的体现。
- **plaintext 零泄露**：不日志 plaintext、不写 NATS plaintext、不写 HTTP plaintext；
  `age_key_file` 默认 `0600`，sops stderr 不含 plaintext（sops 设计如此）。

## 相关 gate

| gate | 与本模块的关系 | 触发时机 |
|------|----------------|----------|
| **G7-legal**（辖区 pre-flight / KYC / 豁免链） | 持有他人资金对应凭证前需过 KYC + 辖区豁免链；非直系亲属 non-founder 出资人扩围前须补 licensed counsel 意见 | 出资人 CapitalContribution 入库前 / 扩大出资人范围时 |
| **G-SoD**（高敏感动作双人审批） | 凭证的新增 / 轮换 / 提币 scope 变更属承重动作，approver ≠ applicant | 云端 arx 侧凭证生命周期操作时 |

## 未来演化路线

- **短期**：Hashicorp Vault provider（team tier），统一 KEK 管理但 token 仍只在 runner。
- **中期**：凭证轮换（rotation）+ 短时租约（lease）机制，减小 key 静态暴露面。
- **长期**：HSM / 硬件签名器集成，把 KEK 从软件金库进一步下沉到硬件不可导出边界，
  强化 agentic capital 底座的信任叙事（vision 支柱四）。
