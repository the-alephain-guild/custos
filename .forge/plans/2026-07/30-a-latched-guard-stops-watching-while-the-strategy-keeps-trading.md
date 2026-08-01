# 30 — 熔断闩住后守卫就不看了，可策略还在交易

> **Status**: 🔲 Not started —— 只记录，不实施。修哪一层要先定，见 §决策
> **Created**: 2026-08-01
> **Project**: custos (`tesseract-trading/custos/`)
> **Depends on**: 无 —— 现有代码即可复现，且已在真机复现
> **Blocks**: 无人值守长跑；也让 Plan 22 的「本地敞口守卫」在闩住之后名存实亡
> **For Claude**: `/forge:execute`；**先定 §决策 的修复层级再动手**
> **multi_session_scope**: false

## 上下文 (Context)

### 真机时间线，2026-08-01

在测 `max_total_notional` 该设多少的时候撞见的。当时 spec 还没有 `risk_config`，守卫用的是
最严默认 $200：

```
02:44:57.127  notional_breach            对账接管来的 353 USDT 空仓 > 200
02:44:57.127  offline_exposure_guard_latched
03:25:00      ENTRY  size=449.24
03:36:00      ENTRY  size=449.17
04:21:00      ENTRY  size=449.11
05:24:00      ENTRY  size=449.12
```

闩住之后的 **2 小时 40 分**里，策略自己开了 4 笔仓，**每一笔都是那条把守卫闩住的上限的两倍多**，
而守卫一次都没有再看过。

### 为什么会这样，两处代码合起来看

`safety.py:299` 的 `run()` 在全部 watched 都闩住时 **return**：

```python
if self._watched and all(watched.latched for watched in self._watched.values()):
    _log.error("offline_exposure_guard_latched", spec_ids=sorted(self._watched))
    return
```

`evaluate_once()` 也会 `if watched.latched: continue` 跳过。而 `engine_safety.py:57-58` 的 flatten
**只在判定跳闸的那一次**发生：

```python
if verdict.tripped:
    await self._engine.flatten_positions(instance_id, verdict.reason or "fail_closed")
```

所以闩住 = 平一次 + 不再接受新 generation + 守卫循环退出。**策略进程没有被碰过**，它在引擎里继续
跑自己的信号。

### 缺的是那句推理，不是那段代码

`safety.py` 模块 docstring 把设计意图写得很清楚：

> A trip latches: the position is flattened once and no further generation is admitted,
> because flattening alone would be undone by the next generation the operator publishes.

这句话里藏着一个前提：**敞口只会经由「操作者发布新 generation」回来**。真机证据说这个前提是错的
—— 敞口是策略自己按信号开回来的，全程不需要任何 generation。于是「拒绝新 generation」这道闸门
拦的是一条当时没人走的路，而真正在放敞口进来的那条路没有闸门。

代码忠实实现了 docstring 说的东西；错的是 docstring 那句 *because*。

### 为什么这比「操作不便」严重

「latched」这个词读起来像**已控制住**。`make status` 也会给出 unhealthy，看上去像停了。实际是：
仓位在开、敞口在涨、守卫已经不在了 —— 一个**看起来比裸奔更安全**的裸奔。这正是 PS lesson #15
说的那种事：为消除盲区而生的保护机制，自身的失效路径复制了它要消除的那个盲区。

## 决策 (Decision) —— 先定层级再动手

三条路，代价和语义都不同：

| # | 做法 | 换来什么 | 代价 |
|---|---|---|---|
| A | 跳闸时把策略停掉（不只是平一次） | 敞口真的不再回来 | 「停策略」这个动作目前不在 `EngineSafetyPort` 上；且停掉 = 挂单也没人管了，与 Plan 29 的孤儿单问题耦合 |
| B | 闩住后守卫继续跑，敞口每次回到上限之上就再平一次 | 守卫名副其实；不需要新原语 | 变成平仓/开仓拉锯，可能反复吃手续费；要有节流（PS lesson #13 的 rate guard）|
| C | 闩住后至少继续**看**并持续告警，不再动手 | 最小改动，消除「静默」 | 敞口照样在涨，只是这次有人喊 —— 无人值守时等于没修 |

**倾向 B + C**：守卫的职责是「让敞口不超过上限」，跳闸后停止观察是把职责交还给了没人。B 需要
节流与「平不掉怎么办」的处理，那部分与 Plan 28 的 escape hatch、Plan 29 的确认回路是同一片地。
**但这三份 plan 会互相踩，顺序要先定。**

A 看起来最彻底，但它把「停策略」这件事引进安全面，而这条通道现在连撤单确认都还没有
（Plan 29），停下来留一地挂单未必比 B 安全。

## 复现 (Reproduce)

不需要特殊构造，把上限设到当前仓位之下即可：

```yaml
# philosophers-stone/deploy/custos/conf/supertrend/spec-override.yaml
risk_config:
  max_total_notional: 100        # 低于 10% 仓位
```

`make -C deploy/custos start-detached MODE=testnet`，等 `offline_exposure_guard_latched`，然后放着。
下一次趋势翻转时策略会照常开仓，日志里不会再有任何守卫的声音。

## 先不做什么

不在这份 plan 里动手。理由与 Plan 29 相同：Plan 28（平仓 escape hatch）、Plan 29（撤单确认回路）
和本 plan 都要改同一条平仓路径，各自单独实施会互相覆盖。**先定三者顺序，再开第一份。**
