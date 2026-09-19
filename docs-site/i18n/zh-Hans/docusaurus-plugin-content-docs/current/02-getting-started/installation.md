---
title: "安装"
sidebar_position: 1
---

开发环境可从源码安装，也可构建本地容器。CLI 命令为 `arx-runner`。

## 环境要求

| 组件 | 要求 |
|---|---|
| 基础 runner | Python 3.11 或更高版本 |
| Nautilus 包 | Python 3.12（`>=3.12,<3.13`） |
| 依赖管理 | 使用 `uv` 和仓库内的 lock 文件 |
| 凭据加密 | `sops` 与 `age` |
| 容器流程 | Docker 与 Compose v2 |

Nautilus 运行时支持 macOS arm64、Linux arm64 和 Linux x86_64 上的 Python 3.12。Linux 环境需兼容 `manylinux_2_39`。通过 `make install-nt` 安装运行时，使依赖与 runner 匹配。

## 源码安装

```bash
git clone https://github.com/the-alephain-guild/custos.git
cd custos
make install
make install-nt
uv run arx-runner --help
```

`make install-nt` 添加 Nautilus 运行时，要求 Python 3.12。只审计基础 runner 时可省略。本指南使用仓库中的 `uv run arx-runner`；激活虚拟环境后也可直接使用 `arx-runner`。

```bash
make verify
make toolkit-typecheck
```

`make verify` 检查格式、lint、基础测试和仓库权威约束。基础测试可能跳过依赖 Nautilus 的用例。评估引擎行为时应运行 Nautilus 验证目标；文档构建不会验证执行功能。

## 本地容器

```bash
make verify-local-v030
```

该命令构建 `custos-runner:v0.3.0`，写入源码 revision 标签，检查镜像运行契约并输出 image id/revision。它不覆盖完整签名部署往返，也不证明生产就绪。修改后的派生镜像需要单独验证。

支持范围与当前限制见[发布状态](/release-governance/release-status)。本地构建成功不代表生产就绪。

## 下一步

- 不连接 ARX：[独立 sandbox](/getting-started/standalone-sandbox)。
- 连接 ARX：先[注册](/getting-started/enrollment)，再运行[签名 sandbox](/getting-started/first-sandbox-run)。
- 完整命令：[CLI 参考](/reference/cli)。
