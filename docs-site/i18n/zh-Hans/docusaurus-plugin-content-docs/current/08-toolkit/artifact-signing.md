---
title: "产物签名与验证"
sidebar_position: 2
---

签名产物路径在导入策略代码前验证精确发布字节，检查独立 Sigstore bundle 中针对产物成员签名的 in-toto statement。

## 验证条件

| 边界 | 必要检查 |
|---|---|
| 输入 | 稳定普通文件、有效 bundle、无重复 JSON 键 |
| Subject | 包含所需成员摘要，无重复 subject |
| 身份 | 被接受的 issuer、workflow identity 和源码仓库 |
| 签名 | 证书链/有效期、SCT、DSSE PAE 与签名 |
| 透明度 | Rekor entry/body/SET、包含证明与 checkpoint |
| 激活 | 安全解包、原子激活、模块来源位于激活目录内 |

信任来自独立签名的本地发布策略与可信根。产物元数据不能选择二者，策略在使用前先验证。

| CLI 参数 | 输入 |
|---|---|
| `--artifact-release-policy-envelope` | 声明可接受身份与限额的签名策略 |
| `--artifact-release-policy-key-id` | 预期 authority key id |
| `--artifact-release-policy-public-key` | Authority 验证公钥 |
| `--artifact-sigstore-trusted-root` | Sigstore 信任根 |

`release-policy issue` 配置步骤见[部署指南](/operator-guide/deployment)。本地生成的开发 authority 不构成生产批准。

## 失败处理

验证和解包均在导入前完成。加载器还会拒绝从其他 activation 缓存的模块。生产路径没有跳过参数、外部 shell 验证器，也不会仅凭 bundle 结构合理而接受。

产物验证属于签名部署路径。当前尚未开放生产使用，支持范围见[发布状态](/release-governance/release-status)。

离线挂载策略是独立的 sandbox/testnet 流程，不具有上述签名发布保障，也不能产生晋升证据。
