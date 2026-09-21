# 23 - recovery-must-read-the-latest-bar

> **Status**: 🔲 Not started
> **Created**: 2026-09-21
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-lifecycle-boundary-deep-review.md` LB-3

## 根因

`adapter/coordinators/order_reconciler.py:105-106`，重启后恢复 tick 监视器时：

```python
bars = s.cache.bars(ctx.bar_type)
if bars:
    current_price = Decimal(str(bars[-1].close))
```

`bars[-1]` 取的是**缓存里最旧的一根**，不是最新的。

**上游实证**（不是推理）：NautilusTrader 的 `Cache::add_bar` 是 `bars.push_front(bar)`
（`crates/common/src/cache/mod.rs:4415`，`add_bars` 在 `:4444` 同理），而它自己的 doc 注释写着
**"Index 0 is the most recent"**（`:1567`、`:7816`）。所以 `[0]` 最新、`[-1]` 最旧。按默认
`bar_capacity`，那可能是几千根之前的价格。

**后果**：重启后追踪止损用一个陈旧价格去恢复状态。报告复现的形态是**漏激活**——真实行情已到 104
（超过 2% 激活线），恢复却喂 101，激活标记没置上，随后回落到 101.5 不触发退出；读对最新价则会触发。
反方向同样成立：若最旧那根恰好是个高点，恢复出的高水位就偏高，退出会在错误的位置触发。

**覆盖现状**：`grep bars tests/toolkit/test_strategy_state_and_order_protection.py` **零命中**——
这一行目前完全没有测试，连单根替身都没有。报告说「缓存只有一根 K 线的替身测试无法发现顺序错误」
是客气了。

**与 C26 的区分**（报告自己点明了）：fix 07 修的是 `observe()` 内部丢了 trailing 激活标记；
这一条是**调用方喂错了价格**。不是那个缺陷复发，两者独立。

## 修复任务

### Fix 1: 用说得清自己在做什么的 API 取最新 bar [P1]

**Files**: `adapter/coordinators/order_reconciler.py`、`adapter/runtime_types.py`、
`tests/toolkit/test_recovery_reads_the_latest_bar.py`

1. 先写失败测试：缓存里两根（最新在前），恢复后的 trailing 峰值必须来自**最新**那根。
2. 改用 `cache.bar(bar_type)` —— NT 的「最新一根」原生入口（`cache/mod.rs:7810` 返回
   `bars.front()`）。这比 `bars[0]` 好：它把「我要最新的」写进调用本身，不依赖读者记得
   deque 的方向。`runtime_types.Cache` 协议同步加这个方法。
3. **不改 `observe()` 内部**——报告明写「不要仅调整 observe 内部逻辑」，而且那里没有毛病。

### Fix 2: 验收点名的三种输入 [P1]

**Files**: 测试

| 分句 | 要断言什么 |
|---|---|
| 激活后回落 | 最新价已越过激活线 → 恢复后激活标记为真 → 回落到阈值下方触发退出 |
| 旧高价不属于当前持仓 | 缓存里有一根比最新高得多的旧 bar → 恢复出的峰值**不得**是它 |
| 行情缺失 | 缓存空 → 不炸、不留下半个状态 |

**验收**（报告原文）：通过原生 API 明确读取最新 bar，或按 ts_event 选择；增加至少两根原生缓存
bar 的恢复用例，覆盖激活后回落、旧高价不属于当前持仓及行情缺失。不要仅调整 observe 内部逻辑。

## 验证清单

- [ ] 失败测试先红后绿
- [ ] 审查方探针 `restart_uses_oldest_cached_bar` 不再成立
- [ ] 三条验收分句各有独立断言（C27）
- [ ] 至少一条用例喂两根以上的 bar —— 单根替身查不出顺序错误
- [ ] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 取最新 bar | P1 | 🔲 | | LB-3 |
| 2 三种输入 | P1 | 🔲 | | LB-3 验收 |

## 偏离与改进日志
