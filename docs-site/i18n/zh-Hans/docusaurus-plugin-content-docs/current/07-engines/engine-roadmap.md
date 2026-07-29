---
title: "引擎路线图"
sidebar_position: 3
---

# 引擎路线图

Custos 不硬编码交易引擎。引擎之上的一切 —— reconcile 循环、实盘执行门、安全熔断器与签名
RunnerFact —— 只与一个 Python 接口对话：`ExecutionEngineProtocol`。任何满足该接口的引擎都能被 runner 监督，而**无需改动**周边的安全机制。

本页讲：今天随发布提供什么、一个引擎适配器必须提供什么、以及接下来哪些引擎是合理候选。

## 今天随发布提供什么

| 宿主 | 模块 | 模式 |
|---|---|---|
| `NtTradingNodeHost` | `custos.engines.nautilus` | `sandbox` · `testnet` · `live` |
| `SandboxSimulationHost` | `custos.engines.nautilus` | 仅 `sandbox` |

两者位于**同一个模块**；它们是同一个协议的两个实现，不是两个引擎。见
[NautilusTrader](/zh-Hans/engines/nautilus-trader) 与
[Sandbox 模拟宿主](/zh-Hans/engines/sandbox-simulation-host)。

模拟宿主的存在，是为了让运维能在**不下单**的前提下演练注册、指令投递、reconcile 与事实发布。它只声明 `sandbox`，因此准入会在 `testnet` 与 `live` 时拒绝它 —— 拒绝来自宿主**自己的声明**，而不是来自别处维护的一份「禁止组合」清单。见[实盘执行门](/zh-Hans/concepts/live-execution-gate)。

## 一个引擎适配器必须提供什么

适配器实现 `ExecutionEngineProtocol` 的 Tier-1 表面：

| 方法 | 职责 |
|---|---|
| `deploy` | 为一个部署实例启动策略 |
| `reconfigure` | 把新的期望状态应用到运行中的实例 |
| `stop` | 停止实例并释放其资源 |
| `supports_trading_mode` | 声明本引擎可运行哪些交易模式 |
| `supports_venue` | 声明本引擎可触达哪些交易所 |

后两个正是实盘执行门所读取的。一个未声明某模式的引擎，无论期望状态怎么要求，都**永远不会**被准入该模式 —— 门 fail closed，而不是降级。

因为 reconciler 与门只看得见协议，新增一个引擎**不需要**改动它们中的任何一个。

## 候选引擎

以下是编程模型与 runner 足够契合、值得评估的开源引擎。**没有一个已排期**；每一条记录的是「集成实际上要做什么」。

### Hummingbot

[Hummingbot](https://github.com/hummingbot/hummingbot) 是一个用于中心化与去中心化交易所做市与流动性提供的 Python 框架。

- **契合之处**：Python 原生且 async，适配器可跑在现有守护进程内，无需进程桥。
- **不契合之处**：Hummingbot 部署惯例上是一个带自己配置与策略约定的**独立 bot 实例**，而不是嵌入宿主进程的库。监督形态会更接近进程管理，而非 NautilusTrader 那种进程内模型。
- **不可移植**：Hummingbot 策略无法从 NautilusTrader 策略平移过来，必须重写。

### Freqtrade

[Freqtrade](https://github.com/freqtrade/freqtrade) 是一个 Python 加密交易 bot，采用基于
DataFrame 的策略接口，并自带回测引擎。

- **契合之处**：Python 原生，且其声明式策略形态能干净地映射到现有的期望状态与遥测管道。
- **不契合之处**：Freqtrade 通常在一个供自身 UI 使用的 REST API 之后运行。Custos **按设计不暴露任何入站网络面**，因此适配器只能在没有该表面的情况下运行它，而不是把它代理出来。
- **不可移植**：指标驱动的策略接口，与事件驱动的 `on_bar` / `on_trade` 处理器是不同的编程模型。

### 原生 Rust 绑定

NautilusTrader 的执行内核正逐步迁往 Rust。一个**第二套**、Rust 原生的内核绑定 ——
区别于今天这个消费 Python SDK 的适配器 —— 是可以设想的。

两种桥接形态可选：用 `pyo3` 构建的进程内扩展模块，保持 runner 单进程 async 模型不变；或是通过本地 socket 通信的受监督子进程。

这一条的动机是**性能**而非功能，只有当 profiling 显示当前 Python SDK 路径在生产负载下确实构成瓶颈时，才值得立项。

## 红线适用于每一个引擎

无论底下跑的是什么，四条 non-custodial 保证都成立：

- 凭据在 runner 进程**内部**解密，从不写日志、不上传，也不以环境变量或命令行参数的形式传给子进程 —— 那会让进程列表暴露它们。
- 实盘执行要求引擎**声明**支持实盘；否则门 fail closed。
- 本地安全防护在 ARX 不可达时继续生效。
- 金额全程使用十进制运算，并以**字符串**跨越 wire。

一个无法兑现这些的适配器，不是候选。
