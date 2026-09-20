# 02 - Runtime and ledger fixes

> **Status**: ✅ Completed
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

- [x] 六项回归测试先失败后通过。
- [x] `make check`、`make check-authority`、相关全量测试通过。
- [x] 必要的跨仓契约测试通过。
- [x] 自省最多两轮，无发现可提前结束。
- [x] 偏离记录与独立 close-out commit 完整。

## 进度追踪 (Progress)

| Fix | Priority | Status | Completed | Notes |
| --- | --- | --- | --- | --- |
| 1 | P1 | ✅ | 2026-09-20 | 四条失败路径 RED；相关 52 项 GREEN |
| 2 | P1 | ✅ | 2026-09-20 | RED: 提前写入与缺少原子接口；13 项 GREEN，authority gate 通过 |
| 3 | P1 | ✅ | 2026-09-20 | 3 RED；22 GREEN，含真实 native CASH 与行情回放 |
| 4 | P1 | ✅ | 2026-09-20 | 3 RED；26 GREEN，缺价拒绝与必需检查点校验 |
| 5 | P1 | ✅ | 2026-09-20 | 现货库存 RED→GREEN；三交易所、跨语言 digest、Crucible 2+67 GREEN；消费端 1eaca8d |
| 6 | P2 | ✅ | 2026-09-20 | generation、材料失败、部署失败 RED；保留 breaker 的替换与回归 GREEN |

## 偏离与改进日志

先执行独立的 Fix 6，再完成涉及跨仓契约的 Fix 5。替换期间串行化风控观察，保留 breaker 高水位。

全量回归发现计数门禁只扫描 plans。新增回归后扩展为同时扫描 fixes，按月份与文件名排序。当前计数写在本文件，历史报告不改。

## 当前测试文件计数

2026-09-20 的 pytest collection。记录当前修复扩展后的数量，旧计划保留历史数字。

| File | Count |
| --- | --- |
| `tests/test_offline_reconciler.py` | 41 |
| `tests/test_strategy_signal_bridge.py` | 12 |
| `tests/test_portfolio_snapshot.py` | 22 |
| `tests/test_plan_closeout_counts.py` | 25 |

## 自省

Round 1：发现永续账本包括非部署结算币余额，而内部保证金权益只读取部署结算币。两项新增测试复现。OKX、SoDEX 改为与 Binance 一致的结算币范围；现金库存不受此过滤影响。


Round 2：复核两仓实施 diff、现金与保证金范围、原子写入、失败可见性、替换时风控连续性、真实适配器账户类型。无新增阻塞问题。内部自审不构成独立审查或生产验收。

## 完成报告 (Close-out Report)

六项原始 finding 已修复。另修复自省发现的永续余额范围问题及只扫描 plans 的测试计数门禁。主计划 Plan 19 的真实生产验收仍开放。

### 提交

- 计划先行：`6f7e9ee`。
- Fix 1：`1d11fc6`。审计异常进入宿主降级机制，策略原回调由转发器隔离执行。
- Fix 2：`01ab0de`。完整周期分批入库使用同一 SQLite 事务；冲突重放拒绝。
- Fix 3：`6debd44`。现金余额按可信现货价格换算 NAV，避免与持仓重复计算。
- Fix 4：`213adee`。永续钱包、成本、独立价格进入检查点；必需数据缺失不关闭周期。
- Fix 6：`76005b5`。离线更新解析材料后停止再部署，保留 breaker 与高水位。
- 计数门禁：`96eda4f`。当前计数写入修复计划，不重写历史报告。
- Fix 5：Custos `6ae2cfb`，Crucible `1eaca8d`。显式现金库存与跨语言共同价格核对。
- 自省修正：`a463041`。永续余额范围与内部结算币权益对齐。

### 验证证据

- 每个原始 finding 均有失败后通过的回归。Rust 现金契约旧消费者实测拒绝 `cash_inventory`，新消费者成功解析。
- `make check`：371 个文件格式检查与 lint 通过。
- `make check-authority`：Custos、Crucible 均通过。历史 receipts 未刷新。
- `uv run --extra dev --extra nautilus pytest tests/ -q`：2597 passed、28 skipped、1 xfailed。跳过项不计入通过。
- `CARGO_TARGET_DIR=/tmp/custos-fix02-cargo CARGO_INCREMENTAL=0 CARGO_BUILD_JOBS=2 cargo test -p domain --test runner_fact_contract_v1`：2 passed。
- 同一隔离构建目录下 `cargo test -p store --lib`：67 passed。包含 Python checkpoint digest、库存差异、共同价格重估与篡改拒绝。
- 原生 Nautilus CASH 账户与行情回放验证：1000 USDT + 1 BTC × 9000 = 10000 USDT，不依赖策略仓位记录。
- SQLite 验证包含第二批写入故障、完整回滚、序号恢复、重开数据库和冲突重放。
- 独立交易所 HTTP 使用固定响应；未连接真实交易账户、未下单、未启用 live。
- 未重新构建或发布生产镜像，未执行 Ubuntu 部署。既有 testnet 容器未操作。
- Crucible 其他未提交文件未纳入本次提交。

### 功能验证（主路径）

以下是后续在非生产环境执行的操作验证，真实账户路径本轮未执行。

1. 用持有基础币库存的 sandbox/testnet 现货账户启动策略，核对运行状态的总资产包含基础币估值，现金可用余额单独查看。
2. 查看签名对账周期：现货包含 `cash_inventory`，永续包含钱包和共同价格检查点；缺价时周期不完成，审计写入失败时运行状态降级。
3. 对同一个离线 spec 发布更高 generation，确认旧节点停止、新配置运行并报告新 generation；重复投递不再创建节点。

### 部署边界

现金库存路径需要部署配套的 Custos 和 Crucible 源码版本。现金账户要求单一估值币和直接现货换算市场；非零资产缺少支持的独立价格时拒绝完成对账。策略成本与已实现盈亏保留在结算观察中，不伪造现金交易所成本证据。

本次仅关闭代码修复。生产镜像、签名晋升、Ubuntu 主机、真实账户连续对账与下单恢复演练仍需按 Plan 19 验收。
