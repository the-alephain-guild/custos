---
title: "JSON Schema 参考"
sidebar_position: 3
---

# JSON Schema 参考

共 11 份 schema，全部位于仓库的 `docs/gateway-contract/v1/` 下。本章说明每一份的用途，以及这组 schema **不**覆盖什么。

每一份都是 `additionalProperties: false`。未知字段会被拒绝，而不是被忽略 —— 这一点在新增字段时的代价，见[契约版本管理](/zh-Hans/integration/contract-versioning)。

## 各份 schema

**注册（enrollment）**

| 文件 | 标题 |
|---|---|
| `enrollment.schema.json` | `EnrollmentPayload v1` |

**runner 对外发出的内容**

| 文件 | 标题 |
|---|---|
| `runner_fact_batch_v1.schema.json` | `Custos RunnerFactBatchV1` |

全部 20 个字段均为必填。这是整组 schema 中唯一由外部消费者订阅的一份；subject、签名前像与验证者检查清单见[消费 RunnerFact](/zh-Hans/integration/consuming-runner-fact)。

**策略产物边界**

| 文件 | 标题 |
|---|---|
| `strategy_artifact_ref_v1.schema.json` | `StrategyArtifactRefV1` |
| `strategy_manifest_v1.schema.json` | `StrategyManifestV1` |
| `strategy_artifact_pre_import_verification_receipt_v1.schema.json` | `StrategyArtifactPreImportVerificationReceiptV1` |
| `strategy_execution_context_v1.schema.json` | `StrategyExecutionContextV1` |
| `development_source_ref_v1.schema.json` | `DevelopmentSourceRefV1` |

那份回执写在 import **之前** —— 这正是它的意义：它在"还来得及拒绝"的时刻记录下验证了什么。执行上下文是策略真正拿到的东西，且是冻结的。`DevelopmentSourceRefV1` 仅限
sandbox 且显式不可提升 —— 它的存在是为了让本地开发有一条不会被误认成已发布产物的路径。

**Toolkit 候选发布回执**

`toolkit_rc_authority_receipt_v1`、`toolkit_rc_pending_receipt_v1`、
`toolkit_rc_receipt_manifest_v1` 与 `toolkit_rc_t6d_pending_receipt_v1`。

这些是证据资产，不是集成面。它们记录某个 toolkit 候选发布被钉到了什么上。运行时不消费它们，你也不需要产出它们。

## `$id` 刻意不是 URL

```text
custos://gateway-contract/v1/runner_fact_batch_v1.schema.json
```

验证器不会去拉取它，这是有意为之。在校验时从网络取回的 schema，是别人可以在"你跑测试"
与"你跑生产"之间改掉的 schema。请使用你 checkout 里的那一份。

## JSON Schema 表达不了的部分

`runner_fact_batch_v1.schema.json` 带有一个 `x-custos-invariants` 块，因为最要紧的那些性质无法用"形状"表达：

| 键 | 它固定了什么 |
|---|---|
| `subject` | 精确的 subject 模板 |
| `stream_identity_fields` | 标识一条流的那四个字段 |
| `signed_fencing_fields` | spec id、spec digest 与 generation —— 是来源信息，不是身份 |
| `sequence_rule` | `facts[i].seq == source_seq_start + i` |
| `generation_resets_sequence` | `false` |
| `signing_domain_base64` | 域字节，含结尾的 NUL |
| `signing_header_fields` | 18 个头部字段，**按顺序** |
| `payload_digest_formula` | `sha256(canonical_json(facts))` |
| `signing_preimage_formula` | `DOMAIN \|\| canonical_json(header)` |
| `canonicalization` | 编码、键序、数字形式 |

需要格外小心的是字段**顺序**。schema 校验会欣然接受一个你以不同顺序序列化的头部，然后签名验证失败 —— 而且没有任何信息指向"顺序"才是原因。如果你在实现验证者，请把
`signing_header_fields` 当作权威，把 schema 当作形状检查。

## 确认你拿到的是正确的字节

fact batch 的 schema 附带摘要 sidecar：

```bash
cd docs/gateway-contract/v1 && shasum -a 256 -c runner_fact_batch_v1.schema.json.sha256
```

其中若干份 schema 还被仓库的权威索引按 path、size 与 commit 记录在案。这正是它们之所以是**证据**而非**文档**的原因，也是为什么改一份 schema 是一次协调的重新签发，而不是一次编辑。

## 这里没有什么

**没有 DeploymentSpec 的 schema**，而且它的缺席由测试断言，不靠记忆维持。规范化的 spec
归上游所有；runner 持有的是仅在验证通过之后才派生出的本地视图。为那个视图发布 schema，等于邀请生产方照着 runner 的投影去构建，而不是照着已签名的原件。

`v2`、`v3`、`v4` 三个目录存在且为空。它们是占位符，不是路线图 —— 见[契约版本管理](/zh-Hans/integration/contract-versioning)。

## 校验一份文档

这些 schema 没有远程引用，因此校验是离线的：

```bash
uv run python -c "
import json, jsonschema
schema = json.load(open('docs/gateway-contract/v1/runner_fact_batch_v1.schema.json'))
jsonschema.validate(json.load(open('batch.json')), schema)
"
```

`jsonschema` 随 `dev` extra 提供 —— `make install` 会装上它；runner 的基础安装不含它，因为"校验别人的文档"不是 runner 在运行时要做的事。

11 份里有 3 份声明了 `$schema` 为 draft 2020-12：注册载荷、fact batch，以及 import 前验证回执。其余 8 份省略了它，于是 draft 由验证器的默认值决定。请在你自己的工具里**显式钉住 draft**，而不是继承所用库恰好选中的那个 —— 两个验证器在 draft 上不一致，就会在文档上不一致。
