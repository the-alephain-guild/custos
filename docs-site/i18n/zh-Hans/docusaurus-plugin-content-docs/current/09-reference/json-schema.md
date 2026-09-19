---
title: "JSON Schema 参考"
sidebar_position: 3
---

下表从当前 checkout 的 `docs/gateway-contract/v1/` 目录生成。

<!-- generated:schemas -->

| Schema 文件 |
|---|
| `development_source_ref_v1.schema.json` |
| `enrollment.schema.json` |
| `offline_deployment_spec.schema.json` |
| `runner_fact_batch_v1.schema.json` |
| `runtime_candidate_acceptance_v1.schema.json` |
| `runtime_candidate_promotion_receipt_v1.schema.json` |
| `strategy_artifact_pre_import_verification_receipt_v1.schema.json` |
| `strategy_artifact_ref_v1.schema.json` |
| `strategy_execution_context_v1.schema.json` |
| `strategy_manifest_v1.schema.json` |
| `toolkit_rc_authority_receipt_v1.schema.json` |
| `toolkit_rc_pending_receipt_v1.schema.json` |
| `toolkit_rc_receipt_manifest_v1.schema.json` |
| `toolkit_rc_t6d_pending_receipt_v1.schema.json` |

<!-- /generated:schemas -->

## 契约分类

| 分类 | 用途 |
|---|---|
| Enrollment | 本地注册材料 |
| RunnerFact batch | 集成方消费的签名观测批次 |
| 策略产物、manifest、context 和导入前收据 | 执行 ABI 与本地验证边界 |
| Development source | 签名通道中显式的 sandbox 开发材料 |
| Offline deployment spec | 操作者持有的未签名 sandbox/testnet 输入 |
| Toolkit/runtime 候选收据 | 候选发布、验收和晋升证据，不是部署指令 |

离线 schema 不替代正式签名 DeploymentSpec，其 validate/publish CLI 不能创建 ARX 签名指令。

## 校验

使用与生产者/消费者 revision 匹配的 schema。`custos://...` 标识仓库资产，不是在验证时访问的网络端点。声明严格字段集的对象会拒绝未知属性。schema 通过只证明结构，不验证签名、授权、部署资格或生产就绪。

校验离线 spec：

```bash
uv run arx-runner deployment validate --spec-file "$SPEC_FILE" --mode sandbox
```

通用 JSON Schema 检查应使用支持该 schema 声明 dialect 的 validator。额外约束由契约测试和权威检查覆盖：

```bash
make check-authority
```

独立签名的策略信号 envelope 由其契约字段和测试定义，不是 `runner_fact_batch_v1` 中新增的 kind。详见[消费参考](/integration/consuming-runner-fact)。
