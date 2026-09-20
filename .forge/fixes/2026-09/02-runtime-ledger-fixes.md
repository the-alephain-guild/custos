# 02 - Runtime and ledger fixes

> **Status**: ⏳ In Progress
> **Created**: 2026-09-20
> **Project**: Custos
> **Plan**: `.forge/plans/2026-07/19-crucible-command-runner-fact-runtime-convergence.md`
> **Source**: `.forge/reviews/2026-09-20-custos-runtime-code-review.md`
> **For Claude**: Use `/forge:execute` to implement these fixes.

## 修复来源

审查确认五项 P1、一项 P2。修复属于本地源代码与验证范围，不代表生产准入。

## 修复任务 (Tasks)

### Fix 1: 审计失败进入引擎降级状态 [P1 / CR-1]

**Root Cause**: 实现错误。事件桥吞掉异常，外层转发器无法记录失败。
**Files**: `src/custos/core/runner_fact_producer.py`、事件转发相关测试。
**Step 1**: 写订单和持仓审计失败测试。
**Step 2**: 确认失败。
**Step 3**: 将异常交给已有隔离与降级机制。
**Step 4**: 验证原策略回调继续运行，失败状态可见。
**Step 5**: 提交。

### Fix 2: 对账周期原子写入 [P1 / CR-4]

**Root Cause**: 实现错误。分段写入和 ID 去重允许混合采集轮次。
**Files**: `src/custos/core/runner_fact.py`、`src/custos/core/runner_fact_producer.py`、outbox 与生产循环测试。
**Step 1**: 写采集中断、事务失败、重试测试。
**Step 2**: 确认失败。
**Step 3**: 完整准备后在同一事务中发布周期，拒绝冲突重放。
**Step 4**: 验证回滚、重试和重启。
**Step 5**: 提交。

### Fix 3: 现货账户总资产估值 [P1 / CR-6]

**Root Cause**: 实现错误。报价币余额被误当作总资产。
**Files**: `src/custos/engines/nautilus/portfolio_snapshot.py`、组合快照与风控测试。
**Step 1**: 写多币种 CASH 账户测试。
**Step 2**: 确认失败。
**Step 3**: 按可信价格换算资产余额，避免与持仓重复计价，缺价时标记不可靠。
**Step 4**: 验证等值资产变化不产生虚假回撤。
**Step 5**: 提交。

### Fix 4: 永续独立估值检查点 [P1 / CR-2]

**Root Cause**: 实现错误。账本遗漏钱包、成本和独立价格，循环静默省略检查点。
**Files**: OKX/SoDEX 账本、生产循环及相关测试。
**Step 1**: 写非零浮盈亏测试。
**Step 2**: 确认失败。
**Step 3**: 补齐独立估值，缺失必需数据时拒绝关闭周期。
**Step 4**: 验证共同价格下的钱包与权益核对。
**Step 5**: 提交。

### Fix 5: 现货库存对账语义 [P1 / CR-3]

**Root Cause**: 实现与契约缺口。现货库存输出为空仓位并声明完整。
**Files**: 交易所账本、RunnerFact 契约、必要的 Crucible 消费端及测试。
**Step 1**: 写持有基础资产的现货测试。
**Step 2**: 确认失败。
**Step 3**: 明确账户库存与策略持仓边界，使用真实余额和价格，不伪造成本。
**Step 4**: 验证正常库存、外部资产与缺失数据。
**Step 5**: 分仓提交，只 stage 具体文件，保留无关修改与历史凭据。

### Fix 6: 离线新 generation 安全替换 [P2 / CR-5]

**Root Cause**: 实现错误。更新调用不支持的结构性热重配。
**Files**: `src/custos/offline/reconciler.py` 及测试。
**Step 1**: 写 generation 更新测试。
**Step 2**: 确认失败。
**Step 3**: 预先解析材料，停止后部署新配置。
**Step 4**: 验证失败重试、状态记录和重复投递。
**Step 5**: 提交。

## 验证清单 (Verification)

- [ ] 六项回归测试先失败后通过。
- [ ] `make check`、`make check-authority`、相关全量测试通过。
- [ ] 必要的跨仓契约测试通过。
- [ ] 自省最多两轮，无发现可提前结束。
- [ ] 偏离记录与独立 close-out commit 完整。

## 进度追踪 (Progress)

| Fix | Priority | Status | Completed | Notes |
| --- | --- | --- | --- | --- |
| 1 | P1 | ✅ | 2026-09-20 | 四条失败路径 RED；相关 52 项 GREEN |
| 2 | P1 | 🔲 | — | CR-4 |
| 3 | P1 | 🔲 | — | CR-6 |
| 4 | P1 | 🔲 | — | CR-2 |
| 5 | P1 | 🔲 | — | CR-3 |
| 6 | P2 | 🔲 | — | CR-5 |

## 偏离与改进日志

无。
