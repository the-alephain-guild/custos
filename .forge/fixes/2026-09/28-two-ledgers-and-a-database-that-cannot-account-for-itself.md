# 28 - two-ledgers-and-a-database-that-cannot-account-for-itself

> **Status**: 🔲 Not started
> **Created**: 2026-09-21
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-fix03-recheck-review.md` RR-6 / RR-7 / RR-9

三项都是 P2，但凑成一份而不是三份，因为它们共享同一个形态：**同一笔钱或同一份归属，在两处
各说各话，而两处都没人对账**。

## RR-6：同一笔手续费在 OKX 的输出里出现两次

`okx_ledger.py:158` 算 realized PnL 时做的是 `pnl + fee`。OKX 的 `positions-history` 把这两件事
分开给：`pnl` 是不含手续费的毛利，`fee` 是该仓位累计的手续费（负数表示扣款）。现有测试
`test_okx_position_history_paginates_by_time_and_excludes_funding` 喂 `pnl=12 / fee=-2` 并断言
输出 `10` —— 净值。

问题不在 Crucible 怎么算，在 custos 自己：**同一个窗口里，那笔 fee 已经作为独立的 commission
行发过一次了**。`okx_ledger.py:299-307` 对 `fills-history` 的每一笔成交各发一条
`kind="commission"` 的 fee 行，而 `positions-history` 的 `fee` 正是这些成交手续费的累加。开平仓
落在同一个采集窗口时，这笔钱在 `fees` 列表里出现两次：一次藏在 realized PnL 里，一次是它自己。

Binance 走的是另一条路：`binance_ledger.py:433` 直接取 venue 的 `REALIZED_PNL` income，而
Binance 把 `COMMISSION` 作为独立 income 类型 —— 毛值。所以 OKX 是三个 venue 里唯一折算净值的。

审查报告把它记成「消费端再扣一次」，那是它在跨仓层面的症状；**根在生产端重复表达**，而这一半
custos 有权也有义务自己修。消费端那条统一公式（`gross_realized_pnl - commission`）归 Crucible
所有，本 plan 不碰、也不写任何需要改 Crucible 才能满足的验收条款（C14）。

### Fix 1: OKX 的 realized PnL 只报毛值 [P2]

**Files**: `src/custos/engines/nautilus/okx_ledger.py`、
`tests/test_independent_venue_ledgers.py`

1. `pnl` 不再加 `fee`。手续费已经有自己的行，realized PnL 只表达毛利。
2. 既有测试断言 `"10"`，会转红 —— 那正是它该做的事：它钉住的是即将被改掉的口径。改成 `"12"`，
   并在 docstring 里写清为什么是毛值（fee 另有其行），而不是只把数字换掉。
3. 新写一条**跨这两条路径**的测试：同一个窗口里既有 `positions-history` 的平仓、又有
   `fills-history` 的成交，断言该笔手续费在整个 `fees` 列表里**只出现一次**。缺了这条，下一个人
   把 `+ fee` 加回去照样全绿。

**验收**（报告原文）：生产端与消费端统一明确的净/毛口径，避免给不同 venue 套用未经约定的扣费
规则；加入跨 venue fixture 到投影的验证。

拆成两句，各自落点不同：

| 验收分句 | 本 plan 的落点 |
|---|---|
| 统一明确的净/毛口径 | custos 三个 venue 一律毛值 + 独立 commission 行；OKX 从净改毛 |
| 跨 venue fixture 到投影的验证 | **本仓做不到，显式移交** —— 投影在 Crucible。本 plan 做到「跨这两条采集路径的 fixture」为止 |

## RR-7：旧库把「无法归属」推迟到第一次真实平仓

`runner_fact.py:1710` 的 `runner_position_exposure_lot` 是用 `CREATE TABLE IF NOT EXISTS` 建的，
而它**跑在所有旧库形状检查之前**（`:1742` 起三处 `RunnerStateMigrationError`）。于是一个根本
没有这张表的旧库，被静默补上一张空表，随后 `:1766` 的 `side` 列检查在这张刚建的新表上自然通过。

「这张表原本不存在」这个唯一的证据，在有人去看它之前就被抹掉了。相邻那两处检查
（`filled_quantity` 列、`side` 列）之所以有效，恰恰是因为它们查的表在前代里**已经存在**，
`IF NOT EXISTS` 是空操作、旧列得以幸存。

后果：库带着 `filled_quantity > 0` 的预留正常打开，直到第一次带 `position_id` 的平仓走进
`:4828` 才报 `position reduction exceeds durable position lot quantity`，而按 C23「已执行事实不可
被准入规则回滚」，那是记账失败冻结，不是拒单。

**不能用「有 filled_quantity 但没有 lot 行」当判据。** 那是当前格式下的合法状态：
`:4734` 只在 `position is not None` 时建 lot，而 `order_reservation_boundary.py:298-310` 对没有
position id 的减仓走的是按 `source_order_id` 的另一条路。拿这个当判据会把健康的库拒在门外。

### Fix 2: 在建表之前记下哪些表原本就在 [P2]

**Files**: `src/custos/core/runner_fact.py`、`tests/test_a_database_that_cannot_account_for_itself.py`（新建）

1. `_initialize` 在 `executescript` **之前**读一次 `sqlite_master`，记下当时已存在的表名。这是
   唯一能观测到「lot 表原本不存在」的时刻。
2. 若 lot 表原本不存在，**且**库里已有 `filled_quantity > 0` 的预留，抛
   `RunnerStateMigrationError`，消息指名恢复路径（与既有三条同一体例：重建 pre-production 库）。
3. 原本不存在但没有未平敞口 —— 正常开。没有东西需要归属，拒绝它没有道理。
4. 三条测试各自独立：旧库有敞口（拒）、旧库无敞口（开）、当前格式的无 position id 成交（开，
   这条防的是上面那个「不能用的判据」被人重新捡起来）。

**验收**（报告原文）：从可信执行证据恢复归属，或在启动阶段拒绝带未映射持仓的旧库并给出明确
恢复流程；不能默默接受后再在真实成交后失败。

是「或」，选后者。前者做不到：把已有的成交数量归到某个仓位，需要的正是旧库里不存在的那个
position id —— 没有任何可信证据可供恢复，凭空指派一个比拒绝更糟。这个选择要写进 close-out，
不靠读者自己推断。

## RR-9：同一笔手续费，两行说的是两种币

`binance_ledger.py:395-405` 的 fills 行写 `fee=commission` 但 `currency=quote`；同一笔的 fees 行
写 `currency=commissionAsset`。`commissionAsset=BTC` 而 quote 是 USDT 时，同一个数字被贴上两种
币 —— 签名成交记录里那一行是错的。

`fee_currency` 不是新字段：`runner_fact.py:641` 已接受它，`:661` 只在它与 `currency` 不同时才写进
wire，`docs/gateway-contract/v1/runner_fact_batch_v1.schema.json:288` 有它的定义，OKX
（`okx_ledger.py:296`）与 SoDEX（`sodex_ledger.py:266`）都在用。**只有 Binance 没填**。因为只在
不同时才落，填上它对 quote==commissionAsset 的绝大多数成交不改变任何字节。

### Fix 3: Binance 成交行带上手续费的真实币种 [P2]

**Files**: `src/custos/engines/nautilus/binance_ledger.py`、
`tests/engines/nautilus/test_binance_ledger_economic_rows.py`

1. fills 行加 `"fee_currency": commission_currency`。
2. 测试断言**序列化之后**的成交行（报告原文：「验证 venue snapshot 序列化后的成交行，而不只
   测试独立费用列表」），因为 `fee_currency` 的落盘条件在 `runner_fact.py:661`，只测 dict 不经过
   那一步。
3. 同时断言 commissionAsset == quote 时 wire 上**没有** `fee_currency` —— 否则这条改动会悄悄给
   所有既有成交行加一个字段。

## 验证清单

- [ ] 三项各自失败测试先红后绿
- [ ] RR-6 有一条跨「平仓 PnL」与「成交手续费」两条采集路径的 fixture，断言手续费只出现一次
- [ ] RR-7 的判据不会拒绝「当前格式 + 无 position id 成交」的健康库（单独一条测试守）
- [ ] RR-9 的断言落在序列化之后，且 quote==commissionAsset 时 wire 不变
- [ ] 每项修复都做扰动验证，各用独立 `PYTHONPYCACHEPREFIX`（C15）
- [ ] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 OKX 毛值口径 | P2 | 🔲 | | RR-6 |
| 2 旧库归属缺口 | P2 | 🔲 | | RR-7 |
| 3 Binance 手续费币种 | P2 | 🔲 | | RR-9 |

## 偏离与改进日志
