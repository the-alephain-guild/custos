# 17 - a-blocked-stream-cannot-starve-the-rest

> **Status**: ⏳ In Progress
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

- [ ] 失败测试先红后绿
- [ ] 审查方探针 `blocked_stream_starves_other_streams` 不再成立
- [ ] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 事实 outbox 翻页 | P1 | 🔲 | | EE-4 |
| 2 信号队列同处理 | P1 | 🔲 | | EE-4 同构 |

## 偏离与改进日志
