---
title: "NautilusTrader 引擎"
sidebar_position: 1
---

# NautilusTrader 引擎

[NautilusTrader](https://github.com/nautechsystems/nautilus_trader) 是一个事件驱动、
以 Python 为主、内核用 Rust 实现的算法交易平台。它是 Custos 随发布提供的执行引擎，
也是 runner 自身各项契约在设计时对照的那一个。

下文涉及的一切都位于 `src/custos/engines/nautilus/`。这是 runner 中唯一知道
NautilusTrader 存在的部分 —— reconciler、熔断器与事实生产者一律只通过
`ExecutionEngineProtocol` 与它对话。
<!-- disclosure-ok: auditable source location, custos is open for exactly this -->

## 两个执行宿主

`host.py` 提供了引擎协议的两个实现：
<!-- disclosure-ok: auditable source location, custos is open for exactly this -->

**`NtTradingNodeHost`** 监督一个真实的 NautilusTrader `TradingNode`。它声明
`sandbox`、`testnet` 与 `live`，是唯一能抵达交易场所的宿主。

**`SandboxSimulationHost`** 跑完整的本地生命周期 —— artifact 激活、凭据解析、
持久化、就绪判定、事实发布 —— 但不连接任何外部系统。它只声明 `sandbox`，因此
testnet 或 live 部署会在准入处被拒绝，而不是被悄悄模拟掉。

宿主在进程生命周期内二选一：

```bash
arx-runner start --engine nautilus     # 默认，真实执行
arx-runner start --engine sandbox-sim  # 模拟，仅 sandbox
```

若 NautilusTrader 运行时未安装，`--engine nautilus` 会在启动时失败。它**不会**回落到
模拟 —— 一台悄悄换成非交易宿主的 runner 会一边报告健康、一边一单不发。

## 宿主拥有什么，不拥有什么

宿主拥有引擎进程构造、场所客户端配置、就绪观测、停止与重配置行为，以及引擎遥测。

它**不**拥有部署授权、策略发布状态、artifact 验证、凭据范围策略与指令确认。这些属于
它上面的层次 —— 这正是为什么更换引擎不会移动任何一条信任边界。

引擎入口点体现了这个切分：

```python
async def deploy(
    spec: dict,
    credential: dict,
    artifact: ActivatedEngineArtifactV1,
) -> str: ...
```

`artifact` 参数是一个已验证、已激活的策略对象。宿主把它加进 node；它自己从不导入策略
代码。这正是「究竟跑了哪份代码」能在引擎之外被回答的原因。

## 它满足的协议

`ExecutionEngineProtocol` 分两层，两个宿主都实现完整表面。

**Tier-1 —— 生命周期与能力。** `deploy`、`reconfigure`、`stop`、
`supports_trading_mode`、`supports_venue`。它们驱动指令协调器与生命周期监督者；
后两个正是[实盘执行门](/zh-Hans/concepts/live-execution-gate)所读取的。

**Tier-2 —— 风险与连通性状态。** `get_open_notional`、`check_engine_connected`、
`flatten_positions`、`get_positions`、`get_orders`、`get_engine_status`。它们的存在是
为了让与引擎无关的守卫 —— 名义本金上限、兜底熔断器、僵尸看门狗 —— 能在不知道底层是
哪个引擎的前提下，兑现失联依然生效的保证。

Tier-2 是更有意思的那一半。它正是
[失联不等于停止](/zh-Hans/trust-model/safety-survives-disconnect)
成为 runner 的属性、而非 NautilusTrader 的属性的原因。

## 配套模块

| 文件 | 职责 |
|---|---|
| `venue_binance.py` | Binance 行情与执行客户端配置 |
| `binance_ledger.py` | 供对账使用的独立场所账本证据 |
| `portfolio_snapshot.py` | 权益与持仓估值的单一估值边界 |
| `runner_safety.py` | 把场所客户端挡在下单预留闸门之后 |
| `risk.py` | 交易前规则配置 |
| `runtime_loader.py` | 证明策略模块来自不可变的激活根 |
| `sandbox_runner_fact_host.py` | 模拟宿主的事实发布 |
<!-- disclosure-ok: auditable source location, custos is open for exactly this -->

其中两个是本站别处所述保证的承重件。

`portfolio_snapshot.py` 是「未平名义本金与实际权益来自同一个估值边界、而非两个可能互相
矛盾的来源」的原因。熔断器每实例每 tick 只读取一份由它派生的引擎状态。

`runner_safety.py` 包住场所执行客户端，使订单在抵达场所之前必须通过预留边界。这个守卫
是引擎绕不开的一层门面，而不是一项「请策略记得调用」的检查。

## 交易场所

`binance` 与 `binance_perpetual`，比对不区分大小写。宿主未声明的签名连接器会在准入处
被拒。

现货与永续的差别不止于名字 —— 合约标识、账户类型与杠杆配置都按连接器分别推导，这正是
连接器属于签名指令、而非本地配置项的原因。

## 安装运行时

NautilusTrader 是可选依赖，声明在 `pyproject.toml` 的
`[project.optional-dependencies].nautilus` 下。审计用的安装不会拉取它：你无需安装任何
交易引擎，就能读完并测完整条信任边界。

发布的容器镜像已包含它。见[安装](/zh-Hans/getting-started/installation)。
