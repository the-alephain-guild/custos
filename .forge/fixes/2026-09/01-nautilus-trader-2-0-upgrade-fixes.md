# 01 - nautilus-trader-2-0-upgrade-fixes

> **Status**: 🔲 Not started
> **Created**: 2026-09-11
> **Project**: custos
> **Plan**: `.forge/plans/2026-09/01-nautilus-trader-2-0-upgrade.md`（commit `aa0d2a0`，Status 🔲 Not started）
> **Source**: `.forge/reviews/2026-09/01-nautilus-trader-2-0-upgrade-review.md`（`b0ff52a`）+ `.forge/reviews/2026-09/taste-plan-01-nautilus-upgrade-scope.md`（`708cd59`）
> **For Claude**: 修复对象是**计划文本**，不是代码。每个 Fix 的「测试」是对修订后 plan 的 grep 断言（新措辞存在、旧措辞消失），不是 pytest。

## 修复来源

- 计划文件: `.forge/plans/2026-09/01-nautilus-trader-2-0-upgrade.md`
- 审查报告: `.forge/reviews/2026-09/01-nautilus-trader-2-0-upgrade-review.md`（2 CRITICAL / 4 HIGH / 2 MEDIUM / 4 LOW）
- 品味报告: `.forge/reviews/2026-09/taste-plan-01-nautilus-upgrade-scope.md`（🟡 5 / 🟢 3 / ⬆️ 1）

## 分诊

| 来源 | 编号 | 优先级 | 根因类别 | 处置 |
|---|---|---|---|---|
| review | C1 D1 path 依赖代价低估 + 清单缺文件 | P0 | 计划错误 | Fix 1 |
| review | H1 D2「cp313 物理约束」不成立 | P1 | 计划错误（前提失实）| Fix 1（与 C1 同根）|
| review | H2 PS 侧获取 2.0.0rc5 缺失 | P1 | 计划错误 | Fix 1（与 C1 同根）|
| review | C2 `engine_version` 跨仓契约无影响分析 | P0 | 计划错误（mandatory-rules §3 缺影响分析）| Fix 2 |
| review | H4 D4 与 Task 8 冲突（红线 0.2）| P1 | 计划错误（自相矛盾）| Fix 3 |
| review | H3 Task 5 根类形态待定 | P1 | 计划错误（中风险决策推迟到实施期）| Fix 4 |
| review | M2 DAG E 依赖写错 | P2 | 计划错误 | Fix 4（与 H3 同根）|
| review | M1 mypy 基线比对键 / known-red | P2 | 计划错误（判据不可判定）| Fix 5 |
| review | L1–L4 | P3 | 计划错误（锚点/计数/惰性门）| Fix 6 |
| taste | 🟡×5 🟢×3 ⬆️×1 | P2 | 存量代码味道，落在本 plan 将改的文件里 | Fix 7：写进 plan 的「顺手收尾项」与 Task 7 约束，不在本 fix 改代码 |

## 根因分析（P0 / P1）

- **C1 / H1 / H2 同根**：三条都由「fork 以什么形态进入下游」这一个未定决策派生。plan 选了 path 源，于是把本机 `.so` 当约束（H1）、把代价写成只有单仓 clone（C1）、把 PS 的相反选择当注脚而非阻塞（H2）。owner 在审查会话中已倾向改为「fork 发布 wheel，两仓命令式安装」。本 fix 按此方向合并处置，解释器随之保持 3.12。
- **C2**：`engine_version` 出现在 canonical V1 契约实现、3 份 gateway-contract schema 与 Crucible 所有的 vendored golden 中，plan 未做 mandatory-rules §3 要求的跨子系统影响分析，验收项「残留归零」因此不可满足。根因是把版本号当成 custos 自有常量。
- **H4**：`_SUPPORTED_VENUES` 是单一集合、`supports_venue()` 不分 mode，plan 的 D4 正确指出「入集合即声明可跑 live」，Task 8 却仍把 SoDEX 入同一集合。根因是白名单的数据结构与 plan 想表达的「按 mode 的能力」不匹配。**本 fix 采用的假设**：本 plan 不交付 SoDEX live（与「目标」段、D5「只做能跑」、Task 8 Step 3 只验 sandbox/testnet 一致）；owner 若要交付 live，须另按高风险偏离审议。
- **H3 / M2**：根类形态是 deviation-protocol 中风险项（模型结构变更），plan 把它留到实施期，导致 Task 12 与 DAG 不一致。fork 示例 `examples/live/sodex/adaptive_martingale.py:207-244` 已给出可行形态，决策可以现在定。

## 修复任务 (Tasks)

### Fix 1: fork 以 wheel 进入下游；解释器保持 3.12 [P0，合并 C1 / H1 / H2]
**Root Cause**: 计划错误：D1 选 path 源且代价范围写错；D2 前提失实；PS 侧路径缺失
**Files**: plan 的 D1 / D2 行、契约证据表「fork 编译产物」行、File Inventory、Task 1、失败模式契约首行、偏离日志 D1 / D2 行、Task 14 遗留项
**Step 1（证伪）**: `grep -c 'path 依赖直指' plan` = 1；`grep -c 'cpython-313' plan` ≥ 1；`grep -c 'uv.lock' plan` = 0
**Step 2（修改）**:
- D1 → fork 侧 `maturin build --release --out ../dist`（fork `Makefile:337`）产 cp312 的 manylinux + macosx wheel，版本带 PEP 440 local label `2.0.0rc5+sodex.<short-sha>`；custos 与 PS 以**带 sha256 的 wheel 引用**消费（uv `url` 源按平台各一，或 `find-links` 指向 Release 资产页，形态在 Task 1 Step 1 实测后定）。验收判据：`uv.lock` 不含 path 源；`uv export --frozen` 产出的 `docker/runtime-requirements.lock` 带 sha256；3.11 base `uv sync --extra dev` 仍成功。前置依赖：fork 先发布 wheel。
- D2 → 撤回。解释器保持 3.12，`.python-version` / `tech-stack.md`「固定 3.12」/ `requires-python` 均不动；契约证据表该行改为「本机构建产物，`git ls-files` 未跟踪，非约束」。
- File Inventory：删 `.python-version`；`tech-stack.md` 描述改为「仅 NT 版本 pin」；加 `uv.lock`、`docker/runtime-requirements.lock`（`make check-runtime-lock` 重生成）、`Makefile`（`toolkit-dev` 同型的 fork wheel 安装 target）；Slice E 加 PS `pyproject.toml` / `uv.lock` / `Makefile`。
- Task 1 重写：Step 0 前置「fork 发布 wheel 并记录 sha256」；Step 4 失败模式改为 (a) 篡改 hash 后 `uv sync --frozen` 拒绝，(b) 3.11 base 无 nautilus extra 的 `uv sync` 成功，(c) `make check-runtime-lock` exit 0。
- 失败模式契约首行同步；偏离日志 D1 行改写、D2 行改为「撤回」；Task 14 遗留项改为「fork wheel 构建尚未进 fork CI，手工 `maturin build`，CI 化另起 follow-up」。
**Step 3（证实）**: `grep -c 'path 依赖直指' plan` = 0；`grep -c 'cpython-313' plan` = 0（改述为「未跟踪的本机构建产物」）；`grep -c 'uv.lock' plan` ≥ 2；`grep -c 'sodex\.' plan` ≥ 1
**Step 4**: 提交

### Fix 2: `engine_version` 列为跨仓契约变更 [P0，C2]
**Root Cause**: 计划错误：缺 mandatory-rules §3 影响分析；验收项不可满足
**Files**: plan 上下文段新增「跨仓影响分析」、契约证据表新增 4 行、Task 2 拆分、File Inventory、验证清单「残留归零」行
**Step 1（证伪）**: `grep -c 'strategy_execution.py' plan` = 0；`grep -c 'vendor/crucible' plan` = 0
**Step 2（修改）**:
- 契约证据表加：`strategy_execution.py:140,197 engine_version: Literal["1.230.0"]`；`docs/gateway-contract/v1/strategy_artifact_ref_v1.schema.json:130` / `strategy_manifest_v1.schema.json:94` / `..._receipt_v1.schema.json:207` 的 `"const": "1.230.0"`；`docs/authority/vendor/crucible-runner-strategy-release-resolution-v1.golden.json` 8 处（Crucible 所有）。
- 新增「跨仓影响分析（mandatory-rules §3）」小节：受影响方 PS（producer BOM）、Crucible（consumer receipt + vendored golden）；custos 自有 V1 in place 改动可做，对端重签为**阻塞项**，指名 owner。
- Task 2 拆为 2a（custos 自有：`toolkit_rc.py` + `strategy_execution.py` + 3 schema + 3 authority json + 3 scripts + 8 测试 + docs）与 2b（跨仓协调：PS BOM 重产、Crucible receipt 重签、vendored golden 由 Crucible 重新提供；本 plan 内标 ❌ Blocked 直到对端交付）。新值统一为 `2.0.0rc5+sodex.<short-sha>`。
- 验证清单「1.230.0 残留 grep 归零」改为「custos 自有文件归零；`docs/authority/vendor/**` 与 PS / Crucible 侧列为 blocked 并指名 owner」。
**Step 3（证实）**: `grep -c 'strategy_execution.py' plan` ≥ 1；`grep -c 'vendor/crucible' plan` ≥ 1；`grep -c '跨仓影响分析' plan` = 1
**Step 4**: 提交

### Fix 3: 白名单按 mode 拆分，SoDEX 本 plan 不进 live 集合 [P1，H4]
**Root Cause**: 计划错误：D4 与 Task 8 自相矛盾；白名单数据结构表达不了「按 mode 的能力」
**Files**: plan D4 行、Task 8、失败模式契约「SoDEX live 未过 G6」「非白名单 venue」两行、File Inventory drift-guard 行
**Step 1（证伪）**: `grep -c '新增.*SoDEX 两个 connector' plan` ≥ 1（旧措辞：不分 mode 新增）
**Step 2（修改）**:
- D4 → `_SUPPORTED_VENUES` 改为按 mode 的能力表（sandbox / testnet / live 三个集合），`supports_venue(venue, mode)`；SoDEX 本 plan 只进 sandbox 与 testnet；live 集合维持 `binance` / `binance_perpetual`。写明假设「本 plan 不交付 SoDEX live；要交付须另按高风险偏离审议」。
- Task 8 Step 2 去掉「比照 venue_binance 补齐 `_LIVE_MIN_APPROVERS` 与 owner evidence」（那是 live 交付的内容）；Step 4 红线测试改为 (a) SoDEX 请求 live 在 host gate 被拒，理由是不在 live 集合，(b) `NoopHost` 下 live 被拒，(c) Binance live 集合不因本次改动缩小。drift-guard 断言改为「三个集合分别等于各 mode 已接线 connector 的并集」。
- `host.py:211` / `:349` 两个调用点签名变更列入 Task 8 Files。
**Step 3（证实）**: `grep -c '按 mode' plan` ≥ 2；`grep -c '不交付 SoDEX live' plan` ≥ 1
**Step 4**: 提交

### Fix 4: 现在定根类形态；DAG 改为 E 依赖 A + Task 5 [P1，H3 + M2]
**Root Cause**: 计划错误：中风险模型结构决策推迟；DAG 与 Task 12 不一致
**Files**: plan D5 行、Task 5、Task 12 Step 2、Execution Contract `task_dag` 行、Slice E 标题、偏离日志 D5 行
**Step 1（证伪）**: `grep -c '待实施时定' plan` = 1；`grep -c 'E 依赖 A；' plan` = 1
**Step 2（修改）**:
- D5 → 根类 `NautilusTradingStrategyConfig(StrategyConfig)` 为普通子类（fork `config.rs:42` 带 `subclass`），kw-only `__init__` 接收 7 个 section（仍为 msgspec 子 Struct，`msgspec.structs.asdict` 消费点不变）+ `**_kwargs` 透传 `super().__init__()`（形态同 fork `examples/live/sodex/adaptive_martingale.py:207-244`）；冻结用 `__setattr__` 守卫在 `__init__` 完成后启用；`__eq__` 按 section 元组；不提供 `__hash__`（grep 无消费点）。PS 三个 Config 子类同形态，去 `frozen=True`。
- Task 5 Step 1 证伪改为「`class X(StrategyConfig, frozen=True)` 在 import 时抛 `TypeError`」；Step 4 加「`__init__` 后赋值须抛错（冻结守卫）」。
- `task_dag` → 「E 依赖 A + Task 5」；Slice E 标题同步；Task 12 Step 2 引用 D5 定稿形态。偏离日志 D5 行 ⏳ → ✅ 已定。
**Step 3（证实）**: `grep -c '待实施时定' plan` = 0；`grep -c '__setattr__' plan` ≥ 1；`grep -c 'E 依赖 A + Task 5' plan` ≥ 1
**Step 4**: 提交

### Fix 5: mypy 基线前置归零，判据改全绿 [P2，M1]
**Root Cause**: 计划错误：按行号比对的「同一批」在路径拍平后不可判定；known-red 未登记
**Files**: plan「基线」段、Slice A 新增 Task 0、Task 6 / Task 11 Step 4、验证清单 typecheck 行
**Step 1（证伪）**: `grep -c '非全绿' plan` ≥ 1
**Step 2（修改）**: Slice A 前加 Task 0「修 `orders.py:587,742,853` 与 `trade_event_handler.py:279` 四处 `arg-type`，`make toolkit-typecheck` 全绿并单独 commit」；基线段改述为「起草时 4 错误，由 Task 0 归零后 `make verify` 在主干转绿」，并点明 `check-authority` 的「READY_TYPING_CLOSURE」只覆盖 base toolkit 41 文件；Task 6 / 11 / 验证清单改为「全绿」。
**Step 3（证实）**: `grep -c '非全绿' plan` = 0；`grep -c 'Task 0' plan` ≥ 2
**Step 4**: 提交

### Fix 6: 锚点、计数、惰性门、as-of [P3，L1–L4]
**Files**: 契约证据表指标桥接行、Execution Owner `acceptance` 行、Task 2 Files、fork as-of 段
**Step 2（修改）**: 桥接行补 `:96-99 handle_bar`；acceptance 改为「Slice A：`make check-authority` exit 0；Slice B/C/D：该 Slice 失败模式测试 + `pytest --collect-only` 逐文件比对」；「10 个测试文件」→ 8 并附 grep 命令；fork as-of 更新为 `ae98c5c202` 并注明 `examples/live/sodex/paper_trading.py` 未跟踪、引用前须先在 fork 提交。
**Step 3（证实）**: `grep -c 'handle_bar' plan` ≥ 1；`grep -c 'ae98c5c202' plan` ≥ 1
**Step 4**: 提交

### Fix 7: 品味 findings 落为 plan 的「顺手收尾项」与 Task 7 约束 [P2]
**Root Cause**: 存量味道；本 fix 不改代码，只让 plan 明写「顺手」还是「不碰」
**Files**: plan 新增「品味收尾项」小节、Task 7 手法约束
**Step 2（修改）**:
- Task 7 加约束（⬆️）：`LiveNode` 重写前先把 spec 归一为 typed 视图、NT 节点能力收成一个 typed adapter，`deploy` / `_build_runner_fact_context` 改直取；判据「`lost its …` 类重复校验消失」；禁按行数拆。
- 顺手项表：Task 4 `macd.py` docstring 去重；Task 5 `trading_config.py:265,273` getattr 收口；Task 8 测试分隔线改 docstring；Task 9 `runner_safety.py:67-73,109-113` 静默默认链改 typed 读取器 + `modify_order` 补 structlog + `:391` 注释纳入实证清单；D5 `supertrend.py:203-215` 列名双机制二选一；`val`/`result` 命名不动。
**Step 3（证实）**: `grep -c '品味收尾项' plan` = 1
**Step 4**: 提交

## 验证清单 (Verification)

- [ ] Fix 1–7 每条的 Step 3 grep 断言全部成立
- [ ] plan 文件 `Status` 仍为 🔲（本 fix 不启动执行）
- [ ] `.forge/README.md:58` 索引行与修订后 plan 的 D1 / D2 表述一致
- [ ] 偏离日志无 ⏳ 残留；新增 DEV 条目登记本 fix 的两个假设（不交付 SoDEX live；解释器保持 3.12）
- [ ] `git status --short` 只含本 fix 触碰的文件

## 偏离与改进日志

| 类型 | 位置 | 描述 | 已批准 |
|---|---|---|---|
| DEV | 本 fix Step 5 | 修复对象是 markdown 计划文本，TDD 铁律不适用；修复由本会话直接实施而非派 `/forge:execute`，每条 Fix 的 Step 1/3 grep 断言替代 pytest | 自动（低风险，仅文档）|
| DEV | Fix 3 | 假设「本 plan 不交付 SoDEX live」，依据 plan 自述「只做能跑」与 Task 8 Step 3 范围；owner 可推翻 | ⏳ 待 owner 确认 |
| DEV | Fix 1 | 假设「解释器保持 3.12」，依据 fork `requires-python >=3.12` 与 wheel 方案；owner 若坚持 3.13 需另给理由 | ⏳ 待 owner 确认 |

## 进度追踪 (Progress)

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 | P0 | 🔲 | | C1+H1+H2 |
| 2 | P0 | 🔲 | | C2 |
| 3 | P1 | 🔲 | | H4 |
| 4 | P1 | 🔲 | | H3+M2 |
| 5 | P2 | 🔲 | | M1 |
| 6 | P3 | 🔲 | | L1–L4 |
| 7 | P2 | 🔲 | | taste |
