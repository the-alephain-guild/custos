# 08 - scaled-exit-gap-through

> **Status**: ⏳ In Progress
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

- [ ] 跳空测试先红后绿，经扰动验证
- [ ] 逐档与部分成交两支仍绿（不得为修跳空而破坏它们）
- [ ] `make verify` 与 `make verify-nt` 均 exit 0
- [ ] 无真实账户、下单或生产操作

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 一次 tick 处理多档 | P1 | 🔲 | | |
| 2 三支验收对照测试 | P1 | 🔲 | | |

## 偏离与改进日志
