---
title: "运行日志与可观测性"
sidebar_position: 4
---

本地结构化 JSON 日志写入 stdout。签名通道中，显式构造的运行事件也可作为 `RunnerRuntimeLogFact.v1` 进入事实流。runner 不监听 stdout 来转发日志，也不向该流发送原始异常文本。离线操作使用本地日志和未签名状态。

## 运行日志事实

```json
{
  "kind": "RunnerRuntimeLogFact.v1",
  "event_id": "<deterministic uuidv5>",
  "occurred_at": "<RFC3339 UTC>",
  "level": "INFO",
  "component": "local_cap",
  "message": "...",
  "structured_fields": {},
  "correlation_id": "<uuid>",
  "causation_id": null
}
```

级别为 `DEBUG`、`INFO`、`WARN`、`ERROR`。Component 匹配 `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`。外层签名批次绑定租户、模式、runner、实例、spec/generation、能力、序号和签名。

## 脱敏与限制

脱敏器递归匿名化敏感字段名和可识别秘密值，包括 token、机器凭据、age/PEM 密钥及赋值形式片段。如果仍能识别秘密材料，整个事实会在 SQLite 持久化前被拒绝。不支持的对象、二进制浮点数与非有限值也被拒绝。

Message 限制为 4 KiB，structured fields 为 32 KiB，并限制嵌套、键数量及键长。超限事件拒绝处理，不截断。数值使用整数或规范十进制字符串。

## 身份与投递

运行日志 UUIDv5 身份包含流授权范围、correlation id 和脱敏后内容摘要。同一流中的相同脱敏事件保持幂等；不同租户/模式/runner/实例不共用身份。

持久化 outbox 发布后记录 PubAck 完成。重试保留批次身份；某个流失败时，本轮 drain 不再发送其后续批次。发布失败只记录结构化身份和异常类型，不把事件改成原始诊断文本重新发布。

订单生命周期日志可包括初始化、提交、拒绝、撤单和过期。策略信号或提交不代表成交。按实例和 client order id 关联，并单独核对执行结果。

runner 不提供日志查询 API 或可配置的历史日志服务。使用宿主工具收集 stdout，并按运维要求保存消费者证据。
