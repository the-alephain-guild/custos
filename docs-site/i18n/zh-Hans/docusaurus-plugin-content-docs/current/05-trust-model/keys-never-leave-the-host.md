---
title: "密钥不出本机"
sidebar_position: 2
---

# 密钥不出本机

你的交易所凭证在你的机器上加密，在 runner 进程内解密，用于给交易所请求签名。解密它们
的那把密钥同样在你的机器上，从不被传输。

这就是 Custos 开源的原因。我们要求你把 API key 放到一个守护进程上；对"我凭什么信任它"
这个问题，唯一诚实的回答是：你可以自己读它拿这些 key 干了什么。

## 边界在哪

这条保证约束的是 **I/O**，不是内存。

真实的交易所客户端必须在进程内存里持有 key 才能给请求签名 —— 任何与交易所通信的客户端
都绕不开这一点，假装能绕开只是表演。这条保证覆盖的是 key 可能离开的每一条路：日志、
发布出去的消息、HTTP body、进程参数、被子进程继承的环境变量，以及任何明文落盘。

## 靠什么守住

### 存储

```
~/.arx/vault/
├── runner-machine.enc       # Ed25519 签名密钥 + 不透明机器凭据
└── <key-id>.enc             # 每个交易所凭证一个文件
```

一个凭证一个文件，各自是独立的 sops+age 文档。解密用的 age 身份通过
`SOPS_AGE_KEY_FILE` 定位，从不离开本机。目录模式 `0700`，文件 `0600`；更宽松的权限
runner 会告警。

同目录的 `runner.toml` 只保存公开绑定元数据 —— 凭据 id、版本、有效期、key id 与 vault
引用。没有明文。

### 写入

`arx-runner vault put` 通过 **stdin** 把 secret 递给 `sops`，绝不作为参数。放进 argv
的 secret 会出现在 shell 历史里，也会出现在这台机器上任何人的进程列表里。

实现见 `src/custos/cli/subcommands/vault.py`。 <!-- disclosure-ok: auditable source location -->

### 读取

`src/custos/core/per_key_vault.py` 里的 `PerKeyVault` shell out 调 sops，显式传
`--input-type json --output-type json`。解密 argv 在
`sops_json_decrypt_command()` 单点构造，因此 CLI 与运行时不会漂移成两种调用方式。
<!-- disclosure-ok: auditable source location -->

两个金库类都继承 `_BaseVault`，它在每次读取时强制两条 invariant：

- `_verify_permission_scope` 拒绝任何 scope 不是 `trade_no_withdraw` 的凭证；
- `_emit_decrypt_audit` 发出 `CredentialDecrypted` 事件，只携带凭据 id。

机器身份存放在 `MachineCredentialVault`
（`src/custos/core/machine_credential_vault.py`），它把 Ed25519 私钥与不透明机器凭据
一起加密在同一个文件里。enroll 与 rotate 是仅有的写路径。
<!-- disclosure-ok: auditable source location -->

## 一个身份，一处存放

enrollment 证明、传输认证与事实签名，用的都是同一个加密文件里的同一把签名密钥。

刻意不存在第二份明文密钥文件。多一份拷贝就是多一处可能泄漏、也多一处轮换时会被忘记的
地方。

## 如何验证

```bash
# 日志调用里没有凭证材料
grep -rnE 'log\.(info|debug|warning).*api[_-]?key' src/ tests/

# 出站调用里没有凭证材料
grep -rnE 'publish.*password|send.*secret' src/
```

在干净的树上两条都没有输出。

值得一读的测试是 `tests/test_credential_lifecycle.py`。它按真实部署的方式构造引擎对象
图，然后遍历它并断言从中触达不到任何凭证 —— 这能抓住"没有被记录、但被悄悄留存在某处、
日后可能被序列化出去"的情形。
<!-- disclosure-ok: auditable source location -->

`tests/test_per_key_vault.py` 覆盖解密路径与 scope invariant。
<!-- disclosure-ok: auditable source location -->

完整流程见[审计清单](./audit-checklist)。

## 这条保证不覆盖什么

Custos 无法阻止一份**本身就带提币权限**的凭证 —— 它会拒绝存储，但如果你在别处授予了该
权限，那超出 runner 的范围。同样，如果主机本身能被你不信任的人读取，它也帮不上忙：加密
文件与 age 身份都在那台机器上，而 `0600` 只有在账户是你的时候才有意义。
