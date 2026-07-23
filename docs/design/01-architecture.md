# 01 — 架构与信任边界

> 从 `../domain.md` §0-§2 提炼的**架构视角**: 6 BC 边界 + Non-Custodial 分层信任边界的技术兑现.
>
> **v2 canonical boundary**：ARX 只认证/授权；Crucible Rust 是
> DeploymentSpec/DeploymentInstance 与业务投影 owner；Custos 持凭据和执行，
> 产生 exact-instance signed RunnerFacts（含 venue fee/funding evidence）。
> mode 仅 sandbox/testnet/live，Python 无 production fallback。

## 1. 上下游图

```
    ┌────────────────────────────────────────────┐
    │  云端控制面 (闭源, 生态 Tier D 商业许可)      │
    │  ┌─────────┐   ┌────────────┐              │
    │  │  arx    │──▶│  Crucible  │  发布 Spec    │
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

- **控制面**：ARX 提供 ActorAssertion；Crucible Rust 持 immutable spec、mode-local instance 并验收 signed facts；二者从不持 Key 明文
- **数据面** (custos + NT): 持 Key + 跑策略 + 直连交易所

## 2. 数据面 vs 控制面切分 (Non-Custodial 承重墙)

| 面 | 组成 | 开源状态 | 持有的敏感数据 |
|----|------|---------|---------------|
| **数据面** | custos + NautilusTrader | **全部开源 (Apache-2.0 / MIT)** | Key 明文 · 订单簿明细 · Fill 事件明文 · 账户余额明细 |
| **控制面** | arx · Crucible · 生态其他闭源系统 | 全部闭源 (Tier D) | Key 引用 handle · DeploymentSpec · StatusReport · 遥测摘要 |

**关键洞察 (ADR-012 v4)**: 只要数据面开源可审, 控制面即使完全攻破, 用户 Key 依然安全 —
因为 Key 根本不在控制面.

## 3. Key 永不上云的技术锚点 (红线 0.1)

| 环节 | 兑现方式 |
|------|---------|
| 存储 | ExchangeCredential 密文在 `~/.custos/vault/<tenant>/…` 本地文件系统 (fs 权限 0600) |
| 加密 | argon2id (KDF) + aes-256-gcm; KEK / MasterKey 派生只在 custos 进程内存 |
| 使用 | NT 调用交易所 API 时通过 credential_vault 接口即时解密, 明文只在**单次请求 lifetime** |
| 上报 | AlertEvent / telemetry payload 强制脱敏 (`api_key_sha8` / `credential_hint`) |
| 审计 | Vault 解密路径带 `AuditLog` (`last_accessed_at` 更新触发) |

## 4. Engine execution admission (红线 0.2)

任何 venue 部署都必须通过唯一 V1 execution admission：

- `SandboxSimulationHost` 只允许 `sandbox` + `SIM` connector，并产生身份绑定的 ready receipt
- `NtTradingNodeHost` 只接收已验证并激活的 immutable artifact
- `live` 还要求 host capability、`trade_no_withdraw` credential、signed promotion
  evidence 与本地 `live_execution_enabled` 同时成立
- 任一条件缺失都生成 typed terminal outcome；不得降级到路径加载或旧 payload

## 5. 失联 ≠ 停止 (红线 0.3)

Level-triggered reconcile 的核心不变量:

- **command intake 失去云端**: 已应用实例按 durable applied state 继续跑 NT
- **本地 safety breaker 独立守护**: 每策略 / 每账户 drawdown breaker + 结构性
  `max_notional_per_runner` cap 在本地判断, 不依赖云端
- **云端 outage 生存期**: 受已验证 policy 有效期和本地 restart budget 约束
- **重新连上后**: exact command redelivery 按 generation/fingerprint 单调重放

## 6. Money math Decimal + wire str (红线 0.4)

- 所有价格 / 数量 / notional 计算路径用 `decimal.Decimal`
- Wire 序列化 (NATS envelope) 用 `str(Decimal)`, 非 `float`
- Pydantic 模型 `field_serializer` 或 `json_encoders={Decimal: str}` 统一
- Contract test: `test_telemetry_money_contract.py` (18 test)

## 7. 六个限界上下文 (BC)

| BC | 承担实体 | 状态机 | 详细文档 |
|----|---------|-------|---------|
| **Runner 宿主** | `Runner` / `EnrollmentToken` / `HostIdentity` | offline → online / draining | [`enrollment.md`](enrollment.md) |
| **声明式 reconcile** | `DeploymentSpec` / `DeploymentStatus` / `DesiredState` / `ActualState` / `ReconcileLoop` | pending → running → degraded → stopped | [`reconcile.md`](reconcile.md) |
| **本地 Vault** | `VaultNamespace` / `EncryptedKey` / `MasterKey` / `KEK` | derived → active → cleared (TTL) | [`credential_vault.md`](credential_vault.md) |
| **NT 执行适配** | `NTAdapter` / `TradingNodeConfig` / `StrategyMirror` | INITIALIZED → STARTED → STOPPED → DISPOSED | [`nautilus_host.md`](nautilus_host.md) |
| **RunnerFact** | closed 13-kind union / signed batch / instance-keyed sequence | durable outbox → PubAck | [`runner_fact.md`](runner_fact.md) |
| **NATS 通道** | `NatsClient` / `EnvelopeSchema` / `build_subject()` | connected → reconnecting (auto) | [`nats_client.md`](nats_client.md) |

## 8. 用户验证路径

用户只需审计**两个开源 repo** 即可确认信任模型:

1. **NautilusTrader upstream** (`nautilus-trader/nautilus_trader`, MIT) — 确认 NT 无偷 Key 或代下单路径
2. **custos** (`the-alephain-guild/custos`, Apache-2.0) — 确认 custos 无以下反模式:
   - 上传 Key 明文到 arx / Crucible / 任何云端
   - 接收云端下发的 "代解密" 指令
   - 接收云端下发的 "直接下单" 绕过策略指令
   - Vault 密文格式与算法与文档声明不一致

不需要审计 arx / Crucible / 其他闭源子系统 — 即使全被攻破, Key 依然不在攻击范围.

## 9. 承重墙原则

custos **不能被 arx 替代** — 任何 "云端代替 custos 直接下单" 的架构提案都直接击穿
non-custodial 红线. ADR-012 v4 明文钉死:

> Runner 是用户装到自己基础设施上、持自己 Key 的守护进程; 用户必须能审计代码才敢信任
> Key 交给它. 这是 "Key/策略只在本地" 红线从设计声明升级为工程可验证的唯一路径.

---

## 参考

- 顶层 domain spec: [`../domain.md`](../domain.md)
- 红线权威声明: [`../../.claude/rules/mandatory-rules.md`](../../.claude/rules/mandatory-rules.md) §0
- 偏离协议: [`../../.claude/rules/deviation-protocol.md`](../../.claude/rules/deviation-protocol.md)
