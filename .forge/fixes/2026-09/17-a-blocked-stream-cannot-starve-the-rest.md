# 17 - a-blocked-stream-cannot-starve-the-rest

> **Status**: ✅ Completed
> **Created**: 2026-09-20
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-execution-edge-deep-review.md` EE-4

## 根因

outbox 每轮取**全局**前 64 条（`ORDER BY stream_key, source_seq_start LIMIT 64`）。发送器确实会用
`blocked_streams` 避免在同一轮里继续发一个已失败的流，但它只是 `continue` 掉这些行——**不再往后
翻页**。下一轮取回的是同样的 64 条。

一个流只要积压超过一页且首批发不出去，它就把整个发送窗口占满，其他流的事实永远排不上号。

**复现**（审查方，真实 SQLite outbox）：入队 64 个 sandbox 批次 + 1 个 testnet 批次，只让 sandbox
失败。连续三轮 `drain_once` 都返回 0，只尝试 sandbox 首批；testnet 批次的 `attempts` 始终是 0。
把同一个 testnet 批次放进没有积压的 outbox，立刻成功。

后果是切换模式或换部署之后，旧流的磁盘积压能长期挡住健康部署的成交、心跳与生命周期事实。数据没丢，
但云端看不到进展。

## 修复任务

### Fix 1: 受阻的流让开，其余流照常发 [P1]

**Files**: `src/custos/core/runner_fact.py`、测试

1. 先写失败测试：一个流积压满页且发送失败时，另一个健康流的批次必须在同一轮里发出去。
2. `pending()` 接受要排除的 stream_key 集合，发送器在一个流受阻后**带着排除集合再取一页**，
   直到没有新批次或所有流都受阻。
3. 流内顺序不变——排除是按流整体排除，不是跳过流里的某几条。
4. 终止性要能证明：每轮排除集合只增不减，流的数量有限，所以循环必然停。

### Fix 2: StrategySignal 队列同样处理 [P1]

**Files**: `src/custos/core/runner_fact.py`、测试

审查方点名信号队列「具有同类结构」（`:2128-2140`、`:5915-5918`）。同一形状的缺陷不应该只修一半。

**验收**（报告原文）：在保留每个流内部顺序的同时，按流公平调度或继续翻页跳过本轮受阻流。验证
超过页大小的故障积压、健康模式发布、多个部署实例和 StrategySignal 队列。

## 验证清单

- [x] 失败测试先红后绿 —— 实现前 5 红 1 绿，实现后 6 绿
- [x] 审查方探针 `blocked_stream_starves_other_streams` 不再成立
- [x] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 事实 outbox 翻页 | P1 | ✅ | 2026-09-20 | EE-4 |
| 2 信号队列同处理 | P1 | ✅ | 2026-09-20 | EE-4 同构 |

## 偏离与改进日志

### IMPROVEMENT: 终止性不只靠「排除集合只增不减」

计划里的终止性论证是「每轮排除集合只增不减，流的数量有限」。这条成立的前提是**每取一页都至少
发生一次排除或一次投递**——而后者又依赖 `commit_puback` 必然把批次从队列里摘掉。那是当前实现的
事实，但它是另一段代码的性质，不该由发送循环默认。

实现因此另立了一条不依赖该性质的判据：一页里既没有投递成功、也没有新增受阻流时，本轮直接结束。
`test_every_stream_blocked_ends_the_round_without_spinning` 断言两个流各被尝试一次后循环就停，
把「受阻也算进展」这一步单独钉住——扰动验证里把 `progressed = True` 从 except 分支删掉，该测试
连同另外三条一起转红。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-20
- **总 Task 数**: 2
- **偏离数**: 1（一项改进，见上）
- **验证结果**: 全部通过
- **实施 commit**: `d048aef`
- **契约影响**: 无。`pending()` 与 `pending_strategy_signals()` 新增的 `exclude_streams` 形参有
  默认值，wire 契约、schema 与 subject 命名均未变动。
- **红线守护**: 四条红线均未触及——本次改动只在既有 outbox 的读取窗口上加了一个排除集合，不涉及
  凭据、引擎启动门、失联降级与 money math。

### 红线 gate 满足度

| 红线 | code 覆盖 | runtime wire | defer | follow-up |
|---|---|---|---|---|
| 0.1 Key/KEK 不出进程 | N/A（未触及） | N/A | 无 | — |
| 0.2 执行门不绕过 | N/A（未触及） | N/A | 无 | — |
| 0.3 失联 ≠ 停止 | 本次修的正是「失联流拖垮健康流」的反面：受阻流让开后其余流继续发 | `RunnerFactJetStreamPublisher.drain_once` 是守护进程实际跑的那条路径，无新接线 | 无 | — |
| 0.4 Decimal money math | N/A（未触及） | N/A | 无 | — |

### 测试条数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/test_a_blocked_stream_cannot_starve_the_rest.py` | 6 |
| `tests/test_plan_closeout_counts.py` | 55 |

第二行是本份 close-out 自己造成的：探针按带表格的 plan / fix 份数参数化，新增一份即 +2，
所以动了它的人要在自己的报告里重数一遍。

### 扰动验证

| 扰动 | 结果 |
|---|---|
| 事实队列 `pending()` 忽略排除集合 | 4 红 |
| 信号队列 `pending_strategy_signals()` 忽略排除集合 | 1 红 |
| except 分支不再把「新增受阻流」记为进展 | 4 红 |
| 还原 | 6 绿 |

每次扰动都用独立的 `PYTHONPYCACHEPREFIX`，避免 C15 那种同秒还原复用旧字节码的假结论。

### 审查方探针

`.forge/reviews/2026-09-20-custos-execution-edge-deep-repro.py` 的
`blocked_stream_starves_other_streams` 断言的是缺陷现状。修复后它在第 271 行
`assert await publisher.drain_once() == 0` 处中止——正是那条「健康流一轮都发不出去」的断言，
不是别的地方出错。

### 功能验证（主路径）

1. 让一个部署实例的事实积压超过一页（默认 64 批），并让它对应的场所暂时发不出去。
2. 另一个健康实例（不同 mode 或不同 `deployment_instance_id`）照常产生事实。
3. 观察健康实例的 `runner_fact_publication_receipt` 会继续增长；受阻实例每轮只增加一次
   `attempts`，它的积压不再挡住任何人。
