---
title: "产物物化"
sidebar_position: 3
---

# 产物物化

策略产物如何从 registry 落到 runner 上，以及在**任何东西被导入之前**必须成立的条件。

:::note 本章取代了「registry 模式加载」
按 registry 名称加载策略的模式已经不存在了。那是一种由 runner 通过**可变坐标**解析策略的
模式 —— 也就意味着「究竟跑了哪份代码」无法仅凭产物本身回答。现在的做法是按 **digest**
拉取，因此坐标不可能在之后指向不同的字节。
:::

## 按 digest 拉取，绝不按 tag

blob 按精确的 `sha256` digest 获取：

```text
https://{registry}/v2/{repository}/blobs/sha256:{digest}
```

tag 是一个可以被重新指向的**名字**，digest 是**内容**本身。物化路径不接受任何 tag，
因此不存在「你批准的产物」与「你运行的产物」可以不同的时间窗。

## registry 必须在白名单上

runner 持有一组明确许可的 registry 主机名，统一转小写并按主机名模式校验。来自其他任何
地方的拉取在发出请求**之前**就被拒绝。

凭据按 registry 键入，且必须是该白名单的**子集** —— 你不能持有一个你无权拉取的 registry
的凭据。这个次序很重要：它让「这台 runner 能触达哪些 registry」可以从**配置**回答，
而不是取决于手头碰巧有哪些凭据。

配置方式：

| Flag | 默认 |
|---|---|
| `--artifact-registry` | `ghcr.io` |
| `--artifact-registry-username` | — |
| `CUSTOS_ARTIFACT_REGISTRY_TOKEN`（环境变量） | — |

token 从环境变量读取而非作为 flag 传入，因此不会出现在 `ps` 输出里。

## 只拉不推的传输

OCI 客户端刻意做得最小化，且**只拉不推**，使用限定作用域的 bearer 认证。不存在推送路径，
因此一台被攻破的 runner **无法发布**产物 —— 它至多只能运行失败。

每个响应都有大小上限，且收到的字节会与请求的 digest 作校验。哈希对不上的 blob 会被丢弃，
而不是被缓存下来重试。

## 字节流向哪里

```text
拉取 → 隔离区 → 验证 → 激活（不可变根）→ 导入
```

隔离区在前，激活是**原子**的。产物绝不会从它被下载到的位置直接导入，而一个物化到一半的
产物也无法被激活 —— 因为激活是最后一步，不是第一步的副作用。

目录可配置：

| Flag | 用途 |
|---|---|
| `--artifact-cache-dir` | 已下载的 blob |
| `--artifact-quarantine-dir` | 验证前的暂存 |
| `--artifact-activation-dir` | 不可变激活根 |

## 验证先于导入

签名与证明的验证在**任何** Python 模块被导入之前完成。随后 loader 会证明它导入的模块源自
激活根，并拒绝一个从**另一次**激活缓存下来的模块。

第二项检查是最常被跳过的。如果 import 系统端出一个它此前从别处缓存的模块，那么验证磁盘上
的字节什么也证明不了。

验证内容与它如何 fail closed，见[策略工具包](/zh-Hans/toolkit/overview)与
[产物签名](/zh-Hans/toolkit/artifact-signing)。

## 开发源

存在一条仅限 sandbox 的路径，用于在没有已发布产物的情况下迭代策略，由
`--development-artifact-root` 选择。它是一个**显式且不可提升**的联合成员 ——
不能用于 `testnet` 或 `live`，也不能被提升进去。

它存在的意义是：让「我需要快速测一个改动」永远不会变成削弱真实路径的理由。
