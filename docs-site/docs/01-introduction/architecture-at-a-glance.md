---
title: "Architecture at a Glance"
sidebar_position: 3
---


# Architecture at a Glance

> 从 `../domain.md` §0-§2 提炼的**架构视角**: 六个模块边界 + Non-Custodial 分层信任边界的技术兑现.
>
> **v2 canonical boundary**：ARX 只认证/授权；the control plane 是
> DeploymentSpec/DeploymentInstance 与业务投影 owner；Custos 持凭据和执行，
> 产生 exact-instance signed RunnerFacts（含 venue fee/funding evidence）。
> mode 仅 sandbox/testnet/live，Python 无 production fallback。

## 1. 上下游图

```
    ┌────────────────────────────────────────────┐
    │  云端控制面 (闭源, 商业许可)      │
    │  ┌─────────┐   ┌────────────┐              │
    │  │  arx    │──▶│  the control plane  │  发布 Spec    │
    │  │ (SaaS)  │   │            │              │
    │  └─────────┘   └────────────┘              │
    │        ▲                │                   │
    └────────┼────────────────┼───────────────────┘
             │                │
   NATS      │                │  DeploymentSpec + EnrollmentToken 配对
   Status/   │                ▼
   遥测摘要   │       ┌────────────────────────────┐
             │       │  用户本地基础设施 (开源)      │
             │       │  ┌──────────────────────┐   │
             └───────│──│      custos          │   │
                     │  │  ┌────────────────┐  │   │
                     │  │  │  Vault (Key)   │  │   │
                     │  │  ├────────────────┤  │   │
                     │  │  │  ReconcileLoop │  │   │
                     │  │  ├────────────────┤  │   │
                     │  │  │  NT Adapter    │──┼───┼──▶ 交易所 (Binance/OKX)
                     │  │  └────────────────┘  │   │
                     │  └──────────────────────┘   │
                     └────────────────────────────┘
```

- **控制面**：ARX 提供 ActorAssertion；the control plane 持 immutable spec、mode-local instance 并验收 signed facts；二者从不持 Key 明文
- **数据面** (custos + NT): 持 Key + 跑策略 + 直连交易所

## 2. 数据面 vs 控制面切分 (Non-Custodial 承重墙)

| 面 | 组成 | 开源状态 | 持有的敏感数据 |
|----|------|---------|---------------|
| **数据面** | custos + NautilusTrader | **全部开源 (Apache-2.0 / MIT)** | Key 明文 · 订单簿明细 · Fill 事件明文 · 账户余额明细 |
| **控制面** | ARX 与云端控制面 | 全部闭源 | Key 引用 handle · DeploymentSpec · StatusReport · 遥测摘要 |

**关键洞察**: 只要数据面开源可审, 控制面即使完全攻破, 用户 Key 依然安全 —
因为 Key 根本不在控制面.

## 3. Key 永不上云的技术锚点 (红线 0.1)

| 环节 | 兑现方式 |
|------|---------|
| 存储 | ExchangeCredential 密文在 `~/.arx/vault/` 本地文件系统 (fs 权限 0600) |
| 加密 | argon2id (KDF) + aes-256-gcm; KEK / MasterKey 派生只在 custos 进程内存 |
| 使用 | NT 调用交易所 API 时通过 credential_vault 接口即时解密, 明文只在**单次请求 lifetime** |
| 上报 | AlertEvent / telemetry payload 强制脱敏 (`api_key_sha8` / `credential_hint`) |
| 审计 | Vault 解密路径带 `AuditLog` (`last_accessed_at` 更新触发) |

## 4. G6 host gate (红线 0.2)

Live venue 部署前必须过 `NtTradingNodeHost` 的 G6 gate:

- `NoopHost` 只允许 `sandbox` / `testnet`
- 真 `NtTradingNodeHost` 才可申请 `live` capability
- `LIVE_MODE=true` env 独立开关, 与 spec 中 `trading_mode=live` 双守
- G6 gate deny → 上报 `FailureEvent(reason_code=g6_gate_denied)`


## 5. 失联 ≠ 停止 (红线 0.3)

Level-triggered reconcile 的核心不变量:

- **reconcile loop 失去云端**: 按上次缓存的 `DeploymentSpec` 继续跑 NT
- **本地 safety breaker 独立守护**: 每策略 / 每账户 drawdown breaker + 结构性
  `max_notional_per_runner` cap 在本地判断, 不依赖云端
- **云端 outage 生存期**: 数天 (Spec 有 TTL, 但 Key + NT 本地)
- **重新连上后**: `observed_generation` 单调对齐, 无跳跃

## 6. Money math Decimal + wire str (红线 0.4)

- 所有价格 / 数量 / notional 计算路径用 `decimal.Decimal`
- Wire 序列化 (NATS envelope) 用 `str(Decimal)`, 非 `float`
- Pydantic 模型 `field_serializer` 或 `json_encoders={Decimal: str}` 统一
- Contract test: `test_telemetry_money_contract.py` (18 test)

## 7. 六个模块

| 模块 | 承担实体 | 状态机 | 详细文档 |
|----|---------|-------|---------|
| **Runner 宿主** | `Runner` / `EnrollmentToken` / `HostIdentity` | offline → online / draining | [`enrollment.md`](/getting-started/enrollment) |
| **声明式 reconcile** | `DeploymentSpec` / `DeploymentStatus` / `DesiredState` / `ActualState` / `ReconcileLoop` | pending → running → degraded → stopped | [`reconcile.md`](/concepts/reconcile-loop) |
| **本地 Vault** | `VaultNamespace` / `EncryptedKey` / `MasterKey` / `KEK` | derived → active → cleared (TTL) | [`credential_vault.md`](/operator-guide/credential-vault) |
| **NT 执行适配** | `NTAdapter` / `TradingNodeConfig` / `StrategyMirror` | INITIALIZED → STARTED → STOPPED → DISPOSED | [`nautilus_host.md`](/engines/nautilus-trader) |
| **RunnerFact** | closed 13-kind union / signed batch / instance-keyed sequence | durable local queue → PubAck | [`runner_fact.md`](/concepts/runner-fact) |
| **NATS 通道** | `NatsClient` / `EnvelopeSchema` / `build_subject()` | connected → reconnecting (auto) | [`nats_client.md`](/reference/nats-subjects) |

## 8. 用户验证路径

用户只需审计**两个开源 repo** 即可确认信任模型:

1. **NautilusTrader upstream** (`nautilus-trader/nautilus_trader`, MIT) — 确认 NT 无偷 Key 或代下单路径
2. **custos** (`alchymia-labs/custos`, Apache-2.0) — 确认 custos 无以下反模式:
   - 上传 Key 明文到 arx / the control plane / 任何云端
   - 接收云端下发的 "代解密" 指令
   - 接收云端下发的 "直接下单" 绕过策略指令
   - Vault 密文格式与算法与文档声明不一致

不需要审计 arx / the control plane / 其他闭源子系统 — 即使全被攻破, Key 依然不在攻击范围.

## 9. 承重墙原则

custos **不能被 arx 替代** — 任何 "云端代替 custos 直接下单" 的架构提案都直接击穿
non-custodial 保证。设计上明确:

> Runner 是用户装到自己基础设施上、持自己 Key 的守护进程; 用户必须能审计代码才敢信任
> Key 交给它. 这是 "Key/策略只在本地" 红线从设计声明升级为工程可验证的唯一路径.

---

## 参考

- 四条不可绕过的保证: [信任模型](./trust-model)
- live 执行前的准入校验: [G6 host gate](/concepts/g6-host-gate)
- 凭证的存放与轮换: [凭证金库](/operator-guide/credential-vault)
