# 09 - regressions-from-fix-06

> **Status**: ✅ Completed
> **Completed**: 2026-09-20
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

- [x] 两项失败测试先红后绿，经扰动验证
- [x] ST-1 / ST-5 的原有回归仍绿（不得为修回归而破坏原修复）
- [x] `.forge/reviews/...execution-edge-deep-repro.py` 的 EE-2 / EE-5 两支不再成立
- [x] `make verify` 与 `make verify-nt` 均 exit 0
- [x] 无真实账户、下单或生产操作

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 分配拒绝保留旧保护 | P1 | ✅ | 2026-09-20 | EE-2，含相邻的本地派发拒绝回滚 |
| 2 止盈基数追加 | P1 | ✅ | 2026-09-20 | EE-5 |

## 偏离与改进日志

### IMPROVEMENT: 入场路径补上本地派发拒绝的处理
- **原因**: EE-2 报告点名「后续本地派发拒绝/订单构造失败具有相邻风险，需一起检查」。实读发现退出路径早已读取 `submit_order` 的本地拒绝（`dispatched is False`），入场路径**没有**——资金已预留、入场已记账，而订单从未到达场所。
- **影响**: `coordinators/signal_execution.py`
- **决定**: 入场路径对齐退出路径：本地拒绝时归还预留、清 pending signal、无持仓时撤销该次入场记录。
- **性质**: 这是既有不对称，不是 fix 06 引入的；但它与 EE-2 同属「拒绝出口未清理」，一并修掉比留到下一轮更合理。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-20
- **总 Task 数**: 2（+1 项相邻改进）
- **偏离数**: 0（1 项正向改进，见上）
- **验证结果**: 全部通过
- **实施 commit 范围**: `390dae0`..`e2e643a`（plan 自身 `d769234`，早于实施）
- **契约影响**: 无。`make check-authority` 通过。
- **红线守护**: 四条红线全数守住。两项修复都在恢复保护与记账的一致性，方向与红线一致。

### 用审查方自己的探针验收

`.forge/reviews/2026-09-20-custos-execution-edge-deep-repro.py` 的两个函数单独调用：

| 探针 | 结果 |
|---|---|
| `refused_reversal_cancels_old_protection`（EE-2）| 断言失败 → 缺陷不再成立 |
| `first_partial_fill_fixes_tp_base_too_small`（EE-5）| 断言失败 → 缺陷不再成立 |

整脚本线性执行，在 EE-2 处即中止，故逐个函数调用验证。未修改该脚本。

### 测试计数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/toolkit/test_strategy_state_and_order_protection.py` | 54 |
| `tests/test_plan_closeout_counts.py` | 39 |

上表合计 93 条。第一行 fix 08 认领时是 45，本轮增至 54。第二行由本 plan 的表格从 37 推到 39——该探针按带计数表的 plan 份数参数化，每多一份就 +2。

### 扰动验证

- 去掉 reversal cancel 的后移（即放回资金检查之前）：EE-2 的两条断言转红。
- 去掉 `_extend_tick_base` 调用：EE-5 的两笔成交测试转红。
- 还原后 grep 核验代码，54 项全绿。

### 这一轮说明了什么

fix 06 的八项修复通过了：37 项新回归、两轮自省、11 个扰动点、两个发布门、一份自出的代码审计、一份品味审计。**两处由它引入的新失败模式全部逃过了这些，被外部审查抓出。** 二者的形态都是「修复本身正确，但与既有路径的交互产生了修复前不存在的状态」：

- EE-2：分配拒绝是对的，但它落在反向撤单之后，于是「拒绝」变成了「撤掉旧保护且什么也不做」。
- EE-5：固定基数是对的，但基数取自第一个成交 lot，于是分批入场的后半段没有退出配额。

自查查得出「我改的东西对不对」，查不出「我改的东西与别处合起来对不对」。这是 lesson #48 的又一次实证，也是外部审查不可替代的地方。

### 功能验证（主路径）

1. HYBRID 模式、资金额度设得刚好不够反向（新仓 + 平旧仓的总额超限），持有一个带止损的多仓，触发一次反向信号。
2. 预期：日志出现 `Capital allocation refused`，不发任何新单，**且旧仓的止损没有收到撤单请求**。修复前这一步会撤掉止损。
3. 配置 scaled 止盈两档各 50%，用一笔会分两次成交的入场（如限价单被部分成交）建仓，随后让价格越过两档。预期总退出量等于全部持仓，而非一半。
