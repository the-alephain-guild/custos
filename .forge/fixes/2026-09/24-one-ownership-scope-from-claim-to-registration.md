# 24 - one-ownership-scope-from-claim-to-registration

> **Status**: 🔲 Not started
> **Created**: 2026-09-21
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-lifecycle-boundary-deep-review.md` LB-4

## 根因

`src/custos/engines/nautilus/host.py` 的 `deploy` 在取得所有权之后、登记之前有**三个**清理块，
而中间夹着**一段没被任何一个罩住**的校验：

```
_claim_execution_account_partition(...)        # 取账户分区
try: ... builder.build() ...  except: release + raise
fact_context = self._build_runner_fact_context(...)    # ← 无保护
try: _attach_runtime_bridges(...)  except: release + dispose + raise
try: 登记 contexts / add_strategy / run_async  except: release + dispose + pop + raise
self._active_nodes[...] = ...                  # 登记
```

`_build_runner_fact_context` 有五个失败出口（缺 `strategy_id`、缺 spec digest、结算币种不受支持、
声明与运行时 timeframe 不一致、coverage 起点不是 ISO-8601）。任一触发时：**账户分区仍被占着，
node 也没有 dispose**。

而且此刻实例还没进 `_active_nodes`，所以 `stop(instance)` 是 no-op —— 探针日志里那行
`nt_stop_noop_unknown_instance` 就是它。后果是：把配置改对之后，同一个 credential scope 的新实例
会被「已有活跃部署」挡住，而挡住它的是一个根本没起来的部署。

## 修复任务

### Fix 1: 校验挪进已有的所有权作用域 [P2]

**Files**: `src/custos/engines/nautilus/host.py`、`tests/test_a_failed_build_releases_what_it_took.py`

1. 先写失败测试：timeframe 不一致导致 deploy 失败后，账户分区必须已释放、node 必须已 dispose。
2. 把 `_build_runner_fact_context(...)` 移进紧随其后的那个 `try` —— 它的 `except` 已经在做
   `release + dispose`，正是这段校验需要的。**不新增清理路径**，只是让这一步落进本来就对的那个。
3. 「只回滚本次持有的分区」已由 `_release_execution_account_partition` 按
   `deployment_instance_id` pop 满足，不需要改。

### Fix 2: 每个事实校验失败出口都要覆盖 [P2]

**Files**: 测试

五个出口逐个参数化，断言同一组不变量：分区已释放、node 已 dispose、`_active_nodes` 为空。

### Fix 3: 改对配置之后能起来 [P2]

**Files**: 测试

这是用户真正会遇到的那一幕：配置写错 → deploy 失败 → 改对 → **在同一个 host 上**用同一个
credential scope 重新部署，必须成功。探针证明修复前它会被挡住。

**验收**（报告原文）：node 构建后、登记运行前的所有校验都应位于同一个所有权清理范围；只回滚本次
持有的分区。测试每个事实校验失败出口，以及修正配置后同 scope 的新实例能够启动。

## 验证清单

- [ ] 失败测试先红后绿
- [ ] 审查方探针 `failed_fact_context_keeps_account_partition` 不再成立
- [ ] 五个失败出口各有断言
- [ ] 「改对之后能起来」在**同一个 host** 上验证，不是换一个干净 host
- [ ] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 校验进作用域 | P2 | 🔲 | | LB-4 |
| 2 五个出口 | P2 | 🔲 | | LB-4 验收 |
| 3 改对能起来 | P2 | 🔲 | | LB-4 验收 |

## 偏离与改进日志
