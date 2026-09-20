# 04 - recovery-and-post-execution-consistency-fixes

> **Status**: ⏳ In Progress
> **Created**: 2026-09-20
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-recovery-deep-review.md` (`ca49055`)
> **Baseline**: Custos `936940d`; Crucible `e8849a6`
> **Execution**: `/forge:execute --nostop --chain`；`--nostop` 解释为批次间不等待人工确认。

## 修复来源

- 审查报告：`.forge/reviews/2026-09-20-custos-recovery-deep-review.md`
- 历史缺陷探针：`.forge/reviews/2026-09-20-custos-recovery-deep-repro.py`
- 范围：RD-1 至 RD-5，全部 P1
- 约束：已执行事实不得回滚；签名 lane 与 live fail-closed 不放宽；money 全程 `Decimal`；历史 receipt 不刷新

## 跨仓库边界

RD-4 同时修改 Custos producer tests 与 Crucible consumer projector。Crucible 只允许逐文件 stage：

`git add crates/store/src/runner_fact_reconciliation_projector.rs`

禁止 `git add .` / `git add -a`，不得带入 Crucible 当前其他脏文件。

## 根因分诊

| Finding | Priority | Root Cause | Task |
|---|---:|---|---:|
| RD-3 超限成交事务回滚 | P1 | 实现错误：把 post-trade authoritative fact 当成可拒绝 pre-trade intent | 1 |
| RD-5 plain-close 本地拒绝仍耗尽机会 | P1 | 实现错误：平仓语义只看 `reduce_only`，且 strategy submit 的 `None` 无法表达本地拒绝 | 2 |
| RD-2 ACK 续租异常覆盖 applied | P1 | 实现错误：辅助 heartbeat task 的异常优先级高于已持久化主操作结果 | 3 |
| RD-1 node terminal 脱离 durable supervision | P1 | 组合缺失：typed terminal supervisor 存在但未接入 command runtime/daemon | 4 |
| RD-4 generation 切换造成比较集合不一致 | P1 | 跨仓范围错误：producer 是 instance stream，consumer 却按当前 generation 过滤内部事实 | 5 |

## 修复任务

### Task 1: 已执行成交原子记账并持久化风险锁 [P1]

**Files**: `src/custos/core/runner_fact.py`、`src/custos/core/order_reservation_boundary.py`、`tests/test_order_reservation.py`、`tests/engines/nautilus/test_runner_safety_execution_boundary.py`

1. 写失败测试：预留 90、实际成交 120、单笔上限 100、总上限 150；成交数量/成本必须落盘，重开后新增 50 被拒。
2. 增加 runner-scope durable risk latch；超限 fill、lot、checkpoint、event 与 latch 同事务提交。
3. post-trade breach 向边界返回明确异常以冻结内存 breaker，但不能回滚已执行事实。
4. 可信 `rebuild_runner_exposure` 成功后清除 latch；重复 fill 幂等且不重复累计。

### Task 2: 可验证 plain-close 与真实 dispatch receipt [P1]

**Files**: `src/custos/engines/nautilus/runner_safety.py`、`src/custos/core/order_reservation_boundary.py`、Toolkit `signal_execution.py`、相关 gate/Toolkit tests

1. 写失败测试：已有敞口 100、上限 150、等量普通反向平仓必须通过；本地拒绝不得消耗 plain-close；真实 dispatch 后不得重复。
2. 从 canonical cache 验证普通反向订单的 instrument、side、数量不超过唯一开放净持仓；只有满足全部条件才作为 risk-reducing。
3. order gate 记录每个 client order id 的本地 dispatch/refusal 结果；Toolkit 在 submit 返回后读取结果。
4. 仅实际 dispatch 后调用 `mark_plain_close_submitted` 与 `mark_closing`；不改变真实 venue 拒单后的单次降级规则。

### Task 3: ACK heartbeat 不能覆盖已提交成功 [P1]

**Files**: `src/custos/core/runner_command_runtime.py`、`src/custos/core/runner_fact.py`、`tests/test_runner_command_runtime.py`、`tests/test_runner_fact_store.py`

1. 写失败测试：续租失败发生后主操作 applied 成功，`delivered_count=max_deliver` 仍须 ACK，不能追加 retry_exhausted/quarantine。
2. `_with_heartbeat` 明确主操作与辅助任务的异常优先级：主操作成功后 heartbeat 清理异常只记录，不覆盖结果。
3. 主操作失败时保留原业务异常；heartbeat 挂起时取消并有界等待，不能阻塞 command completion。
4. durable terminal transition 在改写 applied 前复核同 generation/fingerprint 的成功 outcome，冲突时 fail closed 而非产生第二终态。

### Task 4: 节点 terminal 接入 generation-aware durable supervision [P1]

**Files**: `src/custos/core/engine_lifecycle.py`、`src/custos/core/runner_command_runtime.py`、`src/custos/cli/_daemon.py`、`src/custos/engines/nautilus/host.py`、生命周期/daemon/host tests

1. 写失败测试：ready 节点 task 异常退出后，必须进入 `wait_terminal → stop → bounded restart/quarantine`，durable 状态不再停留 ready。
2. runtime coordinator 为当前 running generation 保存完整监督上下文；新 generation 或非 running 命令先取消旧监督。
3. daemon 把监督器作为 long-running task 管理；监督任务异常触发 daemon fail-closed，不 silent drop。
4. 每次 restart ready 后继续等待下一 terminal；每次动作前重新确认 desired generation/fingerprint 仍为当前 running。

### Task 5: instance stream 跨 generation 对账范围一致 [P1，跨仓]

**Custos Files**: `src/custos/core/runner_fact_producer.py`、`tests/test_runner_fact_production_loop.py`

**Crucible File**: `crates/store/src/runner_fact_reconciliation_projector.rs`

1. 写 producer 回归：同周期 generation 1/2 使用同一 instance stream period，外部 coverage 不丢切换前区间。
2. 消费端内部 ledger 查询以已验签 tenant/mode/runner/instance stream + source sequence + business time 为范围，不按当前 generation/spec 删除旧代事件。
3. 写 consumer 单元/形状测试证明 SQL 不含 generation/spec 过滤，并保留 instance、runner、sequence fence。
4. 覆盖换代前后各一笔、停机短于/长于采集间隔、每笔只比较一次。

## 批次

- Batch 1：Task 1–3；完成后直接全量验证并继续（用户指定 `--nostop`）。
- Batch 2：Task 4–5；完成后全量验证、自省两轮并进入 chain review。

## 验证清单

- [ ] 5 个缺陷均有正式失败回归，且失败原因与报告一致
- [ ] 历史审查探针保留不改
- [ ] 扩展定向测试通过
- [ ] `make verify` 通过
- [ ] `make check-authority` 通过
- [ ] Crucible projector 单测、`cargo check -p store --lib` 与 `make check-authority` 通过
- [ ] `git diff --check` 两仓通过
- [ ] 无真实凭据、真实账户、下单、容器或生产节点操作
- [ ] Docker、真实交易所、Ubuntu 与部署验收继续单独报告

## 偏离与改进日志

| ID | 类型 | 描述 | 状态 |
|---|---|---|---|
| DEV-04-REVIEW-AS-SOURCE | 流程 | 用户向 execute 传入 review 而非 plan；先把已提交 review 转为本 fix plan 并独立 commit，再执行 | ✅ |
| DEV-04-NOSTOP | 流程 | skill 无 `--nostop` 正式参数；解释为跳过批次人工等待，不跳过 TDD、验证或安全硬门 | ✅ |

## 进度追踪

| Task | Priority | Status | Completed | Commit | Notes |
|---|---:|---|---|---|---|
| 1 | P1 | ✅ | 2026-09-20 | pending close-out | executed fills + durable latch |
| 2 | P1 | ✅ | 2026-09-20 | pending close-out | verified plain close + dispatch outcome |
| 3 | P1 | ✅ | 2026-09-20 | pending close-out | heartbeat/apply result precedence |
| 4 | P1 | 🔲 | — | — | terminal supervision |
| 5 | P1 | 🔲 | — | — | cross-generation reconciliation |
