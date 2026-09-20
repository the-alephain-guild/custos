# 10 - contract-units-and-protection-registration

> **Status**: ✅ Completed
> **Completed**: 2026-09-20
> **Created**: 2026-09-20
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-risk-state-deep-review.md` RS-1、RS-2
> **分诊**: 两项均 P1。审查方建议的修复顺序把 RS-1/2 排在最前。

## 修复任务

### Fix 1: 反向 sizing 必须按合约乘数换算 [P1 / RS-1]

**Root Cause**: 实现错误（既有）。反向入场把旧仓换算成报价金额时写的是 `current_qty * price`，
而线性合约的名义金额是 `qty * price * multiplier`。乘数被当作 1，旧仓的平仓部分被放大
`1 / multiplier` 倍；下游 `quantity_from_notional()` 又正确地除以 multiplier，于是错误被带进最终
张数。现货与 multiplier=1 的永续看不出问题，这也是它能活到现在的原因。

**复现**（审查方探针）：线性合约 multiplier=0.01、价格 100、旧空仓 10 张、目标新多仓名义 10。
正确应买 20 张，实际提交 **1010 张**——扣掉旧空仓后是 1000 名义，放大 100 倍。

**Files**: `sizing.py`、`coordinators/signal_execution.py`、测试

1. 先写失败测试：multiplier ∈ {0.01, 1, 10} 的双向反转，按**最终净仓张数**验收。
2. `sizing.py` 补 `notional_from_quantity()`，与既有的 `quantity_from_notional()` 对称，乘数与
   inverse 的校验共用同一套。
3. 反向 sizing 改用它，不再手写 `qty * price`。

**验收**（报告原文）：统一原生数量与报价金额的转换；测试 multiplier=0.01、1、10 的双向反转，
按最终净仓数量验收，不能只测单次入场。

### Fix 2: 本地拒绝的保护单不得计入已保护数量 [P1 / RS-2]

**Root Cause**: 实现错误（既有）。三处保护提交都先写 tracker 再 `submit_order`，不看返回值。
`RunnerSafetyOrderGate` 的本地拒绝发生在进入原生缓存之前，因此既没有订单落入缓存，也不会有
`OrderRejected` 回调。而修复器只移除**缓存中已终结**的订单，缓存里根本不存在的订单被无限期当作
在途保护——覆盖数量虚高，自愈永远认为不缺保护。

**复现**（审查方探针）：真实安全门 + OKX ID 校验拒掉一张保护单；零订单进入提交端与缓存，
tracker 覆盖数量却是 1，跨三次修复冷却仍不补单、也不因缺保护暂停。

**Files**: `coordinators/sltp.py`、测试

1. 先写失败测试：保护单被本地拒绝后覆盖数量必须为 0，且下一次修复窗口会补单。
2. 三处提交（`submit_stop_loss` / `submit_safety_stop_loss` / `submit_native_trailing`）与保本换单
   一致处理：读 `submit_order` 返回值，`False` 即撤销刚写入的 tracker 记录并记 warning。
3. 登记与派发结果一致——先登记是为了让并发到达的回报能找到归属，但派发失败必须回滚。

**验收**（报告原文）：注册保护必须与派发结果一致；本地拒绝立即撤销覆盖计数并进入可观察的未保护
状态。覆盖 EXCHANGE/HYBRID/NATIVE_TRAILING 及保本换单的 False 路径。

> 报告同时提到「未知在途订单要有超时和查询确认」。那需要一套在途保护的超时对账机制，范围超出
> 本轮的两项；登记为后续项，不在本 plan 承诺。

## 验证清单

- [x] 两项失败测试先红后绿，经扰动验证
- [x] 审查方探针 `contract_reversal_oversizes` / `refused_protection_stays_counted` 不再成立
- [x] `make verify` 与 `make verify-nt` 均 exit 0
- [x] 无真实账户、下单或生产操作

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 反向 sizing 乘数 | P1 | ✅ | 2026-09-20 | RS-1，参数化覆盖 0.01 / 1 / 10 |
| 2 保护登记与派发一致 | P1 | ✅ | 2026-09-20 | RS-2，四条保护路径 |

## 偏离与改进日志

### IMPROVEMENT: native_trailing 一并纳入
- **原因**: 报告点名 EXCHANGE / HYBRID / NATIVE_TRAILING 与保本换单四条路径。实读确认第四条
  （`submit_native_trailing`）同样是先登记后提交且不读返回值。
- **决定**: 四条统一走新的 `_dispatch_protection`，不留一条按旧形态。

### DEVIATION: 测试替身最初让被拒订单留在缓存里
- **等级**: 低（当场发现，测试从未据此通过）
- **原因**: harness 的 order factory 把每笔构造出来的订单放进缓存，包括从未提交的。于是修复器在
  下一个窗口用 `find_existing_sl_orders` 又把它认领回来，覆盖数量回到 1，重试测试红——而红的理由
  与被测缺陷无关。
- **决定**: 拒绝替身同时把订单移出缓存。报告原文是「没有任何订单进入提交端**或缓存**」；替身必须
  照这个说，否则测的是另一回事。
- **教训**: 替身的不准确会以「测试红」的形式出现，容易被当成实现缺陷去改实现。红的时候先问替身对不对。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-20
- **总 Task 数**: 2
- **偏离数**: 1 项正向改进 + 1 项测试替身修正
- **验证结果**: 全部通过
- **实施 commit 范围**: `072c625`..`4c88837`（plan 自身 `8e96604`，早于实施）
- **契约影响**: 无。`make check-authority` 通过。
- **红线守护**: 四条红线全数守住。RS-1 修的是下单数量正确性，RS-2 修的是保护覆盖的真实性，
  两者都在收紧而非放松红线。

### 用审查方自己的探针验收

| 探针 | 结果 |
|---|---|
| `contract_reversal_oversizes`（RS-1）| 断言失败 → 缺陷不再成立 |
| `refused_protection_stays_counted`（RS-2）| 断言失败 → 缺陷不再成立 |

RS-2 探针的日志显示修复后每个冷却窗口都会重试一次 `runner_order_refused`——这正是期望行为：
覆盖不再虚高，修复器知道自己缺保护。

### 测试计数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/toolkit/test_strategy_state_and_order_protection.py` | 62 |
| `tests/test_plan_closeout_counts.py` | 41 |

上表合计 103 条。第一行 fix 09 认领时是 54。第二行由本 plan 的表格从 39 推到 41。

### 扰动验证

- 去掉 `notional_from_quantity` 的乘数换算：RS-1 的三条参数化测试转红（multiplier=1 一条仍绿，
  正是它掩盖了这个缺陷两年的原因）。
- 去掉 `_dispatch_protection` 的 `remove_order` 回滚：RS-2 的四条测试转红。

### 后续项（本 plan 未承诺）

报告在 RS-2 里还提到「未知在途订单要有超时和查询确认」。那需要一套在途保护的超时对账机制——
知道一笔保护单既不在缓存、也没有回报、也没被本地拒绝时该怎么办。本 plan 只让**已知的**本地拒绝
不再虚增覆盖，超时对账仍是缺口。

### 功能验证（主路径）

1. 在 sandbox 用一个 multiplier 不等于 1 的线性合约（如 OKX 的 BTC-USDT-SWAP，multiplier 0.01）
   建立空仓，然后发一个反向做多信号。
2. 预期：提交的张数等于「平掉旧仓的张数 + 目标名义对应的张数」。修复前在 0.01 乘数下会多出两个
   数量级。
3. 让本地安全门拒绝一笔保护单（例如用带连字符的 client order id 触发 OKX ID 校验），观察日志出现
   `refused locally before dispatch ... coverage released`，且下一个修复窗口会重新尝试补单。
