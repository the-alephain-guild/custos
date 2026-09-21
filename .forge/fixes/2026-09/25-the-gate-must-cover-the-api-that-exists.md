# 25 - the-gate-must-cover-the-api-that-exists

> **Status**: 🔲 Not started
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

- [ ] 失败测试先红后绿
- [ ] 审查方探针 `native_price_type` 与 `native_batch_modify_bypasses_frozen_boundary` 不再成立
- [ ] 覆盖面测试**从原生 API 推导**，NT 新增出站方法时会转红
- [ ] RR-1/RR-2 的测试用真 instrument / 真 ClientOrderId，不用替身
- [ ] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 枚举覆盖面 + 包 modify_orders | P1 | 🔲 | | RR-3 |
| 2 构造合法 Price | P1 | 🔲 | | RR-1 |
| 3 按 ID 转发改单撤单 | P1 | 🔲 | | RR-2 |

## 偏离与改进日志
