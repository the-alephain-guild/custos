# custos — AI 助手项目上下文

> 会话开始自动加载的**导航图**. 本文件只给定位与指针; 各子系统与红线细节在
> `docs/` 与 `.claude/rules/`, 不在此重复.

---

## 1. 这是什么

**custos** (拉丁语: *guardian*) 是 [The Alephain Guild](https://github.com/the-alephain-guild)
生态的 **non-custodial、自托管** 执行 runner. 用户在自己的基础设施上运行本 daemon,
让它跑经过回测的 NautilusTrader 策略, **本地**持有交易所 API Key, **永不上云端**.

它是 "Key 和策略只在用户本地" 红线从**设计声明**升级为**工程可验证**的**唯一路径** —
外部审计员单仓 clone 即可读全部代码, 验证承诺.

**License**: Apache-2.0 (day 1 公开). 详见 [`README.md`](README.md) · [`LICENSE`](LICENSE).

---

## 2. 子系统边界 (Custos / ARX / Crucible Rust)

- **Custos owns** local credentials, strategy process execution, venue
  interaction, local safety enforcement, and signed RunnerFacts.
- **Crucible Rust owns** DeploymentSpec, DeploymentInstance and every cloud
  business projection/state machine. It validates signed execution, venue and
  fee facts before projecting or settling them.
- **ARX owns** identity/tenant/RBAC/TOTP/resource policy and ActorAssertion. It
  authorizes typed control calls but is not a RunnerFact relay authority or
  business-state fallback.
- Transport may use authorized HTTP and JetStream subjects. Transport topology
  never changes ownership; every fact carries tenant, exact
  `deployment_instance_id`, spec id/digest, correlation and signature.
- Gateway contracts are versioned under `docs/gateway-contract/`; accepted mode
  values are only `sandbox`, `testnet`, and `live`.

详见 [`README.md#production-topology`](README.md#production-topology) 与 [`docs-site/docs/01-introduction/what-is-custos.md`](docs-site/docs/01-introduction/what-is-custos.md).

---

## 3. 六模块导航 (六件套)

| 模块 | 职责 | 设计文档 | 承担红线 |
|------|------|---------|---------|
| **enrollment** | nonce-bound Ed25519 PoP; encrypted `rkc1` credential; rotate/revoke | [`docs-site/docs/02-getting-started/enrollment.md`](docs-site/docs/02-getting-started/enrollment.md) | 私钥不出机 + startup fail closed |
| **reconcile** | Verify signed desired state → start/stop NT → enqueue typed lifecycle RunnerFact | [`docs-site/docs/03-concepts/reconcile-loop.md`](docs-site/docs/03-concepts/reconcile-loop.md) | 失联≠停止 (红线 0.3) |
| **engine host** | NT 进程监督 + `ExecutionEngineProtocol` + **live execution admission** | [`docs/authority/nautilus-host-contract.md`](docs/authority/nautilus-host-contract.md) | **执行门不绕过 (红线 0.2)** |
| **runner_fact** | NT MessageBus → typed signed RunnerFact outbox → Crucible | [`docs-site/docs/03-concepts/runner-fact.md`](docs-site/docs/03-concepts/runner-fact.md) | Key 不出进程 (红线 0.1) + Decimal (0.4) |
| **credential_vault** | sops+age exchange key + machine principal vault | [`docs-site/docs/04-operator-guide/credential-vault.md`](docs-site/docs/04-operator-guide/credential-vault.md) | KEK/机器私钥不出进程 (红线 0.1) |
| **nats_client** | Crucible signed desired-state subscriber only | [`docs/authority/nats-transport-contract.md`](docs/authority/nats-transport-contract.md) | schema 版本化 |

顶层 domain 词汇: [`docs-site/docs/01-introduction/what-is-custos.md`](docs-site/docs/01-introduction/what-is-custos.md).

---

## 4. Forge 工作流入口

- **plan 索引**: [`.forge/README.md`](.forge/README.md)
- **plan 目录**: [`.forge/plans/YYYY-MM/`](.forge/plans/)
- **teams 配置**: [`.forge/teams.yaml`](.forge/teams.yaml) (Agent Teams schema)
- **常用命令**: `/forge:plan` · `/forge:execute` · `/forge:review` · `/forge:fix`
- **进度状态**: 🔲 Todo / ⏳ In Progress / ✅ Done / ❌ Blocked (见 `.claude/rules/progress-management.md`)

---

## 5. Non-Custodial 4 红线 (速览)

以下四条**不可绕过**, 违反 = CRITICAL. 详见 [`.claude/rules/mandatory-rules.md`](.claude/rules/mandatory-rules.md) §0:

1. **Key / KEK 永不出进程** — 禁 log / publish / send raw key material; 禁 cloud SDK
2. **G6 host gate 不绕过** — live venue 必须过 `NtTradingNodeHost` G6 gate; `NoopHost` 只允许 sandbox/testnet
3. **Reconcile 失联 ≠ 停止** — 云端断线时本地 safety breaker + `max_notional_per_runner` cap 继续守护
4. **Money math 用 `Decimal`, wire 用 `str`** — 禁 `float()` 参与 money 路径

Signed runtime observability uses `RunnerRuntimeLogFact.v1` inside the existing
RunnerFact stream. It must use explicit structured events and
[`docs-site/docs/04-operator-guide/runtime-log-observability.md`](docs-site/docs/04-operator-guide/runtime-log-observability.md) redaction;
tailing stdout or forwarding raw exception text is forbidden.

紧急预案是**停止新建 live instance，并使用 sandbox/testnet**, 不是绕过红线. 见 [`.claude/rules/deviation-protocol.md`](.claude/rules/deviation-protocol.md) §紧急偏离.

---

## 6. 常用命令

| 用途 | 命令 |
|------|------|
| 装依赖 (dev extra) | `make install` (= `uv sync --extra dev`) |
| 跑测试 (完整, 含已知 fail) | `make test` |
| 跑测试 (独立仓基线) | `make test-baseline` |
| 格式化 | `make fmt` |
| 格式检查 | `make fmt-check` |
| Lint | `make lint` |
| 发布门 | `make verify` (= `check + test-baseline`) |
| 列全部 target | `make help` |
| 单跑 G6 gate 测试 | `uv run pytest tests/test_g6_gate.py -v` |
| Non-Custodial 红线 grep | 见 `.claude/rules/verification.md` §红线专项检查 |
| Docker 门 (image size + non-root + entrypoint smoke) | `make test-docker` |
| Release rehearsal (wheel + docker + sign, 本地) | `make release` |
| Post-publish verify (wheel + image signature + smoke) | `make verify-release VERSION=0.2.0` |

---

## 7. 规则集 / 权威文档 / 教训

- **规则**: [`.claude/rules/`](.claude/rules/) — 9 份 rule 文件, 会话开始自动加载
- **权威文档路径清单**: [`.claude/rules/authority-docs.md`](.claude/rules/authority-docs.md)
- **技术栈**: [`.claude/rules/tech-stack.md`](.claude/rules/tech-stack.md) (Python 3.11+/uv/nats-py/Pydantic v2)
- **代码风格**: [`.claude/rules/code-style.md`](.claude/rules/code-style.md) (ruff 100, 脱敏日志, Decimal money)
- **常见错误**: [`.claude/rules/common-errors.md`](.claude/rules/common-errors.md) (uv/pip 混用, NT lifecycle, async silent drop)
- **历史教训**: [`.claude/rules/historical-lessons.md`](.claude/rules/historical-lessons.md) (生态精华继承)
- **验证入口**: [`.claude/rules/verification.md`](.claude/rules/verification.md)

## 8. 独立开源仓库自足纪律

custos 是**独立仓库**, 外部审计员会 clone 单仓查代码:

- `.claude/rules/` 与 `docs/authority/ecosystem-authority.json` 是**自足**的;
  workspace 文档仅作存在时的可选交叉核对
- 规则思想与生态 `the-alephain-guild/.claude/rules/` 一致, 但文本独立维护
- workspace 场景开发者仍可参考 `../../.claude/rules/*.md` (生态原文), 但独立场景以本仓规则集为准

---

## Forge Agent Teams 接入

<!-- forge-teams-onboarded: 2026-07-07 -->

本项目已接入 Forge Agent Teams (plan-team / execute-team / architect-team / ops-team):

- **配置文件**: `.forge/teams.yaml` (schema: `forge/docs/teams/ORG-CHART.md` §10 + §19)
- **启用 env flag**: `export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` (详见 ORG-CHART §15.1)
- **验证**: 运行 `/forge:plan-team` 触发 Planner Dept dry-run

如需修改 teams 配置 (authority_docs / safety_paths / executor areas / model 分配),
直接编辑 `.forge/teams.yaml`; 重跑 `/forge:bootstrap --teams` 会进入 diff 模式而非覆盖.

**custos 特化 (与 arx 差异)**:
- 单栈 Python daemon, 只有 1 个 executor area (`runner` @ `src/custos`)
- safety.touched_paths 覆盖 `src/custos/` 全部模块 (non-custodial 承重墙)
- planner_team.drafters_per_session=2, codex_audit.max_calls_per_plan=3 (预算收紧, arx 是 4/5)
- architect_team.experts=`[domain, safety, python]` (无 rust / web 专家)
- opus 强角色显式 pin `claude-opus-4-7[1m]` (禁裸 `opus`, CEO 2026-07-06 禁)

---

*方法论权威在 [`docs-site/docs/01-introduction/what-is-custos.md`](docs-site/docs/01-introduction/what-is-custos.md), 红线权威在 [`.claude/rules/mandatory-rules.md`](.claude/rules/mandatory-rules.md).*

---

## Language Policy (Code Artifacts) — RED LINE

Global CLAUDE.md already sets this; executors have historically drifted, so it is restated here at project scope for last-mile compliance.

**English MUST** (source code and runtime artifacts):
- Identifiers: variables, functions, types, files, modules, table/column names
- Comments: inline (`//`, `#`), doc (`///`, `"""..."""`, `/** ... */`)
- Log messages: `structlog` / `tracing` / `println!` / `logger.*`
- Error / panic messages, `unimplemented!()` / `todo!()` payloads
- Commit messages (Conventional Commits scope + subject in English)
- Public API strings: JSON keys, HTTP error bodies, MCP tool names/args

**Chinese MAY**:
- End-user UI copy and product docs aimed at Chinese users
- Planning docs under `.planning/` (existing project convention)
- AI ↔ user conversation (global rule; unchanged)

**Enforcement**: rewrite the offending artifact in English; do not translate in-line or leave `// TODO: translate` markers. Review-time slips are grounds for a rework commit, not a follow-up TODO.

## Authority manifest

authority-manifest.json is the machine-readable entry point for ecosystem
ownership, migration and architecture documents. Run make check-authority when
changing ownership or cross-service protocols.

## First-production V1 contract rule

`authority-manifest.json` defines the only current production contract set. Custos owns the execution ABI, `StrategyArtifactRefV1`, local verification, and RunnerFact/runtime state; it must not copy PS or Crucible owner schemas into its own schema. New features change V1 in place, without predecessor parsers or compatibility aliases. V2 requires a real external production consumer and an explicit migration window.
