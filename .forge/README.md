# custos forge 工作流

`.forge/` 目录是 custos 独立仓库的 forge 工作流物件根. 包含 plan 目录 + Agent Teams 配置.

> **Authority note (2026-07-13)**：计划标题与 close-out 可保留历史
> `paper`/fallback 文字，但它们不是新实现规范。Custos 仅持本地凭据/执行并产生
> signed RunnerFacts/venue fee facts；Crucible Rust 验签投影与结算；ARX 只授权。
> 新契约 mode 仅 sandbox/testnet/live。
>
> **Offline lane note (2026-07-29, Plan 21)**：除签名通道外，Custos 另有一条
> **离线通道**（`src/custos/offline/`），用于操作者在自己完全掌控的机器上验证策略
> 逻辑。它接收未签名的 desired state，**仅限 sandbox 与 testnet**，live 在边界被
> `mode_guard` 拒绝；opt-in（`arx-runner start --reconcile-strategy-id`）；
> 非 promotable，不产生 fact 与 receipt，不解析 canonical command bytes。
> 权威依据见 `.claude/rules/mandatory-rules.md` §Trust 与 `authority-manifest.json`
> 的 `offline_lane`，由 `verify_offline_lane` 强制。这条例外是 CEO 2026-07-29 的
> 高风险偏离决定，四件套记录见 Plan 21 偏离日志 + 本脚注 + `historical-lessons.md` C8。

## 目录约定

```
.forge/
├── README.md              — 本索引 (plan 编号规范 + 现有 plan 表)
├── teams.yaml             — Agent Teams 配置 (plan-team / execute-team / architect-team / ops-team)
├── plans/YYYY-MM/         — Plan 文件, 按月份归档
│   └── NN[a-z]?-<slug>.md
├── reviews/YYYY-MM/       — Peer review 报告 (未来)
├── fixes/YYYY-MM/         — Fix plan (未来)
├── incidents/YYYY-MM/     — 紧急偏离记录 (未来)
├── scratch/               — 临时 scratchpad (gitignore)
└── handoff/               — Agent handoff packet (gitignore)
```

## Plan 编号规范

- **格式**: `NN[a-z]?-<kebab-slug>.md`
- **NN**: 两位数字, 从 `00` 开始; `00` 保留给"起始阶段规划簇"(可用 `00a` / `00b` / `00c`
  子编号并列相关 plan)
- **kebab-slug**: 简短语义标识 (如 `nt-trading-node-host-sandbox`)
- **月份归档**: `.forge/plans/YYYY-MM/` (以 plan 创建月为准)
- **子编号使用场景**: 一个大特性拆多个并行 plan (如 00a NT host + 00b telemetry + 00c
  G6 live release), 每 plan 相对独立可实施

## 现有 Plan 索引

| # | Slug | Status | Depends on | Blocks | 说明 |
|---|------|--------|-----------|--------|------|
| [00a](plans/2026-07/00a-nt-trading-node-host-sandbox.md) | NtTradingNodeHost + Binance sandbox | ✅ Completed (2026-07-07) | arx Plan 60 (已 close-out) | Plan 00b | NoopHost → 真 TradingNode; sandbox 策略打通 (codex peer review 落地 F2-F6) |
| [00b](plans/2026-07/00b-telemetry-bridge-nt-messagebus.md) | telemetry_actor 接 NT MessageBus | ✅ Completed (2026-07-08) | Plan 00a | Plan 00c | NT MessageBus → telemetry uplink; OrderDenied 桥 (fixed dead subscription; deploy attach) |
| [00c](plans/2026-07/00c-g6-gate-live-release.md) | G6 gate 放宽 + Binance testnet/live 逐级 | ✅ Completed (2026-07-07) | Plan 00a + 00b ¹ | Plan 03 (硬化候选) | capability-based G6 + docker compose e2e |
| [03](plans/2026-07/03-nt-host-hardening.md) | NT host hardening (credential lifecycle + capability integration + host×mode matrix + correlation handle 精度提升 + GC-safety 扩展) | ✅ Completed (2026-07-09) | Plan 00a + 00b + 00c ✅ | Plan 05 candidate (subprocess isolation + FailureEvent first-class) | 起源: 00a F1 defer + 00c HIGH triage new-plan; Phase 2 精细化含 evidence-scout 4 latent + 5 drift 消化; Phase 3 execute-team 11 Task ~450 LOC 落地 (214 passed, 4 红线 0 命中), peer review chain codex L1 REQUEST_CHANGES → Path B 契约诚实化 fix → tdd/safety APPROVE_WITH_FOLLOW_UPS |
| [04](plans/2026-07/04-red-line-03-runner-fallback.md) | 红线 0.3 完整兑现: runner-level cap + 状态快照 + zombie detection + arx-disconnect chaos | ✅ Completed (2026-07-10; 04a `3e85c50` + 04b squash `d0dd537` + 04b-fix commits `b04071e`+`1c9f3dd`; drawdown wire + state snapshot publisher wire + WAL-backed path + risk_config live-refresh 三层 runtime-wire live) | Plan 00a + 00b + 00c + 03 ✅ + **05** (结构重构) ✅ | 上 live **1 号硬阻断项** | 起源: Plan 03 close-out 后 safety-validator 跨范围深度审 + Lead 复核 — 红线 0.3 组合级熔断 grep 0 命中 (max_notional_per_runner + drawdown breaker 均无实现); 教科书级 lesson #40 project-level dogfood. Refinement: 14 tasks / 6 tracks / 39 failure-mode tests (4 grep + 35 NEW) / 6 Tier-2 methods owns. **04a squash `3e85c50`** + **04b squash `d0dd537`** landed 全部 Tracks 1+2+3+4+5+6. **Drawdown wire flip 确认真实** (codex + safety + lead 三方 grep 实证 `_breaker_tick → get_engine_status → FallbackBreaker.evaluate`). **04b codex L1 REQUEST_CHANGES 关闭 (04b-fix cycle 2026-07-10)**: HIGH-1 wire `StateSnapshotPublisher` 进 `cli/main.py` via `asyncio.create_task(publisher.run(stop, reconciler.active_spec_ids))` + 新增 `DeploymentReconciler.active_spec_ids()` public 方法 (`b04071e`); MED-2 publisher 切到 `publish_telemetry_envelope` WAL-backed at-least-once path + `--wal-path` CLI flag (`b04071e`, Option A); MED-3 `RunnerNotionalCap.apply_config` + `FallbackBreaker.apply_config` + `DeploymentReconciler._refresh_risk_config(spec)` 在每次 accepted spec 前跑 (`1c9f3dd`, Option A). 见 `.forge/reviews/2026-07/04b-peer-codex.md` |
| [05](plans/2026-07/05-structural-refactor-engine-abstraction.md) | 结构化重构: arx_runner → custos rename + core/engines 分层 + ExecutionEngineProtocol + pyproject extras + NATS subject engine layer | ✅ Completed (2026-07-10; 05a `4f0192a`+`7ffa187` 2026-07-09 + 05b `79c1858`..`e82825d` 2026-07-10) | Plan 00a + 00b + 00c + 03 ✅ | Plan 04 + 06 + 07 (本 plan 是基础重构, 已先行落地避免其他 plan 二次搬迁) | 起源: user 澄清诉求 — custos 后期需支持多引擎 (hummingbot / freqtrade / athanor / nt-rust), 提前规划目录结构 + 命名方式; 消化 arx subtree 遗留 (arx_runner Python 包名 rename, lesson #35 fanout). 17 tasks / 8 tracks 全落地. **05a squash `4f0192a`** 落 Tracks 1-4+8 (46 file rename + core/engines/cli 分层 + Protocol Tier-1 冻结 + g6_gate 抽出 + isinstance 契约测试), unblocks Plan 04/06 START gates; 3 LOW triage `.forge/triage/05a-DEVIATION-triage.md` (含 v2 fabricated close-out event lesson #25/C2 首次 in-codebase 复现). **05b** 落 Tracks 5-7 (pyproject extras `nt-runtime`→`nautilus` + 4 空槽 + `docs/engines/` 5 stub + NATS subject v2 reserved docs) + T-final 完整 close-out; 1 LOW triage `.forge/triage/05b-DEVIATION-triage.md` (Foundation Scan 顺手修复 05a 遗漏的 2 处 `arx_runner` 功能性残留). `make verify` 263 passed / `make verify-nt` 263 passed |
| [06](plans/2026-07/06-ps-supertrend-migration.md) | ps supertrend 迁移: custos registry-mode 加载 + RiskController 启用 + shared/ 依赖打包 + e2e 集成 | ✅ Completed (2026-07-10; 06a slice `306b9e5` for Tracks 1-4 + Plan 08 for Tracks 5-6 + full close-out) | Plan 00a + 00c + 03 ✅ + **05** (结构重构); soft-depends Plan 04 | 生产化 ps supertrend 首次 paper/testnet e2e | 起源: user 澄清 custos 接管 ps supertrend 移除 sidecar/runner; grep 实证 supertrend 已有 register_strategy 无需策略侧改造, custos 只需 registry-mode 分支. Refinement: 12 tasks / 6 tracks / 15 failure-mode tests (3 grep + 12 NEW) / shared toolkit 打包方案 A vendored 推荐. codex peer fix cycle: HIGH-1 option B + HIGH-2 option B + MED-3 promoted DP4 + FU-2 NEW test 已应用. **06a squash 306b9e5 landed Tracks 1-4** (vendored toolkit + strategy_registry_name + RiskController activation + TradingNodeConfig plumb); Track 5-6 remainder + full close-out landed via Plan 08 (`4ac60d7`..`<Plan 08 T6.2 SHA>`) on branch `custos/08-plan/runner` per `DEV-08-RENUMBER-FROM-06B`. Full close-out: 4 red-line gates satisfied with real code_coverage / runtime_wire values (DP1=A partial+manual for testnet real-session opening per `DEV-08-T5.2-MANUAL-VERIFICATION`). |
| [07](plans/2026-07/07-ps-shared-curation-and-convergence.md) | ps shared curation + convergence: custos-as-shared-authority landing + sync discipline real implementation + ps convergence path | ✅ Completed (2026-07-10; runner-executor-07 sonnet ran Tracks 1-4 + T5.1 partial; main-session takeover T5.1 close-out after runner-07 session quota hit; 8 commits base `4437991`..`ce9fce2`) | Plan 06 06a squash `306b9e5` ✅ + Plan 05 (via 06a inheritance) ✅ | Plan 08 START gate now open | 起源: 06a `DEV-06-06A-REVERSE-DEPENDENCY-STRATEGY-D'` — custos toolkit = 权威 body-of-truth, ps = research 副本. Refinement: 5 tracks / 9 tasks / 9 NEW tests + 1 no-regression / no source-code changes (`DEV-07-NO-SOURCE-CODE-CHANGES`). Batch 1 peer chain: L1 REQUEST_CHANGES (10 findings) → in-place fix → L2 APPROVED_WITH_FOLLOW_UPS (2 LOW). **4 CEO DPs ratified 2026-07-10**: DP1=(a) keep 06a 90-file vendor status quo / DP2=(a) short-term keep ps Docker-buildable shared+deploy (HIGH hard-constraint — crucible/nautilus Dockerfile) / DP3=(b) weekly diff review / DP4=(a+b) status quo + formalized trigger criteria. **2 LOW follow-ups applied**: L2-FU-07-1 (fix log CR-10 grep BRE/ERE correction with 6 prose FPs recorded) + L2-FU-07-2 (scout line count 230→255 post-errata). ps cross-repo commit `2bf06e6` on philosophers-stone `develop`. |
| [08](plans/2026-07/08-plan-06-remainder-e2e-and-close-out.md) | Plan 06 remainder: real supertrend e2e (sandbox + testnet) + ps sidecar retirement docs + Plan 06 close-out | ✅ Completed (2026-07-10; runner-executor-08-2 landed 4 tasks on branch `custos/08-plan/runner` @ base `6373f50`, 4 commits `4ac60d7`..`<T6.2 SHA>`) | Plan 07 landing (START gate) + Plan 06 06a landed `306b9e5` ✅ + Plan 00a/00b/00c/03/05 ✅ | Plan 06 close-out (T6.2 flipped 06 → ✅) + first paper→testnet production acceptance for ps supertrend on custos | 起源: Plan 06 06a spawn 显式 defer Track 5-6 到 06b, CEO 2026-07-09 renumber 到 Plan 08 (Plan 07 crosses 中间) per `DEV-08-RENUMBER-FROM-06B`. 4 tasks (T5.1 sandbox e2e + T5.2 testnet DP1-conditional + T6.1 sidecar retirement docs + T6.2 close-out). Batch 1 peer chain: L1 REQUEST_CHANGES (9 findings) → in-place fix → L2 REQUEST_CHANGES CR-1 PARTIAL only (verbatim column synthesized cells), 8/9 RESOLVED, CEO 2026-07-10 accept path 降级 CR-1 为 LOW follow-up. **3 CEO DPs ratified 2026-07-10**: DP1=A real testnet credential via vault (partial+manual verification per `DEV-08-T5.2-MANUAL-VERIFICATION`) / DP2=A independent arx-side follow-up plan / DP3=A golden path only, no chaos test in Plan 08. **5 execution-time DEV entries**: STRATEGY-SOURCE-PATH-SELECTED-III (permanent fixture mirror pinned to ps `3443e969`) / RISK-CONTROLLER-ACTIVATION-PROXY (config-layer proxy for `_risk_controller` assertion since `on_start` not fired under parked `run_async`) / T5.2-MANUAL-VERIFICATION (skip-if-not-provisioned + operator runbook) / INTEGRATION-MARKER-REGISTERED (pyproject.toml) / L2-FU-08-1-VERBATIM-DISCIPLINE (no rewrite; discipline point recorded). `make verify` 299 pass + 2 skip; `make verify-nt` 299 pass + 2 skip. |
| 09 | hook infra formalization (Standard scope): `scripts/hooks/` 目录规范 + `install-hooks.sh` 扩 `pre-commit`/`commit-msg`/`pre-push` + 4 类 hook 实装 (`check-code-english` 已有 / `check-silent-paths` lesson #21 / `check-red-lines` mandatory-rules §0 / `check-changelog-at-tag` Plan 12 FM6) + gateway v1 snapshot pytest (Plan 12 FM3) + `docs/design/hook-infra.md` + Makefile `hooks-install` / `hooks-test` + 各 hook 失败模式测试 + CI 二次 gate | 🔲 Planned (draft deferred; scope frozen 2026-07-10) | **Plan 11 landing** (hard-dep for draft start — Plan 11 lock `arx-runner` script name + `~/.arx/` namespace 是 red-line grep + Makefile target 的稳定引用基础; lesson #35 boundary constant rename fanout 预防) | Plan 12 FM3 (contract v1 backward compat snapshot) + FM6 (CHANGELOG-at-tag) 完整 wire | 起源 (三源汇流): (1) Plan 12 §失败模式表 FM3 + FM6 显式 defer hook wire 到 Plan 09; (2) `historical-lessons.md` #21 (custos) 声明 "setup-pre-commit hook 会 grep silent drop"; (3) `.claude/rules/mandatory-rules.md` §0 Non-Custodial 4 红线 + `verification.md` 目前是**手工 checklist**, Plan 09 抽离为**自动化 hook**。**Scope 边界** (排除项, 避免 lesson #35 boundary 混淆): 运行时 hook (`DEV-04a-CAP-ENFORCEMENT-HOOK-DEFER` NT per-order intercept) 不属于 Plan 09 scope — 那是运行时代码, 归 v1 pre-live follow-up plan; Plan 09 只覆盖 git hook + CI static gate。**Draft deferral rationale**: Plan 11 breaking release 会改 script entry (`python -m custos` → `arx-runner`) + namespace (`~/.custos/` → `~/.arx/`), Plan 09 若在 Plan 11 前起草会命中 boundary constant rename fanout, draft 等 Plan 11 landed 后再补稳定引用。CEO 2026-07-10 拍板 Standard scope (8 tasks, 无外部 framework, 与 sandbox research 单栈简洁诉求匹配) + planning-only phase (README scope frozen)。 |
| [11](plans/2026-07/11-custos-cli-subcommand-align-lifecycle.md) | custos CLI 对齐 lifecycle: `arx-runner` 子命令 (enroll / vault {put,verify,list} / start) + `~/.arx/runner.toml` + per-key `~/.arx/vault/<key-id>.enc` + **breaking change** (delete `python -m custos` + `SopsAgeVault` + `~/.custos/` namespace) + 0.1.0 → 0.2.0 | ✅ Completed (2026-07-11) | Plan 04 + Plan 05 ✅ + arx Plan 78 (in-flight, mocked by shape) | Plan 09 draft start (arx-runner + ~/.arx/ 稳定引用) + Plan 12 execute-team (STRICT SERIAL, cross-plan hard gate) | Wave v1-team-full-loop batch. 9 tasks (T1 runner_toml.py + T2 validators.py + T3 dispatcher + T4 enroll HTTP + T5 vault put + T6 vault verify+list + T7 start + _daemon.py + PerKeyVault + T8 [project.scripts]+SopsAgeVault delete+cli/main.py stub + T9 docs+close-out). **N5 CEO decision** (option a) MockVault runtime fallback removed — `_build_vault` unconditional `PerKeyVault`. **22 failure-mode contract tests**. Non-Custodial 4 红线 全数守 (0.1 Key/KEK 不出进程 code+wire / 0.2 G6 gate 不动 / 0.3 reconciler+FallbackBreaker+ZombieWatchdog preserved / 0.4 no money math touched). |
| [12](plans/2026-07/12-custos-distribution-signed-wheel-docker-lts.md) | custos distribution: signed wheel + docker image + SEMVER/LTS + gateway contract v1 | ✅ Completed (2026-07-11; runner-executor-12 branch `custos/plan-12/runner`, 9 commits base `b8021ad`..`<T9 SHA>`) | Plan 11 landed (STRICT SERIAL) ✅ + Plan 05 + Plan 04 ✅ | 首次公开发布 `custos-runner 0.2.0` (0.x LTS 起点) + Plan 09 hook infra draft start (wire owner for Plan 12 FM3 + FM6) | Wave v1-team-full-loop, 9 tasks (T1 pyproject lts extras + hatch hook / T2 multi-stage Dockerfile + non-root USER 1000 / T3 sigstore keyless wheel signing / T4 8-job CI release workflow / T5 CHANGELOG scaffold + README trim / T6 LTS commitment + upgrade path / T7 gateway contract v1 JSON Schema + golden gate / T8 reproducible build test / T9 CONTRIBUTING+SECURITY+docker mount doc+close-out). **11 failure-mode contract tests** (FM1-FM11, multi-layer 独立可测: sigstore signing / docker non-root / contract v1 backward-compat / LTS doc / reproducible build / CHANGELOG-at-tag / GHCR publish / cosign key rotation / SEMVER minor drift / LTS EOL audit-non-silence / docker image size). CI first real run deferred to first `v0.2.0` tag push. Non-Custodial 4 红线 全数守 — no runtime code touched. |
| [13](plans/2026-07/13-ps-deploy-support.md) | ps `deploy/custos/` support: explicit permission scope + sanctioned sandbox identity + gateway samples + v0.2.0 examples | ✅ Completed (2026-07-11) | Plan 11 + Plan 12 ✅ | ps Plan 49 deploy target T3/T6 | Minor feature support. 5 tasks; 23 focused tests including the self-reflect extension regression; `make verify` 464 passed. T4 retains a modernized testnet Dockerfile because the official image does not yet bundle NautilusTrader, sops, or age. |
| [14](plans/2026-07/14-clean-deployment-runtime-contract.md) | clean downstream deployment runtime: complete official image + strict DeploymentSpec + NATS bootstrap + readiness | ✅ Completed (2026-07-12; 10 implementation commits `a7e256a`..`281cb3b`) | Plan 13 ✅ | ps Plan 49 clean implementation | custos 0.3.0 clean break complete. Base 485 passed; NT 549 passed; Docker 13 passed; standalone real wire passed through `running→stopped→running`. PS minimum: `v0.3.0` containing `281cb3b` or later; official image direct, no derived Dockerfile. |
| [15](plans/2026-07/15-plan-14-release-authority-fixes.md) | Plan 14 fixes: exact release digest promotion + domain authority alignment | ✅ Completed (2026-07-12; `7c5bef4`..`3b16093`) | Plan 14 ✅ | publishing/promoting v0.3.0 | Signed wheel → candidate digest → exact runtime gate → same-digest stable promotion; domain peer/subjects/Vault aligned; Docker lock boundary + lesson C3 landed. Base 502, NT 566, Docker 13, standalone 1. |
| [16](plans/2026-07/16-local-v030-consumer-readiness.md) | local v0.3.0 consumer readiness: safe deployment IDs + public validate hash + verified local Docker tag | ✅ Completed (2026-07-12; 7 implementation commits `61d2d43`..`89b31a1`) | Plan 14 + Plan 15 ✅ | philosophers-stone Plan 49 | Local image `sha256:b47ff765...` verified: base 506, NT 570, Docker 15, standalone 1. PS minimum is the Plan 16 close-out commit plus source revision `89b31a1`; remote GitHub/GHCR/PyPI/cosign publication and namespace decision remain deferred. |
| [17](plans/2026-07/17-vault-cli-json-format-symmetry-fix.md) | fix vault CLI JSON format symmetry | ✅ Completed (2026-07-13; Custos `fdd8a42`..`cec0f8a`, PS `9d3e59b`) | Plan 16 ✅ | downstream real Docker smoke failure on `arx-runner vault verify` | CLI/runtime share explicit JSON decrypt helper; mocked and real Docker gates exercise public verify; PS sandbox balances aligned. Final local image `sha256:95ce38a3...`, revision `cec0f8a`; base/NT 589, Docker 15, standalone 1, PS smoke 1. Remote publication remains deferred. |
| [18](plans/2026-07/18-typed-toolkit-strategy-contracts.md) | typed toolkit + strategy execution ABI + verified artifact consumer | ⏳ In progress (business-named RC5 READY; sandbox and production clone-local artifact paths verified; T5e/T7-T9 blocked on real receipts) | Current toolkit code, schemas, Custos↔Crucible handoff, RC5 authority, immutable OCI acquisition/cache and daemon trust composition are focused verified; PS must now publish the immutable team artifact for Crucible acceptance | RC5 ✅ -> PS54 -> CR88 C6 -> T5d-A -> CR89 -> T5d-B/Plan19 T2 -> T5e -> Plan19 T5; no Speculum gate | Custos owns execution ABI and runtime verification. Earlier RC bytes remain registry audit evidence only; clone-local success does not enable testnet/live or replace launched registry, Sigstore and owner receipts. |
| [19](plans/2026-07/19-crucible-command-runner-fact-runtime-convergence.md) | Crucible command + single RunnerFact state store + production runtime convergence | ⏳ In progress (local immutable full-daemon flow ready; T9-T10 open) | Custos `d6ba1bf` and Crucible `b696b8a` prove one pinned-CA TLS event from isolated authority issuance through encrypted-vault restart/rotation, immutable StrategyRelease activation, policy/command, crash-safe ACK, RunnerFact PubAck and PostgreSQL projection. | Publish and lock the exact PS OCI/Sigstore candidate -> deployed `0029`/`0117` acceptance -> PS56 exact-candidate acceptance | Exact runtime RC, deployed services, Phase-B close-out, live and production readiness remain false. |
| [20](plans/2026-07/20-custos-docs-site-scaffold.md) | custos.alephain.com 文档站 (Docusaurus + i18n + versioning) | ⏳ In progress (6/12 task; T1-T5 + T9 ✅, T6 翻译 4/46 章, T7/T8/T10/T11/T12 未开) | 无 hard-dep (消费 Plans 14-17 已落地的 `docs/**`) | T10 DNS handoff (CEO 手工 CNAME) | Sessions 1-3 落地 (`9f194a5`..`1c594fd`); gh-pages 已部署 `a60bd8b` (2026-07-21, 96 页)。**⚠️ 未决 CRITICAL**: 已发布站点含内部标识 (mandatory-rules §9 / 生态 lesson #42), 9/96 页命中; 站点无 disclosure gate。处置见 plan 20 §偏离日志。 |
| [21](plans/2026-07/21-sandbox-offline-deployment-path.md) | non-live offline deployment lane: `nats bootstrap` + `deployment publish`/`validate` + offline spec contract + reconcile/observed-state loop, all behind a fail-closed live guard, with the surface pinned so it cannot be removed silently again | ✅ Completed (2026-07-29; `34f7307`..`<T8>`) | Plan 13 ✅, Plan 17 ✅ | `philosophers-stone/deploy/custos` on any image built after `324da6e` | Foundation Scan corrected the premise twice: `publish` died at `324da6e` (2026-07-14), not `8c4454f`, and the harness was broken in five places rather than two. The restore baseline is `cec0f8a` — the revision PS pins — because the commit before the removal already required two fields PS never renders. CEO 2026-07-29 widened scope from sandbox-only to sandbox+testnet and authorised amending the authority layer, since `mandatory-rules.md` §Trust admitted no unsigned lane in any mode and the manifest banned the lane's own subjects. The boundary is drawn at live, where the red lines already draw it. |
| [22](plans/2026-07/22-offline-lane-local-exposure-guard.md) | 离线通道的本地敞口守卫: 复用 `EngineSafetySupervisor` + strictest 限额（spec `risk_config` 可抬高）+ 与传输解耦的 tick + 跳闸即锁死 | ✅ Completed (2026-07-29; `c1bcba7`..`b87d763`) | Plan 21 ✅ | 离线通道 testnet 长时间无人值守运行 | 起源: Plan 21 close-out 遗留项 2 —— 红线 0.3 只兑现一半（断线不停机做到了，断线期间无敞口上限）。Foundation Scan 更正了 Plan 21 的说法: `EngineSafetySupervisor` 两条通道都未接线，`RunnerNotionalCap` 与 `ZombieWatchdog` 全仓零接线，故本 plan 只接 breaker，逐单 cap 与 watchdog 明确排除。|
| [23](plans/2026-07/23-offline-lane-standalone-identity.md) | offline lane standalone identity: generate a v1 runner authority document locally (no backend, no enrollment), mark it unattested, refuse it on the signed lane, and agree with the consumer on the status subject | 🔲 Not started | Plan 21 ✅, Plan 13 ✅ | `philosophers-stone/deploy/custos` — the lane cannot start without it | Plan 21 restored delivery but not startup identity. The 2026-07-29 end-to-end run at image `b75e3bc` validated PS's real spec yet the runner exited 1: `runner.toml is not a v1 runner authority document`. PS's producer predates the contract (`init_runtime.py:156-157` emits `long_term_credential`/`enrolled_at_ns`, both rejected), and Custos has no standalone producer — `enroll` requires `--backend`, a reachable service and a server nonce. `generate_machine_identity()` is already local, so only the attestation half is missing. Also fixes a subject mismatch: v1 `runner_id` is a UUID while the consumer subscribes to `ps-supertrend`. |
| [24](plans/2026-07/24-adopt-the-toolkit-test-suite.md) | adopt the toolkit's test suite: move the test files that cover `custos_toolkit*` from philosophers-stone into this repository, so breaking the toolkit here turns this repository's own CI red | ✅ Completed (2026-07-30; `1221103`..`HEAD`) | PS Plan 60 Slice D ✅ | PS Plan 60 Slice E — **still blocked**: five PS files read `shared/` by hardcoded path, two of them invisible to an import scan (see close-out leftovers) | Plan 06 vendored PS's `shared/` tree and Plan 07 declared this repository its authority — but the code came and the tests did not. Measured 2026-07-30: PS has 1720 tests over that code, this repository has 2. The toolkit that executes live trades is therefore protected by two tests inside the repository that owns it, and this repository can break it with a green CI. Owner chose this direction on 2026-07-30 over keeping the inversion or deleting the coverage. |
| [25](plans/2026-07/25-binance-client-order-id-length.md) | Binance rejects every order this runner submits: the generated client order id is 44 characters against the venue's cap of 36, because the trader id carries 20 characters of the deployment instance id | ✅ Completed (2026-07-30; `3d22b82` + `6372b1d`) — the sole criterion was met PS-side: three testnet runs on an image rebuilt at custos HEAD (`3085244`), three of this runner's own 32-character client order ids accepted by the venue, zero `-4015` | none — reproducible on current code | every order on Binance, testnet and live; PS Plan 61 testnet acceptance | PS's first testnet run of the offline lane, 2026-07-30, failed at the first submit_order with `-4015 Client order id length should be less than 36 chars`. Sandbox settles fills locally and never submits to a venue, so no run before this one could reach the check; this repository has no handling for the cap and no test could have caught it. The fix is a choice between shortening the tag and switching to a fixed-width id — the plan carries the arithmetic, because a tag of 12 with a one-digit counter lands on exactly 36 and the cap is 'less than'. |
| [26](plans/2026-07/26-attachment-state-outlives-the-engine.md) | a restart keeps the previous process's attachment state: the reconciler reloads container_id along with generation, so it believes it is attached to something it never created | ⏳ In Progress (2026-07-30; `19b1735`..`0b26b9f`) — code green and **both symptoms now proven on real hardware** PS-side (no `structural reconfigure` with the workaround disabled and a stale attachment in the store; and a pinned same-generation restart emitting `offline_applied_generation_not_in_place` then re-engaging instead of reporting healthy). Still open: leftovers 3 (`container_id` is now read by nothing), 4 (a self-terminating node's cleanup path, plus `attached()` and `deploy()`'s idempotency guard disagreeing — that one needs real-hardware evidence of its own and these runs did not cover it), 5 (no exception boundary for synchronous engine queries; a plan of its own) | none — reproducible on current code | the offline lane's first deployment after any restart; philosophers-stone works around it today | One cause, two symptoms, both hit on 2026-07-30. A higher generation after a restart is dispatched to reconfigure because container_id is non-empty, and the engine refuses it as a structural reconfigure although nothing structural changed — only the attachment was lost. The same generation after a restart takes the equality branch and reports healthy without engaging the engine at all, so a consumer's wait-status passes while nothing runs. The reconciler cannot check: the OfflineEngine protocol has no attachment query, so it can only trust the field it persisted. Generation is right to survive a restart; attachment is not. Owner chose option B on 2026-07-30: add that attachment query to OfflineEngine — deliberately not to the runtime_checkable ExecutionEngineProtocol, whose presence-based isinstance assertions would silently change meaning for every implementer. Implemented as dispatching on that query rather than calibrating the recorded id: calibration fixes only the restart, leaves a node that died inside one process still routed to reconfigure, and would itself be unobservable. Two plan premises were corrected by measurement — an existing test asserted the second symptom as contract, and the host's two registries do not in fact die together. |

| [27](plans/2026-07/27-startup-guards-question-an-unready-portfolio.md) | startup guards question a portfolio that is not ready yet: equity has no declared currency, so a funded multi-currency account is unreliable by construction, and the fail-closed flatten runs before reconciliation delivers the account's existing positions, so it flattens nothing while the record reads as if containment happened | ⏳ In Progress (2026-07-31; `40d94e6` + `1073d36` + `675c0fd`) — the guards now declare their settlement currency, derived from the deployment's pairs at deploy, so a funded multi-currency account no longer makes equity unresolvable (**verified on real hardware**: zero `portfolio_equity_ambiguous`); a flatten that found nothing open is recorded as containment-unconfirmed instead of as a flatten (real hardware exercised only the branch this did *not* change); and **finding C**, which the equity failure had been masking all along: the guards price every position from a mark price that nothing in the tree ever subscribed (`git grep subscribe_mark_prices 675c0fd~1` — zero hits), whose fallback is MID, which needs quote ticks this deployment does not take. With a position open the snapshot therefore could not be reliable and the breaker could do nothing but fail closed — not a race, so no amount of waiting would have fixed it. Owner chose B3, but **B1 cannot be built as written, and its necessity is now downgraded** — it was called necessary on the reading that the breaker was waiting for data in flight, which finding C refuted: the offline lane never waits for engine readiness (`wait_ready` is called only by the signed lane), the one readiness field naming reconciliation is a placeholder (`trading_mode == "sandbox"`, so `ready` is permanently false on testnet), and the alternative needs the latch semantics this plan lists as a non-goal. Its viable form is a bounded startup grace, which changes fail-closed timing and is left to the owner. Leftover 3 is now closed too (`cf70afb`): investigating the placeholder readiness gate showed it was not merely unchecked but **inverted** — sandbox is the one mode where reconciliation is switched off, so `reconciliation_initialized = trading_mode == "sandbox"` was true exactly when nothing had been reconciled, which is what made `ready` unreachable on the only modes that touch a venue. Two of its six siblings were no better: `portfolio_initialized` asked whether an always-present attribute existed, and `strategy_accepting_lifecycle` was character-for-character the same expression as `node_task_alive`. All three were replaced rather than just the named one, because fixing one would have turned a gate that never passes into a gate that always passes — louder is safer than silent. The reconciliation check is possible at all because `NautilusKernel.start_async` returns without starting the trader if reconciliation fails, so a running trader is the completion signal NautilusTrader otherwise does not expose. Not verified on real hardware and cannot be from this lane: the offline lane never calls `wait_ready`; the signed lane does, and that is where this defect actually bit | none — reproducible on current code | the offline lane running on a funded testnet account (today it trips and latches at startup) | Two independent causes, both surfaced by one 34-minute real testnet run on 2026-07-30 — the same run that proved plan 25's -4015 fix. **A**: `_resolve_equity` requires exactly one entry when no currency is requested (`portfolio_snapshot.py:207`), but three of the four call sites pass none (`host.py:690` / `:742` / `:792`; only `:647` declares one, as a required parameter). An account holding USDT + USDC + BTC with `base_currency=None` is therefore ambiguous by construction — funding it in more currencies makes it worse, not better. The settlement currency is already derivable from `spec["pairs"]` by an existing helper (`sandbox_runner_fact_host.py:150`), and that derivation works with zero positions, which is exactly when it is needed. Only `NtTradingNodeHost` is affected; `SandboxSimulationHost` returns constants and never consults the portfolio, which is why sandbox stayed green — same shape as plan 25's `DEV-25-TASK-4-PREMISE-CORRECTED`. **B**: the breaker tripped at 08:55:49.353 and `positions_flattened` recorded `instrument_count: 0`; the account's pre-existing short arrived via reconciliation 2.5s later and was never flattened, the guard having already latched. It is now held only by the strategy's own resting stop. That is C9's own prevention clause — the record must say containment was not confirmed — recurring on a path it had not reached, and C12's error at a third scale: treating something that represents an incomplete past as the current answer. |

| [28](plans/2026-07/28-a-refused-reduce-only-close-needs-an-escape-hatch.md) | a reduce-only close that the venue refuses has no way out: both close paths hard-code `reduce_only=True`, so when the demo matching engine's reduce-only path is broken the position cannot be closed at all | ⏳ In Progress (2026-07-31; `9f38691` + `6f0c0e1`) — the regular exit path falls back and is **proven on real hardware**: a trend reversal at 08:30 UTC had its reduce-only close refused with a live `-2022`, the next bar closed with a plain `BUY MARKET 0.0070 IOC reduce_only=False`, and the position went FLAT. Task 1 was skipped and cost us the same day: with the venue degraded, a rejection carrying no reason at all landed in the classifier's logic tier — its documented default for unrecognised reasons — armed the hatch, and sent a plain reverse-capable order once per bar while -1007 said execution status was unknown. Nothing reversed only because those orders were refused too. `6f0c0e1` now requires positive evidence that reduce-only itself was refused, and allows one plain attempt per position. `cbbff0a` then finished tasks 4 and 5: the rule is now one pure function in `strategy_core` returning three states, because what to do once the single plain attempt is spent genuinely differs by caller — a per-bar exit sends nothing (re-sending each bar is lesson #13's flood shape) while one-shot containment falls back to the reduce-only form, which will likely be refused but cannot reverse anything. The breaker flatten reaches it by duck-typing, so a non-toolkit strategy is untouched. What that flatten still cannot do, stated so the record does not read otherwise: a rejection arrives asynchronously while the flatten is one synchronous pass, so a refusal occurring *during* it is invisible — it helps only when the refusal was already recorded, which is what the real incident looked like. That structural limit is registered as plan 29. Still open: real-hardware confirmation of `6f0c0e1` and `cbbff0a`, and `emergency_close` remains unreachable on this lane, so wiring it is not the same as verifying it | none — but triggering it needs the demo engine in its broken state, which is intermittent | closing reliably on `demo-fapi`; a repeat of lesson #14 still needs manual intervention | Owner's instruction, 2026-07-30: when a reduce-only close fails, a plain opposite-direction order should be allowed. Grounded in PS lesson #14, where Binance **Demo** held a real short with zero open orders and refused reduce-only anyway (`-2022`) from both the strategy and the web UI, and a non-reduce-only market order netted it flat. The order path in use today is that same service — the three testnet runs on 2026-07-30 show REST going to `demo-fapi.binance.com` while market data comes from testnet streams. Those runs had zero `-2022`, which is **not** evidence the environment is sound: the defect is intermittent. The design problem is not the order but the gate, because `reduce_only` is there on purpose — `execution.py:177-180` says it stops a re-submitted exit from opening a reverse position when the cache lags a fill, and dropping it removes exactly that. Compounding it, `classify_rejection_reason` currently buckets `-2022` together with `-2019` margin, so nothing downstream can key on "reduce-only was refused" alone; separating them is the first task and a safety precondition. Also blocked on a scope decision: lesson #14's authoritative check (`positionRisk` + `openOrders`) is unavailable, since the strategy layer has no REST client at all. |


| [29](plans/2026-07/29-a-cancel-counts-as-cancelled-the-moment-it-is-sent.md) | a cancel counts as cancelled the moment it is sent: nothing waits for confirmation, so orphaned reduce-only stops pile up at the venue one per run | ⏳ In Progress (2026-08-01; custos `816548b` + PS `b593718`) — **task 1 only, and deliberately so**: a cancel request and its outcome now leave countable records, a run's logs survive `make stop`, and `make cancel-audit` prints requested / confirmed / refused / unaccounted. Recording is an override of the base class's two venue calls rather than a line at each of the twelve cancel sites — a record every site has to remember to write is the record the thirteenth will not have. The bulk call is the one that mattered: it names an instrument, not orders, so it produced no count of what was asked for, which is exactly the left-hand side of the question. The unit tests stub the handoff to the Cython base, so a separate real-`BacktestEngine` test cancels a real resting order and reads `CANCELED` back — and was falsified by breaking the delegation, because the stubbed step is the one that must not break. Tasks 2-4 are **not** started on purpose: the three candidate causes still cannot be told apart, so the next move is a real run with this in place, not a fix chosen by reasoning | none — reproducible on current code | unattended long runs (each restart leaves one more resting order); and plan 28's breaker flatten, which wants the same confirmation loop | Registered from plan 28 §Follow-up. Measured 2026-07-30: four `0.0070` reduce-only stops from four earlier runs resting at once against a single `0.0070` position. Not merely untidy — resting reduce-only quantity above the position size is what gets a close refused (lesson #13), so the orphans eat the ability to close, and the "cancel first, then close" mitigation already in the code depends on cancels actually landing. Three paths leave them, each with a different gap: `on_stop` cancels fire-and-forget (`trading_strategy.py:548`) and the container has no `stop_grace_period`, so docker's 10s default then SIGKILL; the reversal path's cancel gets **no event at all** from Nautilus when the venue call fails (that file's own docstring, `order_reconciler.py:283-287`); and the per-bar sweep that backstops both is driven by `on_bar` (`strategy_core.py:307`), so a stalled feed stops it — an 11-minute stretch with zero bars was measured on 2026-07-31. The backstop's *classification* is right: checked rule by rule, the three unclaimed stops land squarely on `is_stale_order`'s duplicate branch (`orders.py:287-291`). So what failed is not the rule but whether it ran, or whether its cancels took effect — and those three candidates **cannot be told apart from the evidence that still exists**, which is why the plan's first task is to make the evidence obtainable rather than to pick one. Separately, `handle_order_cancel_rejected` (`:451`) reads every cancel rejection as "the order is already gone" and does nothing at all for SL/TP — C9's shape exactly: no exception to catch, so nobody guarded it. |

| [30](plans/2026-07/30-a-latched-guard-stops-watching-while-the-strategy-keeps-trading.md) | a latched exposure guard stops watching while the strategy keeps trading | ⏳ In Progress (2026-08-03; `c2413cd` + `b291124` + `484d19f`) — owner chose **A + C**: a trip now flattens once *and* ends the deployment, and the loop no longer stands down, so a stop that failed to take is still seen. Whether to keep trying is decided by asking the engine what it still holds (`attached()`), not by remembering that a stop was requested — C12 at a third scale. Foundation Scan corrected two cells of the plan's own cost table: A needed no new primitive (the guard and the reconciler already hold the same engine object, and `OfflineEngine` has had `stop`/`attached` since plan 26), while B needed no breaker change but has no convergence point — `evaluate()` returns `tripped` regardless of `_frozen`, so B would re-flatten every 5s against a strategy that keeps re-entering. Two existing tests asserted the defect as contract and were rewritten, not deleted (plan 26's shape). **Still open: no real-hardware evidence**, per plan 25's precedent this stays ⏳ until a testnet run shows `offline_exposure_latched_deployment_stopping` followed by no further entries | none — reproducible on current code, and already reproduced on real hardware | unattended long runs; and plan 22's local exposure guard, which is in name only once latched | Found on 2026-08-01 while measuring what `max_total_notional` should be. The guard tripped at 02:44:57 on an inherited 353 USDT position against the strictest $200 default, latched — and over the next 2h40m the strategy opened four more positions of ~449 USDT each, every one of them more than twice the ceiling that latched it, with the guard never looking again. `safety.py:299` returns from `run()` once everything is latched, `evaluate_once` skips latched entries, and `engine_safety.py:57` flattens only on the tick that trips. Nothing touches the strategy, which goes on trading its own signals. The code faithfully implements what the module docstring says; the wrong part is the docstring's *because* — it assumes exposure can only return via a new generation the operator publishes, and refusing generations therefore guards a road nobody was using while the road actually carrying exposure has no gate. Worse than plain absence: "latched" and an unhealthy `make status` read as contained, so this is running unguarded while looking safer than unguarded — PS lesson #15's shape, a protection whose own failure path recreates the blind spot it exists to remove. |

| [31](plans/2026-07/31-spec-leverage-never-reaches-a-real-venue.md) | the spec's leverage never reaches a real venue, only the sandbox | ✅ Completed (2026-08-03, custos `9b3af5f`) — wired and verified on real hardware: the exec client now logs `Set default leverage BTCUSDT 3X`, which appears zero times in 46 hours of pre-change logs, and a startup snapshot taken while holding a position opened under it reads initial margin 148.59 against ~445.8 notional — a ratio of 3.00, where the same size previously carried 447.77 against 447.77. Nearly misread on the way: the 447.04 lock that looked like 'no effect' is stamped 168ms *before* the leverage call, and every AccountState after it carries `margins=[]` because those are incremental WS updates that only report the wallet — 'cannot read it' was one step from being recorded as 'it is zero'. The sandbox type mismatch turned out to be the opposite of a defect: `SimulatedExchange` wants `dict[InstrumentId, Decimal]` exactly, so what custos passes is right and the config's `dict[str, float]` annotation is the inaccurate part — do not 'fix' custos to match it. `futures_margin_types` deliberately left unset | none — reproducible on current code, and observed on real hardware | the strategy's stop-loss-vs-liquidation startup check, which trusts the spec's leverage while nothing makes the venue match it | Observed 2026-08-01 on testnet: spec says `leverage: 3`, the exchange reports initial margin 447.77 against a notional of 447.77 at mark — a margin ratio of 1.0, so 1x. Not the strategy: the toolkit only *reasons* with leverage (`trading_config.py:145-190` estimates liquidation distance as `1.0 / leverage` to check a fixed stop can trigger in time) and never sets it, which is the right split. It is the venue builder: `build_futures_leverages` (`venue_binance.py:138`) computes the map but only `build_exec_client_config_sandbox` consumes it, while the testnet/live path (`_build_binance_exec_config`) leaves `BinanceExecClientConfig.futures_leverages` unset, so the account default applies. Its own docstring says "(and any future Binance live exec config)" — the author knew. Wiring it is not a drop-in: the builder returns `dict[InstrumentId, Decimal]`, sandbox declares `dict[str, float]`, Binance declares `dict[BinanceSymbol, int]` — different in both key and value. Harmless in the current direction (checking against 3x while the venue is 1x is stricter than reality) but wrong-way-round: an account set to 20x against a spec saying 3 would pass a stop that cannot trigger before liquidation. Lesson #21's shape — a ratio with no single source of truth. **Unverified and worth checking when wiring**: sandbox is handed keys and values that do not match its declared type either, and msgspec does not validate on direct construction (PS lesson #36), so whether sandbox leverage takes effect at all was not established. |

> ¹ Plan 00b (telemetry 桥) close-out 前, 由 CEO override 提前放行 00c
> (`DEV-00c-DEP-SKIP-CEO-OVERRIDE`, lesson #38 CEO override 4 件套记录路径)。
> 后果: e2e 观测面部分启用 — testnet 真跑 fill/OrderDenied 只走 custos 本地
> structlog, 00b telemetry 桥落地后才对外上报云端 arx。见
> [Plan 00c §偏离日志](plans/2026-07/00c-g6-gate-live-release.md#偏离与改进日志-deviation-log)
> + [historical-lessons C1](../.claude/rules/historical-lessons.md)。
| [01](plans/2026-07/01-forge-bootstrap.md) | Forge 基础设施 bootstrap | ✅ Completed (2026-07-07) | 无 | (逻辑上先于 00a-c) | `.gitignore` / `.claude/rules/` / `Makefile` / `docs/design/ops/guides/` / `CLAUDE.md` |

### 执行顺序建议 (Plan 04/05/06/07/08 + Batch 2 09/11)

```
Plan 05 (结构重构 + arx_runner → custos rename + core/engines 分层) ✅ Completed (05a `4f0192a` + 05b `e82825d`)
  ↓
Plan 04 (红线 0.3 兑现) — 落到 custos.core.*
  ↓ 与 06 可并行 (串行推荐 04→06, 见 04-05-06 packet §12)
Plan 06 (ps supertrend 迁移 06a slice ✅ landed 306b9e5)
  ↓
Plan 07 (ps shared curation + convergence — Batch 1) — 落到 custos.engines.nautilus.toolkit.*
  ↓
Plan 08 (Plan 06 remainder — Batch 1) — real supertrend e2e + Plan 06 close-out
  ↓
Plan 11 (custos CLI subcommand alignment with lifecycle.md — Wave v1-team-full-loop) ✅ Completed
  ↓
Plan 12 (signed wheel / Docker / LTS — STRICT SERIAL after Plan 11 landing) ✅ Completed
  ↓
Plan 09 (hook infra formalization — Batch 2, awaits Plan 11 + Plan 12 landing)
  ↓
Plan 10+ (未来引擎接入, 一引擎一 plan: hummingbot / freqtrade / athanor / nt-rust)
```

**Batch 1** (Plan 07 + Plan 08): 2026-07-10 CEO ratified `APPROVED_WITH_FOLLOW_UPS`. Plan 07 landed 2026-07-10 (base `4437991`..`ce9fce2`, 8 commits, sonnet executor + main-session T5.1 takeover). Plan 08 START gate now open — awaiting Slot 2 dispatch. Batch 1 内 serial (Plan 07 landing → Plan 08 START gate).

**Batch 2** (Plan 09): 独立 dispatch 后续, 独立于 Batch 1 close-out. **2026-07-10 状态**: scope frozen (Standard, 8 tasks) — draft deferred (Plan 11 breaking release 是 script entry + namespace 的稳定引用基础, draft 起草需等 Plan 11 landing 避免 lesson #35 boundary constant rename fanout)。scope 详见下 §"后续 plan 规划" · Plan 09 段。

### 编号顺序说明

Plan `01` 在**逻辑上**应先于 `00a`/`00b`/`00c` 执行 (bootstrap 提供 `make verify` 等基础
设施), 但用户在 arx Plan 60 subtree split 之后**先起草了** 00a-c 三份 execution plan
+ commit (`87598b5`), 之后才补起 01 bootstrap. 编号顺序未倒回, 通过 Plan 01 偏离日志
+ 本索引段落显式记录. 见 [Plan 01 §偏离与改进日志](plans/2026-07/01-forge-bootstrap.md#偏离与改进日志-deviation-log).

## Close-out 归档

Plan close-out 后 (Status: ✅ Completed):

1. plan 文件末尾追加 `## 完成报告 (Close-out Report)` 章节 (模板见
   `../.claude/rules/progress-management.md`)
2. 本 README 表格 Status 列更新为 ✅
3. plan 文件**留在原路径**不迁移 (git history 是唯一真相)
4. 若产出 review / fix 副本, 归档到 `.forge/reviews/YYYY-MM/` / `.forge/fixes/YYYY-MM/`

## Agent Teams 入口

- **配置**: `.forge/teams.yaml`
- **启用 env flag**: `export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`
- **触发命令**: `/forge:plan-team` / `/forge:execute-team` / `/forge:architect-team` /
  `/forge:ops-team`
- **schema 参照**: `forge/docs/teams/ORG-CHART.md` §10 + §19 (workspace 内可见, 独立
  clone 后不适用; 若独立场景需 teams, 手写 teams.yaml)

## 后续 plan 规划

### 内部系统名对外收敛 — 两项 deferred (CEO 2026-07-28 决定先记录不做)

**背景**: 仓库 public。docs-site 有 disclosure gate、README 已清理, 但 `src/` 与
wire 层仍带内部系统名。2026-07-28 清理时只完成了**未被资产 pin 的 7 个源文件**的
散文改名 (commit `0d20b49`), 另两项判定为契约动作而非文档清理, 暂缓。

#### Deferred 1 — pinned 资产内的措辞改名

`docs/authority/**` 的资产索引按 **path + size_bytes + commit** 固定了 16 个
`src/custos/**` 文件。为措辞修改它们会让证据链对不上 —— 2026-07-28 实测:
改 `machine_credential_vault.py` 一句 docstring 即触发
`test_machine_request_consumer_assets_are_exactly_pinned` 失败 (26599 != 26594)。

**结论**: 这批改名必须与下一次 receipt 重新签发**同批**做, 单独改 = 破坏 pin。
识别命令:

```bash
grep -rho '"src/custos/[^"]*\.py"' docs/authority/ | sort -u
```

**副作用 (同源)**: pin 同时把这些文件冻结在 formatter 之外。
`src/custos/core/runner_fact.py` 与 2 个 pinned integration test 当前不是
format-clean, 所以 `make fmt-check` (进而 `make verify`) 在主干上就是红的。
另有 2 个未 pin 的 test 文件 (`test_runner_fact_contract_v1.py` /
`test_toolkit_release_authority.py`) 可以直接 `make fmt` 修掉, 与 pin 无关。

#### Deferred 2 — wire 层标识改名

以下都是对端按字面读取的契约表面, 改动需要**双方协同的 V1 迁移**, 且
`CLAUDE.md` §First-production V1 contract rule 不允许留兼容别名:

| 标识 | 位置 |
|------|------|
| 签名域 `CRUCIBLE-RUNNER-FACT-BATCH-V1\0` | 签名前像, golden 锁定 |
| NATS subject `crucible.runner_fact.*` / `crucible.runner.enrollment.pop.v1` | wire |
| HTTP header `X-Crucible-*` (11 个) | `machine_credential_vault.py` / `enroll.py` |
| env var `CRUCIBLE_*` · CLI flag `--crucible-*` | 运维产品面, 测试断言 |
| 类型名 `Crucible*V1` | authority 登记的消费者类型 |

**结论**: 属协议迁移, 不属清理。起 plan 时须同时排 producer 侧配合与迁移窗口。

#### Deferred 3 — `docs/` 剩余散文

剩余 19 篇中约 86 处内部名, 但多数文件是待 DEEPEN / MOVE 的合并目标
(见 `.forge/handoff/2026-07/docs-consolidation-map.md`), **跟随合并步骤自然清除**,
不单独处理。


Plan 01 close-out 之后:

- **02+**: 按需起 (如 `pyright` 集成 / OKX venue 支持 / 签名 release pipeline /
  Python 模块 rename `arx_runner` → `custos_runner`)
- **03 候选 `03-nt-host-hardening.md`** (来自 00a codex peer review F1, high red-line 观察):
  NtTradingNodeHost 通过 `_active_nodes` 的 `node` 引用间接内存持有 credential (via
  data/exec config)。Lead 判定这是 NT ↔ exchange 通信的**设计必要** (custos daemon 本就要
  本地持 key), 红线 0.1 原文限 log/publish/send I/O 边界, in-process 内存持有不违反 —
  **不阻塞 00a close-out**。后续 plan 加 credential lifecycle test suite, 验证三层 invariant:
  no credential in (1) `node` repr / (2) `node.__dict__` recursive dump / (3) structlog
  processor output。
- 编号沿用 `02` `03` ..., 不复用 `00` / `01`

### Plan 09 (Batch 2, hook infra formalization) — scope frozen 2026-07-10, draft deferred

**Status**: 🔲 Planned · scope frozen · draft deferred (waiting Plan 11 landing)

**Scope 决策 (CEO 2026-07-10)**: **Standard 档** (8 tasks, 无外部 framework, 与 sandbox
research + 单栈简洁诉求匹配)。拒绝 Full 档 (`pre-commit` framework 迁移) 与 Minimum 档
(只做 Plan 12 defer 两项)。

**依赖锁定**:

| Dep | 类型 | 原因 |
|-----|------|------|
| **Plan 11 landing** | Hard-dep (draft start) | Plan 11 breaking release 改 script entry (`python -m custos` → `arx-runner`) + namespace (`~/.custos/` → `~/.arx/`); Plan 09 的 `check-red-lines.py` grep + Makefile target 引用需要**稳定的名字空间**。若 Plan 11 前起草会命中 lesson #35 boundary constant rename fanout — 起草成本 + 重写成本翻倍。 |
| Plan 12 (`FM3` + `FM6`) | Soft-dep (Plan 09 反过来 unblocks Plan 12) | Plan 12 §失败模式表 FM3 (contract v1 backward compat) + FM6 (CHANGELOG-at-tag) 只出 stub / snapshot pytest 结构, wire 归 Plan 09 承载。Plan 12 可先 landed (只 stub 不 wire), Plan 09 后补 wire; 或 Plan 09 先 landed, Plan 12 直接引用。串行任意方向皆可。 |

**Scope 边界** (排除项 — 避免 lesson #35 boundary constant 混淆):

- **不含**运行时 hook (`DEV-04a-CAP-ENFORCEMENT-HOOK-DEFER` NT per-order intercept
  hook)。那是运行时代码 (`nautilus_host.py` submit-time `guard.allows`), 不是 git hook,
  归 v1 pre-live follow-up plan (独立编号)。
- **不含** `pre-commit` framework (Python 生态标准) 集成。CEO 明确拒绝, 因为引入外部
  dep 与 sandbox research + 单栈简洁诉求偏离。若未来 v1 后诉求变化, 单独起 follow-up
  plan 评估。
- **不含**新增 skill / plan-mode / CLAUDE.md 能力载体 — 全部产出 = shell script +
  Python static check + Makefile target + docs, 纯 static artifact。

**Standard scope 8 tasks 清单** (draft 时精细化):

1. **`scripts/hooks/` 目录规范化** — 现有 `pre-commit` wrapper 保留, 加 `commit-msg`
   与 `pre-push` 空槽 wrapper (即使 v1 无实装), 建立 `<hook-name>` → `run all
   scripts/hooks/checks/<name>.d/*.py` 的 fan-out 约定, 支持后续 hook 复合。
2. **`install-hooks.sh` 增强** — 支持 `pre-commit` + `commit-msg` + `pre-push` 三种 hook
   symlink, 幂等安装, 保留 backup pre-existing 逻辑。
3. **`check-silent-paths.py` (lesson #21)** — grep `src/custos/**/*.py` 里 silent 控制流
   (bare `except:` / `except: pass` / fire-and-forget task 未 `add_done_callback` / drop
   policy 未接 `structlog.warning` /`# noqa: SILENT-OK <reason>` 豁免)。落 `historical-lessons`
   #21 从 "红线宣言" 到 "自动化 gate" 的固化。
4. **`check-red-lines.py` (mandatory-rules §0 4 红线)** — 抽离 `verification.md`
   §"Non-Custodial 4 红线专项检查" 段的 grep 到自动化 pre-commit hook: 0.1 (Key/KEK 出
   进程) / 0.2 (G6 gate 绕过) / 0.3 (失联即停止) / 0.4 (float 用于 money math)。命中即
   阻断 commit + 输出 red-line 名 + 违反行号 + `mandatory-rules.md` 引用锚点。
5. **`check-changelog-at-tag.py` (Plan 12 FM6)** — 检测 `git push origin --tags` 前
   `CHANGELOG.md` 是否有对应 `## [<version>]` section, 未匹配则阻断。落到 `pre-push`
   hook 而非 `pre-commit` (tag 场景语义)。
6. **Gateway v1 snapshot pytest (Plan 12 FM3)** — 非 hook 但 CI gate 同族。
   `tests/test_gateway_contract_v1_backward_compat.py` snapshot golden diff (`arx-side`
   `CustosGateway` trait 的 4 方法 wire 契约), 消费 arx-side JSON schema fixture (Plan 11
   HTTP enroll endpoint fixture 已 landed 后可复用)。
7. **`docs/design/hook-infra.md`** — hook 系统架构 (fan-out convention +
   `<hook-name>.d/` 目录) + 添加新 hook 的流程 (write check → add to `.d/` → add
   failure-mode test → update this doc) + 豁免机制 (`# noqa: <check-name> <reason>`) +
   与 Non-Custodial 红线的对应表。
8. **Makefile `hooks-install` / `hooks-test` + CI 二次 gate** — `make hooks-install` 幂等
   调 `scripts/install-hooks.sh` + `make hooks-test` 跑所有 `tests/test_check_*.py` 失败模式
   测试。CI (GitHub Actions 或未来自建 runner) 同样跑 `make hooks-test` +
   `.githooks/pre-commit` 二次 gate (防 `git commit --no-verify` 绕过 local hook 后进主线)。

**Draft 起草时机**: Plan 11 landed 后 (预计与 Plan 11 execute-team dispatch 后续同批
或独立起 planning session)。届时:

- Foundation Scan Iteration 1: Plan 11 landed 后的 `scripts/` / `~/.arx/` namespace / new
  `arx-runner` entry 骨架 as-of 时间锚 (lesson #33)
- Foundation Scan Iteration 2: gateway contract v1 (Plan 12 若同期在 flight 需协调
  fixture ownership)
- Foundation Scan Iteration 3: mandatory-rules §0 4 红线 grep 现状 (哪些是"手工 checklist
  已定义 grep" vs "需要新起草 grep")

**Follow-up hooks (不属于 Plan 09 scope, 但登记以防遗漏)**:

- 运行时 NT per-order intercept hook (`DEV-04a-CAP-ENFORCEMENT-HOOK-DEFER`) — 独立 plan
- `pre-commit` framework 迁移评估 — v1 后诉求变化再起 follow-up
- pyright 集成 (Plan 02+ 候选) — 独立
