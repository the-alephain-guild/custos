---
title: "安全策略"
sidebar_position: 5
---

# 安全策略

**[SECURITY.md](https://github.com/the-alephain-guild/custos/blob/main/SECURITY.md)** 是权威策略，本页是它的摘要。

## 报告

通过 GitHub Security Advisories 私下报告：

**[报告漏洞](https://github.com/the-alephain-guild/custos/security/advisories/new)**

请**不要**为疑似漏洞开公开 issue。一份公开报告在告知我们的同时，也同时告知了每一位正在运行受影响版本的运维 —— 而在补丁存在之前，他们无法据以行动。

## 我们的承诺

| 承诺 | 时限 |
|---|---|
| 确认收到 | 72 小时 |
| 确认后发布修复 | 30 天，尽力而为 |
| 公开 advisory | 补丁发布后 24 小时内 |

安全修复会落到**每一条**活跃支持线，而不只是最新的那条。哪些线活跃见
[SemVer 与 LTS](/zh-Hans/release-governance/semver-lts)。

## 请包含什么

你观察到问题的版本、你做了什么、发生了什么、你预期是什么。有复现最好，但不是必需 ——
一段对「你认为哪条边界被跨越」的精确描述，比一个不完整的利用更有用。

## 边界在哪里

信任模型声明了四条保证，一份报告若点明它破坏的是哪一条，最便于处理：

| 保证 | 此处的发现意味着 |
|---|---|
| [密钥不出本机](/zh-Hans/trust-model/keys-never-leave-the-host) | 凭据或密钥材料进入了日志、消息或 HTTP body |
| [实盘执行始终受门控](/zh-Hans/trust-model/live-execution-is-gated) | 存在绕过准入抵达真实交易所的路径 |
| [失联不等于停止](/zh-Hans/trust-model/safety-survives-disconnect) | 本地防护在上游不可达时停止了 |
| [金额运算精确](/zh-Hans/trust-model/exact-money-arithmetic) | 有 float 进入了金额路径 |

## 若你是想审计而非报告

如果你想核实这些主张、而不是报告其中一条被破坏，[审计清单](/zh-Hans/trust-model/audit-checklist)会带你走同样的边界，并给出覆盖每一条的命令与测试。
