# 16 - plain-close-cannot-reverse

> **Status**: ✅ Completed
> **Completed**: 2026-09-20
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

- [x] 两项失败测试先红后绿
- [x] 审查方探针 `duplicate_plain_closes_open_reverse_while_frozen` 不再成立
- [x] 红线相关测试全绿（`tests/engines/nautilus/` 共 132 项）
- [x] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 在途平仓量归属 | P1 | ✅ | 2026-09-20 | EE-1 判定 |
| 2 close_position 入门 | P1 | ✅ | 2026-09-20 | EE-1 出站路径，采取拒绝而非放行 |

## 偏离与改进日志

### DEVIATION: 原生普通平仓采取「拒绝」而非「同样判定」
- **等级**: 低（在验收范围内，但做法是选择）
- **原因**: `close_position` 是编译方法（`method_descriptor`，无 `__code__`），实测确认它构造的
  订单不经过 `submit_order` 的 hook。要让它走同样的判定，就得从 position 合成一个等价 order
  （含唯一 client_order_id，用于在途登记），代价不小。
- **决定**: 参照既有的 `market_exit` ——reduce-only 的原生平仓**原样放行**（它不可能开仓），
  普通形式**拒绝**，让策略改走 `submit_order`（那条路已经被判定覆盖）。
- **判据**: 红线 0.2 要的是「门不被绕过」，拒绝是满足它的最小手段；而合法的平仓手段一个没少。

### DEVIATION: 一条既有断言声明 close_position 不该被门包住
- **等级**: 低
- **原因**: `test_cancelling_is_not_something_the_gate_can_refuse` 把 `close_position` 列进了
  「不得被包装」的清单。那正是 EE-1 推翻的假设。
- **决定**: 从清单移除，并把**为什么**写进 docstring——包住它不等于让它像 cancel 那样可被拒绝：
  reduce-only 的形式仍然原样通过。悄悄删掉一条断言等于抹掉它记录的判断。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-20
- **总 Task 数**: 2
- **偏离数**: 2（均为做法选择，已说明理由）
- **验证结果**: 全部通过
- **实施 commit 范围**: `7faf7b9`（plan 自身在其前）
- **契约影响**: `OrderSemantics` 协议新增 `order_instrument_id` / `order_quantity`，
  `order_is_risk_reducing` 新增可选参数。`install_order_gate` 的包装清单从 4 个增至 6 个——
  这是运维读「门覆盖了哪些路径」的唯一凭据，日志同步更新。`make check-authority` 通过。
- **红线守护**: 本项**收紧**红线 0.2（执行门不绕过）。原先有两条路可以绕过：普通平仓的判定盲区，
  与完全不在门后的原生平仓接口。

### 用审查方自己的探针验收

| 探针 | 结果 |
|---|---|
| `duplicate_plain_closes_open_reverse_while_frozen`（EE-1，**真实 BacktestEngine + 真实门**）| 断言失败 → 缺陷不再成立 |

### 测试计数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/engines/nautilus/test_plain_close_cannot_reverse.py` | 8 |
| `tests/engines/nautilus/test_runner_safety_execution_boundary.py` | 36 |
| `tests/engines/nautilus/test_runner_safety_host_wiring.py` | 6 |
| `tests/test_plan_closeout_counts.py` | 53 |

上表合计 103 条。第一行是本轮新建；中间两个文件本轮补了替身与修正了一条断言，按规则重新计数
认领；最后一行由本 plan 的表格从 51 推到 53。

### 一个值得记下的副产物

给两个策略替身补 `close_position` / `close_all_positions` 时，`install_hook` 是 **fail-loud** 的：
策略没有这个方法就抛 `StrategyHookUnsupported`，而不是静默跳过。这正是这类门该有的形状——
如果真实策略哪天少了一个出站方法，装门会当场失败，而不是留下一条没人看守的路。

### 功能验证（主路径）

1. 在 sandbox 让熔断器冻结（例如触发回撤上限），然后连续发两笔方向相反、非 reduce-only 的整仓
   平仓单。
2. 预期：第一笔通过（它确实在平仓），第二笔被拒，日志 `custos_runner_fallback_breaker_frozen`。
   修复前两笔都通过，最终留下一个反向仓位。
3. 直接调用原生 `close_position(position, reduce_only=False)`，预期被拒，日志
   `runner_native_plain_close_refused`；改用 `reduce_only=True` 则正常平仓。
