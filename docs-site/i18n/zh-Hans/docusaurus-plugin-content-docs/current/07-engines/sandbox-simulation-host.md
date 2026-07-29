---
title: "Sandbox 模拟宿主"
sidebar_position: 2
---

# Sandbox 模拟宿主

`SandboxSimulationHost` 跑完整的本地部署生命周期，但不连接交易场所。通过
`--engine sandbox-sim` 选择。

它不是一个什么都不做的桩。artifact 激活、凭据解析、生命周期持久化、就绪回执与
RunnerFact 发布全部**真跑** —— 缺的只有场所连接。这使它成为「除交易本身之外一切」的一次演练。

## 它用来做什么

用它来验证注册、签名指令接收、reconcile 与事实投递在你的基础设施上端到端跑通，而这一切发生在任何具备交易权限的凭据介入之前。

它同时支撑契约测试 —— 这是它存在的更重要理由：生命周期在每次跑测试套件时都被走一遍，而不是只在有人手头正好有可用场所时才被验证。

## 它为什么只声明 `sandbox`

`supports_trading_mode` 只对 `sandbox` 返回真，别的都不返回。因此 `testnet` 或 `live`
部署会在准入处被拒 —— 即[实盘执行门](/zh-Hans/concepts/live-execution-gate)的条件 3 ——
且发生在其他任何动作之前。

这个拒绝来自宿主自己的声明，而不是来自别处维护的一份「禁止组合」清单。一个不能交易的宿主如实声明，准入采信它。

反面的做法糟糕得多：一个被路由到「悄悄什么都不做的宿主」的实盘订单，从外部看与一个成功的实盘订单毫无区别。

## 观测到的敞口

模拟器不持仓，因此 `get_open_notional` 恒返回零，`flatten_positions` 是一个写日志的空操作。熔断器仍然对它运行，其触发仍然可观测 —— 这正是熔断行为可以在没有场所的情况下被测试的原因。

## 源码

`src/custos/engines/nautilus/host.py`，与 `NtTradingNodeHost` 并列。两者满足同一个
`ExecutionEngineProtocol`；协议表面见
[NautilusTrader 引擎](/zh-Hans/engines/nautilus-trader)。
<!-- disclosure-ok: auditable source location, custos is open for exactly this -->
