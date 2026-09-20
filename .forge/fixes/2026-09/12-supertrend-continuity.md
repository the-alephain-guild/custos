# 12 - supertrend-continuity

> **Status**: ✅ Completed
> **Completed**: 2026-09-20
> **Created**: 2026-09-20
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-risk-state-deep-review.md` RS-9

## 根因

SuperTrend 是路径依赖的：每根的上下轨要与**前一根已收敛的轨**比较后夹逼，方向要延续前一根的
方向，而 ATR 本身是 Wilder 递推。当前实现把最近 `length + 50` 根塞进 deque，每根用
`ta.supertrend` 对这个滑动窗口重算一次——窗口滚动时递推的起点跟着滚动，于是历史上早已确定的
方向会被重新"推导"出来。

**复现**（审查方）：length=10、multiplier=3；15 根 200，逐根降 5 到 100，再横盘 100 根。实际类在
索引 80 从空头翻成多头，而这段时间价格一直是 100；同一个 pandas-ta 在**完整历史**上算，索引 80
与末尾都是空头。不是版本差异，也不是浮点容差。

扩大窗口不是修复：它只是把错误推迟到更长的横盘。

## 修复任务

### Fix 1: 用真正的递推替换滑动窗口重算 [P1]

**Files**: `indicators/supertrend.py`、`tests/toolkit/test_supertrend_continuity.py`

1. 先写对照测试：拿 vendored pandas-ta 在**完整历史**上的结果做基准，逐根比较方向与值。
   覆盖审查方那条序列、随机游走、长横盘、`high == low` 退化。
2. 递推实现，状态是：Wilder ATR 的分子/分母与样本数、前一根收盘、前一根**夹逼后**的上下轨、
   前一根方向。
   - ATR 用 `ewm(alpha=1/length, adjust=True)` 的语义：`num = tr + (1-α)·num'`、
     `den = 1 + (1-α)·den'`、`atr = num/den`，样本数够 `length` 前为 NaN。
   - 夹逼比较的必须是**前一根夹逼后**的轨，不是原始轨——pandas-ta 在循环里原地改写，等价于此。
3. 不再保留价格 deque：递推不需要历史，留着只会让人以为还能重算。
4. 公开接口（`trend` / `value` / `upper_band` / `lower_band` / `initialized` / `handle_bar` /
   `update_raw` / `reset` / `load_snapshot` / `export_snapshot`）一律不变。

**验收**（报告原文）：采用与目标算法一致的持续递推状态，持久化所需 ATR/轨道/方向；不要仅扩大
窗口。以完整历史为参考，比较每根方向，覆盖长时间单边、横盘、窗口移出拐点和重启延续。

## 预研已完成

递推公式已先于实现验证过，与 vendored pandas-ta 完整历史结果**逐根一致**（方向精确相等，值
`rtol=atol=1e-9`）：审查方序列 10/3 与 7/3、三条随机游走（n=150/300/500，参数 7/1.5、10/3、
14/2）、以及 200 根 `high == low` 的退化序列。公式不对就没有集成的意义，所以先验证再动手。

## 验证清单

- [x] 对照测试先红后绿（7 项红 → 11 项全绿）
- [x] 审查方探针 `rolling_supertrend_invents_reversal` 不再成立
- [x] 既有 supertrend 测试仍绿（snapshot 7 项未改一字）
- [x] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 递推替换窗口重算 | P1 | ✅ | 2026-09-20 | RS-9，含快照格式升版 |

## 偏离与改进日志

### IMPROVEMENT: 快照格式升到 version 2，改存递推状态
- **原因**: 原快照存的是价格窗口（`highs`/`lows`/`closes`）。递推实现下它**无法**还原指标——重放
  一段窗口等于让递推从那段窗口的第一根重新开始，正是本轮要消灭的缺陷。审查方的验收里也写了
  「持久化所需 ATR/轨道/方向」与「重启延续」。
- **决定**: `SNAPSHOT_VERSION` 1 → 2，state 改存 Wilder 分子/分母/样本数、前收、前一根夹逼后的
  上下轨、前一根方向。version 1 的快照按既有的版本校验被拒绝（抛 `ValueError`），策略重新预热——
  拒绝比静默重放安全。
- **测试**: 中途保存/恢复后继续喂 bar，结果与一次性喂完全等（`rel=1e-12`）。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-20
- **总 Task 数**: 1（+1 项必要的快照升版）
- **偏离数**: 0（1 项正向改进）
- **验证结果**: 全部通过
- **实施 commit 范围**: `b187945`（plan 自身 `505a51f`，早于实施）
- **契约影响**: 指标快照格式 v1 → v2。消费方是 `strategy_core.py:274` 与
  `trading_strategy.py:378` 的 `from_snapshot`，两者都走版本校验，旧快照被拒后走正常预热路径。
  非跨仓契约，`make check-authority` 通过。
- **红线守护**: 四条红线全数守住。本项修的是信号正确性。

### 先验证公式，再动手

递推公式在实现之前就与 vendored pandas-ta 的完整历史结果比对过，逐根一致（方向精确相等，值
`rtol=atol=1e-9`）：审查方序列 10/3 与 7/3、三条随机游走（n=150/300/500，参数 7/1.5、10/3、
14/2）、200 根 `high == low` 的退化序列。公式不对，集成就没有意义。

### 用审查方自己的探针验收

| 探针 | 结果 |
|---|---|
| `rolling_supertrend_invents_reversal`（RS-9）| 断言失败 → 缺陷不再成立 |

### 测试计数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/toolkit/test_supertrend_continuity.py` | 11 |
| `tests/test_plan_closeout_counts.py` | 45 |

上表合计 56 条。第一行是本轮新建。第二行由本 plan 的表格从 43 推到 45。

### 覆盖的场景（对应验收「长时间单边、横盘、窗口移出拐点和重启延续」）

| 场景 | 测试 |
|---|---|
| 长时间单边 + 横盘 | `test_the_review_sequence_never_flips_on_a_flat_price` 等 4 项 |
| 随机游走逐根 | `test_a_random_walk_agrees_bar_for_bar` 三组参数 |
| 窗口移出拐点 | `test_a_turning_point_older_than_the_window_is_still_honoured`（拐点比窗口老 400 根）|
| 退化 bar | `test_a_degenerate_bar_where_high_equals_low` |
| 重启延续 | `test_a_restored_indicator_continues_the_same_series` |

### 功能验证（主路径）

1. 用一段「下跌后长期横盘」的行情跑一个 SuperTrend 策略（如 length=10、multiplier=3，跌到 100
   后横盘 100 根以上）。
2. 预期：横盘期间方向保持空头，不会凭空翻多。修复前在第 80 根左右会翻多并发出平空/开多信号。
3. 重启策略（触发快照保存与加载），确认恢复后的方向与不重启时一致。旧的 v1 快照会被拒绝并重新
   预热，日志可见版本不匹配。
