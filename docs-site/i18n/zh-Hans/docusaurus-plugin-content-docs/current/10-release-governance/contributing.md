---
title: "参与贡献"
sidebar_position: 3
---

# 参与贡献

完整指南位于仓库中，紧挨着它所描述的代码与 hook：

**[CONTRIBUTING.md](https://github.com/the-alephain-guild/custos/blob/main/CONTRIBUTING.md)**

## 动手之前

有三件事值得先知道，因为它们决定了「一个可评审的改动」长什么样。

**四条保证不可协商。**密钥不出本机、实盘执行始终受门控、失联时安全防护依然生效、金额运算精确。一个**看起来**可能削弱其中之一的改动，需要在代码评审**之前**先做设计讨论，而不是在评审过程中。见[红线](/zh-Hans/trust-model/red-lines)。

**源码工件用英文。**注释、日志字符串、异常消息、标识符与 commit message。pre-commit hook
会拒绝新增的含 CJK 字符的行 —— 部署主机不能可靠渲染它们，而日志输出必须保持可 grep。

**测试先行。**每个行为改动都先落一个失败测试，再写最小实现。`make verify` 是 PR 必须通过的门，也正是 CI 跑的同一个 target。

## 有些文件不能随手改

源码树的一部分被权威资产索引**按字节固定**。改动其中之一 —— 哪怕只是修个注释或重排格式 ——
都会破坏证据链，而且失败表现为大小不匹配，看起来完全不像权限问题。先查：

```bash
grep -rho '"src/custos/[^"]*\.py"' docs/authority/ | sort -u
```

## 涉及安全的改动

若你的改动触及金库处理、网络出站或密钥派生，请在提 PR **之前**先开私有 advisory。见[安全策略](/zh-Hans/release-governance/security-policy)。
