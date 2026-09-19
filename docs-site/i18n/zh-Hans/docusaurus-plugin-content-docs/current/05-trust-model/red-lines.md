---
title: "四项安全保障"
sidebar_position: 1
---

以下要求用于 runner 的实现和审查，具体运行范围见各章节。

| 要求 | 执行范围 |
|---|---|
| [密钥保留在本地](./keys-never-leave-the-host) | 本地加密；遥测、日志和上游消息不包含秘密密钥 |
| [Live 执行准入](./live-execution-is-gated) | 签名准入；当前 daemon 禁用 live；离线输入拒绝 live |
| [断线时持续保护](./safety-survives-disconnect) | 本地敞口/回撤检查独立于传输 |
| [金额使用十进制运算](./exact-money-arithmetic) | 类型化金额边界与规范 wire 表示 |

签名通道验证上游授权并报告签名观测。显式选择的离线通道使用本地未签名输入验证 sandbox/testnet，保留本地凭据和安全要求，但不声明签名发布或晋升权威。

这些要求不能消除宿主被入侵、交易所权限误配或策略亏损。应按[审计清单](./audit-checklist)确认测试范围及实际产物。
