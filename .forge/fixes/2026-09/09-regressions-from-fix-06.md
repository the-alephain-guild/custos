# 09 - regressions-from-fix-06

> **Status**: ⏳ In Progress
> **Created**: 2026-09-20
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-execution-edge-deep-review.md` EE-2、EE-5
> **性质**: 这两项是 **fix 06 的修复引入的**，不是既有缺陷。优先于其余待办。

## 为什么先做这两项

外部审查在 fix 06/07/08 之后又审了一轮，抓出两处**由我的修复造成**的新失败模式。二者都通过了
fix 06 的全部测试、两轮自省、11 个扰动点和两个发布门——自查没有抓到，外部审查抓到了。

`.forge/reviews/2026-09-20-custos-execution-edge-deep-repro.py` 在当前 HEAD 上两项均复现成立。

## 修复任务

### Fix 1: 分配拒绝不得以撤掉旧仓保护为代价 [P1 / EE-2]

**Root Cause**: 实现错误，由 fix 06 Fix 4（ST-5）引入。`execute_entry_for_pair` 的反向分支先
`cancel_all_orders` + `order_tracker.clear()`（有副作用），我随后加入的资金预留检查在它**之后**
才拒绝并 `return`。于是出现一个 fix 06 之前不存在的状态：新入场 0 笔、旧仓的唯一止损已收到撤单
请求、tracker 覆盖归零——旧仓在保护空窗里裸奔，直到后续 bar 的自愈补单。

**Files**: `coordinators/signal_execution.py`、`tests/toolkit/test_strategy_state_and_order_protection.py`

1. 先写失败测试：HYBRID 已有多仓 1（止损 95），资本已被旧仓占满，请求反向。断言旧止损**未**收到
   撤单请求且覆盖数量不变，同时新入场仍为 0 笔。
2. 重排顺序，让所有**无副作用的检查与资源预留先完成**：算出 final_size（含反向 sizing）→ 申请
   资金预留 → 拒绝则原样返回，不碰任何既有订单 → 通过后才 `cancel_all_orders` + 建单 + 提交。
3. 反向 sizing 的计算与撤单必须分开：前者只读持仓，后者是副作用。
4. 同时检查相邻出口：`final_size <= 0`、`create_entry_order` 返回 None、本地派发拒绝——每一条
   都不得在撤掉旧保护之后才退出。

**验收**（取自报告原文）：所有拒绝出口都保留旧仓已接受的保护，不能仅断言「未发送新入场」。

### Fix 2: 止盈基数必须是最终开仓量，不是第一笔成交量 [P1 / EE-5]

**Root Cause**: 实现错误，由 fix 06 Fix 6（ST-1）引入。基数取自 `init_position` 时的
`position.quantity`，而 `_init_tick_position` 只在第一个正敞口 lot 上触发
（`initialize_position=True`）。同一入场单分两笔成交 0.5 + 0.5 时，基数固定为 0.5，两个 50%
层级各卖 0.25，全部层级标记完成，仓里仍留 0.5 且再高的价格也不会退出。

**Files**: `sltp_mode.py`、`tick_monitor.py`、`coordinators/trade_event_handler.py`、测试

1. 先写失败测试：一笔入场分两次成交 0.5 + 0.5，两档各 50%，总退出量必须等于 1。
2. 后续成交**追加**基数而非重置：新增 `extend_base(additional)`，由后续正敞口 lot 调用。
3. 不得用 `init_position` 重置——那会清掉已完成层级与已成交记账（审查方明确点名）。
4. 覆盖：入场分批成交、入场尚未结束时已触发止盈、加仓。

**验收**（取自报告原文）：维护最终开仓暴露或按每笔新增成交追加止盈配额，同时保留已完成量。

## 验证清单

- [ ] 两项失败测试先红后绿，经扰动验证
- [ ] ST-1 / ST-5 的原有回归仍绿（不得为修回归而破坏原修复）
- [ ] `.forge/reviews/...execution-edge-deep-repro.py` 的 EE-2 / EE-5 两支不再成立
- [ ] `make verify` 与 `make verify-nt` 均 exit 0
- [ ] 无真实账户、下单或生产操作

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 分配拒绝保留旧保护 | P1 | 🔲 | | EE-2 |
| 2 止盈基数追加 | P1 | 🔲 | | EE-5 |

## 偏离与改进日志
