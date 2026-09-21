# 25 - the-gate-must-cover-the-api-that-exists

> **Status**: ✅ Completed
> **Created**: 2026-09-21
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-fix03-recheck-review.md` RR-1 + RR-2 + RR-3

## 这份报告此前完全没被处理

`2026-09-20-custos-fix03-recheck-review.md` 共 9 项（6 P1 / 3 P2），此前**零 fix plan、全仓零引用**。
本 plan 做其中一族三项；RR-4/5/8（熔断语义）与 RR-6/7/9（账务）另起。

报告基线是「`037dc26` 加当时**未提交**的 fix 03 改动」，距今 20 余 commit，所以下面每条都在 HEAD 上
重验过，证据不取自探针而取自真实 NT API。

## 三项是同一族：执行门对原生出站面的覆盖不完整

红线 0.2 是「执行门不绕过」。`install_order_gate` 的 docstring 写着：

> Put the gate in front of **every way** this strategy can reach the venue.

这句话目前不成立。实测 NautilusTrader 2.0 的 `Strategy` 公开出站动作与我们的覆盖：

| 原生方法（实测签名） | 是否被包 |
|---|---|
| `submit_order(order, ...)` | ✅ |
| `submit_order_list(order_list, ...)` | ✅ |
| `modify_order(client_order_id, quantity=None, price=None, trigger_price=None, ...)` | ✅ 但**参数形态读错** |
| `modify_orders(updates, ...)` | ❌ **完全没包** |
| `market_exit()` | ✅（直接拒） |
| `close_position(position, ...)` | ✅ |
| `close_all_positions(instrument_id, ...)` | ✅ |
| `cancel_order` / `cancel_orders` / `cancel_all_orders` / `cancel_gtd_expiry` | ❌ 未包（见下「范围」）|

> 顺带澄清一条：`post_market_exit` 看起来像 `market_exit` 的兄弟，实际是 NT **回调进策略**的钩子
> （`crates/trading/src/strategy/mod.rs:1662-1665`「Called after a market exit has completed」，
> 由 `finalize_market_exit` 调用），不是出站动作，**不是缺口**。

### RR-1 — 金额计算给原生接口传了 Decimal

`runner_safety.py:237-251` 的 `_order_price` 用 `_positive_decimal` 把价格规范化成 `Decimal`；
`:263-275` 的 `_instrument_notional` 又把它交给 `instrument.notional_value(quantity, price)`。

**实测**（真实 `TestInstrumentProvider.btcusdt_perp_binance()`）：

```
native Price/Quantity -> 100.00000000 USDT
Decimal price         -> TypeError: 'Decimal' object is not an instance of 'Price'
```

所以任何**非 quote-quantity 且带价格**的订单在风险边界上会抛 TypeError。现有测试发现不了，
因为它们的假 Instrument 接受 Decimal。

### RR-2 — 改单与撤单按旧的「传 Order 对象」形态调用

`modify_order` 与 `cancel_order` 在 2.0 收的是 **`ClientOrderId`**（上表签名为实测）。而：

- `runner_safety.py:317-326` 的 `_ModifyIntent.__init__` 做 `self.client_order_id = order.client_order_id`
  —— 传进来的若是 `ClientOrderId`，这里 AttributeError。
- `host.py:1197` 的 preserve 停机做 `canceler.cancel_order(order)` —— 传的是整个 `Order`。

后果：上一轮的减仓改单修复只在「订单对象替身」上成立；默认 preserve 停机遇到开仓挂单时也完不成，
新 generation 的有序替换因此可能失败。

### RR-3 — 批量改单完全绕过门

`modify_orders(updates, ...)` 在原生层直接构造批量修改命令，不经过 `modify_order`。
**`batch_modify` / `modify_orders` 在本仓 `src/`、`packages/`、`tests/` 全部零命中** —— 这个面从未被认识过。

报告的实证：真实 BacktestEngine，冻结同一个 `RunnerReservationBoundary`，装上实际 gate，
调用 `modify_orders` 后原生缓存订单 ACCEPTED、数量从 1 变 200，名义额超出总限额 150，
`before_modify_order` 调用次数 **0**，SQLite 预留仍为 1。

## 修复任务

### Fix 1: 覆盖面从原生 API 枚举出来，不靠人记 [P1 / RR-3]

**Files**: `src/custos/engines/nautilus/runner_safety.py`、`tests/engines/nautilus/test_the_gate_covers_the_native_api.py`

1. 先写失败测试：一条**从 `Strategy` 的公开属性枚举**出站动作、断言每一个要么被包、要么在显式的
   「不属于本门」清单里的测试。硬编码一份清单只能证明「有人曾经这么写过」（C7）。
2. 包上 `modify_orders`：批量修改要么整批过门（多腿预留必须原子），要么**显式拒绝**。
   报告给的是「覆盖或在支持前显式拒绝」，选后者——多腿预留的原子性是另一件事，
   在没有真实需求的情况下先把洞堵上比半做要诚实。
3. 拒绝走既有的 `_refuse` 路径，理由码与 `market_exit` 同族。

### Fix 2: 给原生接口构造合法 Price [P1 / RR-1]

**Files**: `src/custos/engines/nautilus/runner_safety.py`、测试

1. 先写失败测试：**真实** instrument + 真实 LimitOrder，`order_notional` 必须算出 100 而不是抛错。
2. 内部金额仍用 `Decimal`；调用 `notional_value` 前用 `instrument.make_price` / `make_qty`
   构造原生类型。
3. 测试必须用真 instrument —— 假 Instrument 接受 Decimal，正是它让这个缺陷活到今天。

### Fix 3: 按 ID 转发改单与撤单 [P1 / RR-2]

**Files**: `src/custos/engines/nautilus/runner_safety.py`、`src/custos/engines/nautilus/host.py`、测试

1. 先写失败测试：给 gate 传**真实 `ClientOrderId`** 调 `modify_order`，必须走完判断并转发；
   preserve 停机遇到开仓挂单必须能完成。
2. `_ModifyIntent` 从 ID 取风险语义（cache 查订单），不再假设入参是 Order。
3. `host.py` 的 preserve 撤单改传 `order.client_order_id`。

**验收**（报告原文，三条合并）：按原生 ID 接口转发，从 canonical cache 读取订单风险语义；同步审计
改单、撤单及其批量入口，增加真实原生调用测试。内部金额仍用 Decimal，但调用原生接口时保留或构造
合法 Price；增加原生现货和永续订单的 submit → reservation 验证。覆盖批量改单并保证多腿修改的预留
原子性，或在支持前显式拒绝该入口；按当前原生公开 API 枚举覆盖面。

## 范围

`cancel_order` / `cancel_orders` / `cancel_all_orders` / `cancel_gtd_expiry` 目前也不过门。
撤单不直接增加敞口，报告也没点名它们，**本轮不扩范围**——但 Fix 1 那条枚举测试会把它们列进
「显式声明不属于本门」的清单里，连同理由。声明出来的缺口和没人知道的缺口是两回事。

## 验证清单

- [x] 失败测试先红后绿 —— 实现前 4 红 2 绿，实现后 7 绿
- [x] 审查方两个探针不再成立，各自中止在核心缺陷断言上
- [x] 覆盖面测试从原生 `Strategy` 类推导，NT 新增出站方法时会转红
- [x] RR-1 用真 instrument + 真 LimitOrder；RR-2 用真 `ClientOrderId`
- [x] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 枚举覆盖面 + 包 modify_orders | P1 | ✅ | 2026-09-21 | RR-3 |
| 2 构造合法 Price | P1 | ✅ | 2026-09-21 | RR-1 |
| 3 按 ID 转发改单撤单 | P1 | ✅ | 2026-09-21 | RR-2（含 host preserve 那一半）|

## 偏离与改进日志

### 差点引入的二阶缺陷：拿掉 `intent.order` 让减仓快路径死掉

`_ModifyIntent` 原本带着 `order` 对象，而 `before_modify_order` 靠
`getattr(command, "order", None)` 走「这是减仓改单，不用预留」的快路径。我把 `.order` 拿掉之后，
那条路径直接失效：每一次改单都落到 `_require_risk_increasing_allowed()` 加预留查询，
一笔没有预留的减仓改单当场 KeyError。

按 RR-2 验收原文「**从 canonical cache 读取订单风险语义**」改：`before_modify_order` 现在用 id
去缓存里取订单。这也是更对的来源——引擎认为什么在挂着，比调用方递过来什么更可信。
扰动验证（把它改回 `getattr(command, "order", None)`）会红两条。

### 两条断言最初是空的

1. **`test_the_declared_coverage_is_what_actually_gets_installed` 第一版比较绑定方法**：
   `getattr(strategy, name)` 每次都造一个**新的**绑定方法对象，`is` 恒为 False，所以无论装没装
   它都「通过」。扰动（把 `modify_orders` 从安装列表里删掉）没咬。
   改成查 `vars(strategy)`——`install_hook` 做的是实例 `setattr`，装过的会出现在 `__dict__` 里，
   没装的还在类上。改完扰动立刻红。
2. 这是本轮第三次「断言存在但不成立」（前两次在 fix 20 与 fix 24）。共同形态：**断言的对象
   不是它自称在断言的那个东西**。

### 替身比真货宽松，是这三项能活下来的共同原因

改的替身有四处，逐条说明（均为让替身更忠实，不是改行为）：

| 替身 | 原来 | 现在 |
|---|---|---|
| `_GatedStrategy` / `_Strategy`（wiring） | 没有 `modify_orders` | 有——否则安装器的 fail-loud 会替我们把洞藏起来 |
| 假 `Instrument` | `notional_value` 接受 `Decimal` | 像真货一样只收原生 `Price` / `Quantity`，否则抛 `TypeError` |
| `_CacheWithPrice` 的 instrument | 同上 | 同上 |
| `_Downstream.modify_order` | 收 Order 并读 `.client_order_id` | 收 id |
| `_ShutdownAwareStrategy.cancel_order` | 收 Order | 收 id，传 Order 就抛 `TypeError` |

报告在 RR-1 里点了这件事：「假 Instrument 接受 Decimal 的测试无法发现这一问题」。

### 范围内未做、已声明

`cancel_order` / `cancel_orders` / `cancel_all_orders` / `cancel_gtd_expiry` 仍不过门。撤单不增加
敞口，报告也没点名。它们现在**列在 `_DELIBERATELY_UNGATED` 里并写明理由**——声明出来的缺口和没人
知道的缺口是两回事，而且新增一个原生出站方法会让枚举测试转红，逼下一个人做同样的判断。

顺带澄清一条查过但**不是**缺口的：`post_market_exit` 看着像 `market_exit` 的兄弟，实际是 NT 回调
进策略的钩子（`crates/trading/src/strategy/mod.rs:1662-1665`，由 `finalize_market_exit` 调用），
不是出站动作。枚举里按 `post_` 前缀排除，并写了理由。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-21
- **总 Task 数**: 3
- **偏离数**: 1 二阶缺陷（已修）+ 1 空断言（已修）+ 1 范围声明
- **验证结果**: 全部通过
- **实施 commit**: `ba47d68`（三项主体）· `1ab57e4`（RR-2 的 host preserve 那一半）
- **契约影响**: `modify_order` 包装器的第一个参数从 order 变成 client_order_id，与 NT 2.0 的真实
  签名对齐；`RunnerReservationBoundary` 新增 `cached_order`；`OrderSemantics` 实现新增
  `cached_order`。都是内部接口，无 wire / schema 变动。
- **C6 核对**: `runner_safety.py` / `host.py` / `order_reservation_boundary.py` 均非字节 pin。

### 红线 gate 满足度

| 红线 | code 覆盖 | runtime wire | defer | follow-up |
|---|---|---|---|---|
| 0.1 Key/KEK 不出进程 | N/A（未触及） | N/A | 无 | — |
| 0.2 执行门不绕过 | **本 plan 的主体**：批量改单曾完全绕过；金额计算对真实订单必抛 TypeError 从而在门上误拒；改单按旧签名调用则根本到不了判断 | `install_order_gate` 是宿主装门的唯一入口，覆盖面测试查的是**实际安装结果** | 撤单族已声明不在门内 | — |
| 0.3 失联 ≠ 停止 | preserve 停机现在能真正撤掉增险挂单并完成 | `_preserve_and_confirm_shutdown` 是 `stop()` 的默认路径 | 无 | — |
| 0.4 Decimal money math | 金额全程 `Decimal`，只在调用原生接口那一步构造 `Price`/`Quantity`，不经 float | 同上 | 无 | — |

### 测试条数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/engines/nautilus/test_the_gate_covers_the_native_api.py` | 7 |
| `tests/engines/nautilus/test_runner_safety_execution_boundary.py` | 38 |
| `tests/engines/nautilus/test_runner_safety_host_wiring.py` | 6 |
| `tests/test_nt_trading_node_host.py` | 40 |
| `tests/test_plan_closeout_counts.py` | 71 |

后三个文件本轮改了替身，按 progress-management 的规则重数。

### 扰动验证

| 扰动 | 结果 |
|---|---|
| 不再包 `modify_orders`（RR-3 原形态） | 2 红（含实际安装那条，空断言修好之后才咬） |
| 金额计算回到直接传 `Decimal`（RR-1 原形态） | 4 红 |
| 改单风险语义回到从入参读（RR-2 原形态） | 2 红 |
| preserve 撤单回到传 Order（RR-2 后半原形态） | 1 红 |
| 还原 | 全绿 |

每次扰动都用独立的 `PYTHONPYCACHEPREFIX`。

### 审查方探针现状

| 探针 | 结果 |
|---|---|
| `native_price_type` | 中止于 `:92` `raise AssertionError("expected Decimal passed to native Price API")` —— 它等的那个 TypeError 不再发生 |
| `native_batch_modify_bypasses_frozen_boundary` | 中止于 `:411` `assert order.quantity.as_decimal() == 200` —— 批量改单没能把数量改上去 |

### 功能验证（主路径）

1. sandbox 下下一笔**普通限价单**（非 quote-quantity）。修复前它会在风险边界上因
   `TypeError: 'Decimal' object is not an instance of 'Price'` 被拒；现在应正常预留并发出。
2. 对一笔挂单调 `modify_orders` 批量改数量：应被拒，日志出现
   `custos_runner_batch_modify_has_no_atomic_reservation`，交易所侧数量不变。
3. 挂一笔增险单，正常 `stop` runner：preserve 停机应把它撤掉并确认，而不是超时。
