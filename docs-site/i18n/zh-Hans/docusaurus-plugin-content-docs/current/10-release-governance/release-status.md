---
title: "发布状态"
sidebar_position: 1
---

本页区分源码支持、候选版本发布和生产验收。以下快照于 2026-09-19 对照仓库记录核查，源码 revision 为 `c1d27024312210590bc0716fe1ddff592e5f8012`。

| 范围 | 登记状态 | 能证明什么 |
|---|---|---|
| Runner 源码包 | `0.3.0` | checkout 的包版本，不是稳定版发布公告 |
| Nautilus 2 契约交接 | 消费者交接完成 | 登记的消费者接受了协调后的契约 |
| Toolkit 候选版本 | `0.1.0rc7`，源码 `8bf45ac6b0f42018aae2a74ac9e743e41f9ca789` | 已登记候选版本，不代表生产执行 |
| Runtime 候选镜像 | 已发布并 attested，源码 `4afffb96b1a768fb34f66692d4bb7f96652aeccf` | 该 revision 的发布及精确镜像验证记录 |
| 本地 runtime 组合 | 已实现，并记录本地激活证据 | 本地组合及测试证据 |
| 已部署 runtime / 生产 | 验收仍开放 | `runtime_ready=false`、`production_ready=false` |
| Live 执行 | 当前 daemon 组合禁用 | 没有可供操作者开启实盘的参数 |

登记的 runtime 候选镜像为：

```text
ghcr.io/the-alephain-guild/custos@sha256:2e9081c14df31cac15112ba0a38100da94cb271a6bbaf7f9ad3c1096548c6753
```

这是历史候选坐标，不是当前部署推荐。本次文档更新未重新检查 registry 可用性。该镜像早于当前 Nautilus 2 checkout，发布记录不能证明当前 HEAD 已通过验收。

## 选择产物

开发当前源码时，按[安装指南](/getting-started/installation)执行，并记录 Git revision 和本地 image id。使用远端候选版本前，核对摘要、签名、源码 revision 和验收范围。候选版本发布不会启动稳定版本支持窗口，也不会关闭生产验收门。

历史验收记录应保留在原 revision 上。新 revision 需要新证据，不应为匹配当前源码而刷新旧收据。
