# 08 - scaled-exit-gap-through

> **Status**: ✅ Completed
> **Completed**: 2026-09-20
> **Created**: 2026-09-20
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-strategy-deep-review.md` ST-1 验收条款未满足项
> **Prior**: fix 06 修了 ST-1 的根因（比例基数），但验收的「跳空触发」一支未覆盖

## 为什么还有这一轮

fix 06 按每项 finding 的**根因**收尾，没有逐条对照审查方写在每项末尾的**验收条款**。
ST-1 的验收是三支并列：

> 50%+50% 与 33%+33%+34% 在**逐档成交、跳空触发、部分成交**下的总退出量一致。

fix 06 覆盖了逐档成交，self-reflect 阶段顺带覆盖了部分成交（当时是作为 ST-2 的余量问题修的），
**跳空触发没有覆盖，且实测不成立**。

实测（`init_position` 基数 1，两档各 50%）：

| 场景 | 退出 | 合计 |
|---|---|---|
| 逐档 103→105 | 0.5, 0.5 | 1.000 |
| 跳空单 tick 110 | 0.5 | **0.500** |
| 三档跳空单 tick | 0.33 | **0.330** |

## 根因

`TickMonitorManager._check_scaled_tp` 遍历层级，命中第一个 ARMED 且达标的档就 `return`。
一次 tick 只产出一个 `ExitAction`，而 `ExecutionCoordinator.handle_trade_tick` 也只消费一个。

价格跨过两档时，第二档留在 ARMED。后续 tick 若仍在目标价之上会补上，但价格瞬间冲高再回落
（正是分批止盈要抓的行情）时，第二档的目标量再也不会退出——本该在高位落袋的部分留在仓里。

这与 fix 06 修的比例基数是两回事：基数决定**每档退多少**，本项决定**一次 tick 能处理几档**。

## 修复任务

### Fix 1: 一次 tick 处理完所有已达标的层级 [P1]

**Root Cause**: 实现错误。tick 回调消费单个动作，而跳空可以同时满足多个层级。

**Files**: `coordinators/execution.py`、`tests/toolkit/test_strategy_state_and_order_protection.py`

1. 先写失败测试：单次 tick 跳过两档 / 三档，总退出量必须与逐档触发一致。
2. tick 回调在动作是分批止盈时继续向 monitor 取下一个，直到无更多或持仓已清空。
   - 只对 `partial_pct` 类动作循环。fixed / trailing 的 `check()` 无状态标记，循环会不终止。
   - 每轮重新读持仓：中途可能被平掉。
3. 扰动验证：去掉循环，新测试须转红。

### Fix 2: 把三支验收一并钉住 [P1]

**Files**: `tests/toolkit/test_strategy_state_and_order_protection.py`

补一张按验收条款组织的对照测试：50%+50% 与 33%+33%+34% 两种配置 × 逐档 / 跳空 / 部分成交
三种路径，总退出量一致。部分成交一支此前由 ST-2 的余量测试间接覆盖，这里按 ST-1 的措辞
显式验一次总量。

## 验证清单

- [x] 跳空测试先红后绿，经扰动验证
- [x] 逐档与部分成交两支仍绿（不得为修跳空而破坏它们）
- [x] `make verify` 与 `make verify-nt` 均 exit 0
- [x] 无真实账户、下单或生产操作

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 一次 tick 处理多档 | P1 | ✅ | 2026-09-20 | 循环有两道终止保证，见偏离 |
| 2 三支验收对照测试 | P1 | ✅ | 2026-09-20 | 4 项，两种配置 × 三条路径 |

## 偏离与改进日志

### DEVIATION: 首版实现引入无限循环，测试挂死 120s 被中止
- **等级**: 低（当场发现，未进入任何 commit）
- **原因**: 循环的终止条件寄托在「层级状态会推进」上。但 `abandon_level()` 把派发不出去的层级**有意**退回 ARMED——那正是本地拒绝保持可重试的机制——于是同一档被无限次提供。我在注释里写了「只对 partial 循环，因为 fixed/trailing 无状态标记会不终止」，却没想到 partial 自己也能不终止。
- **决定**: 每档每 tick 只得到一次机会，用 `offered` 集合计数。终止不再依赖状态推进，而依赖层级数有限。
- **教训**: 循环的终止要能证明，不能论证「状态应该会前进」——尤其当另一条路径**有意**让它后退时。

### DEVIATION: 第二版把层级卡在 PENDING，踩了本轮自己修过的坑
- **等级**: 低（由既有测试当场抓出）
- **原因**: `offered` 判断放在 `check()` **之后**，而 `check()` 是有副作用的查询——它在返回前把层级标记为 PENDING。于是「这一轮不执行、直接 return」留下一个 PENDING 层级，后续 tick 再也不会提供它。ST-2 的 `test_a_locally_refused_partial_retries_at_the_same_price` 立刻转红。
- **决定**: 不执行的那次提供，离开前用 `release_level` 还回去。
- **讽刺之处**: ST-2 的根因就是「check() 在成交前消费层级」。修它的这一轮，我在新写的循环里以另一种形式重犯——**查询有副作用时，任何提前退出都必须归还它拿走的东西**。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-20
- **总 Task 数**: 2
- **偏离数**: 2（均为实现过程中的自伤，当场修正，见上）
- **验证结果**: 全部通过
- **实施 commit 范围**: `69b3525`（plan 自身 `513e612`，早于实施）
- **契约影响**: 无。改动限于 tick 回调的消费方式，`make check-authority` 通过。
- **红线守护**: 四条 non-custodial 红线全数守住。本项恢复的是计划内的退出量，方向与红线一致。

### 修复前后实测（基数 1，两档各 50% / 三档 33+33+34）

| 路径 | 修复前 | 修复后 |
|---|---|---|
| 逐档 103→105 | 0.5 + 0.5 = **1.000** | 0.5 + 0.5 = 1.000 |
| 跳空单 tick 110 | 0.5 = **0.500** ❌ | 0.5 + 0.5 = **1.000** |
| 跳空后回落 110→101 | 0.5 = **0.500** ❌ | 0.5 + 0.5 = **1.000** |
| 三档逐档 | 0.33+0.33+0.34 = 1.000 | 同 |
| 三档跳空单 tick | 0.33 = **0.330** ❌ | 0.33+0.33+0.34 = **1.000** |

### 测试计数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/toolkit/test_strategy_state_and_order_protection.py` | 45 |
| `tests/test_plan_closeout_counts.py` | 37 |

上表合计 82 条。第一行 fix 07 认领时是 41，本轮增至 45，由本 plan 重新认领。

第二行写 37 而不是 35：我起初以为本 plan 的表格只是「替换 fix 07 对该文件的认领、份数不变」，实跑是 37。该探针按**带测试计数表的 plan 份数**参数化，本 plan 是新增的一份，不是替换——它自己一落盘就把计数推高 2。推理错了，数字以实跑为准。

### 扰动验证

- 去掉循环（回到一次一档）：4 条跳空测试转红。
- 去掉 `release_level` 归还：ST-2 的本地拒绝重试测试转红——证明第二道保证同样是承重的，不是冗余。
- 还原后 grep 核验两处代码各 1 处命中，45 项全绿。

### ST-1 验收三支的最终状态

| 验收分支 | 覆盖 |
|---|---|
| 逐档成交 | `test_halves_agree_across_modes` / `test_thirds_agree_across_modes`（fix 06）|
| **跳空触发** | `test_a_single_tick_through_every_level_exits_the_same_total`（两种配置）+ `test_a_gap_that_falls_straight_back_still_took_everything` + `test_a_gap_exits_each_level_its_own_share`（本轮）|
| 部分成交 | `TestAPartiallyFilledLevelKeepsItsRemainder` 两项（fix 06 self-reflect）|

### 功能验证（主路径）

1. 在 sandbox 配置 scaled 止盈两档（如 +2% 退一半、+4% 退一半），建仓后制造一次跨过两档的快速上涨（或直接喂一根跨越两个目标价的 tick）。
2. 预期：这一个 tick 就发出两笔减仓单，合计等于计划总量；修复前只发一笔，另一半留在仓里。
3. 让价格随后立刻跌回目标价以下，确认没有遗留未退出的计划量。
