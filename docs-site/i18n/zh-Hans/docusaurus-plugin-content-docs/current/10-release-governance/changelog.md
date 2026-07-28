---
title: "变更日志"
sidebar_position: 2
---

# 变更日志

变更日志位于仓库中 —— 它紧挨着它所描述的代码，并在同一个 commit 里更新：

**[CHANGELOG.md](https://github.com/the-alephain-guild/custos/blob/main/CHANGELOG.md)**

它遵循 [Keep a Changelog](https://keepachangelog.com/) 与
[SemVer 与 LTS](/zh-Hans/release-governance/semver-lts) 里的版本契约。每一条都归入承载它的
那一级 bump，因此**条目出现在哪一节，就告诉你升级到它是否需要动作**。

## 怎么读一条记录

`### Removed` 与 `### Changed` 是会花你时间的两节。它们只出现在 MAJOR 版本里（或对于
已公告的移除，出现在 MINOR 里），且都指向
[升级路径](/zh-Hans/release-governance/upgrade-paths)中的迁移步骤。

`### Deprecated` 是提前预警。列在那里的东西至少保留到下一个 minor 版本，并在**每一次**
发布通告中重复，直到它真正被移除 —— 因此一次弃用不可能在「公告」与「移除」之间悄悄溜过去。

## 当前版本

**0.3.0**，发布于 2026-07-12。它的远端产物处于 deferred 状态：本版本没有已发布的 wheel，
也没有可拉取的镜像，消费门是本地构建并验证的镜像。见
[安装](/zh-Hans/getting-started/installation)。
