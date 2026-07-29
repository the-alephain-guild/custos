---
title: "实盘执行始终受门控"
sidebar_position: 3
---

# 实盘执行始终受门控

在 Custos 让一个部署对接真实交易所运行之前，一组固定的条件必须成立。任何一项不成立，部署即被拒绝。这道门从不降级到更弱的模式，也从不静默接受。

机制细节见[实盘执行门](/zh-Hans/concepts/live-execution-gate)。本页讲的是：它为什么是一项**保证**而不是一个功能，以及你要怎么核实它确实成立。

## 它防的是哪种失败

Custos 随发布提供不止一个执行宿主。其中一个是模拟宿主：它接受部署、把本地生命周期完整跑一遍、但从不连接交易所 —— 这正是你演练注册与 reconcile 时想要的。

到了真实交易所模式，同样的行为就成了危险的那个。一个被路由到「不交易的宿主」的部署，从外部看和一个交易正常的部署毫无区别：状态显示 running，日志显示健康，而你要到对账时才发现一单都没发出去。

所以这道门选择拒绝，而不是降级。被拒绝的部署是响亮且可恢复的；被静默模拟掉的部署两者皆非。

## 实际校验了什么

七项条件，在引擎被构造之前于同一处完成。其中四项适用于所有部署，三项专门针对真实交易所与实盘模式。

| 条件 | 适用范围 |
|---|---|
| artifact 运行时能力为 `READY` | 全部模式 |
| 运行时模式与签名模式一致 | 全部模式 |
| 引擎声明支持该模式 | 全部模式 |
| 引擎声明支持签名指定的连接器 | 全部模式 |
| 凭据权限范围为 `trade_no_withdraw` | `testnet` · `live` |
| 该构建启用了实盘执行 | `live` |
| 携带签名的放行证据 | `live` |

其中两项值得强调，因为它们正是这项保证难以被绕开的原因。

**实盘能力由构建携带。**实盘执行默认关闭，只在消费最终镜像回执的组装根处被打开。它不是环境变量，也不是配置项，因此不是运维人员在压力下能打开的东西 ——
包括来自他人的压力。

**凭据范围被独立校验。**即便金库本身已经拒绝存入具备提币权限的凭据，这里仍会再拒一次。两个执行点，因为一个执行点永远只差一次失误就等于没有。

## 批准留在上游

实盘部署携带由 ARX 签发的放行证据。Custos 验证该证据存在且绑定到本部署，但不评判是谁批准的、有几个批准人，也不评判这次批准是否稳妥。

这个切分正是要点所在：Custos 拒绝在缺少「决策发生过」的证据时行动，同时对决策本身不持立场 —— 因此攻破 runner 并不能凭空造出一个批准，攻破批准路径也不能在没有一台愿意执行的 runner 的情况下抵达交易所。

## 核实

有意思的问题不是「这些检查是否存在」，而是它们究竟是**活的**还是死代码。一项永远不会触发的检查，在一份全绿的测试套件里，和一项永远通过的检查长得一模一样。

去读 `src/custos/core/engine_lifecycle.py` 里的 `_require_authorized_runtime`。这一个函数就是整道门 —— 不存在第二条准入路径，也没有任何调用方能绕过它抵达引擎构造。请自己核实，而不是采信本页的说法：一台开源 runner 的价值就在于主张与代码在同一个仓库里。
<!-- disclosure-ok: auditable source location, custos is open for exactly this -->

它所查询的宿主能力面在 `src/custos/engines/nautilus/host.py`。
`SandboxSimulationHost.supports_trading_mode` 只对 `sandbox` 返回真；
`NtTradingNodeHost` 三种模式全接受。正是这条声明拒绝了落到模拟宿主上的实盘部署 ——
宿主自己说明它能做什么，准入采信它，而不是另外维护一份可能漂移的清单。
<!-- disclosure-ok: auditable source location, custos is open for exactly this -->

然后确认根本不存在绕过这道门的路径：

```bash
grep -rn 'CEXOMS\|BinanceClient\|OKXClient' src/ \
  --exclude=host.py --exclude=venue_binance.py
```

干净的代码树上应无输出。在别处构造的交易所客户端等于完全绕开准入，此时门内部再正确也无法弥补。

覆盖情况：

| 内容 | 测试 |
|---|---|
| 各宿主的模式与连接器声明 | `tests/test_nautilus_host_capability.py` |
| 给定选择绑定哪个宿主 | `tests/test_main_host_selection.py` |
| 能力受阻与实盘模式在任何引擎动作前被拒 | `tests/test_engine_lifecycle.py` |
| 交易所适配器与凭据接线 | `tests/test_nt_binance_venue.py` |
<!-- disclosure-ok: auditable source location, custos is open for exactly this -->

第三行对本页最关键。它断言的是：能力受阻与实盘拒绝都发生在**任何引擎动作之前** ——
这正是它是一道准入门、而非一段善后逻辑的原因。

## 这道门不做什么

它管准入，不管行为，对某笔交易是否明智不持立场。敞口上限与回撤熔断由不关心底层是哪个引擎的模块独立且持续地执行 —— 见[失联不等于停止](./safety-survives-disconnect)。
