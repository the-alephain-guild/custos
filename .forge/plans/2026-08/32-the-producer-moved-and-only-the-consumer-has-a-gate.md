# 32 — producer 改了 golden，而只有 consumer 那侧有门，且那道门 CI 看不见

## Approved single-direction producer receipt correction (2026-08-08)

This section is normative over the open A/B/C choice and task wording below.
The approved solution is A+C, implemented through the producer receipt that
already exists. The symmetric producer-side sibling proposal in B is rejected:
Crucible must not depend on, inspect or pin Custos.

1. Crucible extends the existing
   `CRUCIBLE-RUNNER-COMMAND-PUBLICATION-V1` receipt in place so it binds the
   canonical command golden and sidecar by exact path, digest and byte length.
2. Crucible's local authority gate proves the receipt matches its own producer
   assets. It remains independently verifiable and has no consumer checkout
   dependency.
3. Custos vendors that exact producer receipt plus the current golden and
   sidecar, and records the producer receipt publication commit separately to
   avoid a circular self-pin in the producer receipt.
4. Custos's generated asset index and consumer receipt bind the vendored
   producer receipt and every producer asset. Independent-clone CI therefore
   verifies immutable producer evidence instead of silently skipping a missing
   sibling repository.
5. An optional workspace comparison may diagnose that a newer producer receipt
   is available, but it is supplemental freshness evidence, not the consumer's
   authority root and not a producer gate.
6. The current `cooldown_seconds` bytes must parse through the real signature
   verifier and risk-policy digest validation. Mutation tests must prove that a
   stale receipt, changed golden or incorrect digest makes the gate fail.

No new command version, parser, dataclass, schema, golden or authority entry is
created. The current technically correct shape remains the sole first-production
V1. Completion of this Plan proves contract consumption only; runtime and
production readiness remain governed by their existing receipts.

## Completion checkpoint (2026-08-08)

- Crucible corrected the complete DeploymentSpec digest and exact-event command
  fingerprint chain at contract commit
  `8c2c4eff20ae1ba38bbab54cdf7844ef25e1187d`, then published the existing
  V1 receipt at `e7a2bb97b17a887b081f60b1a9ae3620d3a592d0`.
- Custos vendors that receipt and its exact golden/sidecar, records the two
  commits separately, and includes the producer receipt plus the cross-language
  fingerprint vector in its generated consumer asset index.
- The five command-consumer targets passed 52 tests with the opt-in real-NATS
  case skipped. Standalone `make check-authority`, targeted Ruff format/lint
  and the independent receipt-mutation gate passed.
- The repository-wide `make verify` reaches the global format gate and then
  stops on three pre-existing, untouched files:
  `src/custos/core/runner_fact.py`,
  `tests/integration/runner_fact_publication_process.py` and
  `tests/integration/runner_nats_transport_service_consumer.py`. This Plan
  neither changes nor claims those unrelated bytes.
- Runtime and production readiness remain false as before. This checkpoint
  closes the exact producer-to-consumer command contract only.

> **Status**: ✅ Completed — one-way producer receipt handoff and independent-clone gate verified
> **Created**: 2026-08-04
> **Project**: custos (`tesseract-trading/custos/`)
> **Depends on**: 无 —— 现有代码即可复现
> **Blocks**: workspace checkout 下的 `make verify`（`check-authority` 恒红）；以及「custos 能接住
> producer 当前真实字节」这件事目前**没有证据**
> **For Claude**: `/forge:execute`；**先定 §决策 的层级再动手**
> **multi_session_scope**: false

## 观察到的事实

`make verify` 在 workspace checkout 下恒红，两道门同时红，都指向同一件事：

```
authority gate failed:
  - runner command golden differs from optional sibling:
    .../crucible-rust/docs/authority/runner-deployment-command-golden-v1.json
```

```
FAILED tests/test_runner_deployment_command_golden.py::
       test_golden_hash_matches_snapshot_sidecar_and_optional_sibling
```

## Foundation Scan（2026-08-04 实测）

### 差的不是空白，是一个字段

| | custos | crucible-rust |
|---|---|---|
| 字节 | 18481 | 18598 |
| sha256 | `8660b25e…` | `a52851e0…` |

`diff` 27 行，全部同型：`risk_policy` 对象里多了 `cooldown_seconds: 300`（3 处），
`risk_policy_digest` 随之由 `96d56dad…` 变为 `b8f16990…`（3 处）。

### 漂移只有一个来源，且只有一天

| 时间 | custos | crucible-rust |
|---|---|---|
| 2026-07-23 | `b0655ef` | `5778397` |
| 2026-08-03 | —— | `2a9e1b9 docs(authority): publish policy-owned cooldown v1` |

**实证**：把 producer 回退到 `5778397` 那一版取 sha256，与 custos 当前文件**逐字节相同**
（`8660b25e…` = `8660b25e…`）。所以唯一漂移源就是 `2a9e1b9`，不是长期积累的腐烂。

`2a9e1b9` 在它自己那侧是**完整自洽**的一次改动：schema / receipts / goldens / manifest /
它自己的 gate 脚本 / sidecar 一起改，sidecar 与新字节一致（实测两侧 sha 相同）。
问题不在 producer 改得潦草，在于没有任何东西要求 consumer 跟上。

### 运行时不会被拒——这一点必须写清楚，否则会被高估

custos 把 `risk_policy` 当**不透明字节**处理。`src/custos/contracts/deployment.py:344` 的
`validate_risk_policy_snapshot` docstring 原文是
"Verify the opaque control-plane-owned policy bytes without interpreting them"，它只做两件事：
要求 `risk_policy` 是非空 dict，然后对其 canonical JSON 重算 sha256 与 `risk_policy_digest` 比对。
`max_single_strategy_allocation_ratio` 在 `src/` 里 **0 命中**——custos 根本不读那些字段。

producer 是把 body 与 digest **一起**更新的，所以一条真带 cooldown 的指令**会验过**。

> **一处容易读错的地方，先钉住**：`RunnerAggregateCapPolicyV1`
> （`src/custos/contracts/crucible_runner_safety_policy.py:128`）确实是
> `extra="forbid"`，但**长 cooldown 的不是它**——它的字段是
> `max_order_notional` / `max_total_notional` / `exposure_model` / `breach_action` …，
> 与 golden 里那个对象（`max_total_exposure` / `max_total_drawdown_ratio` /
> `max_daily_loss` / …）是两个东西。起草本 plan 时第一遍就读错成它，靠 grep 字段名纠回来。
> 下一个读者不必再走一遍这条弯路。

### 所以真正坏掉的是三件事

1. **custos 的 pin 是陈的，而且四处自洽地陈**：golden 本体、`.sha256` sidecar、
   `docs/authority/ecosystem-authority.json` 的 `runner_command_golden_fixture.sha256`、
   `docs/authority/crucible-runner-command-consumer-assets-v1.json`（sha + `size_bytes: 18481`）
   四者互相印证——印证的是 producer 已经不再产出的字节。这正是 C7 那个形状：
   自洽不等于对。
2. **「custos 接得住当前字节」没有证据**。消费 golden 的四个测试
   （`test_runner_material_authority.py` / `test_command_intake.py` /
   `test_runner_fact_store.py` / `tests/integration/test_nats_revocation.py`）跑的都是旧字节。
   它们证明的是 custos 接得住一个**已经不存在**的形状。
3. **有门的一方看不见，看得见的一方没有门**：
   - custos 侧有两道（pytest + `scripts/check-authority-docs.py:2377-2382`），但两者都挂在
     `optional_sibling_path` 上——**要两个仓库同时 checkout 才会触发**。独立 clone 的 CI 永远绿。
   - crucible-rust 侧 `scripts/check-authority-docs.py` **grep `sibling` 0 命中**，
     完全没有反向比对。能改的一方没有门。

### 改动的连带面（executor 必须先知道）

`docs/authority/crucible-runner-command-consumer-assets-v1.json` 按 sha + `size_bytes`
pin 住了 **`tests/test_runner_deployment_command_golden.py` 本身**
（`249d6570…` / 5993 字节）与 `src/custos/contracts/crucible_runner_command.py`。
**改那个测试文件会同时打破资产 pin**——这是 C6 的形状。索引由
`scripts/generate_strategy_contract_assets.py` 生成，`make check-authority` 里以 `--check` 校验。

另有一处结构性缺口，值得在决策时一并看：`crucible-runner-safety-policy-consumer-assets-v1.json`
带 `producer_receipt_commit` + vendored receipt 文件，而**command 这份不带**
（`docs/authority/vendor/` 下只有 safety-policy 一份 receipt）。也就是说 command 契约的
consumer pin 没有 receipt 握手，唯一把两侧绑在一起的就是那条 optional 文件系统比对。

## 决策 (Decision) —— 先定层级再动手

**方向没有疑问**：按 `.claude/rules/authority-docs.md`，crucible-rust 是 runner-command
schema/golden 的唯一 producer，custos 是按精确字节 pin 的 consumer。所以 custos 跟进，不是
反过来。有疑问的是**跟进之外还做多少**：

| # | 做法 | 换来什么 | 代价 |
|---|---|---|---|
| A | 只重新 pin：更新 golden + sidecar + 两份 authority 索引，补一条「当前字节能被接受」的测试 | 门变绿，且第一次有了「接得住 producer 现字节」的证据 | 下一次 producer 再动，同一件事原样重演——**本 plan 不改变复发概率** |
| B | A + 让盲区在 CI 里可见：把 sibling 比对从「有就比」升级为在 workspace 下必比，并在 producer 侧加对称门 | 漂移在产生它的那一侧当场被拦 | 要改 crucible-rust（跨仓，另一个 `.git`）；且要想清楚独立 clone 下这条门该表达成什么 |
| C | A + 给 command 契约补 receipt 握手（对齐 safety-policy 那份的形态） | 两侧由一份 producer 签发的声明绑定，而不是靠"两个仓库恰好都在磁盘上" | 最重；要 producer 配合发 receipt，属协议动作 |

**倾向 A + B。** 理由：A 是必须做的（否则 `make verify` 恒红、且证据缺口留着）；
B 针对的是**为什么没人发现**，而 A 只针对**这一次的差异**。只做 A 等于修好这一次、
把下一次留给下一个人在 workspace 里偶然撞见。

C 更彻底，但它是协议动作，需要 producer 侧排期；且在 B 已经能当场拦住的前提下，
它的边际收益要单独评估——**不建议裹在本 plan 里**。

**B 有一处需要一并想清楚**：独立 clone 没有 sibling，这条门在那里表达成什么？
「文件不存在就跳过」正是今天 CI 看不见的原因；「文件不存在就失败」会让独立 clone 无法验证。
可能的第三种答案是把 producer 的当前 sha 写进 custos 的 manifest 当作**声明**，
这样独立 clone 也能校验一个数字，而不是校验另一个仓库是否在磁盘上——这条留给决策时定。

## Tasks

> 任务按 A + B 写；若决策落在别处，实施前按 §决策 重排。

### Task 1 — 先写会红的测试：证明 custos 接得住 producer 的当前字节

在重新 pin **之前**写。它此刻必须是红的，否则证明不了缺口存在。
断言 producer 当前 golden 里的 `risk_policy` 能过 `validate_risk_policy_snapshot`
（body 与 `risk_policy_digest` 一致），而不是只断言两个文件字节相同。

### Task 2 — 重新 pin（A）

更新 golden 本体 + `.sha256` sidecar + `ecosystem-authority.json` 的
`runner_command_golden_fixture.sha256` + `crucible-runner-command-consumer-assets-v1.json`
（sha 与 `size_bytes` 都要，18481 → 新值），并按需重跑
`scripts/generate_strategy_contract_assets.py`。

**注意连带面**：若 Task 1 改动了被 pin 住的测试文件，资产索引必须同批重生成，
否则会打破 C6 那道 pin。

### Task 3 — 让消费 golden 的四个测试跑在新字节上

`test_runner_material_authority.py` / `test_command_intake.py` /
`test_runner_fact_store.py` / `tests/integration/test_nats_revocation.py`。
逐个确认它们读的是更新后的 golden，而不是各自另存的旧副本。

### Task 4 — 关掉盲区（B）

按 §决策 定下的形态实现：custos 侧把 sibling 比对升级为在 workspace 下必比（或写死声明 sha），
crucible-rust 侧加对称门。**跨仓改动分别 commit**（`mandatory-rules.md` §6）。

### Task 5 — 门自身要能红

新加的门必须**实跑证伪一次**：构造一次真实的漂移，确认它会红。
一条从未失败过的门不是门（C7）。

## 失败模式覆盖契约

| # | 场景 | 期望 |
|---|---|---|
| FM1 | producer 单方面改 golden | 有门在 producer 那一侧当场红 |
| FM2 | 独立 clone（无 sibling） | 门有明确且**被测过**的行为，不是静默跳过 |
| FM3 | `risk_policy` body 与 `risk_policy_digest` 不一致 | `validate_risk_policy_snapshot` 拒绝 |
| FM4 | 只更新 golden 而漏更 sidecar / 两份索引之一 | 现有 pin 检查红（回归保护，勿在本 plan 削弱） |
| FM5 | 改被 pin 住的测试文件而未重生成资产索引 | `make check-authority` 红 |

## 复现 (Reproduce)

两个仓库都在磁盘上时：

```bash
make check-authority          # authority gate failed: ... differs from optional sibling
uv run pytest tests/test_runner_deployment_command_golden.py -q
```

只 clone custos 时两者都绿——**这正是本 plan 要处理的那一半**。

## 范围之外（登记以防混淆）

- **不评价 cooldown 这个字段本身**。它是 producer 的领域决定，custos 是消费方。
- **不改 `risk_policy` 的不透明处理**。「不解释控制面拥有的策略字节」是现有设计，
  且正因如此这次才不是运行时故障。
- **不顺手做 C（receipt 握手）**，理由见 §决策。
