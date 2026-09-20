# 12 - supertrend-continuity

> **Status**: ⏳ In Progress
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

- [ ] 对照测试先红后绿（红是因为当前实现与完整历史不符）
- [ ] 审查方探针 `supertrend_window_restart_flips_trend` 不再成立
- [ ] 既有 supertrend 测试仍绿
- [ ] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 递推替换窗口重算 | P1 | 🔲 | | RS-9 |

## 偏离与改进日志
