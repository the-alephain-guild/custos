---
title: "密钥保留在宿主机"
sidebar_position: 2
---

交易所秘密、机器签名密钥和 age identity 保存在 runner 宿主机上，不通过遥测发送给 ARX，也不写入日志。本地交易客户端按交易所认证协议使用凭据。

## 存储与解密

机器材料位于 `runner-machine.enc`，交易所凭据各使用一份 `<key-id>.enc` 文件。`runner.toml` 只含公开绑定元数据。文件权限为 `0600`，私有目录为 `0700`。

Custos 调用本地 sops 加解密，秘密输入通过 stdin 传递，并为 `.enc` 文件显式指定 JSON 格式。解密材料由本地 runner/交易客户端使用。这里约束的是宿主边界；正常客户端在认证期间必须在内存中持有秘密材料。

`enroll`、凭据轮换和 `identity standalone` 都可写入机器材料。独立身份没有远端认证，不能授权签名通道操作。

## 权限范围

金库在写入/解密边界只接受 `trade_no_withdraw` 声明。操作者还需在交易所实际禁用该密钥的提现权限。本地校验不会查询交易所来证明权限设置。

使用 `vault verify` 验证 runner 的真实解密与本地校验路径，详见[金库操作](/operator-guide/credential-vault)。

## 核查

`tests/test_credential_lifecycle.py` 检查日志/对象路径中的凭据暴露，`tests/test_per_key_vault.py` 覆盖本地金库行为。源码搜索可帮助定位外发和日志代码，但不是完整安全证明。

保护宿主访问与备份：同时取得加密金库和 age identity 即可解密。文件权限不能防止拥有该账户权限的攻击者访问。
