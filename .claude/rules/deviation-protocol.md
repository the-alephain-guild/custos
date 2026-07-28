# 偏离协议 (custos)

当需要偏离既定架构、技术栈或方法论时, 必须遵循本协议. custos 独立开源仓库,
偏离处理不依赖 workspace councils, 但需在 plan 偏离日志中详实记录.

## 偏离等级

### 低风险 — 记录即可

- 模块内部重构 (不影响 6 模块契约边界)
- 新增内部工具函数 / 辅助模块
- 测试策略调整 (增加测试覆盖)
- 文档格式微调

**处理**: 在 commit message 中说明偏离理由, 或在 plan 偏离日志追加一条即可.

### 中风险 — 更新契约文档

- 6 模块之间的接口变更 (如 telemetry_actor ↔ nats_client envelope schema)
- Pydantic 模型 (DeploymentSpec / DeploymentStatus) 结构变更
- NATS subject 命名规则变更
- 新增 / 移除 Python 依赖 (超出 pyproject.toml 已声明)
- **G6 gate 参数微调** (非绕过, 只调节 tolerance)

**处理**:
1. 在 PR / commit message 中说明变更原因和影响
2. 更新 `docs-site/docs/` 对应章节; 若涉及跨仓协调契约, 同步 `docs/authority/` 记录
3. 更新 `docs-site/docs/01-introduction/what-is-custos.md` (如涉及核心词汇或不变量)
4. 更新受影响的 test (wire contract test / envelope test)
5. `.forge/plans/.../NN.md` 偏离日志追加一条

### 高风险 — Non-Custodial 红线 / 顶层 spec 变更

以下必须先起 councils / peer review, 不能直接改代码:

- 触及 **Non-Custodial 4 红线** 任一条 (`mandatory-rules.md` §0)
  - Key/KEK 出进程规则松动
  - G6 gate 绕过或降级
  - 失联降级策略调整
  - Money math 从 Decimal 松动到 float
- 顶层领域词汇结构性变更 (`docs-site/docs/01-introduction/what-is-custos.md`)
- 技术栈根本性变更 (语言 / 主框架 / 数据模型库)
- 新增 6 模块之外的 core module
- LICENSE / NOTICE 变更

**处理**:
1. 发起 peer review (workspace 内: 走 forge:peer-reviewing skill; 独立场景: 邮件 / issue 讨论)
2. 讨论结论记录到 `.forge/reviews/YYYY-MM/` 目录 (workspace 内) 或 `.forge/deviations/` (独立场景)
3. 更新顶层文档 (`README.md` / `CLAUDE.md` / 站点 introduction 章节 / `mandatory-rules.md`)
4. Plan 偏离日志同步

## 偏离记录模板

在 forge 计划文件的 "偏离与改进日志" 中记录:

```markdown
### DEVIATION: {简短标题}
- **等级**: 低/中/高
- **原因**: {为什么需要偏离}
- **影响**: {受影响的模块和文件}
- **决定**: {最终采取的方案}
- **更新的文档**: {列出已更新的权威文档}
```

## 紧急偏离 (生产事故)

用户机器上跑的 custos daemon 出问题时可先修复再补记录:

1. 立即修复 (允许跳过 councils / peer review)
2. 24 小时内补充偏离记录到最近的 plan 或新建 `.forge/incidents/YYYY-MM/`
3. 若涉及红线松动, 必须在 `historical-lessons.md` 中新增一条教训
4. 下一个工作日重新起 councils / peer review 审议是否长期化偏离

## Non-Custodial 红线不可紧急偏离

即使生产事故:

- Key / KEK 出进程 → **禁止**紧急偏离. 优先降级 (`paper_only=true`), 而非把 key 送云端
- G6 gate → **禁止**紧急绕过. 事故期间用 `NoopHost` 停 live, 不是绕过 gate 直接下单
- Money math float → **禁止**紧急退回 float. 用户资金精度事故不可挽回
- 失联降级 → local fallback 是**本来就该 work** 的行为, 不算偏离

红线的紧急预案是**降级到 paper**, 不是绕过红线.
