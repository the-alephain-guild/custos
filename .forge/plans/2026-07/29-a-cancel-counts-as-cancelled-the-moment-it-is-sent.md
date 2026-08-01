# 29 — 撤单发出即当作已撤销：这条通道没有确认回路，孤儿止损单逐轮累积

> **Status**: 🔲 Not started
> **Created**: 2026-08-01
> **Project**: custos (`tesseract-trading/custos/`)
> **Depends on**: 无 —— 现有代码即可复现
> **Blocks**: 无人值守长跑（每重启一轮就多留一张 resting reduce-only 挂单）；Plan 28 的熔断 flatten
> 想要的那个「确认回路」也在这里
> **For Claude**: `/forge:execute`；**先定 §决策 的修复层级再动手**
> **multi_session_scope**: false

## 上下文 (Context)

### 观察到的事实

2026-07-30，启动时交易所侧**同时挂着 4 张** `0.0070` 的 reduce-only 止损单
（`17fb6e05` / `5dbb46ff` / `4ccc9459` / `a39769df`），分别来自前四轮，而持仓只有 `0.0070`。
每一轮都往交易所多留了一张。这条记录来自 Plan 28 §Follow-up，本 plan 是它点名的那份独立 plan。

**为什么这不只是脏数据。** reduce-only 挂单的累计数量超过持仓就会让新的 reduce-only 请求被拒
（本仓 lesson #13 记的机制），也就是说孤儿单会**把平仓能力吃掉**；而现有代码里"先撤单再平仓"这条
`-2022` 缓解措施，恰恰依赖撤单真的生效。撤单不生效，缓解措施就是空的。

### 三条路径各自会留下孤儿，缺口不同

| # | 路径 | 代码 | 缺口 |
|---|---|---|---|
| A | 关停 | `trading_strategy.py:548` `on_stop` 里 `cancel_all_orders(ctx.instrument_id)` | fire-and-forget，**不等确认**；容器 10s 后被 SIGKILL |
| B | 反转 | `signal_execution.py:145` / `order_reconciler.py:408` `cancel_all_orders` | 传输失败时 **NT 不给策略发任何事件**（该文件 `:283-287` 的 docstring 自己写了这一点）|
| C | 兜底 | `order_reconciler.py:280 sweep_stale_orders_for_pair` | **bar 驱动**：`strategy_core.py:307 on_bar → _on_bar_risk_hygiene`。行情断了它就不跑 |

A 的时间账要算清楚：`deploy/custos/docker-compose.yaml` 的 `custos-runner` **没有设
`stop_grace_period`**，所以是 docker 默认的 10 秒，之后 SIGKILL —— Plan 27 §Follow-up 记的
`exit 137` 就是它。10 秒里发得出撤单请求，但没有任何东西**等**它被确认。

### 兜底本来应该兜住，所以要问的是它为什么没兜住

PS lesson #17 的修复已经在 toolkit 里了 —— 每根 bar 对账一次开放挂单，把策略不再认领的 reduce-only
单撤掉。而且按 `is_stale_order`（`orders.py:238`）的规则，那 4 张里被启动恢复认领 1 张之后，**另外
3 张恰好落进"已跟踪 SL 时的同侧 STOP_MARKET → 重复 → 撤"这一条**（`orders.py:287-291`）。逐条核过：
reduce-only ✓、不在 tracked_ids ✓、超过 30s 最小时龄 ✓、有持仓所以不走"无仓即孤儿" ✓、
BUY 是空仓的保护侧所以不走"反向即孤儿" ✓ —— 命中的正是重复那一条。

**也就是说：兜底的判定逻辑是对的，没生效的是别的东西。** 三个候选，需要实证区分，不要先挑一个动手：

1. **那几轮里没有 bar 到达。** 2026-07-31 实测过一段 **11 分钟零 bar** 的窗口（交易所降级期间），
   零 bar 就零 sweep。
2. **撤单请求发出了但没在交易所生效。** 这就回到 A / B 的确认缺口 —— 而这恰好也是最难证伪的一个，
   因为本地看到的 `OrderCanceled` 不等于交易所侧真的撤了。
3. **时间窗没走完。** 30s 最小时龄 + per-order 120s 撤单冷却（`orders.py:234-235`），
   把有效的撤单推到了进程结束之后。

### 还有一处：撤单被拒一律被读成"这单已经没了"

`handle_order_cancel_rejected`（`order_reconciler.py:451`）的注释写的是「filled, expired, or already
cancelled」，然后**只清理 entry order 的 tracker**。两个问题：

- 它把「撤单被拒」**一律**当成「订单已不存在」。撤单被拒也可能是交易所侧出问题 —— 那张单还在。
- SL / TP 的撤单被拒**什么都不做，也不记**。没有异常可抓，没有人设防 —— **C9 的字面形状**
  （"fail-closed 守的是'错了'，不是'不吭声'"）。

### 现在查不了的部分（诚实标注，别让下一个读者以为已经查过）

容器与日志都已清理。`deploy/custos/runtime/` 下只有 `deployment.json` 与 `.arx`，**没有留存日志，
也没有 NT 缓存库** —— 后者顺带证实了每轮都是空缓存起步，那 4 张单是交易所报回来的，即确实还挂着。

但「前几轮日志里的 `OrderCanceled` 到底对应哪几张单」**现在无法回溯判定**。所以上面三个候选原因
谁是主因，本 plan 起草时**没有实证**。这不是可以靠推理补上的空缺 —— 见 Task 1。

## 决策 (Decisions)

### 需要 owner 定：修到哪一层

- **选项 1 —— 让关停等撤单确认（有界超时）。** 最小。但 SIGKILL 会打断它，所以单做它价值有限，
  且要同时给 compose 配 `stop_grace_period`，否则等待窗口本身就没有时间。
- **选项 2 —— 给撤单一个确认回路。** 记下已请求撤销的单，直到 `OrderCanceled` 到达或超时；超时按
  C9 的要求记成 **containment/cancel unconfirmed**，不静默。这是治本的一层，也是 Plan 28 熔断
  flatten 想要的那个东西。
- **选项 3 —— 让 sweep 脱离 bar 驱动。** 改用定时器（NT 有 `clock.set_timer`），行情断了照样跑。
  这直接消掉候选原因 1。

**推荐 2 + 3。** 理由：2 让"发出去了"和"生效了"不再是同一件事，3 让兜底不再依赖一个可能停掉的输入。
1 可以顺带做（`stop_grace_period` 是一行配置），但不要单独做它就宣布修好。

⚠️ **但先做 Task 1。** 上面三个候选原因没有实证，直接按"推荐"动手就是凭推理修 bug。

### 一条要小心的反作用

撤单确认回路会**延长关停**。lesson #34 的判据在这里适用：best-effort 层整段要被
`wait_for(总预算)` 硬上限住，兜底动作放 `finally`，绝不能让"等撤单确认"变成一个会挂住关停的层。
这也是选项 1 必须和 `stop_grace_period` 一起考虑的原因 —— 两个超时要对得上，否则等待窗口会被
SIGKILL 从外面切断。

## 目标 (Goal)

一次撤单请求的结果是**已知的**：要么确认撤销，要么在记录里明确说未确认。重启若干轮之后，交易所侧
不再累积无人认领的 reduce-only 挂单。

## 非目标 (Non-goals)

- **不改 `is_stale_order` 的判定规则** —— 逐条核过，它对那 4 张单的分类是正确的。问题在它没被调用，
  或在它的撤单没有生效。
- **不给策略层加 REST 客户端** —— lesson #14 的权威查询（`positionRisk` / `openOrders`）在策略层拿不到，
  这条边界与 Plan 28 一致，不在本 plan 打破。
- **不顺手改 demo/testnet 端点配置** —— 与 Plan 28 同一条：值得单独判断，不在这里。
- **不处理 testnet 上现存的那个空仓与它的挂单** —— 见 Plan 27 §Follow-up。本 plan 修的是不再产生新的，
  不是清理旧的。

## 实现任务 (Tasks)

### Task 1 — 先让证据可得，再谈修哪一个

三个候选原因当前无法区分，因为日志不留存。先做两件成本很低的事：

1. **留存 runner 日志**（compose 侧落盘或 `docker logs` 导出到 `deploy/custos/runtime/logs/`），
   下一轮长跑结束后可回溯。
2. **给撤单请求与 `OrderCanceled` 打成对的结构化记录**（各带 `client_order_id`），使"发了几张、
   确认了几张"可以直接数出来，而不是靠读散文日志推断。

**不要跳过这一步直接实现选项 2 + 3。** 跳过 Task 1 会让 close-out 只能写"改完了，没再看到孤儿" ——
而在一个本来就间歇的现象上，那不构成证据（与 Plan 28 §验证清单 末项同一条纪律）。

### Task 2 — 先写会红的测试

1. 撤单请求发出后 `OrderCanceled` **未到达**，超时后必须产生一条明确说"未确认"的记录，
   **不得**静默，也不得写成已撤销。
2. 撤单被拒（`OrderCancelRejected`）且订单是 SL / TP 时，**当前什么都不做** —— 先锁住这个现状，
   再断言修复后它至少被记录。
3. sweep 在**没有任何 bar 到达**的情况下仍会运行（选项 3 落地后）。这条在实现前必然是红的，
   因为现在它只由 `on_bar` 驱动。
4. 关停路径：撤单未确认时，等待受**总预算**约束，且兜底动作照常执行（lesson #34 的判据）。

### Task 3 — 落 owner 选定的层级

按选项实施。若含选项 1，`stop_grace_period` 与等待预算必须一起定，并在 plan 里写下两个数字的关系。

### Task 4 — 两个 host 都要被测到

`--engine nautilus` 与 `--engine sandbox-sim` 各自断言。sandbox 不连交易所、撤单不会失败，
所以它的断言是**不回归**，不是"也有确认回路"。（同 Plan 26 / 27 / 28 Task 5 的要求。）

## 验证清单 (Verification)

- [ ] `make verify` 全绿（注意 C6 记录的既有 `fmt-check` 恒红：3 个被 `docs/authority/**` 按字节 pin
      住的文件 —— `src/custos/core/runner_fact.py` + 2 个 integration test。实施前先测基线，只对照增量）
- [ ] Task 2 的四条测试修复前红、修复后绿（两侧输出都记进 close-out）
- [ ] **撤单未确认时记录明确说"未确认"** —— 单独列项，这是 C9 在本条路上的兑现
- [ ] **关停不被撤单等待挂住** —— 单独列项，lesson #34 的判据（`wait_for(总预算)` + `finally`）
- [ ] sandbox 路径有测试钉住不回归，且本 plan 未改动它
- [ ] **真机证据**：连续重启 ≥3 轮后，交易所侧 resting reduce-only 挂单数**不随轮数增长**。
      注意这条要在**行情正常**的时段取 —— 行情断掉时 sweep 本来就不该被指望（除非选项 3 已落地，
      那正好是它的判据）。**不得**因为"跑了一轮没看到孤儿"就宣布验证通过

## Follow-up hooks（不属于本 plan scope，登记以防遗漏）

- **Plan 28 的熔断 flatten 需要同一个确认回路。** 它调 `close_all_positions_with_fallback` 之后不
  确认任何东西，且交易所的拒单是异步事件、单次同步遍历看不到 —— 与撤单同一个形状（"发出去了"
  被当成"生效了"）。本 plan 若做选项 2，设计时应让 containment 的确认能复用同一层，而不是各写一套。
- **`handle_order_cancel_rejected` 的语义需要一次单独判断。** 把"撤单被拒"读成"订单已不存在"在
  entry order 上大概率成立，在 SL / TP 上没有依据。改它会动到 tracker 的清理时机，范围比本 plan 大。
- **testnet 上现存的孤儿单与空仓** —— Plan 27 §Follow-up。本 plan 不清理它们。
