# 24 - one-ownership-scope-from-claim-to-registration

> **Status**: ✅ Completed
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

- [x] 失败测试先红后绿 —— 实现前 6 红 2 绿，实现后 9 绿
- [x] 审查方探针 `failed_fact_context_keeps_account_partition` 不再成立
- [x] 五个出口：三个可达的逐个断言，两个不可达的**说清为什么**并各自另立断言
- [x] 「改对之后能起来」在**同一个 host** 上验证
- [x] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 校验进作用域 | P2 | ✅ | 2026-09-21 | LB-4 |
| 2 五个出口 | P2 | ✅ | 2026-09-21 | 三可达 + 两不可达各有交代 |
| 3 改对能起来 | P2 | ✅ | 2026-09-21 | 同一 host |

## 偏离与改进日志

### 更正：我的三条用例写错了，不是发现了新缺陷

写完第一版测试跑出来 7 红，其中 3 条不是缺陷：

1. **对照组写错了对象**。我本想用「两个 live 部署共用一个 credential scope 必须被拒」当对照，
   但第二次 deploy 被**另一道**守卫（`_require_the_only_node`，一个 event loop 只跑一个 node）
   先拦下了，根本走不到分区冲突。改成两条各自成立的断言：成功的部署必须**保留**分区，
   以及分区冲突守卫本身（直接调 `_claim_execution_account_partition`）仍然拒绝第二个占有者。
2. **空 `deployment_spec_digest` 从 deploy 走不到事实校验**：`EngineLifecycleAuthority.from_spec`
   在取分区之前就拒了（`engine_protocol.py:196`）。这不是缺口，是另一道更早的门。单独立一条断言：
   它被拒时**什么都没取**（分区没占、node 都没建）。
3. **`settlement_currency` 不从 spec 读**，是 `_deployment_identity` 从 pairs 推的。改成给一个
   结算币种不受支持的 pair 才真正触达那个出口。

记下来是因为这三条都曾短暂地看起来像「第五、六、七个缺陷」。**红的测试不等于代码有问题**——
与 fix 23 那条同型，这一轮里第二次了。

### 补测：「只回滚本次持有的分区」一开始没人守

第一轮扰动把 `_release_execution_account_partition` 改成 `clear()`（释放整张表），**八条测试全绿**。
那正是验收里「只回滚本次持有的分区」这一句，而我没有任何断言覆盖它。

补 `test_a_failure_rolls_back_only_what_this_call_took`：先让一个兄弟实例占住 scope，再让另一个
部署在校验处失败，兄弟的分区必须还在。补完之后 `clear()` 那个扰动立刻转红。

这是 C27 的形状——验收是并列多支，修完根因不等于分句都覆盖了。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-21
- **总 Task 数**: 3
- **偏离数**: 1 更正 + 1 补测
- **验证结果**: 全部通过
- **实施 commit**: `96dcefe`
- **契约影响**: 无。只是把一次调用移进它旁边那个 `try`，没有新增清理路径，也没有改任何签名。
- **C6 核对**: `host.py` 不是字节 pin（`docs/authority/` 零命中），证据链不受影响。

### 红线 gate 满足度

| 红线 | code 覆盖 | runtime wire | defer | follow-up |
|---|---|---|---|---|
| 0.1 Key/KEK 不出进程 | N/A（未触及） | N/A | 无 | — |
| 0.2 执行门不绕过 | 账户分区是「一个 venue 账户同时只有一个部署」的执行面保证；泄漏会让**没起来的**部署挡住真部署 | `deploy` 是宿主唯一的启动入口 | 无 | — |
| 0.3 失联 ≠ 停止 | N/A（未触及） | N/A | 无 | — |
| 0.4 Decimal money math | N/A（未触及） | N/A | 无 | — |

### 验收分句逐条对照

| 分句 | 覆盖它的测试 |
|---|---|
| node 构建后、登记运行前的所有校验位于同一清理范围 | `test_a_timeframe_mismatch_releases_the_account_scope` + 参数化的三个可达出口 |
| 只回滚本次持有的分区 | `test_a_failure_rolls_back_only_what_this_call_took` |
| 测试每个事实校验失败出口 | 三个可达出口参数化；两个不可达的在 `test_a_spec_refused_before_the_claim_takes_nothing_either` 与用例 docstring 里交代 |
| 修正配置后同 scope 的新实例能够启动 | `test_the_corrected_config_can_start_on_the_same_host`（**同一个 host**，换干净 host 证明不了泄漏）|

另加对照：成功的部署必须保留分区；分区冲突守卫仍然拒绝第二个占有者。

### 测试条数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/test_a_failed_build_releases_what_it_took.py` | 9 |
| `tests/test_plan_closeout_counts.py` | 69 |

### 扰动验证

| 扰动 | 结果 |
|---|---|
| 校验挪回清理范围之外（原样缺陷） | 5 红 |
| 释放改成清空整张表（过度回滚） | 1 红 —— 补测之前**全绿**，见上 |
| 还原 | 9 绿 |

每次扰动都用独立的 `PYTHONPYCACHEPREFIX`。

### 审查方探针现状

`failed_fact_context_keeps_account_partition` 中止在 `:259` 的
`assert not node.disposed and not host._active_nodes` —— 它的第一条缺陷断言：node 现在**会**被
dispose 了。

### 功能验证（主路径）

1. 写一份 testnet 部署，故意让 spec 里声明的 timeframe 与策略实际配置的不一致。
2. 部署，应当失败并报 timeframe 不一致。
3. **不重启 daemon**，把 timeframe 改对，用同一个 credential scope 再部署一次 —— 应当成功。
   修复前这里会被「已有活跃部署」挡住，而挡住它的那个部署从来没起来过。
