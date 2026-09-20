# 10 - contract-units-and-protection-registration

> **Status**: ⏳ In Progress
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

- [ ] 两项失败测试先红后绿，经扰动验证
- [ ] 审查方探针 `contract_reversal_oversizes` / `refused_protection_stays_counted` 不再成立
- [ ] `make verify` 与 `make verify-nt` 均 exit 0
- [ ] 无真实账户、下单或生产操作

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 反向 sizing 乘数 | P1 | 🔲 | | RS-1 |
| 2 保护登记与派发一致 | P1 | 🔲 | | RS-2 |

## 偏离与改进日志
