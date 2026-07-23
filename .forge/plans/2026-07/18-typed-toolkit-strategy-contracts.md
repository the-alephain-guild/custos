# 18 - Publish typed toolkit and strategy execution contracts

> **Status**: ⏳ In progress — sole V1 contract, sandbox development runtime and authenticated production StrategyRelease authority consumption are focused verified; immutable OCI materialization, daemon composition, PS publication and final cross-repo acceptance remain open
> **Created**: 2026-07-14
> **Revised**: 2026-07-22 for the business-named first-production V1 release relock
> **Project**: Custos
> **Source**: PS Plan 53 strategy/toolkit convergence roadmap and v1.team review
> **For Claude**: Use `/forge:execute` for exactly one canonical slice per session.
> **multi_session_scope**: `true`
> **Depends on**: revised PS Plan 53 authority boundary
> **Hard gates**: T5c corrected pre-sign contract; T5d-A exact PS Plan 54 and Crucible Plan 88 evidence contracts; T5d-B exact Crucible Plan 89 command producer receipt; T5e plus Custos Plan 19 T3-T5 durable runtime gates before production runtime readiness
> **Soft depends on**: Custos Plan 19 integration receipt
> **Original plan-first**: `b898ee1`; this live-plan revision supersedes its erroneous decisions
> **Cross-repo dependency name**: historical requirements may cite the full `Custos Plan 18` artifact-contract receipt, but T5c is pre-sign-only. Production command/runtime consumers MUST depend on the T5d-A corrected evidence receipt plus the T5d-B / Plan 19 Task 2 exact Crucible Plan 89 consumer receipt, never on internal slice aliases.
> **Task 2 READY gates**: exact Crucible Plan 88 consumer requirements-review receipt plus exact PS Plan 54 producer requirements-review receipt for the same Custos producer commit and asset-index digest
> **Downstream, not Task 2 READY gates**: PS Plan 54 immutable artifact/BOM production and Crucible Plan 88 StrategyRelease completion consume the READY Task 2 receipt; they cannot be prerequisites of that receipt

## 2026-07-20 normative first-production V1 contract

This plan defines one production contract generation only.

- Custos owns `StrategyArtifactRefV1`, `StrategyExecutionContextV1`, the
  runner-local pre-import verification receipt, and toolkit authority V1.
- PS owns `StrategyOciArtifactRefV1`,
  `StrategyArtifactOciPublicationReceiptV1`, deterministic artifact bytes and
  internal immutable OCI publication. Publishing an artifact is not production
  deployment approval; Crucible owns that business lifecycle.
- Crucible owns verified publication facts, immutable `StrategyRelease`
  snapshots and signed `DeploymentSpec` desired state.
- Superseded parsers, models, schemas, goldens, indexes, receipts, vendor pins
  and authority entries are deleted in the same coordinated cutover. Git
  history and immutable OCI digests provide audit.
- Future compatible feature additions modify V1 in place. V2 is permitted only
  after a real external production consumer exists and a migration window is
  approved.

### Coordinated cutover order

1. Custos generates the sole V1 execution ABI and toolkit assets.
2. PS consumes those exact bytes and generates the sole V1 OCI publication
   assets without copying Custos ownership.
3. Crucible consumes the exact Custos and PS V1 bytes, persists
   `StrategyRelease`, and publishes signed V1 DeploymentSpec events.
4. Custos resolves Crucible-owned immutable release material, verifies local
   bytes and policy, then activates the engine.
5. All three repositories regenerate local authority manifests and truthful
   readiness receipts from the final bytes.

### Sandbox development material

`DevelopmentSourceRefV1` is the sole V1 development alternative to an attested
production artifact. It is not a compatibility fallback:

- Crucible returns it only from the authenticated preview-deployment material
  resolver for a persisted development-artifact registration and a sandbox
  DeploymentInstance. It never appears as a StrategyRelease snapshot.
- `source_path` must resolve beneath the configured read-only development artifact
  root and name a content-addressed published directory; Custos must not import
  from a PS checkout or accept an arbitrary command-provided path.
- Custos verifies the canonical directory inventory against `source_sha256` before
  loading using its sole `sha256-canonical-directory-v1` profile and records the
  development source digest in durable applied state and RunnerFacts.
- `promotable=false` and `trading_mode=sandbox` are invariant. Testnet/live,
  missing root confinement, symlinks, byte drift or a production runtime profile
  fail closed before import.
- this path produces a development runtime receipt only. It cannot satisfy T5e
  production artifact acceptance, Plan 19 runtime RC or any GHCR release gate.

### CR89 runner resolution producer contract

Plan 18 T5e can close only after Crucible Plan 89 publishes one
instance-authorized runner resolution contract over the reusable Crucible Plan
100 machine-request V1 authentication capability. Plan 88 owns the immutable
StrategyRelease snapshot deep module, but it does not know about runners or
deployment instances. The runtime endpoint is a direct Custos-to-Crucible
interface and MUST NOT reuse the ActorAssertion-protected ARX control-plane GET
route.

- Input binds the authenticated runner, tenant, trading mode, exact
  `deployment_instance_id`, `deployment_spec_id`, spec digest, generation,
  signed command fingerprint and `strategy_release_id`.
- The response returns strict V1 StrategyRelease snapshot fields, exact
  canonical ArtifactRef/BOM/statement/detached-reference bytes, Crucible
  evidence plus its exact digest-input bytes, and the command-specific
  execution binding. It never returns a production filesystem path, trust root,
  selectable policy or mutable tag.
- The signed command binds the immutable StrategyRelease snapshot digest plus
  instance/spec/generation/config authority. Custos recomputes the response
  binding digest and validates its transitive snapshot -> ArtifactRef/BOM/
  evidence chain before using any coordinate; the endpoint does not invent a
  second release schema or response-signing authority.
- Custos fetches immutable OCI execution material into runner-local quarantine
  and independently verifies every byte that can be executed or imported,
  together with the detached statement/bundle and signed local policy, before
  import. Source trees and other non-runtime provenance remain Crucible
  acceptance evidence and are not fabricated as runner-local dependencies.
  Crucible does not download into the runner filesystem.
- Authentication reuses the enrolled Custos machine credential, the sole
  `crucible.runner.machine.request.v1` proof domain and `X-Crucible-*` headers.
  ARX neither relays this request nor receives artifact material.
- The producer receipt must pin the exact request/response schema and golden,
  command signing profile, machine-request proof profile, endpoint, Crucible
  commit, Plan 88 acceptance receipt, control `0029` and mode `0116` heads.
  Until that receipt is consumed, `StrategyReleaseArtifactResolverV1` remains
  unavailable and the daemon reconcile path stays fail closed.

## 上下文 (Context)

Custos 当前仍把公共策略实现放在：

```text
src/custos/engines/nautilus/toolkit/
├── shared/              91 deterministic source inputs
└── vendor/pandas_ta/   150 deterministic source inputs
```

当前实现通过 `sys.path` 暴露顶层 `shared.*`，注册伪造的 `pkg_resources`
distribution，并把 vendored pandas-ta 暴露为顶层 `pandas_ta`。PS 和 Custos
存在公共实现权威漂移。

原 Plan 18 的方向正确，但审查确认以下设计不能执行：

1. `deployment_id: str` 与现有 runtime authority 冲突。
2. Plan 自行冻结 `strategy_key/version/ArtifactRef`，形成第二套 StrategyRelease
   authority。
3. 根 package Python 基线被错误提升到 3.12。
4. source-path 与 production wheel 混成同一发布模型。
5. attestation 缺少 issuer、workflow、bundle 和 trust-policy binding。
6. 241 个当前 donor/vendor deterministic source inputs、契约、发布、四仓切换被当作单次原子任务。
7. `StrategyArtifactRefV1` 必须只包含签名前 execution identity；`bundle_sha256`
   和 verifier-local trust-policy claims 属于下游 attestation/verification receipt，
   放入 pre-sign 对象会形成自引用，禁止进入 production chain。

本修订直接冻结唯一首次生产 V1。被替换文本只由 Git history 保留，不作为兼容契约。

## 目标 (Goal)

发布两个独立、typed、不可变且 Python baseline 清晰分离的 distributions，并提供：

- Python 3.11 compatible `custos-strategy-toolkit` base/contracts distribution；
- Python 3.12-only `custos-strategy-toolkit-nautilus` engine distribution；
- Custos-owned strategy execution ABI；
- Custos-owned artifact schema 与本地 fail-closed verifier；
- Nautilus toolkit 的单一 canonical implementation；
- 对现有 NT 策略业务源码的 zero-rewrite 迁移；
- Crucible StrategyRelease authority 下的 exact artifact execution；
- Custos、PS 和 Crucible v1.team artifact chain 的 staged producer/consumer receipts。

本计划不拥有 StrategyRelease、artifact selection、最终 effective config、部署审批、
组合风控、资本分配或结算。

## Authority Boundary

| 能力 | Canonical owner | Plan 18 职责 |
|---|---|---|
| Strategy source and build input | Philosophers-Stone | 消费 build receipt，不接管策略研究源码 |
| Execution ABI and toolkit implementation | Custos | 定义、实现并版本化 |
| Pre-sign execution artifact ABI (`StrategyArtifactRefV1`) and local verifier | Custos | 生产不含 bundle/policy 的 schema；消费 PS attestation 与 Crucible command 后在本地 fail closed |
| Canonical BOM, signed statement and detached attestation reference | Philosophers-Stone | Plan 54 生产严格 schema、canonical bytes、bundle reference 和 producer receipt |
| Post-bundle `ArtifactEvidenceV1` and release acceptance | Crucible | 原生验证 PS statement/bundle，以本地 trust policy 生成不可伪造 acceptance |
| StrategyRelease lifecycle | Crucible | 消费 Custos schema receipt，生产 released artifact binding |
| Artifact selection and manifest digest | Crucible | 不根据 `strategy_key` 自行选择 artifact |
| Final effective config | Crucible | ABI 仅接收已签名命令绑定的 config |
| Advisory strategy sizing | Toolkit/strategy | 非授权建议，不可放宽 mandatory safety |
| Mandatory local safety | Custos runtime | Plan 19 执行 signed policy |
| Canonical portfolio/risk policy | Crucible | Plan 18 不实现 D2-D4 |

### Independent legacy compatibility lane

现有 PS `build-image.sh` -> Crucible Python image 发布/部署链继续保留为独立
compatibility lane。它不消费或生产 Custos execution ABI、artifact schema 或 receipt，
不属于 v1.team artifact chain，不得作为 team fallback、验收证据或 close-out gate；
Plan 18 也不要求删除、迁移或阻断该链。

`strategy_key` 只能是作者侧 catalog alias。它不是授权 ID、release ID、runtime
address 或幂等 key。

## Runtime Identity

`deployment_instance_id` 是唯一 runtime address。`deployment_spec_id`、
`deployment_spec_digest` 和 `generation` 只提供 provenance/ordering：

```python
from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Literal, Protocol, TypeAlias, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
JsonScalar: TypeAlias = None | bool | int | Decimal | str
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
FrozenJsonObject: TypeAlias = Mapping[str, JsonValue]
ConfigT = TypeVar("ConfigT")
StrategyT_co = TypeVar("StrategyT_co", covariant=True)


class StrategyExecutionContextV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    engine: Literal["nautilus"]
    trading_mode: Literal["sandbox", "testnet", "live"]
    deployment_instance_id: UUID
    deployment_spec_id: UUID
    deployment_spec_digest: Sha256Hex
    effective_config_digest: Sha256Hex
    generation: Annotated[int, Field(ge=1)]


class StrategyRuntimeAdapterV1(Protocol[ConfigT, StrategyT_co]):
    def build_config(
        self,
        effective_config: FrozenJsonObject,
        execution_context: StrategyExecutionContextV1,
    ) -> ConfigT: ...

    def build_strategy(self, config: ConfigT) -> StrategyT_co: ...
```

约束：

- strict model 必须拒绝 legacy `deployment_id`。
- ABI 不得把 `strategy_key` 用作 runtime identity。
- effective config 只能来自已验证的 signed Crucible command；JSON number 使用
  `Decimal` 解析，object/list 递归冻结为 read-only mapping/tuple，禁止把 mutable
  `dict`/`list` 或任意 Python object 交给 strategy。
- runner 必须在调用 adapter 前重算 canonical JSON digest 并匹配
  `effective_config_digest`；adapter 不得重新 merge defaults 或改变已签名 config。
- ABI 不宣称建立安全边界；mandatory order enforcement 属于 Plan 19。

固定 entry-point group：

```text
alephain.strategy_runtime.v1
```

## Contract Freeze Rules

本计划不在 plan 文本中提前冻结未经 producer/consumer requirements review 的完整
`StrategyManifestV1` 或 `StrategyArtifactRefV1` 字段表。Task 2 由 Custos 生产
versioned JSON Schema；Crucible 和 PS 必须先确认 requirements。随后
Crucible Plan 88 消费 exact schema bytes/hash，并拥有 StrategyRelease business binding。

不可降级要求：

- Manifest 是 artifact 内的语义/compatibility metadata，不是 release authority。
- Custos `StrategyArtifactRefV1` 是签名前 immutable execution ABI，只描述 exact
  artifact/manifest bytes、required runtime artifacts、SBOM 和 contract schema；它绝不
  包含 bundle coordinate/digest、certificate/transparency proof、trust-policy identity 或
  release/spec business state。
- PS `StrategyReleaseBomV1` 是严格 object，不是 member array。PS 另行生产严格
  `StrategyReleaseStatementV1` in-toto/DSSE payload 和 detached
  `ArtifactAttestationRefV1`；canonical BOM 与 ArtifactRef 均不得回填未来 bundle digest。
- Crucible `StrategyRelease` 消费 ArtifactRef、完整 PS BOM object 和 detached attestation，
  独立绑定验签后 `ArtifactEvidenceV1`；它不
  绑定、创建或依赖任何 `DeploymentSpec`。
- Crucible `DeploymentSpec` 引用 `strategy_release_id`，并独立绑定 effective config、
  config digest 和 canonical policy references。
- signed runner command 绑定 `deployment_instance_id`、spec id/digest、generation、
  `strategy_release_id`、release BOM digest 和 effective config digest。
- Custos verifier receipt 必须无损回显 command 中的 identity/digests，并证明本地
  loaded bytes 与完整 PS BOM object 的成员投影一致；不得接收独立
  `release_bom_members` 作为第二真相，也不得只验证一个笼统 artifact digest。
- unknown fields 和 unknown schema versions fail closed。
- live/testnet production path 只接受签名 wheel/artifact。
- source-path 仅允许 sandbox development，必须携带 source hash，并明确
  non-promotable、non-live。
- artifact digest、manifest digest、source hash 不得互相代用。

签名与验签对象必须分层：

- `StrategyReleaseStatementV1` 的 signed producer claims 绑定 BOM、artifact、manifest
  subjects，以及 source/build/runtime/toolkit provenance；它不能包含尚未生成的 bundle digest。
- `ArtifactAttestationRefV1` 在 bundle immutable 后绑定 statement coordinate/digest 和
  bundle coordinate/digest；该 detached reference 本身不宣称被同一 bundle 签名。
- Crucible 验签后的 `ArtifactEvidenceV1` / acceptance receipt 绑定 signed claims、完整
  Sigstore proof、ArtifactRef、PS BOM、attestation reference 和 verifier-local
  trust-policy id/version/digest。composite evidence digest 只在验签成功后计算，绝不要求
  原 bundle 反向签名该 composite digest。

Expected trust roots、issuer、workflow identity 和 trust-policy version/digest 必须来自
runner-local、签名验证通过且 immutable 的 Custos release configuration。Artifact、
manifest 或 Crucible command 只能引用该本地 policy digest，不得提供、替换或选择
trust root。Runner 必须先验证 local release configuration，再读取 artifact metadata。

验证必须在 unpack/import 前完成。安全解包拒绝绝对路径、`..`、symlink escape
和 artifact/manifest mismatch。

### Release BOM and digest semantics

Candidate/final receipts 绑定 canonical PS `StrategyReleaseBomV1` object，至少包含：

- base/contracts wheel SHA-256；
- Nautilus wheel SHA-256；
- strategy wheel SHA-256；
- strategy manifest SHA-256；
- 每个 runtime artifact 的 role、size 和 SHA-256；
- SBOM 和 contract schema SHA-256；attestation bundle 只存在于 detached reference；
- PS source repository、exact commit 和 normalized source-tree digest。

`release_bom_digest` 是 canonical BOM bytes 的 SHA-256，只用于完整性索引，不能代替
成员 digest。所有 receipt 必须同时记录 BOM digest 和完整成员表；任一 wheel、manifest、
runtime artifact、SBOM、schema 或 source revision 改变都会产生新 BOM 并使旧 receipts
失效。bundle 改变会产生新的 detached attestation/evidence receipt，但不得倒灌并重写 BOM。
禁止使用“一个 candidate digest”代表多制品 release。

## Python and Runtime Baseline

| Surface | Baseline |
|---|---|
| Root Custos and lightweight contracts | Python `>=3.11` |
| `custos_toolkit` platform-neutral modules | Python `>=3.11` |
| `custos-strategy-toolkit` base/contracts distribution | Python `>=3.11` |
| `custos-strategy-toolkit-nautilus` distribution | Python `>=3.12,<3.13`, exact NT `==1.230.0` |
| PS Nautilus artifact acceptance | Python 3.12, exact NT `1.230.0` |

Importing `custos_toolkit.contracts` on Python 3.11 must not load NautilusTrader,
modify `sys.path` or execute strategy code.

Python packaging cannot express an extra-specific `Requires-Python` constraint safely.
Therefore Nautilus is a separate distribution, not a conditional extra. It must declare
`requires-python = ">=3.12,<3.13"`, depend on an exact compatible base/contracts version and
pin `nautilus-trader==1.230.0` without a `python_version` marker. Installing the Nautilus
distribution on Python 3.11 must fail dependency resolution; silently skipping NT is forbidden.

## Zero-Rewrite Acceptance

迁移必须证明现有 NT 策略的业务信号、indicator、position sizing 和 order intent
源码无需重写。允许的变更仅限：

- package/import namespace；
- manifest/build metadata；
- entry-point wrapper；
- thin engine adapter；
- 因公开 ABI 引入的类型适配。

验收必须包含：

1. 迁移前后的 strategy business-source semantic diff。
2. 固定输入下的 signal/order-intent characterization parity。
3. clean venv wheel-only import。
4. 不依赖 sibling checkout 或顶层 `shared`/`pandas_ta`。

如业务逻辑必须改变，必须移出本计划并由 PS 独立 strategy change plan 审批。

## Architecture

```text
packages/custos-strategy-toolkit/
└── src/custos_toolkit/
    ├── contracts/       # lightweight models + generated JSON Schema
    ├── strategy/        # execution ABI and fixed entry-point loader
    ├── config/
    ├── filters/
    ├── indicators/
    ├── advisory_risk/   # strategy advisory helpers, never canonical policy

packages/custos-strategy-toolkit-nautilus/
└── src/custos_toolkit_nautilus/
    ├── adapter/         # Python >=3.12,<3.13 / NT ==1.230.0
    └── _vendor/
        └── pandas_ta/   # private implementation detail

Custos Task 2 schema/toolkit ─> PS Plan 54 immutable artifact/BOM
PS Plan 54 artifact/BOM ─────> Crucible Plan 88 StrategyRelease acceptance
Crucible StrategyRelease ────> Custos verifier/runtime exact-artifact execution

PS build-image.sh ───────────> Crucible Python image publication/deployment
                               (independent compatibility lane; non-gating)
```

## File Inventory

| 文件路径 | 操作 | 描述 |
|---|---|---|
| `.forge/plans/2026-07/18-typed-toolkit-strategy-contracts.md` | 修改 | 本 live-plan 修订 |
| `.forge/README.md` | 修改 | 修订依赖和说明 |
| `docs/design/strategy-toolkit.md` | 新增 | authority、ABI、risk taxonomy |
| `packages/custos-strategy-toolkit/**` | 新增 | 独立 distribution |
| `packages/custos-strategy-toolkit-nautilus/**` | 新增 | 独立 Python 3.12 NT distribution |
| `src/custos/engines/nautilus/toolkit/**` | 最终删除 | 完成 consumer cutover 后移除旧 authority |
| `tests/test_toolkit_distribution.py` | 新增 | namespace/import/wheel gates |
| `tests/test_toolkit_contracts.py` | 新增 | schema/runtime identity gates |
| `tests/test_toolkit_zero_rewrite.py` | 新增 | semantic/behavior parity |
| `tests/test_toolkit_consumer_receipts.py` | 新增 | v1.team artifact-chain exact receipts |
| `.github/workflows/release-toolkit.yml` | 新增 | reproducible build/sign/release |
| `pyproject.toml`, `uv.lock` | 修改 | workspace 与两个 distribution 的 disjoint Python baselines |
| `CHANGELOG.md` | 修改 | candidate/final release notes |
| `docs/authority/ecosystem-authority.json` | 修改 | 记录 execution ABI/artifact schema authority 和 lifecycle chain |
| `authority-manifest.json` | 修改 | 纳入 toolkit authority/schema artifacts 和 drift inputs |
| `.claude/rules/authority-docs.md` | 修改 | 记录 Task 2 schema receipt 与 precedence |
| `Makefile` / authority checker inputs | 修改 | `make check-authority` 覆盖 schema/BOM/authority drift |

## Canonical Multi-session Slices

本 Plan 是 `multi_session_scope: true`。内部 slice 名只用于 Custos 执行和 handoff；任何
跨仓 plan 必须引用 `Custos Plan 18 Task 2 schema receipt`，不得依赖 `18a` 名称。

| Slice | Tasks | 独立 DoD | Stop gate |
|---|---|---|---|
| 18a Contract authority | T1-T2 | inventory、authority docs、source-generated schemas、lossless lifecycle mapping、Task 2 receipt 和 `make check-authority` 全部 PASS | schema/receipt commit 与 handoff packet 未记录前不得开始 18b |
| 18b Extraction and verification | T3-T5 + T4b | 两个 distributions 可独立构建；3.11 negative install、zero-rewrite、typing closure、deep-frozen config、attestation-before-import gates PASS | 18a exact Task 2 receipt 未锁定，任一 migration batch 未有 parity evidence，或 extracted typing baseline 未清零时停止 production-ready 声明 |
| 18c Immutable RC | T6 | reproducible RC、完整 release BOM、签名、SBOM、local trust-policy binding PASS | RC BOM/成员 digests 未固定前不得请求 consumer receipt |
| 18d Consumer cutover | T7-T9 | PS Plan 54、Crucible Plan 88 和 Custos verifier/runtime candidate/final receipts、final BOM、旧 authority 删除、close-out gates PASS | 仅 v1.team artifact-chain BOM/receipt 不匹配会停止并发布新 RC；不得等待 Speculum |

18b 的 production-ready 声明只在 T3-T5 与 T4b 整体 DoD 全部满足后成立。T3 单独完成只建立
distribution 和 typing boundary，不得标记 18b production-ready，也不得冒充 artifact verifier
或 runtime cutover 已完成。

T4 必须把 canonical implementation **move** 到两个新 distributions。旧
`src/custos/engines/nautilus/toolkit/` 在迁移期间最多保留不含 implementation 的临时 shim；
不得留下第二份 writable canonical source。该 shim 不是 v1.team fallback，并必须在 T8
consumer cutover 后删除。

18d START 只要求 18c immutable RC、PS Plan 54 immutable artifact/BOM receipt 和
Crucible Plan 88 StrategyRelease acceptance。Speculum plan、receipt 或运行结果不是 START、
STOP、candidate/final acceptance 或 Plan 18 close-out 输入。

每个 session 只能推进一个 slice。每个 slice 结束必须提交 handoff packet，至少记录：

1. start base、landed commit 和 touched files；
2. 执行的 exact commands、PASS/FAIL/skip 与环境版本；
3. schema、wheel、BOM、attestation、SBOM 和 source digests；
4. deviations、剩余 blockers 和下一 slice 的 immutable inputs；
5. authority drift 检查结果。
6. 下一 slice 的 START/STOP evidence；只记录 canonical v1.team artifact-chain receipts，
   不把 Speculum 或 legacy compatibility lane 写成 gate。

Slice 可独立 close out，但不能把 slice PASS 冒充整个 Plan 18 Completed。

## Tasks

### Task 0: Repair the live plan

1. 用本修订替换错误 schema、identity、authority 和 Python 决策。
2. 更新 Custos index。
3. 同步修订 PS Plan 53，不把旧 `b898ee1` 当作最终 contract approval。
4. 只 stage 计划和索引，提交：

```bash
git commit -m "docs(custos): repair plan 18 authority and execution contracts"
```

### Task 1: Freeze read-only migration inventory and ownership

1. 为当前 241 个 donor/vendor deterministic source inputs 生成 machine-readable inventory。
2. 分类为 platform-neutral、Nautilus-specific、private vendor、PS-owned strategy、
   PS-owned Hummingbot 或 delete。
3. 写失败测试，拒绝顶层 `shared`/`pandas_ta`、path mutation 和双 canonical source。
4. 写 `docs/design/strategy-toolkit.md` authority/risk taxonomy。
5. 更新 authority snapshot、manifest、precedence 和 drift-check inputs。
6. 此任务不得移动或重写生产源码。

提交：

```bash
git commit -m "docs(toolkit): freeze extraction inventory and authority"
```

### Task 2: Coordinate and freeze versioned contracts

Hard gate：Crucible Plan 88 和 PS Plan 54 必须分别提交针对同一 Custos producer
commit、asset-index digest 和 schema digest set 的 consumer/producer requirements-review
receipt。Custos 是 execution ABI/artifact schema producer；本 Task 的 READY receipt 是
PS Plan 54 artifact/BOM production 和 Crucible Plan 88 StrategyRelease completion 的输入，
不得反向依赖 Plan 54 final BOM receipt 或 Plan 88 completion。

1. 先写 runtime identity、unknown-field、legacy `deployment_id` 和 Python 3.11
   lightweight-import 失败测试。
2. 定义 recursive JsonValue、deep-freeze/canonical digest、typed runtime adapter、
   Manifest、ArtifactRef 和 `StrategyReleaseBomV1` requirements。
3. 生成 versioned JSON Schema；schema 与实现来自同一 source model。
4. 生成 lossless mapping golden：ArtifactRef → StrategyRelease → DeploymentSpec →
   signed command → Custos verifier receipt；明确 release 独立于 spec。
5. 记录 Custos producer SHA、schema digest 和 Crucible/PS requirements-review receipts；该
   artifact 的 canonical 名称是 `Custos Plan 18 Task 2 schema receipt`。
6. 运行 `make check-authority` 并记录 PASS。
7. 明确 schema 不授予 release/selection authority。

提交：

```bash
git commit -m "feat(toolkit): define coordinated strategy execution contracts"
```

### Task 3: Build the minimal distribution

1. 建立 uv workspace、Python >=3.11 `custos-strategy-toolkit` 和
   Python >=3.12,<3.13 `custos-strategy-toolkit-nautilus` 两个 distributions。
2. platform-neutral/contracts 支持 Python 3.11，Nautilus distribution 依赖 exact
   base version 和 `nautilus-trader==1.230.0`。
3. 禁止用 `python_version` marker 静默跳过 NT；添加 Python 3.11 安装 Nautilus
   distribution 必须失败的 negative integration test。
4. 添加 `py.typed`、strict package mypy、clean-wheel import 和 two-wheel isolation tests。
5. 不迁移 donor implementation。

提交：

```bash
git commit -m "feat(toolkit): create typed strategy toolkit distribution"
```

### Task 4: Extract toolkit with zero-rewrite proof

按 inventory 分批迁移：

1. config/protocol/filter/indicator/platform-neutral helpers；
2. Nautilus-specific adapters；
3. private `pandas_ta` vendor；
4. advisory sizing/risk helpers。

每批必须：

- 先写 characterization/typing tests；
- 保持 strategy business source unchanged；
- 禁止 `sys.path` mutation 和 fake distribution；
- 记录 semantic diff 与 fixed-input behavior parity；
- 独立提交，避免 241 个 source inputs 一次性不可审查迁移。

T4 执行时确认 namespace cutover 必须保持原子性，否则任一中间 commit 都会同时缺失旧路径
和部分新路径。审查粒度因此由 `strategy-toolkit-extraction-v1.json` 的 241 条逐文件
source/target digest records 提供，而不是制造不可运行的 partial-move commits。冻结 parity
golden 绑定 T3 exact commit，独立于迁移后 fixture。

### Task 4b: Close extracted-source typing debt without behavior change

T4 首次把 inventory source 纳入 mypy 后，确认 T3 的 strict PASS 只覆盖当时的 4 个 base
contracts 与 2 个 Nautilus package source，不代表 241 个提取文件 strict。T4b 必须：

1. 保持 Custos-owned contracts/package shell strict PASS；
2. 以 `strategy-toolkit-typing-baseline-v1.json` 逐条锁定 extracted implementation 的
   path、line、error code 和 message，任何未审查变化 fail closed；
3. 当前 baseline 为 platform-neutral 75 errors、Nautilus adapter 289 errors；private vendor
   是第三方 source，排除 mypy 但继续受 digest 与 fixed-input parity gate 约束；
4. 只做 no-behavior-change typing closure；每批必须同时通过 zero-rewrite replacement review、
   fixed-input parity 和 exact debt reduction；
5. baseline 清零前不得宣称整个 distribution strict、18b production-ready 或 runtime-ready。

T4b 使用独立的 `strategy-toolkit-typing-closure-v1.json` 和 receipt 保存版本化证据：
历史 T4 extraction manifest、typing baseline 与 receipt 不修改；closure manifest 必须逐文件
绑定 exact T4 implementation `b5ff7ee9cea0e78f4462a478bafa42f8f6e18805` 的 source digest
到 typed target digest，并单独覆盖 local stubs/support files。Exact standalone T4b
implementation `5a19a816d4f6d90e7d3fbde80d39f562decd8c4b` 已通过 clean exact-HEAD
验证，因此 receipt 为 `READY_TYPING_CLOSURE`、`handoff_ready=true`，但 handoff 仅限
T4b typing closure。T5 public pre-import verifier contract 与 T6 immutable RC 仍阻塞
18b/runtime/production-ready。

T4b 可与 T5 实现并行，但属于 18b close-out hard gate。

### Task 5: Implement the sole V1 artifact verifier and runtime

1. Generate only the V1 artifact-ref, pre-import receipt, golden, negative
   vectors and asset index from the canonical toolkit model.
2. Consume PS-owned BOM, release statement, detached attestation and OCI
   publication receipt by exact-byte pins; do not copy their schemas into a
   second authority model.
3. Consume Crucible-owned StrategyRelease material through the Plan 89
   instance-authorized resolver over Plan 100 machine-request V1 and bind it to
   the signed command's instance, spec, generation, release, snapshot, artifact
   and manifest digests.
4. Verify every BOM member, detached Sigstore bundle, trusted identity, archive
   limit, entry point and runner-local policy before Python import.
5. Persist staged/active/quarantined activation state under
   `deployment_instance_id`; no path, mutable tag or historical receipt may
   authorize execution.
6. Keep runtime and live fail closed until the real resolver, immutable PS
   publication and Crucible acceptance are present.

Execution checkpoint (2026-07-21):

- The sole V1 engine ABI now requires `ActivatedEngineArtifactV1`; source paths,
  code hashes, registry aliases and legacy factories are not accepted.
- `RunnerCommandRuntimeCoordinator` performs durable intake, authenticated
  release resolution, activation, local credential resolution, lifecycle apply
  and ACK/NAK/TERM ordering. Exact pending redelivery reloads the durable
  activation rather than treating its directory as a conflict.
- The old `DeploymentReconciler`, G6 module, strategy loader, local DeploymentSpec
  schema and their compatibility tests/fixtures are removed.
- Focused canonical V1 artifact/toolkit verification is `138 passed`; the
  repository authority gate also passes after repairing its canonical consumer
  receipt digest check.
- The sandbox development-source branch and its command-runtime convergence are
  focused verified on 2026-07-23: `tests/test_development_artifact.py` plus
  `tests/test_runner_command_runtime.py` report `10 passed`.
- Status remains `READY_V1_CODE_PENDING_STRATEGY_RELEASE_RESOLVER`: daemon,
  runtime, live and production readiness remain false until the real resolver,
  protected PS publication and Crucible acceptance are composed.

### Task 6: Publish the immutable V1 toolkit RC

1. Build the toolkit from the canonical source once and prove reproducibility
   and zero rewrite.
2. Publish one digest-pinned OCI coordinate and one
   `ToolkitRcAuthorityReceiptV1`; candidates and protected publication use the
   same V1 document shapes.
3. Root contracts remain usable without Nautilus; the Nautilus execution extra
   carries its exact runtime requirements and attestation bindings.
4. Record only current V1 assets in the authority manifest. Planning receipts
   cannot act as runtime capability switches.
5. Promotion reuses identical bytes and requires independent registry readback,
   signature verification and downstream consumer receipts.

Execution checkpoint (2026-07-21):

- Protected release run `29846674028` published `0.1.0rc2` from source
  `ccae31ef1d906cea86bda00066b9ffbc159f2c6e` after required-reviewer approval on
  the main-only `toolkit-rc-release` environment with admin bypass disabled.
- The immutable OCI manifest is
  `sha256:7e5415877e8240aa316fba28d25fa5a41a9b0aeaa3b604e1015de619dfb5dcaf`.
- Independent read-only promotion run `29846846571` emitted the exact
  `READY_TOOLKIT_RC` receipt with workflow-artifact digest
  `sha256:d2756e8c8f9e069e5f4f83a064707338da6054a915b55fff240041c249aa0858`.
- The `0.1.0rc2` receipt remains immutable historical evidence for its exact bytes.
  Current source now uses business-named `ToolkitRcPendingReceiptV1`,
  `toolkit_extraction_receipt`, `toolkit_typing_closure_receipt` and
  `pre_import_verifier_receipt` fields. Those technically correct V1 names change
  the wheel and schema bytes, so the current-byte toolkit handoff is reopened until
  a replacement RC is built, protected-published and independently read back.
- Candidate `0.1.0rc3` run `29860702301` was cancelled before execution because
  it was bound to pre-relock Custos commit
  `39493964e165afbd688beac7205ed4d6943c2c49`; its candidate identifier is not
  reused or repointed.
- Replacement `0.1.0rc4` run `29861323881` completed protected publication at
  commit `3b738162a9223498078075be537093252caec21d`; independent run
  `29879590051` read manifest
  `sha256:e75897a20c5d0b6c8db9c2c7d81fc39a46bdc6b0cefd33c7838c5a9bbff7e719`
  and emitted `READY_TOOLKIT_RC`. That immutable receipt exposed two remaining
  plan/task-derived prerequisite filenames, so RC4 is audit evidence only and is
  not a current consumer pin.
- Protected release run `29880238705` published `0.1.0rc5` from the sole
  business-named V1 source commit
  `a3fd88c4e7e25433b508b2fece94be876630f380`; its immutable OCI manifest is
  `sha256:670a50cf3725e03744bc2e9efeff74230db934bd83d10a927839254f3d6283d5`.
- Independent read-only promotion run `29883298628` resolved that exact digest,
  verified the production signature and emitted the current
  `READY_TOOLKIT_RC` artifact
  `sha256:940bd28ca11b5a8d92f814a0cf39113d93afa89d571648041c5bf002ad418acb`.
  The canonical receipt checksum is
  `570f69cbb9c796bfa82a57543ba163a4f3586d4fd57ca08a4174ed52ee713702`.
- RC5 is the sole current V1 consumer pin. No alias, old parser or compatibility
  reader is introduced; earlier immutable RC bytes remain registry audit
  evidence only.
- 2026-07-23 authority-gate correction replaced stale hard-coded RC2 expectations
  with the exact RC5 source, manifest, release run, promotion run and receipt
  digest. Immutable T4/T4b and RC5 evidence bytes were not rewritten;
  `make check-authority` passes with RC5 as the sole current pin.
- The business-named contract handshake is exact before publication: Crucible
  consumer commit `a30a62bb2e4115adaad9c036386ebb2b731e0526` is bound by the Custos producer
  handoff, and Crucible final readback commit `014edd4` pins that handoff while
  keeping runtime and production readiness false.
- Engine/runtime/production readiness remains false until the PS artifact and
  Crucible StrategyRelease receipts complete Tasks 7-9. Historical tags and
  receipts must not be overwritten or repointed.

### Task 7: Collect v1.team artifact-chain receipts

必须全部指向 exact candidate release BOM digest 和完整 BOM member digests：

- PS Plan 54：existing strategy zero-rewrite immutable artifact/BOM producer；
- Crucible Plan 88：StrategyRelease acceptance、manifest digest 和 exact BOM binding；
- Custos verifier/runtime：attestation-before-import 与 exact-artifact execution；
- Custos Plan 19：signed command exact-artifact runner integration。

任一 BOM member 或 canonical BOM bytes 变化使全部 receipt 失效。
Speculum 不生产本 Task receipt，也不 gate candidate、final 或 close-out。PS legacy
`build-image.sh` compatibility lane 不得替代上述任何 receipt。

### Task 8: Final release and consumer cutover

1. 构建 final version 和全新 final release BOM，重新执行完整 receipts，不继承 RC PASS。
2. PS、Crucible 和 Custos 锁定 exact final BOM 和全部 member digests。
3. 确认无 active consumer 后删除 Custos 旧 vendored toolkit 和 in-repo compatibility adapter；
   不以删除或迁移 PS legacy `build-image.sh` compatibility lane 为前提。
4. 禁止重建或重指向历史 Custos image/tag。

提交：

```bash
git commit -m "release(toolkit): publish verified strategy toolkit"
```

### Task 9: Close out

1. 运行全部 package、typing、wheel、attestation、zero-rewrite 和 consumer gates。
2. 回填 exact commits/digests/receipts。
3. 更新状态和 index。
4. 提交：

```bash
git commit -m "docs(custos): mark plan 18 as completed"
```

## Verification

- [x] Legacy `deployment_id` 被拒绝
- [x] `deployment_instance_id` 是唯一 runtime address
- [x] StrategyRelease 独立于 DeploymentSpec；Spec 只引用 release
- [x] ArtifactRef → release → spec → command → verifier receipt 无损 mapping PASS
- [x] StrategyRelease/artifact selection/effective config 仍由 Crucible 拥有
- [x] `strategy_key` 仅是 catalog alias
- [x] Root/contracts 保持 Python >=3.11
- [x] 独立 Nautilus distribution 使用 Python >=3.12,<3.13 和 exact NT 1.230.0
- [x] Python 3.11 安装 Nautilus distribution fail closed，不静默跳过 NT
- [x] effective config 是 recursive typed/deep-frozen JSON 并匹配 signed digest
- [x] production 只接受 signed wheel
- [x] source-path contract 仅允许 sandbox、non-promotable、non-live
- [x] attestation schema 绑定 issuer/workflow/bundle/trust policy
- [x] canonical `StrategyArtifactRefV1` is pre-sign only and contains no bundle/policy field
- [x] full PS `StrategyReleaseBomV1` object replaces any member-array wire authority
- [x] detached statement/attestation/evidence chain has no self-signed composite digest
- [x] trust roots/policy 只来自 runner-local signed release configuration
- [ ] candidate/final receipts 绑定 release BOM 和全部 member digests
- [x] schema 由 source model 生成并经 Crucible/PS requirements review
- [x] wheel 不提供顶层 `shared` 或 `pandas_ta`
- [x] lightweight contracts import 不修改 `sys.path`
- [x] existing NT strategy business source zero-rewrite
- [ ] PS Plan 54、Crucible Plan 88 和 Custos verifier/runtime receipts 指向 exact BOM/member digests
- [x] Speculum 不属于 START/STOP、candidate/final acceptance 或 close-out gate
- [x] PS legacy `build-image.sh` -> Crucible Python image 链保留为独立 compatibility lane，且不作为 team fallback 或验收证据
- [x] toolkit advisory risk 不冒充 Custos/Crucible authority
- [ ] final 重新锁定并重新验证
- [x] `make check-authority` 覆盖并通过 toolkit schema/BOM/ownership drift
- [ ] 18a-d 每个 slice 有独立 DoD、stop gate 和 handoff packet

## Progress

| Work | State | Current boundary |
|---|---|---|
| Canonical V1 models and source | local gates pass | sole V1 source, generated assets and focused verification are current |
| Immutable toolkit RC | READY | RC5 protected publication and independent digest readback succeeded; its business-named receipt is the sole current V1 consumer pin |
| Old contract/runtime generations | removed | old runtime module and command-owned evidence path are absent |
| PS V1 handoff | pending final pins | PS source uses the sole V1 OCI topology |
| Crucible V1 handoff | Custos contract exact; artifact acceptance open | final Custos producer receipt is pinned at Crucible `014edd4`; real PS publication and Plan 88 C6 native acceptance remain open |
| Runtime activation | sandbox ready; production authority focused verified | development-source runtime passes; Crucible `10b85e4` exposes complete production authority and Custos `4ad12b2` strictly consumes it; immutable OCI materialization, daemon composition and real publication remain open |
| Production/live | STOP | requires final exact-byte receipts and real runtime evidence |

## Deviations and Improvements

- The earlier command model incorrectly embedded StrategyRelease evidence in a
  runner command. The V1 runtime now consumes Crucible-owned release material
  through a dedicated authority seam and uses the command only for signed
  deployment binding.
- Planning and cross-repository receipt digests are no longer runtime feature
  flags. Runtime readiness is derived from composed production capabilities.
- Producer handoff receipts are producer-scoped. Custos pins the business-named
  Crucible Custos-contract consumer receipt only; PS schema/BOM/attestation pins
  remain between PS and Crucible and cannot invalidate the Custos contract handoff.
- No compatibility parser or predecessor asset remains an accepted input.
- Plan/task-derived identifiers are removed from current files, classes, fields,
  functions and status values, including toolkit prerequisite receipt paths and
  exact-byte authority members. Published earlier RC bytes remain registry audit
  evidence only; no current runtime contract parses them through a compatibility
  path.

## Quantitative Summary

- Production contract generations: 1 (`V1`).
- Runtime identity: `deployment_instance_id` plus signed spec/digest/generation.
- Business release owner: Crucible.
- Artifact producer: PS.
- Execution ABI and local verifier owner: Custos.

## 2026-07-22 sandbox development-artifact checkpoint

Custos now has one business-named development-artifact verifier and a shared
activation boundary. It derives content-addressed storage from the configured
artifact root plus owner-provided digests, verifies the source and publication
receipt bytes, quarantines invalid material and never accepts a path from an
ARX/Crucible DTO. The snapshot is sandbox-only and non-promotable.

The daemon production StrategyRelease path remains deliberately fail-closed
until immutable materialization, local trust configuration and real protected
publication receipts exist. No local artifact can satisfy T5e, live or
production readiness.

## 2026-07-23 production StrategyRelease authority checkpoint

Crucible `10b85e4486cf4d422f8d0703a9ad69cab9f1c08a` extends the sole authenticated
runner-material response with the exact StrategyRelease snapshot, immutable
artifact binding, complete Crucible evidence and the evidence digest-input JSON.
Development and StrategyRelease material remain mutually exclusive branches of
the same V1 endpoint.

Custos `4ad12b2aba2e1865b2921434d9f053bc036fa75a` consumes that response directly,
recomputes the instance-bound execution-binding digest, validates the
domain-separated snapshot digest and exact evidence preimage, and keeps owner
lookup separate from runner-local immutable materialization. The focused gate
passes 30 tests plus Ruff and mypy; Crucible store tests pass 3/3 and the
server-http test target compiles with `cargo fmt --check`.

This is a clone-local producer/consumer contract checkpoint, not T5e or
production readiness. Immutable OCI execution-byte acquisition, daemon trust
composition, real machine-authenticated HTTP/PG, Sigstore, engine activation and
RunnerFact receipts remain mandatory.
