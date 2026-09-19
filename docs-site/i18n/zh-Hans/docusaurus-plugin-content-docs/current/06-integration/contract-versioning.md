---
title: "契约版本"
sidebar_position: 4
---

V1 是当前首个生产契约。协调后的变更在该契约内更新，不维护前代 parser 或兼容别名。

## 协调变更

| 变更 | 协调要求 |
|---|---|
| 新增可选字段 | 严格 schema 的消费者需先更新，生产者再发送新字段 |
| 新增必填字段 | 同步更新生产者与消费者 |
| 删除或重命名字段 | 同步更新解析、生成、fixture 和消费者 |

持续演进的源码、schema、golden 文件和内部契约使用 Git commit/tag、review 与 CI 管理。有意改变行为时同步相关测试和 fixture。已有验收收据描述其记录的 revision，源码修改不构成重写历史记录的授权。新的验收应标明新 revision 与验证范围。

不可变发布产物仍按内容寻址，需要验证精确发布字节。这不等于每次源码编辑都要重新签发历史证据。

## 未来 V2

新的 wire 版本需要公开迁移窗口，并与受影响的生产消费者协调。当前支持范围见[发布状态](/release-governance/release-status)。

包 SemVer 与 wire 版本独立，包版本变化不意味着新的 wire 版本。公开兼容性政策见[SemVer 与 LTS](/release-governance/semver-lts)。
