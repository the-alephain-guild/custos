---
title: "安装"
sidebar_position: 1
---

# 安装

目前有两条受支持的获取途径：从源码安装，或在本地构建容器镜像。两者产出同一个
`arx-runner` 命令。

:::note 0.3.0 尚无已发布产物
0.3.0 的远端发布处于 deferred 状态。**没有**可 `pip install` 的 wheel，也**没有**可
`docker pull` 的镜像 —— 本版本的消费门是你自己构建并验证的镜像。下面的说明如实反映这一点，而不是描述一个并不存在的软件包。
:::

## 前置条件

| 要求 | 用于 |
|---|---|
| Python >= 3.11 | runner 本身 |
| Python >= 3.12 | 额外要求，当你使用 NautilusTrader 引擎时 |
| [`uv`](https://docs.astral.sh/uv/) | 本仓唯一受支持的 Python 包管理器 |
| [`sops`](https://github.com/getsops/sops) 与 [`age`](https://github.com/FiloSottile/age) | 凭据金库的加解密 |
| Docker with Compose v2 | 仅容器路径需要 |

`uv` 不是可选项。lock 文件被提交，以保证构建可复现 —— 这是 runner 可审计性的一部分；
`pip` 或 `poetry` 会解析出不同的依赖图。

## 从源码安装

```bash
git clone https://github.com/the-alephain-guild/custos.git
cd custos

make install        # 基础 + 开发 extras
make install-nt     # 额外装 NautilusTrader 引擎（需 Python 3.12+）
```

确认命令可用且代码树是绿的：

```bash
uv run arx-runner --help
make verify
```

`make verify` 跑格式化、lint 与基线测试套件。在配置任何东西之前先跑一次，能把日后的「我的环境不对」与「我的配置不对」区分开。

## 作为容器

```bash
make verify-local-v030
```

它构建 `custos-runner:v0.3.0`，打上当前 Git revision 标签，并针对构建出的镜像跑完整运行时契约与独立验收，成功时打印 image ID 与 revision。

**不要**在此镜像之上再写一个派生 Dockerfile 去加 NautilusTrader、sops 或 age。被门覆盖的产物是那个已验证镜像，它的派生物不是。

## 命令面

```bash
arx-runner enroll                  # 取得可证明的机器身份
arx-runner vault put|verify|list   # 管理交易所凭据
arx-runner credential              # 验证、轮换或吊销机器凭据
arx-runner publish-capability      # 发布下一个能力修订
arx-runner nats-transport          # 签发、轮换、吊销或验证传输授权
arx-runner start                   # 运行守护进程
arx-runner health                  # 检查就绪状态
```

每个子命令都支持 `--help`。请直接跑它而不是猜 —— 这些 flag 又长又大多是刻意设为必填的。

## 你还需要什么

装好 runner 并不等于有东西可跑。第一次部署之前你还需要三样东西，且它们**没有一样**是
runner 能自行签发的：

1. 来自 ARX 的**一次性注册令牌**；
2. 本机上的一份 **age 身份**，由你在本地生成；
3. 权限范围为 `trade_no_withdraw` 的**交易所 API 凭据**。

这种不对称正是设计要点 —— 见[信任模型](/zh-Hans/introduction/trust-model)。一个能自己造出授权的 runner，不值得把凭据交给它。

## 下一步

[注册](/zh-Hans/getting-started/enrollment) —— 给 runner 一个它能证明的身份。
