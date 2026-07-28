---
title: "The Trust Model"
sidebar_position: 2
---


# The Trust Model


## 是什么

**custos** (拉丁语: *guardian*) 是 the Alephain Guild
生态的 **non-custodial、自托管** 执行 runner. 用户在自己基础设施上运行本 daemon,
跑经过回测的 NautilusTrader 策略, 本地持交易所 API Key, **永不上云端**.

## 为什么必须公开开源

custos 是 Apache-2.0 开源, day 1 公开. 这不是偶然, 是生态 non-custodial 红线的
**可验证兑现**:

1. 用户机器上跑的 daemon 持交易所 Key + 下真实订单
2. 用户理性信任此 daemon 的**唯一条件**是: 能读代码验证它对 Key 的处理
3. 开源把 "Key 和策略只在本地" 从**设计声明**升级为**外部审计员逐行可查的工程事实**

见 the published ownership boundary 与。

## Trust Boundary (custos 承担的红线)

生态整体 trust boundary 的**承重墙**是 *the custos line*:

1. **Key / 策略永不离开用户机器**. 唯一出 custos 边界的数据是**执行遥测 + 状态报告**
2. **控制是 declarative, 不是 imperative**. custos *pull* 期望态并 reconcile 本地
   NT 进程去匹配; 产品面**写期望态**, 不会 `docker run` 进用户机器. 这是 custos
   作为**真自托管** runner 而非**远程受控 agent** 的边界
3. **云端断线优雅降级**. 组织级跨账户熔断在云端可达时聚合; 每 runner 保留本地
   fallback breaker (每策略/每账户 drawdown) + 结构性 `max_notional_per_runner` cap.
   云端 outage **永不停**本地交易或移除本地防护

红线的技术兑现分层见 [`01-architecture.md`](/introduction/architecture-at-a-glance).

## 六模块 (六件套)

custos 由六个核心模块组成:

| 模块 | 职责 | 承担红线锚点 |
|------|------|-------------|
| **enrollment** | 一次性 `EnrollmentToken` 配对; `runner_id`; `paper_only` 默认 | Token 一次性 (防重放) |
| **reconcile** | Declarative loop: pull `DeploymentSpec` → start/stop NT → report `DeploymentStatus` | 失联≠停止 |
| **nautilus_host** | NT 进程监督 + `ExecutionEngineAdapter` (CEX/NT) + **live execution gate** | **live execution gate live release gate** |
| **runner_fact** | NT MessageBus → closed typed facts → signed durable local queue → ARX | Key 不出进程；unsigned fallback 禁止 |
| **credential_vault** | sops+age 本地 KEK vault; `trade_no_withdraw` scope | KEK / API key 不出进程 |
| **nats_client** | JetStream client + envelope schema + subject naming | Wire schema 版本化 + 契约防漂移 |

各模块设计详情见本目录下同名 `.md` 文件.

## 与 arx / ARX 的边界

- **custos → arx**: pull `DeploymentSpec` + push 遥测/heartbeat/reconcile status
- **custos ↛ ARX**: 从不直接对话, arx 中介 (arx 是 gateway)
- **单一外部入口**: 所有外部访问 custos 状态必须经 arx 的 `gatekeeper` + `CustosGateway`
- **custos 未暴露 API**: custos 不给终端用户 / API 客户端 / dashboard 提供任何直接接口

详见 [`../domain.md`](/introduction/what-is-custos) §3 跨系统契约.

## 独立开源纪律

- 仓库自足: 外部审计员单仓 clone 即可读全部代码, 不需要访问闭源云端
- License: Apache-2.0 (LICENSE + NOTICE)
- SemVer + long-term support (EOL ≥ 12 月)
- 规则集 / 权威文档 / 验证入口皆在本仓库内, 不依赖 workspace root

## 下一步阅读

- **架构与信任边界详情**: [`01-architecture.md`](/introduction/architecture-at-a-glance)
- **模块导航**: [`02-module-design.md`](/concepts/deployment-spec-vs-instance)
- **技术实现细节**: [`03-implementation.md`](/concepts/reconcile-loop)
- **顶层 domain 词汇**: [`../domain.md`](/introduction/what-is-custos)
