# 16 - plain-close-cannot-reverse

> **Status**: ⏳ In Progress
> **Created**: 2026-09-20
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-execution-edge-deep-review.md` EE-1
> **红线**: 触及红线 0.2「执行门不绕过」。本 plan 是**收紧**该门，不是放宽。

## 根因（两处，必须一起修）

**其一，减仓判定不扣在途量。** `order_is_risk_reducing` 判一笔非 reduce-only 的订单是否"在减仓"
时，只比较**这一笔**的数量与缓存里的持仓量。已经获准、尚未成交的平仓量不在扣减之列。于是持仓 1
时两笔各卖 1 的普通平仓都被判为减仓，双双跳过冻结检查与预留——第一笔平掉多仓，第二笔开出空仓。

**其二，`close_position` 根本不在门后。** `install_order_gate` 只包了 `submit_order`、
`submit_order_list`、`modify_order`、`market_exit` 四个方法。原生的 `close_position` /
`close_all_positions` 是另一条出站路径，没有被包住——只修判定而不补这条路，缺口照旧。

**原生引擎复现**（审查方，真实 BacktestEngine + 真实门）：breaker 已冻结、缓存持多仓 1，同一回调
内发两笔 `SELL 1, reduce_only=False`，两次都返回 True、两次都没碰 reservation store，撮合后实际
持仓是 **SHORT 1**。改用 `reduce_only=True` 的对照组最终持仓为空。

## 修复任务

### Fix 1: 普通平仓要占用剩余可平数量 [P1]

**Files**: `src/custos/engines/nautilus/runner_safety.py`、测试

1. 先写失败测试：冻结状态下连发两笔普通平仓，第二笔必须被拒；最终净仓不得翻向。
2. 门按 instrument 记住**已获准尚未终结**的普通平仓数量；减仓判定用「持仓量 − 在途平仓量」。
3. 订单终结（成交完成、取消、拒绝）时释放对应的在途量——不释放会让后续正当的平仓被永久挡住。

### Fix 2: `close_position` 与 `close_all_positions` 也走门 [P1]

**Files**: `src/custos/engines/nautilus/runner_safety.py`、测试

1. 先写失败测试：装好门之后连续两次 `close_position(position, reduce_only=False)`，最终净仓
   不得翻向。
2. 两个方法纳入 `install_order_gate` 的包装清单，与既有四个同样处理。
3. 安装日志里的 `methods` 列表随之更新——那个列表是运维看「门包住了什么」的唯一凭据，漏一项就是
   假的完整。

**验收**（报告原文）：普通平仓也需要可串行化的剩余可平数量归属，订单终态后释放；穷举原生
close_position/close_all_positions 出站路径。测试两笔普通平仓、普通平仓与止损竞争、部分成交后
补单，以及冻结期间最终净仓不得翻向。

## 验证清单

- [ ] 两项失败测试先红后绿，经扰动验证
- [ ] 审查方探针 `duplicate_plain_closes_open_reverse_while_frozen` 不再成立
- [ ] 红线相关测试全绿（`tests/engines/nautilus/test_runner_safety_execution_boundary.py` 等）
- [ ] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 在途平仓量归属 | P1 | 🔲 | | EE-1 判定 |
| 2 close_position 入门 | P1 | 🔲 | | EE-1 出站路径 |

## 偏离与改进日志
