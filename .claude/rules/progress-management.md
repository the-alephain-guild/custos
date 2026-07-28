# 进度管理 (custos)

custos 单栈 Python daemon 项目进度跟踪规范.

## 状态标记

| 标记 | 含义 | 适用场景 |
|------|------|---------|
| 🔲 | Todo | 未开始 |
| ⏳ | In Progress | 正在进行 |
| ✅ | Done | 已完成并验证 |
| ❌ | Blocked | 被阻塞 |
| ⚠️ | Needs Review | 需要审查 |

## 版本化 (SemVer)

- Python: `pyproject.toml` → `[project].version`
- 当前: 0.0.0 (pre-release, 未发布 pip)
- 首次发布目标: 0.3.0 (Plan 00a `NtTradingNodeHost` 落地后)
- 稳定发布目标: 1.0.0 (G6 gate + 6 模块全实现 + 签名 release pipeline 落地后)

- **Major**: 破坏性 API 变更 (envelope schema 不兼容 / 6 模块契约变更)
- **Minor**: 新增功能 (新增可选依赖 / 新的 wire schema 字段 backward compatible)
- **Patch**: bug 修复 (不改 wire contract)

## Plan 生命周期

1. **起草**: `.forge/plans/YYYY-MM/NN-<slug>.md` 头部 Status: 🔲 Todo
2. **审议**: peer review (workspace 内走 forge:peer-reviewing / 独立场景 issue 讨论)
3. **执行**: 状态改 ⏳ In Progress, 逐 Task ✅ 打钩
4. **验证**: `make verify` 全绿 + 6 模块契约测试 + 失败模式测试
5. **Close-out**: 状态改 ✅ Completed, 末尾追加完成报告

## Plan 检查点

对于涉及 6 模块契约或红线的 Plan, 关键检查点:

```
[起草] → 6 模块契约影响面已列 (docs-site/docs/ 对应章节 + docs/authority/ 记录的更新点)?
    ↓
[实施] → TDD 每 Task 先写失败测试? Non-Custodial 4 红线 grep 通过?
    ↓
[验证] → 失败模式覆盖 (nats down / vault_locked / g6 gate deny)?
    ↓
[Close-out] → 契约测试全绿? 偏离日志已记?
```

## 阻塞依赖上报

Plan 之间依赖时:

1. 在 plan 的 `Depends on:` 段声明前置 plan (如 `00a → 00b → 00c`)
2. 如前置 plan 未 close-out, 后置 plan 头部标 ❌ Blocked + 阻塞原因
3. 若阻塞超过一周, 起 workaround plan (如降级到 `paper_only` 兜底)

## Plan 文件的 git 跟踪

- Plan 文件必须在**执行前** commit (`/forge:execute` 会验证)
- 执行完成后, plan 状态更新 + close-out 追加 → commit `docs(custos): mark plan NN as completed`
- Plan 文件是**活文档**, 执行过程中的偏离即时更新 plan 内 "偏离与改进日志"

## 完成报告模板

Plan 执行完毕后, 在 plan 文件末尾追加:

```markdown
## 完成报告 (Close-out Report)

- **完成日期**: {YYYY-MM-DD}
- **总 Task 数**: {N}
- **偏离数**: {N} (详见偏离日志)
- **验证结果**: 全部通过 / 部分通过
- **实施 commit 范围**: {first_sha}..{last_sha}
- **契约影响**: {列出更新的站点章节 / `docs/authority/` 记录, 或 "无"}
- **红线守护**: Non-Custodial 4 红线全数守住 (grep 记录) / 触发某条 (走偏离协议)
- **失败模式覆盖**: {新增测试列表}
- **遗留项**: {列表或"无"}
```

## Roadmap

高层里程碑跟踪在:

- `.forge/README.md` — plan 索引 (机械状态)
- `CLAUDE.md` — 项目导航图 (语义指针)
- README.md §"Not Included Yet" — 已声明的 follow-up 列表 (对外承诺)
