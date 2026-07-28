# docs/ → docs-site/ 合并对照表

> 起草 2026-07-28。待 CEO 确认后执行。
> 判据：custos 这层可展开实现细节（开源、鼓励审计）；ARX 那层只留契约描述。

## 结论摘要

| 动作 | 篇数 | 说明 |
|---|---|---|
| **FILL** 填补站点 stub | 8 | 站点章节存在但只有 12 行占位，`docs/` 有完整内容 |
| **DEEPEN** 深化站点已有章节 | 11 | 两边都有内容，`docs/` 的实现细节按新标准可以补回站点 |
| **ABSORBED** 已被站点取代 | 9 | 站点重写版已覆盖，原文可删 |
| **MOVE** 移入贡献者文档 | 2 | 属贡献流程，归 CONTRIBUTING 而非产品站 |
| **KEEP** 不动 | 2 类 | 机器可读契约 / 权威链资产，非散文 |
| 合计 | 27 篇散文 + 2 类资产 | 清理量 182 处 |

**篇数核对**（`find docs -name '*.md'` = 29）：

```
29 总数 − 2 KEEP = 27 待处理
27 = FILL 5 源文件 + DEEPEN 11 + ABSORBED 9 + MOVE 2   ✅
```

FILL 一栏列了 8 个目标章节，其中 3 章无 1:1 源文件（从对应 design 文档抽取），
故源文件只计 5 个。

---

## FILL — 填补站点 stub（8 篇）

站点章节已存在但只有占位，`docs/` 侧有完整内容。合并收益最高。

| docs/ | 行 | 违规 | → 站点章节 | 备注 |
|---|---|---|---|---|
| `ops/05-deployment.md` | 152 | 16 | `/operator-guide/deployment` | Docker/systemd 部署，站点当前 12 行 stub |
| `ops/runbook.md` | 144 | 11 | `/operator-guide/troubleshooting` + `/operator-guide/emergency-playbook` | 一篇拆两章，按「日常排障 / 紧急处置」切 |
| `design/nats_client.md` | 38 | 5 | `/reference/nats-subjects` | subject 命名规则，集成方需要 |
| `guides/04-testing.md` | 120 | 10 | `/trust-model/audit-checklist` | 测试分层即审计清单的骨架；剥离内部编号后可直接用 |
| `design/runner_safety_policy.md` | 56 | 3 | `/trust-model/safety-survives-disconnect` | 站点该章 52 行，policy 消费契约可补 |
| — | — | — | `/trust-model/keys-never-leave-the-host` | 从 `design/credential_vault.md` 抽保证部分 |
| — | — | — | `/trust-model/live-execution-is-gated` | 从 `design/nautilus_host.md` 抽 gate 部分 |
| — | — | — | `/trust-model/exact-money-arithmetic` | 从 `design/03-implementation.md` 抽 Decimal 部分 |

> 后三章无 1:1 源文件，需从对应 design 文档抽取。`/trust-model/red-lines` 索引页同步补齐四条链接。

## DEEPEN — 深化站点已有章节（11 篇）

两边都有实质内容。`docs/` 的源码锚点与实现机制按新标准**可以补回**站点，这正是审计者要读的。

| docs/ | 行 | 违规 | → 站点章节 | 补什么 |
|---|---|---|---|---|
| `design/01-architecture.md` | 127 | 13 | `/introduction/architecture-at-a-glance` | 信任边界的分层兑现 |
| `design/03-implementation.md` | 170 | 17 | `/reference/configuration` + `/concepts/*` | 技术栈、依赖、项目结构、运行方式 |
| `design/credential_vault.md` | 129 | 9 | `/operator-guide/credential-vault` | 三层 invariant 与其覆盖测试 |
| `design/enrollment.md` | 128 | 5 | `/getting-started/enrollment` | nonce-bound PoP 协议细节 |
| `design/nautilus_host.md` | 96 | 7 | `/engines/nautilus-trader` | 进程监督与状态机 |
| `design/reconcile.md` | 91 | 4 | `/concepts/reconcile-loop` | level-triggered 不变量 |
| `design/runner_fact.md` | 117 | 9 | `/concepts/runner-fact` | 13-kind union、签名前像 |
| `design/runtime_log_fact.md` | 75 | 2 | `/operator-guide/runtime-log-observability` | 脱敏规则 |
| `design/strategy-toolkit.md` | 125 | 11 | `/toolkit/overview` | 执行 ABI |
| `design/engine_protocol.md` | 90 | 3 | `/engines/engine-roadmap` | Protocol 分层契约 |
| `lts-commitment.md` | 94 | 3 | `/release-governance/semver-lts` | LTS 窗口表 |

## ABSORBED — 已被站点重写取代（9 篇）

站点版本已覆盖且按对外标准重写过，原文清理后无剩余价值，直接删。

| docs/ | 行 | 违规 | 被谁取代 |
|---|---|---|---|
| `design/00-overview.md` | 74 | 7 | `/introduction/architecture-at-a-glance` |
| `design/02-module-design.md` | 81 | 6 | `/introduction/trust-model` 的六模块表 |
| `engines/athanor.md` | 55 | 9 | `/engines/engine-roadmap`（Athanor 节已按纪律删除） |
| `engines/nt_rust.md` | 54 | 4 | `/engines/engine-roadmap` 的「原生 Rust 绑定」节 |
| `engines/nautilus.md` | 23 | 1 | `/engines/nautilus-trader` |
| `engines/hummingbot.md` | 48 | 0 | `/engines/engine-roadmap` 的 Hummingbot 节 |
| `engines/freqtrade.md` | 45 | 0 | `/engines/engine-roadmap` 的 Freqtrade 节 |
| `reproducible-build.md` | 89 | 1 | `/trust-model/signed-release-chain`(91 行) |
| `upgrade-path.md` | 23 | 2 | `/release-governance/upgrade-paths`(125 行，已远超原文) |

## MOVE — 归贡献者文档（2 篇）

面向改代码的人，不是产品文档。

| docs/ | 行 | 违规 | → |
|---|---|---|---|
| `guides/dev-guide.md` | 156 | 14 | `CONTRIBUTING.md`（已有 Local setup 段，合并去重） |
| `domain.md` | 68 | 3 | 拆：领域词汇 → `/introduction/what-is-custos`（站点仅 21 行）；边界声明 → `README.md` |

## KEEP — 不动（2 类）

不是给人读的散文，是机器消费的资产。

| 路径 | 理由 |
|---|---|
| `docs/authority/**`（20 JSON + 1 md） | 签名证据与权威链，`make check-authority` 消费 |
| `docs/gateway-contract/**` | JSON schema + README，代码与 manifest 直接消费 |



---

## 硬约束：authority-manifest.json

grep 实证 `authority-manifest.json` 登记了 **10 个 `.md`**：

```
docs/domain.md
docs/design/engine_protocol.md      docs/design/nats_client.md
docs/design/nautilus_host.md        docs/design/reconcile.md
docs/design/runner_fact.md          docs/design/runner_safety_policy.md
docs/design/strategy-toolkit.md
docs/gateway-contract/v1/README.md          (KEEP，不移动)
docs/authority/strategy-toolkit-provenance.md (KEEP，不移动)
```

即 **8 篇待移动的文件被登记为权威文档**。移动必须同步改 manifest，
并跑 `make check-authority` 确认权威链未断。

**两个选项**：

- **A**：manifest 指向新位置 `docs-site/docs/**`，`docs/` 完全消失
- **B**：manifest 登记的 8 篇留在 `docs/`，其余合并 —— 权威链零改动，但「一个权威」目标打折

## 执行顺序建议

1. FILL 8 章（收益最高，站点当前是空的）
2. DEEPEN 11 章（逐篇合并，每篇独立 commit 便于回退）
3. ABSORBED 9 篇删除
4. MOVE 2 篇
5. 更新 `authority-manifest.json` → `make check-authority`
6. `npm run verify` + `make test-baseline`

## 待确认

1. authority-manifest 走 A 还是 B？
2. `ops/runbook.md` 拆成 troubleshooting + emergency-playbook 两章，切分点是否合理？
3. 中文：站点 zh 侧目前仅 4 章真实翻译。合并进来的新内容是先只出英文（zh 挂待翻译提示），还是同步翻译？
