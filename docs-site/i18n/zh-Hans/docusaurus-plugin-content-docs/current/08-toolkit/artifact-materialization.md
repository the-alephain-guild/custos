---
title: "产物物化"
sidebar_position: 3
---

签名发布路径下载精确产物字节，完成验证后激活本地不可变目录，再导入代码。

## Registry 访问

OCI blob 按摘要拉取：

```text
https://{registry}/v2/{repository}/blobs/sha256:{digest}
```

Registry 必须在本地允许范围内，凭据按允许的 registry 绑定。客户端仅拉取，使用限定范围的 bearer 认证，限制响应大小，并按请求摘要验证收到的字节。

| 输入 | 用途 |
|---|---|
| `--artifact-registry` | 允许的 registry，默认 `ghcr.io` |
| `--artifact-registry-username` | 私有 registry 用户名 |
| `CUSTOS_ARTIFACT_REGISTRY_TOKEN` | Registry token，不放入命令参数 |

## 本地阶段

```text
pull -> quarantine -> verify and extract -> activate -> import
```

缓存、隔离和激活目录分别通过 CLI 参数配置。验证使用独立配置的发布策略和 Sigstore 根。安全解包与原子激活均在导入前完成；加载器检查模块来源，拒绝来自其他 activation 的缓存模块。

持久化重启恢复所需的激活与运行状态。缓存不能替代授权或已接受的目标状态记录。

## 开发输入

签名通道的 `DevelopmentSourceRefV1` 是显式、按内容寻址的 sandbox 专用输入，使用 `--development-artifact-root`。它不能用于 testnet/live，也不能晋升为生产发布。

离线通道另行在 sandbox/testnet 加载操作者挂载的目录。目录 hash 记录本地内容，不提供签名发布保障。详见[离线 testnet](/operator-guide/offline-testnet)和[产物签名](/toolkit/artifact-signing)。
