# 19 - Converge Crucible command, RunnerFact, and local execution runtime

> **Status**: ⏳ In progress — T2-T8a, clone-local development runtime and production StrategyRelease authority consumption pass; immutable OCI materialization/daemon composition, real PG/NATS/engine command-to-fact evidence, physical-mode policy/NATS receipts, runtime RC and T9-T10 remain open
> **Created**: 2026-07-14
> **Revised**: 2026-07-26 through RunnerFact V1 Phase A exact-byte acceptance
> **Project**: Custos
> **Source**: Audit of pre-plan migration `324da6e`, PS Plan 53, and v1.team review
> **For Claude**: Use `/forge:execute` to implement this plan.
> **Immediately executable**: T7 contract handoff is complete; launched policy publication/PubAck acceptance remains gated by real `0117` mode-database and NATS receipts
> **19d-T8a gate**: 19c STOP only; it produces the immutable RunnerFact candidate before Crucible Plan 90 Phase A
> **Runtime RC gates**: Crucible Plan 89 migration 0116 signed command producer and `CR89-0116-GENERATION-STORAGE`; Crucible Plan 90 Phase-A schema/golden compatibility receipt; Crucible Plan 99 runner-safety-policy-authority; Crucible Plan 100 runner NATS transport authority and revocation receipts; Custos Plan 18 staged candidate and exact final required by the selected RC/final-candidate BOM
> **Close-out gates**: Crucible Plan 90 Phase-B real runtime round-trip receipt; PS Plan 56 exact final-candidate acceptance
> **Original plan-first**: `3ce4048`; this live-plan revision supersedes its erroneous decisions

## 上下文 (Context)

`324da6e` 已把 Custos 从 standalone deployment authority 迁移为：

- Crucible 发布 signed runner deployment command；
- Custos 消费 command、执行本地策略并发布 signed RunnerFacts；
- Crucible 验签、投影和结算；
- ARX 只负责授权，不恢复 telemetry gateway。

方向正确，但 original Plan 19 存在五个架构错误：

1. 计划新建第二套 `runner_fact_outbox`，与现有 SQLite `RunnerFactOutbox`
   的 seq、dedup、签名和 PubAck 删除语义冲突。
2. journal 以 `spec_id` 为中心，而 runtime address 必须是
   `deployment_instance_id`。
3. strict command/schema 被当作 Custos 单仓任务，但 Crucible 尚未生产完整字段。
4. runner-wide cap 没有独立 authority，多实例下当前行为是最后一个 spec 覆盖。
5. 计划缩减现有 engine protocol、删除已经描述 typed RunnerFact 的
   `telemetry_actor.md`，并缺少 ACK deadline、poison message、restart budget
   和 generation fingerprint。

本修订直接替换错误设计。不得通过兼容 fallback、第二个 database/outbox 或
Custos-only fixture 修改绕过 producer 缺口。

## Current V1 baseline

- The only command input is a signed Crucible V1 domain event carrying the
  canonical DeploymentSpec and identity
  `deployment_instance_id + deployment_spec_id + digest + generation`.
- RunnerFact desired/applied state, lifecycle and outbox use one SQLite deep
  module and schema V1. No second journal or compatibility store exists.
- ACK follows durable command outcome; PubAck follows durable outbox delivery.
- Runner aggregate-cap policy, machine credential, NATS vault and event
  envelopes use their sole first-production V1 shapes.
- Live remains fail closed until Crucible policy/credential authority, real NATS
  transport, artifact resolver and engine lifecycle are all composed.

## 目标 (Goal)

把 current-main 收敛为可恢复、可监督、fail-closed 的 Custos runtime：

- exact signed Crucible command consumption；
- success/conflict/stale/retry-exhausted/invalid-command outcome 在 ACK/TERM 前 durable；
- applied state 与 lifecycle fact 的单事务提交；
- 唯一 RunnerFact outbox、stream sequence 和 PubAck path；
- 所有 deployment-scoped RunnerFact 受 signed generation fencing；
- additive engine readiness/terminal lifecycle；
- 真实 Nautilus portfolio/equity；
- signed canonical policy 的本地 mandatory enforcement；
- 完整 RunnerFact/capability/projector receipts；
- sandbox、testnet、live 的 mode/capability isolation。

Custos 不接管 StrategyRelease、DeploymentSpec authority、组合风险、审批、资本、
canonical reconciliation 或结算。

## Authority Boundary

| Surface | Canonical owner | Custos responsibility |
|---|---|---|
| DeploymentSpec/Instance and command payload | Crucible | verify and execute exact signed command |
| StrategyRelease/artifact selection/effective config | Crucible | execute exact bound artifact/config |
| Runtime address | `deployment_instance_id` | key all desired/applied/runtime state |
| Local credentials and venue interaction | Custos | keep secrets local and execute |
| RunnerFact sequence/signing/outbox/generation fence | Custos | existing SQLite deep module；generation 进入 signed header，但不切分 stream |
| RunnerFact verification/projector/settlement | Crucible | provide compatibility receipt |
| Runner-level cap policy | Crucible Plan 99 signed versioned policy | enforce locally without loosening；不得内嵌到某个 DeploymentSpec |
| Runner NATS transport credential, ACL and durable authority | Crucible Plan 100 | Custos generates and retains the User NKey seed, consumes the exact JWT/ACL/durable contract, uses pinned TLS and emits reconnect-denial evidence; it never receives signer/admin authority |
| Advisory strategy sizing | Toolkit/strategy | never substitutes mandatory safety |
| Portfolio risk, approvals, capital, settlement | Crucible | out of scope |

### Cross-repository interface completion order

| Order | Producer -> consumer | Exact producer artifact | Custos stop gate |
|---|---|---|---|
| 1 | Crucible 88 -> Custos 18 T5e | machine-authenticated StrategyRelease material V1 schema/golden/receipt, control head `0028` | no daemon reconcile without the exact resolver receipt |
| 2 | Crucible 89 -> Custos 19 T2-T5 | sole signed `CrucibleRunnerDeploymentCommandV1`, mode head `0116` and generation-storage receipt | no command runtime RC from fixture-only producer bytes |
| 3 | Crucible 99 -> Custos 19 T7 | signed runner/tenant/mode policy V1 plus mode head `0117` | policy capability and live remain false without real owner policy |
| 4 | Crucible 100 -> Custos 19 T7C | per-mode public-key enrollment, JWT/ACL/existing-durable/rotation/revocation receipt at control `0029` | no production transport from local JWT fixtures or one broker |
| 5 | Custos 19 T8 -> Crucible 90 | immutable RunnerFact V1 candidate, then exact runtime RC | no Phase-B or ACK/PubAck readiness before Crucible persistence receipt |

ARX is not a transport or business-fact hop in any row. ARX may authorize an
operator intent and forward only ActorAssertion-bound public intent to
Crucible; Custos machine enrollment, StrategyRelease resolution, commands and
RunnerFacts remain direct Custos-Crucible paths.

## Single Durable State Deep Module

Custos 已有 `RunnerFactOutbox`，负责：

- per-stream sequence allocation；
- event deduplication；
- `facts[].seq` injection；
- signed batch construction；
- durable pending batches；
- JetStream PubAck 后删除。

Plan 19 禁止创建第二套 outbox。desired/applied journal tables 必须纳入同一
SQLite deep module、connection 和 transaction boundary。实现可以内部重构文件，
但 public surface 保持单一：

```python
class RunnerStateStore(Protocol):
    def record_desired_command(
        self,
        command: VerifiedRunnerCommand,
        command_fingerprint: str,
        verification_receipt: CommandVerificationReceipt,
    ) -> DesiredRecord: ...

    def commit_verified_command_outcome_and_enqueue_fact(
        self,
        *,
        deployment_instance_id: UUID,
        deployment_spec_id: UUID,
        deployment_spec_digest: str,
        generation: int,
        command_fingerprint: str,
        outcome: Literal["applied", "conflict", "stale", "retry_exhausted"],
        engine_handle: str | None,
        outcome_fact: RunnerFact,
    ) -> CommandOutcomeCommitResult: ...

    def commit_untrusted_command_rejection(
        self,
        *,
        delivery_id: str,
        exact_subject: str,
        raw_envelope_digest: str,
        reason_code: Literal["invalid_signature", "invalid_schema", "unsupported_version"],
    ) -> UntrustedCommandRejectionResult: ...
```

`commit_applied_and_enqueue_lifecycle()` 保留为
`commit_verified_command_outcome_and_enqueue_fact(outcome="applied")` 的 typed wrapper。
verified command 的四种 terminal/success outcome 必须在一个 SQLite transaction 中：

1. 校验 desired instance/generation/fingerprint；
2. 写 applied state；
3. 写 immutable command outcome；
4. 使用现有 event dedup；
5. 分配现有 stream sequence；
6. 注入 `facts[].seq`；
7. 构造并签名 batch；
8. 写入现有 `runner_fact_outbox`；
9. commit。

禁止先提交 applied 再调用另一个 outbox transaction。
invalid signature/schema/version 无法建立可信 instance authority，因此不得伪造 lifecycle
fact；`commit_untrusted_command_rejection()` 必须先把 runner-scoped security/DLQ receipt
durably 写入同一 SQLite deep module，再允许 consumer `term()`。如果任何 durable commit
失败，只能 NAK，不得 ACK/TERM。

最小 tables：

```text
desired_deployments
  deployment_instance_id PRIMARY KEY
  tenant_id
  trading_mode
  runner_id
  deployment_spec_id
  deployment_spec_digest
  generation
  command_event_id
  exact_subject
  command_fingerprint
  verified_event_bytes_digest
  signer_key_id
  signature_profile
  verification_receipt
  canonical_command
  desired_status
  updated_at_ns

applied_deployments
  deployment_instance_id PRIMARY KEY
  deployment_spec_id
  deployment_spec_digest
  generation
  command_fingerprint
  engine_handle
  observed_status
  restart_count
  quarantine_reason
  updated_at_ns

command_outcomes
  outcome_id PRIMARY KEY
  delivery_id
  deployment_instance_id NULLABLE
  generation NULLABLE
  command_fingerprint NULLABLE
  outcome
  reason_code
  durable_disposition
  recorded_at_ns

runner_cap_policy
  policy_id PRIMARY KEY
  policy_revision
  policy_digest
  tenant_scope
  trading_mode
  max_notional
  effective_at_ns
  expires_at_ns
  signer_key_id
  signed_policy

order_reservation
  deployment_instance_id
  client_order_id
  policy_id
  reserved_notional
  filled_exposure
  state
  updated_at_ns
  PRIMARY KEY (deployment_instance_id, client_order_id)

runner_exposure_checkpoint
  policy_id PRIMARY KEY
  open_exposure
  reconstructed_at_ns
  source_digest

existing tables, retained as the only fact path
  runner_fact_stream
  runner_fact_seen_event
  runner_fact_outbox
```

`deployment_spec_id` 和 digest 是 provenance，不是 journal primary key。
SQLite 不得保存 credential material。

Lifecycle event ID 必须由 instance/spec/generation/state/fingerprint 的稳定输入
确定生成，不能在 crash replay 时随机生成新 UUID。

`RunnerFactAuthority` 与 signed batch header 必须增加 `generation`。generation 不得进入
`stream_key`、NATS subject 或 sequence allocator key；同一 instance/spec stream 在 generation
变化时继续单调递增。fill/order/position/equity/log/lifecycle 等所有 deployment-scoped fact
都必须带当前 generation，Crucible projector 必须拒绝或隔离旧 generation fact。

## Command Identity, ACK, and Poison Handling

Command fingerprint 冻结为：

```text
SHA256(
  b"CRUCIBLE-RUNNER-COMMAND-FINGERPRINT-V1\0" ||
  u32be(len(exact_subject_utf8)) || exact_subject_utf8 ||
  u64be(len(verified_exact_event_bytes)) || verified_exact_event_bytes
)
```

`verified_exact_event_bytes` 是通过 Crucible signature 验证的原始 event bytes，不是重编码
JSON。fingerprint 明确排除外层 signature bytes，避免同一 event 合法重签/换钥产生 false
conflict；signer key id、signature profile 和 verification receipt 必须单独持久化。subject
属于 fingerprint，因为它属于签名 authority binding。

| Delivery | Behavior |
|---|---|
| same instance + generation + same fingerprint | idempotent replay |
| same instance + generation + different fingerprint | terminal conflict, quarantine, no apply |
| newer generation | apply after authority/mode/capability checks |
| older generation | terminal stale command |
| invalid signature/schema/version | terminal/DLQ poison message |
| retryable local dependency failure | bounded NAK/backoff |
| long deploy waiting for ready | periodic JetStream `in_progress()` |

必须区分：

- **Inbound command ACK**：Custos 对 Crucible command delivery 的
  ACK/NAK/term/in-progress。
- **Outbound fact PubAck**：JetStream 确认 signed RunnerFact batch 后，
  existing outbox 才删除 pending batch。

两者不能共享状态字段或被称为同一个 ACK。

Consumer 必须显式配置 `ack_wait`、`max_deliver`、backoff 和 DLQ/quarantine
策略。poison message 不得无限 NAK；等待 engine ready 不得静默超过 ack deadline。

Disposition 时序固定如下：

| Outcome | Durable boundary | Delivery disposition |
|---|---|---|
| applied | applied state + signed lifecycle batch 同事务 commit | ACK |
| verified conflict/stale | immutable outcome + signed terminal fact 同事务 commit | TERM/ACK per producer contract |
| retryable local failure | desired record 已 durable；未写 terminal outcome | bounded NAK/backoff |
| retry budget exhausted | terminal outcome + signed terminal fact 同事务 commit | TERM |
| invalid signature/schema/version | untrusted rejection + pending security/DLQ receipt commit | TERM |
| durable commit failure | none | NAK；禁止 ACK/TERM |

Outbound fact publisher 只有收到 PubAck 才删除 existing outbox row。Inbound disposition 与
outbound PubAck 不能复用字段、watermark 或状态枚举。

## Crucible Producer-First Contract

Custos 不得自行修改 parser/golden 后宣称 strict command 完成。前置 producer slice
必须由 **Crucible Plan 89** 独立 plan-first 实现。Custos `19d-T8a` 必须先独立发布
RunnerFact schema/golden/capability candidate；**Crucible Plan 90** 随后在 Custos runtime RC
前提供 Phase-A compatibility receipt，并在该 RC/final-candidate 之后提供 Phase-B real
runtime round-trip receipt：

1. 定义 public、versioned `runner_runtime` 和 `code_provenance` schema。
2. CreateDeploymentSpec/control producer 从 typed model 生成 command。
3. 分离 artifact digest、manifest digest 和 optional source hash。
4. 生成新的 canonical golden fixture。
5. 在 clean landed commit 上记录 repo、SHA、schema digest 和 fixture digest。
6. Custos byte-identical 消费 fixture。
7. Custos `19d-T8a` 发布 immutable RunnerFact schema/golden/capability candidate；此步不依赖 Plan 90。
8. Crucible Plan 90 Phase A 证明该 candidate 的双仓 schema/digest/projector compatibility。
   Phase A must also consume `CR89-0116-GENERATION-STORAGE`; fixture-only compatibility cannot replace the durable generation-storage receipt.
9. Crucible Plan 90 Phase B 消费 exact Custos runtime RC/final-candidate，证明真实
   command → execution → RunnerFact projector round trip。

在 producer receipt 到达前：

- Task 1 可执行；
- journal/engine characterization tests 可起草；
- 不得冻结 strict parser、发布 RC 或声称 runtime contract complete。

## Additive Engine Lifecycle

现有 engine protocol 的以下能力必须保留：

- deploy；
- reconfigure；
- stop；
- supports live/venue；
- open notional；
- connectivity state；
- flatten；
- positions/open orders/status snapshots。

Plan 19 只做 additive extension：

```python
@dataclass(frozen=True)
class EngineReadyReceipt:
    deployment_instance_id: UUID
    deployment_spec_id: UUID
    deployment_spec_digest: str
    generation: int
    ready_at_ns: int


@dataclass(frozen=True)
class EngineTerminalEvent:
    deployment_instance_id: UUID
    deployment_spec_id: UUID
    generation: int
    reason_code: str
    retryable: bool
```

Readiness 至少证明：

- node task alive；
- required data connectivity ready；
- required execution connectivity ready；
- portfolio/account initialization complete；
- reconciliation initialization complete；
- strategy registered and accepting runtime lifecycle；
- mode-specific mandatory capabilities active。

`create_task()` 不等于 ready。长期 task 的异常必须传播到 supervisor。

每个 instance 必须有 bounded restart budget、exponential backoff 和 quarantine。
超过预算后发布 deterministic terminal lifecycle fact，不得无限 crash loop。

## Nautilus Portfolio and Local Safety

Portfolio snapshot 必须使用 NT 1.230.0 的真实 API：

- `portfolio.equity(venue)`；
- 每个 position 的可信 mark price；
- `position.unrealized_pnl(mark_price)`；
- 缺失数据时标记 unreliable，breaker fail closed；
- EngineStatus、breaker、position/equity facts 使用同一个 snapshot provider。

禁止用 open notional + unrealized PnL 冒充 equity。

### Risk taxonomy

| Layer | Meaning |
|---|---|
| Toolkit advisory sizing | strategy-local recommendation, non-authoritative |
| Custos mandatory local safety | exact enforcement of signed policy |
| Crucible canonical risk policy | portfolio/risk authority, approvals and limits |

### Runner-level cap

Final live architecture 必须消费 **Crucible Plan 99 runner-safety-policy-authority** 生产的
独立、签名、版本化 runner-level cap policy。不得从“最后收到的 deployment command”推断
全 runner authority，也不得把 runner policy 塞回某个 DeploymentSpec 的 `risk_config`。

在 producer policy 未完成前，唯一允许的临时行为是：

- sandbox/testnet only；
- effective cap 取所有 active desired instances 中最严格值；
- capability 明确为 provisional/false for live；
- live fail closed。

Reservation contract：

- key: `(deployment_instance_id, client_order_id)`；
- 原子 reserve；
- reject/cancel 释放全部 reservation；
- replace 原子调整差额；
- partial fill 将 reserved 转为 filled exposure；
- fill/close 更新 exposure；
- duplicate event 幂等；
- policy revision、reservation 和 exposure checkpoint 与 desired/applied state 使用同一
  SQLite deep module；
- restart 从 durable reservation/checkpoint 恢复，并用可信 engine/venue state 对账重建；
- flatten、close 和 reduce-only exit 永远不得被 cap 阻止。

必须在不可绕过的 engine/order-intent boundary 证明覆盖所有策略提交路径。
禁止 monkey patch、SuperTrend-only hook 或 docs-only enforcement。

## RunnerFact Compatibility

现有 signed RunnerFact stream 是唯一 runtime output。不得恢复 unsigned
telemetry/status uplink。

新 lifecycle/log/fact type 必须同时具备：

- capability manifest revision；
- Custos schema/version registration；
- Crucible verifier/projector compatibility；
- onboarding/fixture receipt；
- deterministic event identity；
- redaction tests。

在 Crucible 定义 canonical risk-decision fact 前，local deny/reject 使用已有
`RunnerRuntimeLogFact.v1` 的 sanitized structured event，不新增 Custos-owned
canonical business fact。

`docs/design/telemetry_actor.md` 已描述 typed RunnerFact。应原子 rename 为
`docs/design/runner_fact.md`，同步更新 CLAUDE、authority manifest/checker 和 active
references；清晰标记的 historical plan references 可以保留。

## Mode and Capability Isolation

必须增加 negative tests：

- sandbox credential/policy 不能启动 testnet/live；
- source-path 不能启动 live；
- missing signed cap policy 不能启动 live；
- unsupported engine capability 不能被 spec 参数打开；
- reduce-only/flatten 即使 cap exhausted 仍可执行；
- mode mismatch、tenant mismatch、instance mismatch terminal reject；
- secret/raw credential/full order payload 不进入 journal、facts 或 logs。

## Architecture

```text
Crucible typed producer
  │ signed canonical command + exact artifact/policy bindings
  ▼
JetStream command consumer
  │ verify + fingerprint + term/NAK/in_progress policy
  ▼
single RunnerStateStore / SQLite transaction boundary
  ├── desired_deployments
  ├── applied_deployments
  ├── existing stream sequence + event dedup
  └── existing signed RunnerFact outbox
  │
  ▼
DeploymentReconciler
  ├── additive EngineReadyReceipt
  ├── EngineTerminalEvent
  ├── restart budget/backoff/quarantine
  └── signed policy enforcement
  │
  ▼
NtTradingNodeHost
  ├── exact artifact verification
  ├── reliable portfolio snapshot
  ├── native per-order safety
  └── runner-level cap at engine boundary
  │
  ▼
signed RunnerFacts ──PubAck──> existing outbox deletion
```

## File Inventory

| 文件路径 | 操作 | 描述 |
|---|---|---|
| `.forge/plans/2026-07/19-crucible-command-runner-fact-runtime-convergence.md` | 修改 | 本 live-plan 修订 |
| `.forge/README.md` | 修改 | 修订 hard gates 和说明 |
| `src/custos/contracts/deployment.py` | 修改 | producer-backed strict contract |
| `src/custos/core/runner_fact.py` | 修改/内部重构 | 唯一 state/outbox transaction boundary |
| `src/custos/core/deployment_reconciler.py` | 修改 | fingerprint/durable reconcile/restart |
| `src/custos/core/nats_client.py` | 修改 | ACK deadline, bounded retry, term/DLQ |
| `src/custos/core/nats_transport.py` | 新增 | Local NKey custody, JWT/TLS transport profile, rotation and revocation evidence |
| `src/custos/core/per_key_vault.py` | 修改 | Encrypted generation-keyed NKey/JWT material; no control-plane secret copy |
| `src/custos/core/engine_protocol.py` | additive 修改 | ready/terminal without protocol shrink |
| `src/custos/core/local_cap.py` | 修改 | signed policy/reservation semantics |
| `src/custos/core/runner_deployment_lifecycle_fact.py` | 修改 | deterministic IDs |
| `src/custos/core/runtime_log_fact.py` | 修改 | sanitized local safety events |
| `src/custos/cli/_daemon.py` | 修改 | structured supervision |
| `src/custos/engines/nautilus/host.py` | 修改 | readiness/equity/risk |
| `src/custos/engines/nautilus/risk.py` | 修改 | native per-order config |
| `src/custos/engines/nautilus/portfolio_snapshot.py` | 新增 | shared reliable snapshot |
| `src/custos/engines/nautilus/runtime_loader.py` | 新增 | verified activation 的唯一 V1 entry-point loader |
| `src/custos/core/runner_command_runtime.py` | 新增 | command/resolver/activation/engine/ACK 单一路径 |
| `src/custos/core/deployment_reconciler.py`, `src/custos/core/g6_gate.py` | 删除 | 移除 path/hash 驱动的平行部署链 |
| `tests/test_runner_fact_outbox.py` | 新增 | direct characterization + transaction tests |
| `tests/test_runner_deployment_command_golden.py` | 修改 | producer exact-byte/fingerprint tests |
| `tests/test_runner_command_runtime.py` | 新增 | resolver/activation/lifecycle/ACK/retry tests |
| `tests/test_runner_deployment_command_golden.py` | 修改 | exact Crucible receipt |
| `tests/test_runner_fact_parity.py` | 新增 | capability/projector matrix |
| `tests/integration/test_crucible_runner_runtime.py` | 新增 | current real acceptance |
| `tests/test_nats_transport.py` | 新增 | Enrollment, local custody, TLS, exact ACL/durable and fail-closed configuration |
| `tests/test_order_reservation.py` | 新增 | Atomic runner-wide reserve/replace/cancel/fill/close/rebuild and restart recovery |
| `tests/integration/test_nats_revocation.py` | 新增 | Real NATS rotation, forced disconnect and old-generation reconnect denial |
| `docs/design/telemetry_actor.md` | rename | `docs/design/runner_fact.md` |
| `docs/authority/**`, `CLAUDE.md` | 修改 | producer/projector receipts and active references |
| `docs/ops/05-deployment.md` | 修改 | v0.4 mode/policy runbook |
| `Makefile` | 修改 | current verification targets |
| `pyproject.toml`, release workflow | 修改 | RC/final version and gates |

明确禁止新增第二个 `runner_fact_outbox` table 或独立 journal database。

## Approved production roadmap and normative stop gates

This section amends Tasks 2-10 and is part of the DoD for existing slices 19a-d.
It does not create Plan 20, a second journal or a second outbox. Desired state,
applied state, command outcomes, lifecycle enqueue, policy/reservations and the
existing RunnerFact outbox remain one SQLite deep module and one transactional
authority boundary.

### Receipt DAG

```text
R-C18-T5C-PRESIGN -> R-C18-TOOLKIT-RC -> R-PS54-ARTIFACT-BOM
  -> R-CR88-STRATEGY-RELEASE -> R-C18-T5D-A-EVIDENCE-CONSUMER
  -> R-CR89-DEPLOYMENT-COMMAND -> R-C18-T5D-B-C19-T2-COMMAND-CONSUMER
  -> C19-T3-COMMAND-INGRESS -> C19-T4-DURABLE-RECONCILE -> C19-T5-ENGINE-ADAPTER
  -> Crucible Plan 99 migration 0117 -> R-CR99-RUNNER-POLICY -> C19-T7-RUNNER-SAFETY
  -> R-CR100-NATS-TRANSPORT-REVOCATION -> C19-T7C-NATS-TRANSPORT-CONSUMER
  -> R-C19-RUNNER-FACT-CANDIDATE -> CR89-0116-GENERATION-STORAGE -> R-CR90A-FACT-COMPAT
  -> R-C19-RUNTIME-RC -> R-CR90B-RUNTIME-ROUNDTRIP
  -> R-PS56-EXACT-IMAGE -> R-C19-SAME-DIGEST-PROMOTION
```

Crucible Plan 99 migration 0117 is the first Plan 99 runtime slice after the
signed-command receipt. It must be applied and verified in each physical mode
database before policy publication or Custos live-policy consumption. Its receipt
ID is `R-CR99-M0117`; the completed signed policy receipt is
`R-CR99-RUNNER-POLICY`.

### Slice integration

| Slice | Additional mandatory scope | Deliverable | Production stop gate |
|---|---|---|---|
| 19a command and provenance | Consume `R-CR89-DEPLOYMENT-COMMAND`; install a runner-local signed trust bundle for command keys with monotonic version, overlap rotation, revocation, expiry and rollback protection; acquire artifacts into a content-addressed cache; verify complete BOM/signature/attestation before making bytes eligible | Command/fingerprint vectors, durable rejection receipt, trust-bundle receipt, verified cache object receipt | Unknown/revoked/expired key, trust-version rollback, digest/BOM mismatch, non-content-addressed path or ACK before durable outcome stops apply and live execution |
| 19b durable reconcile and lifecycle | Extend the existing SQLite deep module for desired/applied/outcome and artifact activation metadata; atomically activate verified cache content, persist prior generation for rollback, support offline restart and bounded GC; freeze risk-increasing execution and enter quarantine if lifecycle/RunnerFact enqueue is not durable; harden SQLite migration/version, WAL/checkpoint, permissions, disk-full, corruption, backup/restore and power-loss recovery | `commit_applied_and_enqueue_lifecycle()` receipt, atomic activation/rollback receipt, offline restart/GC receipt, crash/power-loss matrix and restored-state receipt | No second journal; disk-full/corrupt/schema mismatch/WAL failure means no ACK and no new apply; enqueue failure freezes and quarantines rather than logging and continuing |
| 19c portfolio and local safety | Require `R-CR99-M0117` and `R-CR99-RUNNER-POLICY`; verify policy-key trust with the same rotation/revocation/rollback discipline; enforce runner-level cap on every risk-increasing order path; keep live fail closed when policy is absent, expired, revoked or mode-mismatched | Real marked portfolio receipt, signed-policy receipt, order-boundary allow/deny matrix, restart persistence receipt | DeploymentSpec mutable risk config cannot define runner aggregate cap; missing migration/policy or unenforced cap blocks live |
| 19d facts, runtime RC and promotion | First consume the exact CR100 transport authority and prove locally generated NKey custody, JWT/TLS authentication, exact durable binding and revocation evidence; then exercise enqueue-freeze/quarantine and SQLite recovery with real RunnerFacts; pin Docker base image by digest and install dependencies from the committed lock without live transitive resolution; emit metrics for command lag/outcome, desired/applied drift, SQLite/WAL/disk, cache/activation, outbox age, fact PubAck, policy expiry, transport expiry/revocation and restart/quarantine; define SLOs, alerts and operator runbook; promote the exact tested and signed digest | CR100 transport-consumer receipt, RunnerFact candidate, runtime RC with complete BOM/SBOM/signatures/OCI provenance, metrics/SLO/alert evidence, recovery runbook, Phase B/PS56 receipts and same-digest promotion receipt | Plaintext/anonymous NATS, wildcard mode filters, runner-only durable identity, signer/admin material, missing revocation attestation, unpinned base/dependency, missing observability/runbook, skipped recovery, changed digest, rebuild or stable-tag promotion before CR90B/PS56 stops release and close-out |

### Artifact cache and activation invariants

1. Cache keys are cryptographic content digests, never strategy names, mutable
   tags, filesystem paths or DeploymentSpec IDs.
2. Download/stage occurs outside the active slot. Signature, attestation, complete
   BOM and every member digest are verified before atomic activation.
3. Activation and applied-generation metadata commit through the existing SQLite
   authority; a crash exposes either the old complete generation or the new
   complete generation, never a mixed directory.
4. Rollback selects a previously verified immutable generation and records a new
   lifecycle fact. It never rewrites history or bypasses generation fencing.
5. Offline restart may load only an already verified, non-revoked cache object
   bound to durable desired/applied state and a valid local trust/policy snapshot.
6. GC is bounded and cannot remove the active generation, rollback generation,
   pending desired generation or any object referenced by an un-PubAcked fact.

### SQLite and fact-durability invariants

1. Migration version and schema fingerprint are checked before subscription
   readiness. Unknown or partial migration is fail closed.
2. WAL/checkpoint policy, fsync behavior, file/directory permissions and free-disk
   thresholds are explicit and observable.
3. Disk-full, I/O error, corrupt page, failed checkpoint or failed lifecycle/fact
   enqueue prevents command ACK and new risk-increasing execution.
4. Backup/restore proves desired/applied/outcome, activation, policy/reservation,
   stream sequence, dedup and outbox consistency. Power-loss tests cover every
   transaction boundary.
5. The RunnerFact bridge may not catch, log and continue after a durability
   failure. It freezes risk-increasing work, records readiness failure when
   possible and moves the affected runner to quarantine for operator recovery.
6. `deployment_instance_id` is the sole runtime stream identity. The durable
   stream key and NATS subject identity are tenant + mode + runner + instance;
   `deployment_spec_id`, `deployment_spec_digest` and `generation` are signed
   fencing/provenance fields only and MUST NOT allocate a new stream or reset
   sequence.
7. This is the first production stream contract. No spec/digest-keyed stream,
   cutover table, migration API or compatibility parser exists. The instance-keyed
   stream starts at sequence 1 and continues across spec/generation changes.

### Observability and release acceptance

- Metrics and alerts cover oldest command age, outcome counts, desired/applied
  generation drift, restart budget, quarantine age, policy expiry, SQLite/WAL/disk,
  artifact cache/activation, RunnerFact outbox depth/age and PubAck latency.
- SLOs state thresholds and paging conditions; the runbook identifies safe retry,
  restore, rollback, re-enrollment and quarantine-release procedures.
- Docker base image and every runtime dependency are locked. BuildKit provenance,
  SBOM and signatures bind the complete runtime BOM.
- Candidate, CR90B, PS56 and stable promotion all use the same image digest. Any
  rebuild or digest change creates a new candidate and invalidates old receipts.
- Speculum is not a runtime, release or close-out gate. The PS legacy
  `build-image.sh` to Crucible Python image lane remains independent compatibility
  only and cannot satisfy a Custos schema, runtime or acceptance receipt.

## Canonical Slices and Stop Gates

Plan 19 是 `multi_session_scope: true`，只允许按以下四个 slice 顺序推进。每个 STOP gate
未满足时不得进入下一 slice，不得用本地 fixture 或 provisional capability 宣称完成。

| Slice | Tasks | START gate | STOP gate |
|---|---|---|---|
| **19a command/provenance** | T1-T3 | T1 可立即执行；T2/T3 等 Crucible Plan 89 clean landed producer | exact producer SHA/schema/golden；冻结 fingerprint；所有 command outcomes commit-before-ACK/TERM |
| **19b durable reconcile/lifecycle** | T4-T5 | 19a STOP | 单一 SQLite deep module；success/terminal outcome 原子；RunnerFact signed generation fence；additive engine readiness/restart/quarantine |
| **19c portfolio/local safety** | T6-T7 | 19b STOP；Plan 99 policy schema 可用 | reliable portfolio；signed policy + durable reservation/exposure recovery；live non-bypass proof |
| **19d facts/release acceptance** | T8-T10 | 19c STOP；T8a 无 Plan 90 依赖 | T8a candidate → Plan 90 Phase A → runtime RC/final-candidate → Plan 90 Phase B → PS Plan 56 exact-candidate acceptance → unchanged promotion/close-out |

无环 release graph：

```text
Custos Plan 18 T5c StrategyArtifactRefV1
  -> PS Plan 54 BOM/statement/detached attestation
  -> Crucible Plan 88 ArtifactEvidence/acceptance
  -> Custos Plan 18 T5d-A producer-owned evidence consumption
  -> Crucible Plan 89 signed command producer
  -> Custos Plan 18 T5d-B == Custos Plan 19 T2 command consumption
  -> Custos Plan 19 T3 fingerprint/ACK
  -> Custos Plan 19 T4 single durable store + instance-only stream migration
  -> Custos Plan 19 T5 engine readiness/supervision
  -> Crucible Plan 99 runner-safety-policy-authority
  -> Custos Plan 19 T7 signed local safety
  -> Crucible Plan 100 runner NATS transport authority/revocation receipt
  -> Custos Plan 19 T7C local NKey/JWT/TLS transport consumer receipt
  -> Custos 19d-T8a immutable RunnerFact schema/golden/capability candidate
  -> Crucible Plan 90 Phase-A compatibility receipt
  -> Custos Plan 18 staged candidate + exact final selected in the release BOM
  -> Custos Plan 19 runtime RC / exact final-candidate image
  -> Crucible Plan 90 Phase-B real runtime round-trip receipt
  -> PS Plan 56 exact final-candidate image acceptance
  -> promote the same Custos bytes unchanged
  -> Custos Plan 19 close-out
```

Handoff 不得倒置：Plan 90 Phase A 不是 19d START gate，而只是 runtime RC gate；
Plan 90 Phase B 不是 runtime RC prerequisite，而是该 exact RC/final-candidate 的 consumer；
PS Plan 56 依赖 exact final-candidate digest，不依赖 Plan 19 `Completed` 状态。任何失败都
在唯一 V1 内修正并从 Phase A 起重跑受影响 receipts，禁止验收后重建或 repoint。

## Tasks

### Task 0: Repair the live plan

1. 用本修订替换 second-outbox、spec-keyed journal、Custos-only schema、
   last-command cap 和 protocol shrink。
2. 更新 Custos index。
3. 同步修订 PS Plan 53 的 T4、dependency graph 和 consumer requirements。
4. 只 stage 计划和索引，提交：

```bash
git commit -m "docs(custos): repair plan 19 runtime convergence design"
```

### Task 1: Restore verification floor

这是当前唯一无外部契约依赖、可立即执行的 implementation task。

1. 固定当前 formatter/Makefile failure。
2. 只做机械 formatting，不夹带语义修改。
3. 把 `verify-runtime-existing` 改到 current integration target。
4. 验证：

```bash
make verify
make verify-nt
make -n verify-runtime-existing
```

提交：

```bash
git commit -m "style(custos): restore runtime verification floor"
```

### Task 2: Land and consume the Crucible Plan 89 producer contract

Hard gate：Custos Plan 18 T5d-A STOP；Crucible Plan 89 独立 plan-first、typed
producer、new golden、clean landed SHA。Crucible Plan 90 在此只登记后续
schema/golden compatibility receipt owner；Task 2 不生成、不要求也不消费该 receipt。

1. 仅在 Crucible 生成 V1 DeploymentSpec/domain-event schema 和 golden；
   Custos 不生成、不重定义、不发布 command schema。
2. 记录 producer SHA、schema digest、fixture digest。
3. Command 只携带 Crucible-owned canonical DeploymentSpec，并绑定
   `strategy_release_id`、release snapshot/artifact/manifest digest 与完整
   instance/spec/generation provenance。完整 BOM、attestation、evidence 和
   acceptance 由 authenticated StrategyRelease resolver 提供，禁止复制进
   command 或形成第二真相。
4. Custos 先写 byte-identical、unknown version、missing field、release
   binding mismatch 和 digest mismatch tests。
5. 删除 legacy `parameters`/`code_hash`/command-provided `strategy_path`
   fallback；显式 sandbox `DevelopmentSourceRefV1` 不是 production fallback。
   它只能由 authenticated Crucible material resolver 返回，路径必须约束在
   runner 配置的 content-addressed dev artifact root 内；command wire 继续只
   绑定 release/spec/instance digests，不携带本地路径。
6. 双仓 compatibility gate 必须从同一 fixture/schema 计算。
7. 本 Task 与 Plan 18 T5d-B 是同一 implementation slice、consumer model 和
   receipt；禁止各自实现一套 DTO 或验收链。

> **Execution status (2026-07-23)**: Task 2 consumer code uses the sole
> `CrucibleRunnerDeploymentCommandV1` DeploymentSpec event and is
> `READY_DEVELOPMENT_RUNTIME_PENDING_REAL_COMMAND_RECEIPT`. Custos pins Crucible
> commit `57783973375055cd8af7cc4e681314a0b633f6fa`, the exact command golden and
> the CR100
> `crucible.runner.command.v1.<tenant>.<runner>.<mode>` subject. Custos retains
> exact signed event bytes and computes the sole command fingerprint from
> subject + exact event bytes; outer signature rotation cannot change that
> identity. Commit `b0655ef725b88a1d5d6c75176d1658253dc3ed15` makes the
> sandbox development runtime resolve an instance/spec/generation-bound
> `DevelopmentSourceRefV1` directly from the machine-authenticated Crucible
> material authority, re-verify the returned binding and confine the absolute
> path to the configured content-addressed root. StrategyRelease material is
> never embedded in the command. Runtime readiness remains fail closed until
> the real PG/NATS/engine command-to-fact receipt and authenticated Custos
> production StrategyRelease consumption exist.

Development execution is a separate, non-promotable acceptance slice: a sandbox
command may resolve a Crucible-owned development-artifact registration to
`DevelopmentSourceRefV1`,
verify the immutable local inventory and run through the same durable desired/applied,
lifecycle and RunnerFact machinery. It does not relax the production resolver,
Sigstore policy, live runner cap or runtime-RC gates.

Custos 提交：

```bash
git commit -m "feat(custos): consume Crucible runner command contract"
```

### Task 3: Implement command fingerprint and bounded ACK policy

1. 按冻结的 domain+subject+exact-event-bytes 算法写 cross-language vectors。
2. 写 same-generation/different-payload terminal conflict tests。
3. 写 invalid signature/schema poison durable-rejection-before-term tests。
4. 写 stale/retry-exhausted durable terminal outcome tests。
5. 写 long readiness `in_progress()` 和 bounded retry tests。
6. 显式配置 ack wait、max deliveries、backoff 和 quarantine。
7. inbound ACK 状态不得与 outbound PubAck 混用。

> **Execution status (2026-07-15)**: `PREPARED_FOR_T4_DURABILITY`.
> Fingerprint/authentication/intake/disposition policy and the focused
> crash/restart/redelivery matrix are implemented. The production durability
> adapter remains Task 4; T3 does not ACK a newly prepared command and cannot
> claim durable/runtime readiness before that adapter exists.

提交：

```bash
git commit -m "feat(custos): enforce command identity and delivery policy"
```

### Task 4: Extend the existing RunnerFact SQLite deep module

先写 direct `RunnerFactOutbox` characterization tests，锁定 seq、dedup、sign、
pending 和 PubAck deletion。

然后：

1. 在同一 store/connection 增加 desired/applied tables。
2. primary key 使用 `deployment_instance_id`。
3. 增加 command outcome、runner policy、reservation 和 exposure checkpoint tables。
4. 实现 verified/untrusted durable outcome APIs；`commit_applied_and_enqueue_lifecycle()`
   作为 success typed wrapper。
5. 将 `RunnerFactAuthority` stream identity 收敛为 tenant + mode + runner +
   `deployment_instance_id`。`deployment_spec_id`、`deployment_spec_digest`、
   `generation` 只进入 signed fencing/provenance header；任一 generation/spec
   变化都不得重置 stream sequence。
6. 以 `deployment_instance_id` 直接初始化首次生产 stream；sequence 从 1 开始，
   spec/generation 变化不重置。不得实现 spec-keyed stream、cutover table、迁移
   API 或兼容 parser。
7. lifecycle event ID deterministic。
8. 覆盖 desired 后 crash、ready 后 commit 前 crash、commit 后 publish 前 crash、
   duplicate delivery 和 restart replay。
9. 验证没有第二个 outbox/database；artifact activation、desired/applied、
   command outcome、policy/reservation 和 fact outbox 全部共享这一 SQLite
   authority 与 migration ledger。

> **Execution status (2026-07-15)**: `READY_DURABLE_STATE_STORE_ONLY`.
> The sole RunnerFact SQLite database/outbox now implements the T3 durability port,
> exact command/outcome persistence, sole instance-continuous stream initialization and atomic
> applied-state + signed lifecycle enqueue. No engine apply, supervision or daemon
> wiring is included; Task 5 remains the runtime boundary.

提交：

```bash
git commit -m "feat(custos): persist reconcile state in runner fact store"
```

### Task 5: Add engine readiness, terminal lifecycle, and supervision

1. characterization tests 锁定全部现有 engine protocol methods。
2. additive 增加 ready/terminal APIs。
3. readiness 覆盖 task/connectivity/portfolio/reconciliation/strategy/capabilities。
4. 实现 timeout、restart budget、backoff 和 quarantine。
5. daemon 任一长期 task 意外退出时取消 siblings 并返回非零。
6. shutdown 顺序：停止 intake → stop deployments → flush fact outbox →
   close NATS/store。

Execution checkpoint (2026-07-15):

- RED proved the additive lifecycle module and structured daemon supervisor were absent.
- GREEN adds exact `EngineLifecycleAuthority`, seven-check `EngineReadyReceipt`, typed
  `EngineTerminalEvent`, bounded restart/backoff/quarantine and restart replay without
  duplicate deploy. Restart state persists in the existing `command_in_progress_lease`;
  ready and terminal outcomes reuse the T4 atomic lifecycle transaction.
- The daemon now fails on any unexpected long-running task exit, cancels siblings, and
  shuts down in intake/deployment/fact-flush/transport order.
- Focused lifecycle/store/protocol and host/daemon/watchdog/breaker suites are 52 passed.
- Status remains `PREPARED_BLOCKED_ARTIFACT_RUNTIME_CAPABILITY`: the real Plan 18 T5e
  capability is false, team daemon composition is disabled, and live remains false.

Canonical V1 reset checkpoint (2026-07-21):

- Engine deploy now requires the verified activated artifact as a third ABI input.
- `RunnerCommandRuntimeCoordinator` owns intake-to-disposition ordering and durable
  activation replay; the old in-memory `DeploymentReconciler` path is deleted.
- Lifecycle/supervision and the reset path are covered by the current focused
  verification. The daemon intentionally fails closed until an authenticated
  Crucible StrategyRelease resolver is composed.
- Current status is
  `READY_V1_CODE_PENDING_AUTHENTICATED_STRATEGY_RELEASE_RESOLVER`; runtime, live
  and production readiness remain false.

2026-07-23 verification correction: the combined T2-T8a focused suite is now
`118 passed`. The local daemon also published signed RunnerFacts over NATS into
Crucible's instance-continuous stream and reached ARX through the owner projection.
This proves the local sandbox path only; it does not satisfy the authenticated
production StrategyRelease or immutable runtime-RC gates.

提交：

```bash
git commit -m "feat(custos): supervise engine readiness and termination"
```

### Task 6: Use reliable Nautilus portfolio semantics

1. 回归测试复现 `decimal.ConversionSyntax`。
2. 实现单一 `NautilusPortfolioSnapshotProvider`。
3. 使用 `portfolio.equity(venue)` 和
   `position.unrealized_pnl(mark_price)`。
4. 缺可信 mark/equity 时标记 unreliable，breaker fail closed。
5. status、breaker 和 RunnerFacts 共用同一 snapshot。

Execution checkpoint (2026-07-15):

- RED reproduced the absent canonical provider and locked the real
  `position.unrealized_pnl(trusted_mark_price)` call against
  `decimal.ConversionSyntax` regressions.
- GREEN adds one `NautilusPortfolioSnapshotProvider`: actual
  `portfolio.equity(venue)`, trusted marked notional and typed unreliable reasons.
- `get_open_notional`, `get_positions`, `get_engine_status` and RunnerFact risk rows
  share that provider. The breaker consumes one status snapshot per tick and freezes/
  flattens fail closed on an exception or unreliable equity/mark.
- Focused provider, Nautilus snapshot, reconciler and breaker suites are 19 passed.
- Status is `READY_RELIABLE_PORTFOLIO_SEMANTICS_ONLY`; runner policy, team daemon,
  live, runtime and production remain false. T7 must wait for Crucible Plan 99.

Canonical V1 correction checkpoint (2026-07-21):

- The deleted in-memory `DeploymentReconciler` is not restored. A narrow
  `EngineSafetySupervisor` now owns the only breaker tick and consumes exactly
  one `EngineStatus` snapshot per active `deployment_instance_id`.
- Unreliable or failed snapshots freeze the shared `FallbackBreaker` and flatten
  locally; the Nautilus execution boundary rejects risk-increasing submissions
  while preserving reduce-only and cancellation paths.
- This is code capability only. The authenticated StrategyRelease resolver and
  team daemon composition remain false, so runtime/live/production stay blocked.
- Fresh focused verification across portfolio, breaker, native order boundary
  and host wiring is `20 passed`.

提交：

```bash
git commit -m "fix(custos): use reliable Nautilus portfolio equity"
```

### Task 7: Enforce signed local safety policy

#### 7A CR99 contract consumer and native per-order safety

1. 固定并验签 CR99 policy contract，不得把 synthetic golden 当 runtime evidence。
2. 从 verified signed Crucible policy 构建 NT public risk config。
3. 黑盒证明超过单笔上限的 order intent 在 engine boundary 被拒。
4. missing/invalid live policy fail closed。

#### 7B Runner-level aggregate cap V1

1. Consume the Crucible-signed V1 aggregate-cap policy and reject unknown,
   expired, revoked, wrong-runner, wrong-tenant, wrong-mode or rollback input.
2. Persist the sole policy revision and reservations in the existing RunnerFact SQLite
   transaction boundary.
3. Intercept every native order submission and reservation change before
   network access; risk-reducing orders remain explicitly modeled.
4. Emit durable RunnerFacts for policy application, breach, reservation and
   release through the existing outbox.
5. Do not copy authorization or approval logic from Crucible.

> **Execution status (2026-07-21)**:
> `READY_PRODUCER_HANDOFF_PENDING_RUNTIME_PUBLICATION_RECEIPT`. Custos now pins the exact
> CR99 producer code commit `d52bb16`, handoff commit `fe93008`, schema, golden,
> producer receipt and SHA sidecar; the current exact-contract, durable
> revision/reservation slice is `20 passed`. Policy identity is one immutable
> `policy_id` per revision with an exact prior reference; the former parallel
> `policy_version`/`generation` axis is deleted. Signed event/outbox publication,
> real mode-database migration, NATS/PubAck, daemon policy consumption and
> runtime/live promotion remain false.
> Command and policy both consume the sole six-field signed-domain-event V1
> envelope; the temporary policy-only nine-field envelope was deleted.

#### 7C Authenticated NATS transport V1

1. Introduce one local `RunnerNatsTransportSet` per supervisor, keyed by the
   closed `TradingMode` enum. The set is composition only and never represents
   cross-mode authority.
2. Store one encrypted V1 vault per enabled mode at the configured transport
   vault directory. Each document contains only that mode's active, pending,
   retiring and revocation state; there is no aggregate document, upgrade
   parser or compatibility fallback.
3. Generate a distinct user NKey seed for every mode. Consume one Crucible
   authority response bound to exactly one tenant, runner, mode and generation;
   delete `authorized_modes`, multi-mode JWTs, runner-only durables and wildcard
   mode permissions.
4. Verify the closed transport-domain mapping: `sandbox` and `testnet` use the
   SIM NATS account, issuer and `CRUCIBLE_RUNNER_CONTROL_SIM_V1`; `live` uses
   the separately configured LIVE account, issuer and
   `CRUCIBLE_RUNNER_CONTROL_LIVE_V1`. A caller cannot select the domain.
5. Open one independent TLS/NKey connection, exact durable command consumer and
   RunnerFact publisher per enabled mode. Shared process capacity, engine
   coordination and watchdog state remain supervisor-owned and do not widen
   any session permission.
6. Require a mode on enrollment, rotation, activation, revocation and inspection
   CLI operations. Daemon configuration declares enabled modes explicitly and
   readiness reports every mode independently; a healthy SIM session cannot
   mask missing, expired, revoked or drifted LIVE authority.
7. Bind command mode twice: the exact delivery/filter subject mode and the
   verified signed command payload mode must equal the selected session.
   Mismatch is terminal before execution or ACK.
8. Route each RunnerFact batch through the session matching its signed
   `trading_mode`; missing or mismatched session fails closed before publish.
   Inbound command ACK and outbound RunnerFact PubAck remain distinct.
9. Rotation is two-phase, restart-safe and per mode. Revocation requires
   replacement, forced disconnect and old-generation reconnect-denial evidence
   for that mode without disturbing healthy sessions for other modes.
10. Until Crucible V1 credential authority, separate SIM/LIVE broker evidence
    and real per-mode round-trip receipts exist, daemon production readiness and
    all live readiness remain false.

> **Execution status (2026-07-21)**: `DIRECT_MACHINE_CREDENTIAL_V1_READY_PENDING_CONTROL_DATABASE_AND_NATS_RUNTIME`.
> Custos now consumes the owner-generated CR100 machine-request golden and calls
> Crucible directly for enrollment and credential verify/rotate/revoke using the
> sole request-ID/freshness/body/binding proof and `X-Crucible-*` header set. ARX
> is absent from this path. Custos also consumes the exact CR100 NATS authority
> shape, stores one encrypted vault
> per mode, composes a supervisor-local `RunnerNatsTransportSet`, opens independent
> mode sessions, binds the exact CR89 command and CR99 policy filters, double-binds
> payload mode and routes RunnerFact batches by their signed mode. Exact CR100
> schema/golden bytes are vendored and pinned, but credential issuance, durable
> readback, SIM/LIVE broker evidence and production readiness remain mandatory.
> The local `make verify-nats-revocation` gate passes against real
> `nats:2.10-alpine` TLS/User-JWT transport and proves forced disconnect plus
> old-generation reconnect denial; it is not a CR100 or dual-broker receipt.

### Task 8: Complete the sole RunnerFact V1 contract

1. Generate one RunnerFact V1 schema, golden, negative set and asset index; the
   transport subject is exactly `crucible.runner.fact.v1.<tenant>.<runner>.<mode>`.
2. Bind every fact to tenant, runner, mode, deployment instance, deployment
   spec, generation and command fingerprint.
3. Consume Crucible compatibility and persistence receipts only after they bind
   these same V1 bytes; no predecessor stream or parser remains.
4. Preserve idempotent event IDs and projector semantics across retry/restart.

> **Execution status (2026-07-26)**: `PHASE_A_CONSUMER_ACCEPTED_RUNTIME_OPEN`. The sole V1
> asset index, schema, golden, signing preimage and projector matrix are pinned
> to producer asset commit `cce76931884de36c9606db02d94cf4124e7164b5`.
> Crucible consumer code commit `bba16f9fd031cd89b78ac3eaa549346b7bf2f8a0`
> accepts the closed fact union and projector map; receipt commit
> `a36b63a73e7dadf27628df064878b0d6db6e8820` pins those exact producer
> bytes with receipt SHA-256
> `a1ce1b59d11f94e1c44c38efaf651f81dfad3f96694cd0ebf90811b16c4ae873`.
> Phase A exact-byte handoff is complete. The real runtime round trip, immutable
> runtime RC and all live/production flags remain open.

### Task 9: Publish the immutable runtime RC / final-candidate

前置：

- Crucible Plan 89 producer contract receipt；
- Crucible Plan 99 signed cap policy receipt；
- Plan 18 exact staged candidate artifact；
- selected Plan 18 exact final bytes recorded in the release BOM；
- Crucible Plan 90 schema/golden compatibility receipt；
- all mode/capability negative tests。

1. 发布 immutable `0.4.0rcN` wheel/image，并把它声明为本轮 exact final-candidate；
   此时 Plan 19 仍未 Completed。
2. 把 exact candidate coordinate、image digest、SBOM/signature 和 release BOM 交给
   Crucible Plan 90 Phase B。
3. Phase B 运行真实 Crucible command → Custos execute → RunnerFact projector acceptance。
4. 任一 bytes/BOM 变化递增 RC，重新执行 Plan 90 Phase A 和 Phase B；不得覆盖旧 candidate。

### Task 10: Accept, promote unchanged, and close out

Hard gate：Crucible Plan 90 Phase-B real runtime round-trip receipt + PS Plan 56 对 Task 9
exact final-candidate image 的 acceptance receipt。PS Plan 56 不能依赖 Plan 19 Completed；
Plan 19 必须保持 open 直到该 receipt 到达。

1. 验证 Plan 90 Phase B 与 PS Plan 56 都引用 Task 9 的同一 coordinate/image digest/BOM。
2. PS Plan 56 锁 exact final-candidate digest，运行 Docker、mode/capability、durable-state
   recovery 和 operator acceptance。
3. 不重建、不覆盖、不 repoint；把 Task 9 已验收的同一 bytes 原样 promote 为 final。
4. promotion 后重新验证坐标和 digest 未变，再更新 docs、changelog、plan/index 和 receipts。
5. 标记 Completed 并提交：

```bash
git commit -m "docs(custos): mark plan 19 as completed"
```

## Verification

- [x] verification floor 全绿
  - `make verify`: `620 passed, 4 skipped, 1 xfailed`; base mypy `41` files and Nautilus mypy `59` files passed; authority gate passed.
  - `make verify-nt`: `620 passed, 4 skipped, 1 xfailed`.
  - `make -n verify-runtime-existing`: Docker runtime contract and standalone runtime targets resolve without Make drift.
- [x] command schema 来自 clean landed Crucible producer
- [ ] schema/golden/digest 双仓 compatibility gate
- [x] same generation + different fingerprint terminal reject
- [x] fingerprint 与 domain+exact subject+verified exact event bytes cross-language vector 一致
- [x] signer/profile/verification receipt durable；signature bytes 不进入 fingerprint
- [x] poison message term/DLQ，不无限 NAK
- [x] success/conflict/stale/retry-exhausted/invalid command 均 commit-before-ACK/TERM
- [x] long deploy 使用 `in_progress()`
- [x] desired/applied primary key 为 `deployment_instance_id`
- [x] 只有一个 SQLite state/outbox deep module
- [x] applied state 与 lifecycle batch 单事务提交
- [x] existing seq/dedup/sign/PubAck semantics 保持
- [x] stream identity 只有 tenant + mode + runner + `deployment_instance_id`
- [x] spec id/digest/generation 只作 signed fencing/provenance，generation 不重置 sequence
- [x] 首次生产只创建 instance-keyed stream；无 spec-keyed/cutover compatibility
- [x] lifecycle event ID deterministic
- [x] signed RunnerFact header 带 generation，stream key/sequence 不按 generation 重置
- [x] Crucible projector 拒绝或隔离 old-generation facts
- [x] engine protocol 是唯一首次生产 V1，不保留 predecessor protocol
- [x] readiness 覆盖 task/connectivity/portfolio/reconciliation/strategy
- [x] restart budget/backoff/quarantine
- [x] runtime-health V1 从唯一 RunnerFact SQLite store 原子投影 command outcome/lag、desired/applied drift、restart/quarantine、PubAck backlog、policy expiry、SQLite/WAL/disk，并通过 `health --json` 暴露 exact per-mode transport state；聚焦回归 30/30
- [x] equity/unrealized 使用真实 NT API
- [x] runner cap 来自 Crucible Plan 99 signed versioned policy，且不在 DeploymentSpec 内
- [x] policy/reservation/exposure checkpoint durable 并可 restart reconcile
- [x] multi-instance reservation/release/recovery semantics 完整
- [x] flatten/close/reduce-only 不被 cap 阻止
- [x] unsupported live capability fail closed
- [x] NATS User seed is generated and encrypted locally and never leaves Custos
- [x] production NATS requires User JWT/NKey authentication, pinned TLS CA and exact server name
- [x] one supervisor composes a `RunnerNatsTransportSet`; every enabled mode has an independent encrypted vault, seed, JWT, connection, durable, publisher and readiness result
- [x] `authorized_modes`, multi-mode JWT, aggregate transport vault, runner-only durable and wildcard mode permission do not exist
- [x] sandbox/testnet bind only to the SIM account/issuer/stream and live binds only to the LIVE account/issuer/stream
- [x] command subscription uses the exact CR100 tenant+runner delivery subject and durable; no mode wildcard or runner-only durable
- [x] command subject mode and signed payload mode both equal the selected session before execution/ACK
- [x] every RunnerFact batch is published through the session matching its signed trading mode
- [x] command ACK and RunnerFact PubAck remain distinct over the authenticated transport
- [x] per-mode rotation rollback preserves the prior generation; incomplete revocation suspends that mode and cannot be hidden by another mode's health
- [ ] Custos old-generation reconnect-denial receipt is consumed by Crucible before broker revocation completes
- [x] RunnerFact capability revision + Crucible projector receipt
- [x] `telemetry_actor.md` 原子 rename 为 `runner_fact.md`，不删除 typed RunnerFact authority
- [x] sandbox/testnet/live negative matrix
- [x] journal/facts/logs 无 credential 或 secret
- [x] `19d-T8a` candidate 在 Plan 90 Phase A 前独立生成并可消费
- [x] Plan 90 Phase A receipt 只 gate runtime RC，不 gate 19d/T8a START
- [ ] runtime RC/exact final-candidate 后已有 Plan 90 Phase-B real round-trip receipt
- [ ] PS Plan 56 消费 exact final-candidate，不依赖 Plan 19 Completed
- [ ] Phase-B + PS 56 receipts 到达后原 bytes unchanged promotion，再 close-out

## Progress

| Work | State | Current boundary |
|---|---|---|
| Signed command V1 consumer | development runtime focused verified | consumes real DeploymentSpec domain events; exact-event fingerprint, direct Crucible material resolution, contract assets and authority gate pass |
| RunnerFact SQLite V1 deep module | focused verified | one store, one outbox and one instance-continuous sequence |
| Engine lifecycle | local verified; production materialization blocked | 118-test T2-T8a gate passes; production authority contract is consumed at `4ad12b2`, while immutable OCI materialization, trust composition and launched activation remain open |
| Runner policy V1 | producer handoff and local failure matrix verified, runtime blocked | exact `d52bb16` code + `fe93008` producer receipt pinned; commit-before-ACK, NAK/TERM and missing/expired fail-closed pass `22/22`; `0117` mode execution, NATS/PubAck and real daemon consumption pending |
| Machine credential and NATS vault V1 | direct credential contract ready; NATS runtime blocked | Crucible `d9df475` and Custos `09b870c` exact machine-request golden pass; control `0029`, durable replay receipt, JWT/ACL/durable readback and dual-domain broker evidence pending |
| RunnerFact V1 | Phase A exact-byte accepted; runtime open | producer asset commit `7d8f3d3`, Crucible consumer code `e8a5f44` and receipt `3a93a06` are pinned; real runtime round trip and immutable runtime RC remain required |
| Local sandbox runtime | focused code PASS; launched receipt open | instance-bound development material reaches the common runtime path; real NATS delivery/redelivery, engine launch and projected fact receipt remain required |
| Production/live | STOP | StrategyRelease authority bytes are clone-local verified, but immutable materialization, CR99/CR100 real receipts, runtime RC, Phase B and PS56 acceptance remain absent |

The machine-readable boundary is pinned by
`docs/authority/crucible-runner-machine-request-consumer-assets-v1.json` and
`docs/authority/receipts/custos-runner-machine-request-v1-consumer-receipt.json`.
Those records deliberately keep durable replay, per-mode NATS issuance, and
production transport claims false until CR100 migration `0029` and runtime
receipts exist.

## Deviations and Improvements

- Removed the nonexistent runner-command generation and now consumes the two
  actual Crucible DeploymentSpec event types.
- Removed additive SQLite/vault versioning and kept the technically correct
  current shapes as V1.
- Replaced the runner-policy `policy_version + generation` double fence with the
  sole CR99 `revision` axis and immutable per-revision policy identity.
- Replaced generic audit subjects and the temporary policy-only envelope with
  CR100 exact command/policy subjects over one signed-domain-event V1 envelope.
- Deleted the ARX machine-request domain/header/proxy contract in place. Direct
  Custos-Crucible enrollment and credential lifecycle now share one exact
  owner-generated V1 golden; no alias or fallback parser remains.
- Removed planning receipt digests from artifact runtime readiness.

## v1.team Scope

This plan closes Custos local execution only. ARX owns authorization intent,
Crucible owns business decisions and facts, PS owns artifact publication, and
Custos owns local execution plus signed RunnerFacts.

## Quantitative Summary

- Production protocol generations: 1 (`V1`).
- Runtime databases in Custos: 1 SQLite deep module.
- Runtime outboxes in Custos: 1 `runner_fact_outbox`.
- Command event types: 2 canonical DeploymentSpec events.

## 2026-07-22 unified local-runtime checkpoint

The daemon now composes one signed command consumer, signed runner-policy
consumer, RunnerState/RunnerFact deep module, safety boundary and engine
supervisor. The artifact branch is selected by the owner DeploymentSpec:
`development_source` is accepted only for sandbox and resolves through the
machine-authenticated Crucible material authority before Custos confines and
verifies the returned path under the operator-configured root.
`strategy_release` remains fail-closed until authenticated production
consumption is composed. Both branches converge on the same activation,
command fingerprint, safety and RunnerFact path.

This does not claim real transport readiness. Exact SIM/LIVE NATS, redelivery,
restart, policy publication/readback and production engine evidence remain open.
On 2026-07-23, the sandbox development-artifact and command-runtime focused gate
passed `10/10`; that evidence covers only local artifact materialization through
the common runtime path.


## Active T15 contract correction (2026-07-23)

Status: CLONE-LOCAL COMPLETE; LAUNCHED RUNTIME RECEIPT OPEN

- Custos consumes the sole Crucible V1 machine surface: `POST /api/v1/runner-nats/transport-operations` plus authenticated polling of `POST /api/v1/runner-nats/transport-operation-results`. All earlier enroll/rotate/activate/challenge/evidence routes, DTOs and parsers are deleted in place.
- Before the first network call, Custos MUST atomically persist an encrypted pending operation containing `authorization_intent_id`, `operation_id`, exact mode/generation bindings and the locally generated User NKey seed. Restart resumes the same business operation; it never creates a replacement key or operation implicitly.
- Enroll, rotate and revoke consume an explicit ARX-approved `authorization_intent_id`. Custos supplies a canonical NKey possession proof, never the seed.
- Crucible owns signing, provisioning, exact readback, rotation revocation and broker receipts. Custos accepts a User JWT only from a succeeded result, verifies the immutable authority locally, proves the replacement can connect and proves the retired JWT cannot reconnect.
- The local vault has one V1 shape: active credential plus optional pending operation. It has no activation revision, retiring challenge or evidence-submission compatibility state.


T15 clone-local completion evidence (2026-07-23):

- `uv run pytest tests/test_nats_transport.py -q` passed (`17 passed`).
- Focused ruff checks passed for the transport, authority client, CLI and real-NATS integration test.
- Production code contains no superseded lifecycle endpoint, challenge, evidence, activation-revision or old credential-parser symbol.
- Crucible producer handoff is pinned to `e2499bb`; the remaining T15 evidence is one launched signer + provisioner + server worker + Custos begin/poll round trip, not more contract code.


Validation boundary:

- `make verify-base-clean` reached pytest collection but the existing base profile had removed `requests` and the Nautilus toolkit while those tests still imported both unconditionally. No transport test executed in that failed gate; this unrelated profile drift is not repaired in T15.
- The current-session Docker-backed `make verify-nats-revocation` rerun was not authorized by the execution environment. The existing real-NATS receipt remains historical evidence; the launched cross-process round trip remains explicitly false.

## 2026-07-23 development command and material handoff

Crucible producer checkpoint
`57783973375055cd8af7cc4e681314a0b633f6fa` publishes the sole V1 exact-event
command fingerprint and direct, machine-authenticated instance-bound material
resolver. Custos consumer checkpoint
`b0655ef725b88a1d5d6c75176d1658253dc3ed15` pins the producer golden, removes
the duplicate producer-fingerprint concept and consumes the development
material response without reconstructing a path from the command digest.

The command-intake, durable store, development-artifact and material-authority
focused gate passed `48/48`; focused mypy, Ruff, generated-asset drift,
standalone authority and diff gates also passed. This closes the clone-local
development command/material implementation. Real PG/NATS delivery,
redelivery/restart, launched engine and projected RunnerFact evidence remain
open; no production StrategyRelease readiness is inferred.

## 2026-07-23 production StrategyRelease authority handoff

Crucible `10b85e4486cf4d422f8d0703a9ad69cab9f1c08a` now returns the complete immutable
StrategyRelease authority through the same instance-bound machine endpoint used
by development material. Custos `4ad12b2aba2e1865b2921434d9f053bc036fa75a`
strictly consumes the mutually exclusive production branch, validates exact
snapshot/evidence/binding semantics and separates owner resolution from local
byte materialization.

The focused production-authority consumer gate passes 30 tests, Ruff and mypy;
the Crucible producer passes store 3/3, fmt and server-http compile gates. The
daemon still composes the fail-closed production default because immutable OCI
execution-byte acquisition, runner-local signed trust material and a real
StrategyRelease publication receipt are not yet present. No live, runtime-RC or
command-to-fact readiness is inferred.

## 2026-07-23 immutable materialization and daemon composition

Custos `d926ab0` closes the clone-local implementation gap between the
StrategyRelease authority response and the shared activation boundary. The
production resolver now acquires signed detached OCI material from an
allowlisted HTTPS registry, caches it by digest without overwrite, validates
the exact strategy wheel and its required runtime members, and supplies only
verified execution bytes to quarantine and activation.

The daemon composes this resolver only when the artifact cache, signed local
release-policy envelope and key, and trusted Sigstore root are all present.
Partial configuration is a startup error; registry credentials are paired and
the token is environment-only. Development material remains a distinct,
non-promotable branch of the same V1 authority response and is never a fallback.

The focused gate passes 62 tests, Ruff and mypy over all changed modules.
Custos `45d41fad5f7649fef0e93242071e686ee42ab51c` subsequently replaces the daemon's
untyped engine/fact-host factory and variadic lifecycle-store protocol with the
exact first-production interfaces; the daemon and lifecycle module now pass
mypy together, with 33 lifecycle/command/composition tests and Ruff green.

The remaining Plan 19 boundary is launched evidence: real Crucible HTTP/PG,
registry and Sigstore verification, NATS delivery/redelivery, engine action,
atomic applied-state plus lifecycle outbox commit, and signed RunnerFact PubAck.

## 2026-07-23 clean base runtime gate

Custos `3fd8aaa0556da35562e381edcca7aeac51f08cd0` restores
`make verify-base-clean` as an honest gate. Sigstore crypto tests no longer
depend on undeclared `requests` and collect only when the `lts` dependency is
installed; Nautilus-only registry tests collect only with the Nautilus extra.
The framework-independent order reservation boundary now lives in `core`, so
the base `sandbox-sim` daemon can construct owner-policy enforcement without
importing NautilusTrader. The Nautilus adapter supplies only its cache-specific
order semantics.

The final gate passes 581 tests with 14 capability-based skips and one
documented xfail, followed by generated authority drift checks, the 241/241
zero-rewrite extraction gate, and strict mypy closure for both toolkit
packages. The independent `make verify-nt` gate passes 639 tests with 6
capability-based skips and one documented xfail, including the real Nautilus
host, Binance venue, execution safety boundary and toolkit adapter.

This closes the known base-profile and NT-profile local code/verification debt;
it does not replace the still-open launched HTTP/PG, registry/Sigstore,
NATS/engine and RunnerFact PubAck receipts.

## 2026-07-23 RunnerFact authority cleanup

Custos `fcf8df5fd3592257c48cfa22464c1ef6cedf23cf` removes the remaining
`phase_a`, `T8b` and candidate-compatibility fields from the RunnerFact
generator, exact-byte assets, producer receipt, ecosystem authority and drift
checker. Those values were plan progress, not execution capabilities, and
Crucible never consumed them. The current authority keeps only the business
coordinate, exact assets, consumer-receipt slot and runtime/live/production
readiness facts.

The focused contract gate passes 9/9, generated assets are exact, the authority
checker passes and the removed planning names are absent from source, tests and
authority documents.
