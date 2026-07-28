# 20 - custos.alephain.com docs site scaffold (Docusaurus + i18n + versioning)

> **Status**: ⏳ In progress — CEO decisions ratified 2026-07-19 (see §CEO Decisions); Sessions 1-3 landed 6/12 tasks (T1-T5 + T9 ✅, T6 at 4/46 chapters, T7/T8/T10/T11/T12 open)
> **Created**: 2026-07-19
> **Project**: Custos
> **Source**: Ecosystem-wide landing discovery Track α close-out (2026-07-19); the Guild landing, Alchymia landing and Tesseract landing all link to `custos.alephain.com` as the docs surface; the domain has no site yet.
> **For Claude**: Use `/forge:execute` per canonical slice. Multiple sessions expected — see multi_session_scope.
> **multi_session_scope**: `true`
> **Depends on**: none — Custos 0.3.0 code + docs/ authoritative material already landed (Plans 14-17); this plan surfaces existing docs, does not rewrite them
> **Soft depends on**: `the-alephain-guild/custos` GitHub org access for Pages settings and CNAME
> **Hard gates**: none (documentation only; no red-line risk)
> **Non-goals**:
> - Rewriting or forking authoritative content in `docs/domain.md` / `docs/design/*` — the site consumes these as source of truth
> - Publishing `docs/authority/*` receipts (internal artifacts; may reference by hash only, not embed)
> - Marketing copy that speculates on unshipped ARX capabilities — must obey the ecosystem `naming-authority.md` phased-integration discipline

## CEO Decisions (ratified 2026-07-19)

- **D1 — Deploy repo**: `the-alephain-guild/custos` main repo, `gh-pages` branch. No
  separate `custos-docs` repo. Single-repo self-sufficiency preserved.
- **D2 — DNS ownership**: CEO owns the Cloudflare CNAME record
  (`custos.alephain.com CNAME the-alephain-guild.github.io.`); T10 emits a runbook
  but does not run DNS changes.
- **D3 — Homepage narrative**: Follow the Guild landing "Two Faces of one
  product" narrative (ARX kernel + custos edge) — consistent with the ecosystem
  story. T7 will implement `src/pages/index.tsx` accordingly.

## 上下文 (Context)

Track α (2026-07-19) 完成生态 discovery 拓扑修复:三个 landing (`alephain.com` /
`alchymia.alephain.com` / `tesseract.alephain.com`) 现在都出链 ARX private beta
CTA 和 **custos.alephain.com** 作为 custos 的公开文档面。**但 custos.alephain.com
本身还没有站** — 存在死链风险。

同时,custos 已有丰富的 authoritative documentation 存在 `docs/` 目录内:

```text
docs/
├── domain.md                       # Bounded context, canonical terms, invariants
├── design/
│   ├── 00-overview.md              # Design overview (non-custodial pillars)
│   ├── 01-architecture.md          # Layer architecture
│   ├── 02-module-design.md         # 6-module navigation
│   ├── 03-implementation.md        # Implementation contract
│   ├── enrollment.md               # Enrollment lifecycle (red line 0.1)
│   ├── reconcile.md                # Reconcile loop (red line 0.3)
│   ├── nautilus_host.md            # NT process supervision + G6 (red line 0.2)
│   ├── credential_vault.md         # sops+age vault (red line 0.1)
│   ├── runner_fact.md              # Signed RunnerFact outbox
│   ├── runner_safety_policy.md     # Local safety enforcement
│   ├── nats_client.md              # Crucible signed subscriber
│   ├── runtime_log_fact.md         # RunnerRuntimeLogFact.v1 redaction
│   ├── engine_protocol.md          # ExecutionEngineAdapter contract
│   └── strategy-toolkit.md         # Toolkit ABI
├── engines/                        # Per-engine onboarding stubs (5 files)
├── gateway-contract/v1-v4/         # Versioned gateway JSON schemas
├── guides/dev-guide.md
├── ops/                            # Operational runbooks
├── authority/                      # Internal receipts (NOT for docs site)
├── lts-commitment.md               # SEMVER + LTS window
├── upgrade-path.md                 # 0.2 → 0.3 → 1.x migration
└── reproducible-build.md           # sigstore + SOURCE_DATE_EPOCH
```

**这份 material 已经权威、完整,是 audit-grade 的**。缺的**不是内容,是可读取的 surface**:
- 外部审计员 clone repo 才能读(单仓自足纪律满足,但 discovery 差)
- 集成方(Crucible / ARX / 第三方)从生态 landing 跳过来找不到入口
- 中英双语(生态 landing 已双语,docs 目前只有 English + 中文技术注释)

现有 3 个存量文档站栈候选:Docusaurus / VitePress / MkDocs Material。选择依据:
i18n + versioning 都是一等公民 → **Docusaurus 3.x**。custos SEMVER + LTS 契约天生
需要 versioned docs(与 `docs/lts-commitment.md` 对齐),Docusaurus 的 `versioned_docs/`
文件系统模型是最贴合的方案。

## 目标 (Goal)

发布 **custos.alephain.com** 作为 custos 的**公开文档站**,满足:

1. **信任兑现兑现**:外部审计员单一 URL 即可读全部 non-custodial 红线设计、代码
   锚点、审计清单,无需 clone repo(clone 仍然是 primary,但站是快速 discovery layer)
2. **集成方入口**:Crucible / ARX / 第三方开发者从生态 landing 跳来,能顺读 6 大模块
   设计 + Gateway contract + Getting started
3. **中英双语**:公开英文为主,中文完整 mirror(与生态 landing 一致)
4. **版本切换**:第一版 pin 到 `0.3.0`,与 CHANGELOG 保持同步;未来 breaking release
   保留旧版本 docs 一致于 LTS 承诺
5. **零内容漂移**:站上章节是 `docs/*.md` 的 rendered surface,而非独立复制;`docs/*`
   仍是 SSOT,`docs-site/` 是构建产物
6. **首页承接生态叙事**:显式 ARX section(讲清授权层 vs 执行层配套关系)+ 极简 mast-strip
   与三个存量 landing 视觉呼应(#14 内容合并到本 plan)

## Authority Boundary

| 能力 | Canonical owner | Plan 20 职责 |
|---|---|---|
| Bounded context, domain terms, invariants | `docs/domain.md` | 消费 as-is;文档站不重写 |
| Red-line design docs (`design/*.md`) | `docs/design/` | 消费 as-is;文档站按章节结构 surface |
| Gateway contract | `docs/gateway-contract/v1-v4/` | 消费 as-is,展示 JSON schema + 变更历史 |
| SEMVER + LTS commitment | `docs/lts-commitment.md` | 消费 as-is |
| Site build/deploy pipeline | Plan 20 | 定义并版本化(`docs-site/`, GitHub Action) |
| Chinese translations | Plan 20 | 建立并维护 (i18n) |
| Site theming and IA | Plan 20 | Docusaurus theme + sidebars.js |
| Public domain and CNAME | Plan 20 + operator (DNS) | Plan 提供 CNAME 文件;operator 加 Cloudflare 记录 |
| Ecosystem naming discipline | `the-alephain-guild.github.io/data/naming-authority.md` | 消费 as-is — canonical positioning phrases 与 forbidden aliases |
| ARX section on homepage | Plan 20 | 遵守 naming-authority 阶段化措辞纪律(Phase 3/4 不冒充当前能力) |

## Foundation Scan (as-of 2026-07-19)

Grep-verified 现状:

- `docs/` 已完备 (13 design docs + 5 engine stubs + gateway v1-v4 + 5 top-level meta docs)
- `docs-site/` **不存在**(no prior scaffold)
- `.github/workflows/` 存在:CI + release workflows (Plan 12);无 docs 相关 workflow
- 无现存 GitHub Pages 配置(check `.github/pages.yml` — 无)
- CHANGELOG 顶部为 `[0.3.0] - 2026-07-12` — 第一版 docs 应 pin 到 `0.3.0`
- 无 CNAME 文件(需新建)

## Task 拆分

- **T1 — Plan authoring** (this file) — status: ✅ Completed (2026-07-19)
- **T2 — Docusaurus init**: `docs-site/` 目录 + `package.json` (Docusaurus 3.6) +
  `docusaurus.config.js` (i18n locales=['en','zh-Hans'], baseUrl='/', url='https://custos.alephain.com')
  + `sidebars.js` (10 parts skeleton) — status: ✅ Completed (2026-07-19, Session 1); `npm ci` verification pending operator run
- **T3 — Theme + branding**: 与生态 landing 视觉呼应(Guild `alephain.com` 深金 accent
  `--accent-2`, Newsreader/JetBrains Mono 字体系),浅色 paper + 深色 slate 双主题;
  favicon placeholder gold 'c' monogram — status: ✅ Completed (2026-07-19, Session 1);
  final custos-branded favicon / og-card assets pending T7
- **T4 — Chapter skeleton (empty MDX files)**: 46 chapters across 10 parts per outline
  agreed in the ecosystem discovery conversation (2026-07-19); each stub carries
  chapter title, target audience, and TODO list — content filled in T5 and T6 —
  status: ✅ Completed (2026-07-19, Session 1); 46/46 stubs grep-verified
- **T5 — Migrate existing `docs/**.md` to `docs-site/docs/`** (Part I-VIII) — status: ✅ Completed (2026-07-19):
  - `docs/domain.md` → Part I intro / concepts
  - `docs/design/00-overview.md` → Part I architecture
  - `docs/design/01-architecture.md` → Part I architecture
  - `docs/design/02-module-design.md` → Part III concepts
  - `docs/design/03-implementation.md` → Part III + IV
  - `docs/design/enrollment.md` → Part IV
  - `docs/design/reconcile.md` → Part III + V
  - `docs/design/nautilus_host.md` → Part VII
  - `docs/design/credential_vault.md` → Part IV + V
  - `docs/design/runner_fact.md` → Part VI
  - `docs/design/runner_safety_policy.md` → Part V
  - `docs/design/nats_client.md` → Part VI
  - `docs/design/runtime_log_fact.md` → Part IV + V
  - `docs/design/engine_protocol.md` → Part VII
  - `docs/design/strategy-toolkit.md` → Part VIII
  - `docs/engines/*` → Part VII engine index
  - `docs/gateway-contract/v1/*` → Part VI
  - `docs/lts-commitment.md` → Part X
  - `docs/upgrade-path.md` → Part X
  - `docs/reproducible-build.md` → Part V (signed release chain)
  - `docs/guides/dev-guide.md` → Part IX reference
  - **Migration mode**: MDX front-matter added; content copied verbatim; `docs/**.md`
    remains SSOT — site pages carry `<!-- source: docs/domain.md -->` header for
    provenance grep-verifiability
  - `docs/authority/*` **NOT** migrated (internal receipts; may reference by hash)
- **T6 — Chinese translation (initial pass)** — status: 🟡 In progress (Session 3 first slice · 4/46 chapters translated · 2026-07-20): build zh-Hans locale scaffold; translate
  Part I introduction + Getting started (chapters 1-7) as first slice; remaining
  parts get `TODO: 翻译` placeholder pages so URL structure is complete but content
  is deferred (multi-session scope; T6 spans multiple slices). Session 3 delivered
  Part I (3 chapters) + Part II enrollment (1 chapter); the remaining 3 Part II
  chapters wait for en content (they are still T4 stubs — no explicit T5 source).
- **T7 — Homepage + ARX section**: `docs-site/src/pages/index.tsx` custom homepage
  featuring:
  - Hero: "custos — the non-custodial execution runner"
  - Two-column: What custos is · What ARX is (explains layering; obeys
    naming-authority.md phased-integration discipline; ARX private-beta CTA to
    `mailto:contact@alephain.com`)
  - Trust anchor callout: 4 red lines (KEK/G6/reconcile/Decimal) with links
  - Getting started cards: 4 quick-start entries
  - Ecosystem strip in footer: attribution to Alephain Guild, links back to
    `alephain.com`
- **T8 — mast-strip in site header**: reproduce the Guild ecosystem discovery pattern
  from Track α (`ARX · private beta ↗` + `part of Alephain Guild ↗`); consistent
  with `alephain.com` / `alchymia.alephain.com` / `tesseract.alephain.com` topbars
- **T9 — GitHub Action (docs-deploy.yml)** — status: ✅ Completed (2026-07-19, brought forward from Session 5): on push to `main` under `docs-site/**`,
  build with Node 20 + npm ci + npm run build, deploy to `gh-pages` branch via
  `peaceiris/actions-gh-pages@v4` (v3 in original spec upgraded to v4 for
  fresh-run compatibility). Concurrency guard prevents gh-pages force-push
  races. Repo Pages settings: source = `gh-pages`
  branch. CNAME file at `docs-site/static/CNAME` = `custos.alephain.com`
- **T10 — DNS handoff to operator**: emit runbook file
  `.forge/handoff/2026-07/20-cnAME-dns-setup.md` documenting what Cloudflare
  record to add (`custos.alephain.com CNAME the-alephain-guild.github.io.`) and
  verification steps (`dig` + `curl -I`); operator applies manually
- **T11 — Versioning enable**: after content stable, run `npm run docusaurus
  docs:version 0.3.0` to snapshot current docs as v0.3.0; future minor/major
  bumps freeze accordingly per `docs/lts-commitment.md`
- **T12 — Close-out**: verification receipts (see below), plan Status ← ✅,
  update `.forge/README.md` plan index

## File Inventory

**New files**:

```
docs-site/
├── docusaurus.config.js
├── sidebars.js
├── package.json
├── package-lock.json
├── tsconfig.json
├── babel.config.js
├── src/
│   ├── pages/index.tsx              # T7 homepage
│   ├── css/custom.css               # T3 theme
│   └── components/MastStrip.tsx     # T8
├── docs/
│   ├── 01-introduction/…            # Part I (3 chapters)
│   ├── 02-getting-started/…         # Part II (4 chapters)
│   ├── 03-concepts/…                # Part III (5 chapters)
│   ├── 04-operator-guide/…          # Part IV (6 chapters)
│   ├── 05-trust-model/…             # Part V (6 chapters)
│   ├── 06-integration/…             # Part VI (5 chapters)
│   ├── 07-engines/…                 # Part VII (3 chapters)
│   ├── 08-toolkit/…                 # Part VIII (3 chapters)
│   ├── 09-reference/…               # Part IX (4 chapters)
│   └── 10-release-governance/…      # Part X (6 chapters)
├── i18n/
│   └── zh-Hans/…                    # T6 mirror
└── static/
    ├── CNAME                         # T9
    ├── img/favicon.svg
    └── img/og-card.png

.github/workflows/
└── docs-deploy.yml                   # T9

.forge/handoff/2026-07/
└── 20-cname-dns-setup.md             # T10
```

**Modified files**:

- `.forge/README.md` — add plan 20 to index (T12)
- `README.md` (repo root) — add `## Documentation` section pointing to
  `custos.alephain.com` (T12)
- `.gitignore` — add `docs-site/node_modules/`, `docs-site/build/`,
  `docs-site/.docusaurus/` (T2)

**NOT touched** (SSOT preserved):

- `docs/**` — remains authoritative; site consumes verbatim via `<!-- source: -->`
  provenance headers, does not fork
- `src/custos/**` — no code changes
- `packages/**` — no changes
- `Makefile` — no changes (docs-site has its own `docs-site/Makefile` if needed)
- `pyproject.toml` — no changes

## Verification (Docker-runtime-grade evidence, per lesson #24)

Not merely tsc / lint / build — real runtime evidence:

- **T2-T4 acceptance**: `cd docs-site && npm ci && npm run build` produces `build/`
  directory with all 45 chapter routes rendered (including empty stubs)
- **T5 acceptance**: `curl` renders `docs-site/build/docs/01-introduction/what-is-custos/`
  and content matches `docs/domain.md` verbatim (provenance grep verify:
  `grep 'source: docs/domain.md' docs-site/build/**/*.html`)
- **T6 acceptance**: `docs-site/build/zh-Hans/docs/01-introduction/…` exists;
  language toggle in navbar switches between locales at runtime
- **T7 acceptance**: `docs-site/build/index.html` includes ARX section with
  canonical positioning phrase (`the neutral quant operating system`); grep
  verifies no forbidden alias from naming-authority.md is present
- **T8 acceptance**: mast-strip visible in header on all site pages; ARX CTA
  points to `mailto:contact@alephain.com?subject=ARX%20private%20beta`
- **T9 acceptance**: GitHub Action runs green on a test commit; `gh-pages`
  branch updated; GitHub Pages URL returns HTTP 200
- **T10 acceptance**: after operator adds DNS record, `dig custos.alephain.com`
  returns CNAME to `the-alephain-guild.github.io`; `curl -I https://custos.alephain.com`
  returns 200 + valid TLS cert
- **T11 acceptance**: `docs-site/versioned_docs/version-0.3.0/` exists;
  version dropdown in navbar shows `0.3.0` as current
- **T12 acceptance**: plan Status ← ✅; `.forge/README.md` updated; README.md
  root has documentation link

## Success criteria (business acceptance)

1. External auditor visits `custos.alephain.com`, reads Part V "Non-Custodial
   Trust Model" red-line pages, can trace each red line to code anchor without
   cloning repo — signals discovery layer works
2. Chinese-language operator uses the language toggle, reads translated Part IV
   Operator Guide, deploys custos successfully — signals i18n works for the
   subset already translated
3. Ecosystem landings (`alephain.com` / `alchymia.alephain.com` /
   `tesseract.alephain.com`) `custos · open source ↗` CTAs no longer point to
   GitHub as the primary target for depth content — instead route to
   `custos.alephain.com` for docs (GitHub remains linked but for source review
   only)
4. `docs-site/build/` size < 20 MB (Docusaurus static output; if exceeds, audit
   image assets)
5. Lighthouse score on homepage ≥ 90 for Performance, Accessibility, Best
   Practices, SEO

## 偏离与改进日志

### DEVIATION-08: 仓库迁移到 the-alephain-guild org (CEO 2026-07-28 执行)

- **等级**: 中 — 改变发布身份与 Pages 服务源, 但不触及红线
- **原因**: `the-alephain-guild/custos` 是 custos 的 canonical 归属。D1 起草时写
  `alchymia-labs`, CEO 2026-07-28 在 GitHub 完成 org 迁移。
- **影响**:
  1. **Pages 服务源变更** — CNAME 目标从 `alchymia-labs.github.io` 变为
     `the-alephain-guild.github.io`。**Cloudflare 记录不更新则 `custos.alephain.com`
     解析到旧 org 会 404**。D2 / T10 runbook / T2 验收项已同步更正。
  2. **发布身份变更** — `toolkit_rc` 契约用单值 `Literal` pin `source_repository`
     与 `workflow_identity`, 无兼容别名 (符合 first-production V1 in-place 规则)。
     新 org 下的 workflow 若不改常量, 会在自身 repository guard 处失败。
  3. **已发布 `0.1.0rc5` 失效** — 其 Sigstore 证书 subject 绑定旧 org 的 workflow
     identity, 无法通过新契约验证。`docs/authority/` 下的 receipt 与 golden **有意
     不改** (证书是外部不可变事实, 改 JSON 不改变证书), 需在新身份下重发 RC。
- **决定**: 只改活引用 (git remote / workflows / scripts / packages / 测试正例),
  重新生成两份 source-generated schema, 负例换成旧坐标以继续断言异身份被拒。
- **验证**: `make test-baseline` 644 passed — 与迁移前基线逐项一致
  (既有 2 failed 未变多也未变少)
- **遗留**: RC 需重发; `docs/gateway-contract/v1/toolkit_rc_t6d_pending_receipt_v1.schema.json`
  经 grep 确认无任何代码/测试/manifest 引用, 疑似孤儿文件 (且文件名含内部 task 编号),
  未擅自删除, 待确认

### DEVIATION-07: T5 verbatim migration 把内部文档发布上线 (2026-07-27 审计抓)

- **等级**: **高** — 触及 `mandatory-rules.md` §9 对外产出物纪律, 且已实际发布
- **原因**: T5 的迁移模式定为 "content copied verbatim, `docs/**.md` 保持 SSOT"。
  但 `docs/**.md` 是**内部设计与规划文档**, 其写作目的是在内部系统之间划分职责。
  逐字搬运把这层内容整体带上了公网。素材源与受众错配 —— 内容属实, 但不该对这个受众说。
- **影响**: 406 处内部标识跨 45 章 (含 en + zh)。gh-pages `a60bd8b` (2026-07-21)
  已发布 96 页, 其中 9 页命中。不是"尚未上线的侥幸", 是**已发生的对外泄漏**。
- **决定**:
  1. 建 `docs-site/scripts/check-disclosure.mjs` + 19 条回归测试, 扫全文含代码块与
     HTML 注释, 置于 CI 构建之前 (泄漏不可逆, 构建失败可逆)
  2. 三章无"清洗后保留"版本, 整章重写: engine roadmap / G6 host gate /
     configuration reference (后者标题是配置参考、内容是贡献者指南)
  3. 上游引用统一归一为 "the control plane"
  4. 顺带修一个真缺陷: vault 路径停留在 0.2.0 前的命名空间
  5. **更正**: 本轮曾把 clone URL 由 `the-alephain-guild/custos` 改为 `alchymia-labs/custos`,
     依据是前者当时不可达。判断错误 —— 那是迁移目标而非笔误。CEO 2026-07-28 完成 org
     迁移后已全部改回, 见 DEVIATION-08
  6. 修 T5 迁移遗留的 46 处 broken link; 修 typecheck 配置
  7. 纪律写进 `docs-site/README.md` 动笔处
- **验证**: `npm run verify` 全绿 — 19/19 gate 自测 / 94 文件 0 违规 / en+zh build
  成功 / 0 broken link / typecheck 通过
- **遗留**: 已发布的 gh-pages 快照仍是旧内容, 需重新部署覆盖 (下次 push 触发);
  镜像坐标 `ghcr.io/the-alephain-guild/custos` 两个 namespace 均 403, 尚未公开发布,
  文档承诺了用户当前无法执行的 pull
- **教训**: 见 `.claude/rules/historical-lessons.md` C5

### DEVIATION-01: doc-id 引用漏剥 `NN-` 前缀 (Session 1 verify 抓)

- **等级**: 低(单 session 内发现即修,不影响生产)
- **原因**: T3 authoring 时假设 Docusaurus 3 保留目录名 `NN-` 前缀作为 doc-id;实际 Docusaurus 3 默认 slugify **剥前缀**,使 `01-introduction/what-is-custos` 等 46 处 sidebar 引用 + 4 处 config footer link + 3 处 index.tsx CTA 全部与真实 doc-id 不匹配 → build 报 "Available document ids are: introduction/…"
- **影响**: 3 文件(sidebars.js/docusaurus.config.js/src/pages/index.tsx)共 54 行
- **决定**: 剥前缀 → doc-id 无 `NN-`,目录名保留(供磁盘排序 + URL slug 自动派生也无前缀)
- **发现渠道**: Session 1 verify — `npm run start` 报 sidebar checkSidebarsDocIds 失败,列出 46 available id 无前缀
- **修复 commit**: `11f4e42`

### DEVIATION-02: MDX v3 花括号被当 JSX 表达式 (Session 1 verify 抓)

- **等级**: 低(单文件单行,build error 阻断即修)
- **原因**: MDX v3 把 `{expr}` 解析为 JSX 表达式;`docs/09-reference/cli.md` stub TODO 里 `arx-runner enroll / vault {put,verify,list} / …` 被当作 tuple → `ReferenceError: put is not defined` during SSG(仅 `npm run build` 触发,dev-mode 未暴露)
- **影响**: `docs/09-reference/cli.md` 1 行
- **决定**: 用反引号包 code,MDX 遇到 `` `…` `` 不解析花括号;46 章 stub grep 未发现其它 `{...}` 命中
- **发现渠道**: Session 1 verify — `npm run build` SSG 失败于 `/reference/cli`
- **修复 commit**: `11f4e42`

### DEVIATION-03: Session 1 close-out A-scaffold(zh-Hans locale)

- **等级**: 低(计划范围扩展,不改变技术契约;未占用后续 Session 的 Task 名额)
- **原因**: Plan 20 原设计 T4 只 scaffold 英文 stub;zh-Hans i18n 目录只留空 README。Session 1 verify 时 Chrome 测 locale switcher 发现 `/zh-Hans/**` 全 404(dev-mode 单 host 单 locale 限制放大了空态),UX 洞明显。CEO 选 A 方案「最小 zh scaffold 避免 404」
- **影响**:
  - `i18n/zh-Hans/docusaurus-theme-classic/{navbar,footer}.json` — navbar 4 keys + footer 16 keys 手动中文翻译
  - `i18n/zh-Hans/docusaurus-plugin-content-docs/current.json` — 11 sidebar 分类 label 中文翻译(I·概览 … X·发布与治理)
  - `i18n/zh-Hans/code.json` — 82 Docusaurus 3 内置 UI 短语(已自带简体中文包,`write-translations` 自动填充)
  - `i18n/zh-Hans/docusaurus-plugin-content-docs/current/{01-10}-…/*.md` — 46 zh chapter stubs(cp en 内容 + 顶部注入 `:::warning 🔄 中文翻译进行中 · PLAN 20 T6` banner)
- **决定**: A 方案作为 Session 1 close-out 补丁,不占用 T6 名额;T6 正式内容翻译仍待 Session 3
- **发现渠道**: Session 1 verify — Chrome locale switcher 端到端测试
- **相关 commit**: Session 1 close-out commit(见 git log)

### DEVIATION-05: T9 提前到 Session 2 tail(Session 5 → Session 2)

- **等级**: 低(纯 scope reorder,不改变 T9 契约)
- **原因**: CEO 在 Session 2 close-out 前决定先配 CI,不等 Session 5。理由:push Session 2 T5 22 章内容到 origin/main 后,CI 直接 auto-deploy 可以立刻在 `custos.alephain.com` 上线,不需要手动 `npx gh-pages`;后续 Session 3-4 每次 push 自动更新;GitHub Pages settings + DNS(D2 CEO 准备)可以并行准备
- **影响**: 单文件新增 `.github/workflows/docs-deploy.yml`(peaceiris/actions-gh-pages@v4;concurrency guard;PR builds compile-verify but do not publish)
- **决定**: T9 完成 · Session 5 少 1 个 task
- **对 Session 5 影响**: 剩 T10 (DNS runbook) + T11 (versioning 0.3.0) + T12 (顶级 close-out)
- **相关 commit**: Session 2 tail T9 commit(见 git log)

### DEVIATION-04: MDX v3 verbatim-migration 兼容性 (Session 2 verify 抓)

- **等级**: 低(4 处 pattern,build error 阻断即修,内容语义未改)
- **原因**: `docs/**.md` SSOT 里的合法 Markdown 部分被 MDX v3 拒绝:
  - 缩进 code block(4-space 前缀)含 `<placeholder>`(如 `<tenant>` / `<deployment_instance_id>`)→ MDX 把 placeholder 当 unclosed JSX tag(reconcile-loop.md L15 + reference-implementations.md L16)
  - 版本约束 `Python >=3.12,<3.13` → MDX 把 `<3.13` 当 JSX 开标签(toolkit/overview.md L85)
  - Heading 里含 `<placeholder>`:`## 0.<prev>.x → 0.<next>.0` → MDX 解析为 JSX(upgrade-paths.md L112)
- **影响**: 4 文件本地修复(不改 SSOT `docs/**.md`,只改 migrated 副本)
- **决定**: 缩进 code block → fenced (```-fenced);inline 版本约束 → backtick 包 code;heading placeholder → backtick 包 code
- **发现渠道**: Session 2 verify — `npm run build` MDX SSG 失败
- **修复 commit**: `0b5a73a`(整合到 T5 migration commit,因为 fix 是在新迁移的内容上做的)
- **副作用**: 3 处 broken-link warning(non-blocking,不阻断 build)在 `configuration.md`(`../../.forge/README.md` + `../../.claude/rules/common-errors.md`)+ `trust-model.md`(`../domain.md`)—— 这些是 SSOT 原文里对 repo 内部路径的引用,site 上下文里不可达。**留待 Session 3 fix**(改成 site-internal link 或 remove reference),不阻断 T5 关闭。

### DEVIATION-06: Session 3 部分 broken-link 在翻译时顺手修 (zh 侧,en 侧待补)

- **等级**: 低(non-blocking warning,不影响 build)
- **原因**: DEVIATION-04 遗留的 broken-link warning 里,`trust-model.md` 和 `architecture-at-a-glance.md` 的 repo-internal `.md` 引用在 zh 翻译改写时被顺手改成 site-internal path(如 `../01-architecture.md` → `./architecture-at-a-glance`)。en 侧同源引用未同步修 → **zh 已修,en 未修的暂时不对称状态**
- **影响**: `configuration.md` 的 `.forge/README.md` 和 `.claude/rules/common-errors.md` 引用改成 GitHub raw URL 更 clean(SSOT 保留内部路径,site 侧改绝对 URL)—— 这个决策留 Session 4
- **决定**: Session 3 zh 侧的 broken-link fix 是 T6 翻译工作的自然副产品,不单独 commit;en 侧的对称 fix 与其他 en 迁移后 polish 一起做,推到 Session 4-5
- **相关 commit**: `1c594fd`(T6 partial · zh 侧修 broken-link 在同一 commit 内)

### DEVIATION-05: T9 提前到 Session 2 tail(Session 5 → Session 2)

## Non-Custodial Red Line Verification

- **红线 0.1 (Key/KEK never leaves process)**: docs site has no runtime access
  to keys; static generator only. ✅ N/A
- **红线 0.2 (G6 gate not bypassed)**: docs describe G6 semantics; no code
  change. ✅ N/A
- **红线 0.3 (Reconcile disconnect ≠ stop)**: docs describe policy; no code
  change. ✅ N/A
- **红线 0.4 (Decimal money math)**: docs describe convention; no code change. ✅ N/A

Documentation-only plan; no red-line risk.

## 完成报告 (Close-out Report)

*(顶级 close-out 填于 T12;各 Session 分段追加。)*

### Session 1 (T2 · T3 · T4 · zh-Hans A-scaffold) — 2026-07-19

- **完成 Task**: T2 ✅ · T3 ✅ · T4 ✅ · Session 1 close-out A-scaffold ✅
- **偏离数**: 3 (DEVIATION-01/02/03 详见「偏离与改进日志」)
- **验证结果**:
  - `npm run build` — 双 locale 均通过(en 872ms client · zh-Hans 6.5s client;仅 non-blocking `Cannot infer update date` warning,commit 后消失)
  - `npm run serve` (single-host build) — locale switcher 端到端双向切换、route 保留 ✅
  - Chrome 视觉验证 pass(截图见 verify 会话记录):
    - en homepage · paper 浅色 · brand accent(Newsreader 300 hero + gold em-dash + JetBrains Mono eyebrow)
    - en 章节 · 10 Part × 46 章 sidebar + gold-edge `:::info` admonition + Next 卡片
    - 深色模式 slate 主题 + theme toggle
    - zh homepage · navbar/footer 中文(文档/生态/代码 · ARX·内测中 · 隶属 The Alephain Guild 生态)
    - zh 章节 · warning `🔄 中文翻译进行中` + info `STUB` 双 admonition
    - locale switcher route-preserving 双向切换
- **交付 commit**(main branch,3 commit 分开原子提交):
  - commit 1(scaffold): `feat(custos): plan 20 T2-T4 — docusaurus 3.6 docs site scaffold`
  - commit 2(fix): `fix(custos): plan 20 verify — strip NN- prefixes from doc-ids + escape MDX braces`
  - commit 3(close-out): `feat(custos): plan 20 close-out — zh-Hans locale A-scaffold with 待翻译 banner`
- **遗留项**(按 Session 顺序):
  - Session 2:T5 verbatim migration(46 章从 `docs/**.md`,加 `<!-- source: docs/… -->` provenance)
  - Session 3:T6 zh 正式翻译(Part I-II 初版,替换 warning banner)
  - Session 4:T7 完整 homepage + Two Faces of ARX 叙事(D3 已定)+ T8 mast-strip 生态引流
  - Session 5:T9 GitHub Action `docs-deploy.yml`(D1 gh-pages branch)· T10 CNAME DNS runbook 交接(D2 CEO 准备 DNS)· T11 versioning 0.3.0 freeze · T12 顶级 close-out

### Session 2 (T5 — verbatim migration) — 2026-07-19

- **完成 Task**: T5 ✅(22 en 章节 verbatim 迁移 + 46 zh mirror sync)
- **偏离数**: 1(DEVIATION-04 — 4 处 MDX-hostile pattern + 3 处 broken-link warning 留待 Session 3)
- **验证结果**:
  - `npm run build` 双 locale 全绿(en + zh-Hans);post-fix 编译无 error
  - `npm run serve` production build,port 3000 单 host 双 locale
  - Chrome 视觉验证 4 章 pass(截图见 verify 会话记录):
    - `/introduction/what-is-custos` — domain.md §Bounded context 干净提取 · Next 卡片正常
    - `/concepts/reconcile-loop` — fenced code block `<placeholder>` 完美渲染 · 5 TOC sections · 9 步 numbered list
    - `/zh-Hans/introduction/what-is-custos` — zh warning banner + en real content + zh chrome 三段清晰
    - `/toolkit/overview` — Python 版本 backtick fix 生效 · 大量 inline code
- **交付 commit**(main branch,3 commit 分开原子提交):
  - commit 1(en migration + MDX fixes): `feat(custos): plan 20 T5 — migrate 22 chapters from docs/**.md to en site`
  - commit 2(zh sync): `feat(custos): plan 20 T5 — sync 46 zh chapter mirrors (T6 待翻译 banner 保留)`
  - commit 3(close-out): `feat(custos): plan 20 Session 2 close-out — T5 status ✅ + DEVIATION-04`
- **本 Session 数据**:
  - 22 en 章节 verbatim 迁移(其中 domain.md 拆 §Bounded context + §Core terms 到 2 章;engine-roadmap 从 4 engine files 合成 index)
  - 24 en 章节仍为 T4 stub(无 explicit SSOT source — Part II walkthroughs / Part IX reference tables / Part X license/security-policy inlines)
  - 46 zh 章节全量 sync(en 内容 + `:::warning 🔄 中文翻译进行中` banner)
  - 21 SSOT source 完整读取:20 全文 verbatim + 1 file(domain.md)2 section 提取
- **遗留项**:
  - Session 3:T6 zh 正式翻译(Part I-II 初版,替换 warning banner)+ Session 2 遗留的 3 处 broken-link warning 修
  - Session 4:T7 完整 homepage + Two Faces of ARX 叙事(D3 已定)+ T8 mast-strip 生态引流
  - Session 5:T10 CNAME DNS runbook 交接(D2 CEO 准备 DNS)· T11 versioning 0.3.0 freeze · T12 顶级 close-out(T9 提前完成,详见 DEVIATION-05)
  - 24 无 explicit source 的 en stub 章节(hold until Session 4-5 by content type)

### Session 2 tail — T9 前置(2026-07-19)

- **完成 Task**: T9 ✅(`.github/workflows/docs-deploy.yml` 单文件交付)
- **偏离数**: 1(DEVIATION-05 — T9 提前到 Session 2 而非 Session 5)
- **交付内容**:
  - `.github/workflows/docs-deploy.yml` — peaceiris/actions-gh-pages@v4 · Node 20 · docs-site/** paths trigger · concurrency guard · PR builds verify but don't publish · CNAME preservation
- **CI 生效条件**(user 需在 GitHub UI 完成):
  - `Settings → Pages → Source = Deploy from a branch → gh-pages / (root)` 或 `Source = GitHub Actions`(前者与 peaceiris workflow 匹配,推荐)
  - `Settings → Pages → Custom domain = custos.alephain.com`(+ Enforce HTTPS,cert 15-60 min)
  - DNS: CNAME `custos` → `the-alephain-guild.github.io`,Cloudflare proxy 关闭
- **首次 deploy**: push origin/main → workflow 触发 → `gh-pages` branch 自动生成 → GitHub Pages 拉起 → CNAME 生效后 `custos.alephain.com` 上线

### Session 3 (T6 partial — Part I-II first slice) — 2026-07-20

- **完成 Task**: T6 🟡 partial(4/46 chapters translated · Part I 3 章 + Part II enrollment 1 章)
- **偏离数**: 1(DEVIATION-06 — zh 侧顺手修 3 个 broken-link,en 侧同源待 Session 4-5 补对称 fix)
- **交付内容**:
  - `01-introduction/what-is-custos` → **什么是 custos**(22 行 · 全译 · warning banner 移除)
  - `01-introduction/trust-model` → **信任模型**(81 行 · 已中文 polish · English section titles → Chinese · repo-internal links → site-internal)
  - `01-introduction/architecture-at-a-glance` → **架构一览**(135 行 · 已中文 polish · BC 表 cross-reference 改指真实 site path)
  - `02-getting-started/enrollment` → **注册 (Enrollment)**(132 行 · 全译 · CLI/paths/struct names 保英文)
- **术语字典 (session-canonical)**:non-custodial → 非托管 · reconcile → 对账 · trust boundary → 信任边界 · red lines → 红线 · circuit breaker → 熔断器 · watchdog → 看门狗 · control plane / data plane → 控制面 / 数据面 · machine credential → 机器凭据 · proof of possession (PoP) → 所有权证明 (PoP) · fail closed → 失败关闭 · rotation / revocation → 轮换 / 撤销 · long-term support (LTS) → 长期支持
- **保留英文 (technical identifiers)**:custos · ARX · Crucible · NautilusTrader (NT) · DeploymentSpec · RunnerFact · EnrollmentToken · MessageBus · JetStream · sops · age · Ed25519 · KEK · 所有 `arx-runner` subcommand · 所有 file paths / env vars / API endpoints
- **验证结果**:
  - `npm run build` 双 locale 全绿
  - `npm run serve` 本地 200 for all translated routes
  - Chrome 视觉验证 2 章 pass(截图见 verify 会话记录):
    - `/zh-Hans/introduction/what-is-custos` — 5 中文 bullet · warning banner 移除 · 下一页"信任模型 »"
    - `/zh-Hans/getting-started/enrollment` — 标题双语(中 + 英括号)· 6 步骤 · fenced code + `~/.arx/*` inline code · sidebar 混合(1 中 + 3 英未译反映进度)
- **交付 commit**(main branch,2 commit + 1 push 触发 CI):
  - commit 1(4 zh translations): `feat(custos): plan 20 T6 partial — translate Part I 3 chapters + Part II enrollment to zh-Hans` (`1c594fd`)
  - commit 2(plan close-out): `feat(custos): plan 20 Session 3 close-out — T6 partial + terminology dict + DEVIATION-06`
- **Session 3 数据**:
  - 4 章 translated / 46 章 total = **9% 翻译率**(第一片切片)
  - 42 章仍带 warning banner(**未翻译**);其中 24 章英文本身也是 T4 stub 尚未写(需要 en 内容先行,如 Part IX reference 表、Part X license inline)
  - 3 处 broken-link warning(zh 侧修完,en 侧待 Session 4-5 补对称)
- **T6 剩余切片规划**(未来 sessions):
  - Slice 2:Part III 核心概念 5 章 · Part IV 运维指南 6 章(共 11 章 有 en real content)
  - Slice 3:Part V 信任模型 5 章 · Part VI 集成指南 5 章(共 10 章)
  - Slice 4:Part VII 引擎 3 章 · Part VIII 工具包 3 章(共 6 章)
  - Slice 5:Part IX + X 剩余 · 24 en stubs 转 en real 之后再补 zh 翻译
- **CI 状态**: push 后 GitHub Actions docs-deploy workflow 会自动 rebuild + push gh-pages → 用户在 `custos.alephain.com/zh-Hans/introduction/what-is-custos` 等路由看到中文翻译

### 顶级 Close-out(填于 T12)

- **完成日期**: TBD
- **总 Task 数**: 12
- **偏离总数**: TBD (Session 1-3 至今 5)
- **验证结果**: TBD (per T2-T12 acceptance)
- **遗留项**: expected — Chinese translation for remaining 42 chapters spans multiple future sessions (T6 slice 2-5)
