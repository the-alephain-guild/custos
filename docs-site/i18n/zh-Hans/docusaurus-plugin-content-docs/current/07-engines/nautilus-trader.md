---
title: "NautilusTrader 引擎"
sidebar_position: 1
---

Custos 通过可选的 `nautilus` extra 使用固定版本的 NautilusTrader fork。在 Python 3.12 环境执行 `make install-nt` 安装。

<!-- generated:nautilus-version -->

当前依赖：`nautilus-trader==2.0.0rc5+sodex.1`.

<!-- /generated:nautilus-version -->

lock sources 提供 macOS arm64 和 Linux arm64/x86_64 的 CPython 3.12 wheel。Linux wheel 要求兼容 `manylinux_2_39`。请使用仓库 lock，不要以版本号相近的上游包替代。

## 选择宿主

| CLI 值 | 宿主 | 行为 |
|---|---|---|
| `--engine nautilus` | `NtTradingNodeHost` | 使用真实行情客户端；sandbox 本地模拟成交，testnet 向测试网下单 |
| `--engine sandbox-sim` | `SandboxSimulationHost` | 本地模拟生命周期，不导入交易策略或连接交易所 |

签名模拟组合会在模拟宿主外添加事实发布功能；离线组合报告未签名的本地状态。两者输出都不能证明真实交易所往返成功。

## Connector 能力声明

<!-- generated:venues -->

| Connector | sandbox | testnet | live |
|---|---|---|---|
| `binance` | 声明支持 | 声明支持 | 声明支持 |
| `binance_perpetual` | 声明支持 | 声明支持 | 声明支持 |
| `sodex` | 声明支持 | 声明支持 | 不支持 |
| `sodex_perpetual` | 声明支持 | 声明支持 | 不支持 |

<!-- /generated:venues -->

表中为宿主能力声明，不是生产验收结果。当前 daemon 未启用 live 执行。SoDEX testnet 还有输入契约限制，配置前请阅读[SoDEX](/engines/sodex)。

## 并发与就绪

Nautilus 2 使用线程本地 runner/message bus。Custos 拒绝同一 event loop 上的第二个活动 node。并发 node 应使用不同进程，并隔离身份和状态目录。

引擎就绪检查 node 任务、行情/执行连接、投资组合初始化与可靠估值、对账、策略生命周期接收状态及必要能力。创建 node 成功或 daemon 健康检查通过，不能证明这些条件全部满足。

## 引擎接口

`ExecutionEngineProtocol` 定义部署、重新配置、停止、能力查询、类型化就绪/终止事件、连接状态、订单/持仓快照、估值和风险控制。签名部署传入已激活的产物：

```python
async def deploy(
    spec: dict,
    credential: dict,
    artifact: ActivatedEngineArtifactV1,
) -> str: ...
```

离线部署使用操作者挂载的产物，其身份从目录派生，不具有签名发布保障。

## 配置与停止行为

宿主读取 `nautilus_config` 中的启动超时与对账回溯配置。交易标识、账户类型、杠杆和客户端配置由各 connector 的实现决定，不要跨交易所复用标的名称或账户字段。

停止行为遵循部署的 shutdown policy，默认保留持仓。runner 停止不代表持仓已经平掉，详见[应急恢复](/operator-guide/emergency-playbook)。
