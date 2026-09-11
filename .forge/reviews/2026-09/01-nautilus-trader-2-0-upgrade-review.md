# 审计报告: 01 - NautilusTrader 1.230.0 → fork 2.0.0rc5 升级（plan 起草期审计）

> **审计日期**: 2026-09-11
> **计划文件**: `.forge/plans/2026-09/01-nautilus-trader-2-0-upgrade.md`（commit `aa0d2a0`，Status 🔲 Not started）
> **审计员**: Claude Code
> **审计性质**: 计划尚未执行，本报告审的是**计划文本与代码事实的一致性**、决策前提是否成立、验收判据是否可判定。没有实施 diff 可审，「行为审计 / 测试覆盖审计」只能审计划对它们的承诺。

## Executive Summary

计划的**锚点质量很高**：fork 侧 13 条、custos 侧 18 条「契约证据」我逐条 `sed -n` 实读，全部命中；fork HEAD 已由计划记录的 `cda7fd412e` 前进到 `ae98c5c202`，锚点在新 HEAD 上依然成立。mypy 基线实跑与计划一致（4 错误 / 2 文件 / exit 2），`make check-authority` 实跑 exit 0。

问题集中在**决策层与影响面**，不在锚点层：

1. **D2「3.13 是物理约束」的前提不成立**。那份 cp313 `.so` 不在 fork 的 git 跟踪之列，是本机 `maturin develop` 的产物；fork 自己声明 `requires-python = ">=3.12,<3.15"`。据此推出的 `.python-version`、`tech-stack.md`、`requires-python` 三处改动和 Task 1 Step 4 那条失败模式测试都建立在一个错误前提上。
2. **D1 的「已知代价」写窄了**。fork 的 build backend 是 maturin，path 依赖意味着每次 `uv sync` 现场编 Rust；而 PS 在 Plan 60 里已实测 uv 会读 lock 里每个 path source 的元数据、不管 extra 有没有被请求，这会连带击穿 custos 自己声明的「base 包在 Python 3.11 可装（audit / paper）」和整条 Docker 发布链（`--require-hashes` 的 runtime lock 无法容纳 path 源）。`uv.lock`、`Dockerfile`、`docker/runtime-requirements.lock` 都不在文件清单里。
3. **`engine_version` 是跨仓 V1 契约字段，计划没有影响分析**。它以 `Literal["1.230.0"]` 写在 canonical V1 契约实现里、以 `const` 写在 3 份 gateway-contract v1 schema 里、并出现在 Crucible 所有的 vendored golden 里 8 处。「1.230.0 残留 grep 归零」这条验收项，不动 Crucible 的文件就无法满足，而那份文件 custos 无权改。
4. **D4 与 Task 8 互相矛盾**。D4 论证「列入白名单即等于声明可跑 live」，Task 8 又把 SoDEX 列入同一个白名单，而计划自述「本次只做能跑」、Step 3 只验 sandbox/testnet。红线 0.2 的入口会被 Task 8 自己放宽。

用户点名的三处：D1 —— 代价被低估，见 C1；Task 5 frozen —— 方向可行但缺口比 frozen 大，见 H3；mypy 基线 —— 判据本身合理，但「同一批」按行号比对在本 plan 下必然失效，且更省事的做法是先修掉那 4 处，见 M1。

## 整体匹配率

| 维度 | 结果 |
|---|---|
| 契约证据锚点（31 条） | 31/31 实读命中 |
| 关键设计决策（D1–D6） | D3 / D5 / D6 成立；**D1 范围有误、D2 前提不成立、D4 与 Task 8 冲突** |
| 文件清单完整性 | 缺 6 类必然被触碰的文件（见 C1 / C2 / H2） |
| 验收判据可判定性 | 2 条不可判定或无效（见 M1 / L2） |

## 严重度分布

| 严重度 | 数量 |
|--------|------|
| 🔴 CRITICAL | 2 |
| 🟠 HIGH | 4 |
| 🟡 MEDIUM | 2 |
| 🔵 LOW | 4 |

## 问题列表

### 🔴 CRITICAL

#### C1: D1 path 依赖的代价被低估——它击穿的不只是「单仓 clone」，还有 3.11 base 安装、`verify-base-clean` 与整条 Docker 发布链；相关文件全部不在清单里

- **文件**: plan `D1` 行与「偏离与改进日志」首行；`pyproject.toml:29-32`；`Makefile:76-77,97,105,139`；`Dockerfile:12,25-45`；`docker/runtime-requirements.lock:175`
- **计划定义**: 「`[tool.uv.sources]` path 依赖直指 fork `python/`……已知代价：单仓 clone 装不起来」；Task 1 Step 3 期望「同一命令输出 `2.0.0rc5`」。
- **实际代码**:
  - fork `python/pyproject.toml` `[build-system]`：`requires = ["maturin==1.15.0", "patchelf"]`，`build-backend = "maturin"`。uv 对 path source 走 build backend 构建，即**每次 `uv sync` 都要 Rust toolchain 并完整编译**；本机已有的 `.so` 不会被 path 依赖复用。
  - PS `pyproject.toml:74-80`（commit `6dd552d` 落下的注释）实测记录了 uv 行为：「uv reads metadata for every path source in the lock **even when its extra is not requested** — so a checkout without a custos build next door cannot run `uv sync`, `uv run`, or anything built on them」。
  - custos 根 `pyproject.toml:29-32` 声明「package stays installable on Python 3.11 (audit / paper)」；`Makefile:76-77` `verify-base-clean` 跑 `uv sync --package custos-runner --extra dev`（不带 nautilus extra）。按上一条，这两处在 lock 含 path 源后都会失败。
  - `Dockerfile:12` 基础镜像 `python:3.12.13-slim`；`:25-45` 用 `--require-hashes` 装 `docker/runtime-requirements.lock`（`:175` `nautilus-trader==1.230.0`），该 lock 由 `Makefile:97` `uv export --frozen --extra nautilus` 生成。path 源没有 PyPI hash，`check-runtime-lock` / `dist` / `test-docker` / `verify-local-v030` 整链断裂；而且 darwin 的 `.so` 本来也进不了 Linux 镜像。
- **影响**: 计划承诺「Non-Custodial 4 红线逐条仍然咬得住」，但 red line 之外 custos 还有一条自足纪律（CLAUDE.md §8）和一条发布纪律（verification.md §Release artifact identity）。D1 只登记了前者的一半代价，后者完全没提。`uv.lock`、`Dockerfile`、`docker/runtime-requirements.lock`、`Makefile` 都不在文件清单，Slice A 的 close-out 会在这些文件上静默失败或被迫无记录地改动（审计 5 会判 CRITICAL）。
- **建议**: D1 改为「fork 侧 `make`（Makefile:337 `maturin build --release --out ../dist`）产 wheel → custos 以 `make toolkit-dev` 同型的命令式安装消费」，与 PS Plan 60 同向；lock 中不出现 path 源。若 owner 坚持 path 源，D1 必须补写：Rust toolchain 前置、3.11 base 安装作废、Docker 链作废的处置、以及上述 4 个文件的清单条目。

#### C2: `engine_version` 是跨仓 V1 契约字段，改它是 mandatory-rules §3 的跨子系统改动，计划无影响分析且验收项「残留归零」不可满足

- **文件**: `packages/custos-strategy-toolkit/src/custos_toolkit/contracts/strategy_execution.py:140,197`；`docs/gateway-contract/v1/strategy_artifact_ref_v1.schema.json:130`；`.../strategy_manifest_v1.schema.json:94`；`.../strategy_artifact_pre_import_verification_receipt_v1.schema.json:207`；`docs/authority/vendor/crucible-runner-strategy-release-resolution-v1.golden.json:36,95,114,132,148,150,158,203,234`；`scripts/toolkit_rc_build.py:177`；`scripts/toolkit_rc_release_readiness.py:755`；`scripts/generate_strategy_contract_assets.py:180`
- **计划定义**: Task 2 只列 `toolkit_rc.py` + 3 个 authority json + 10 个测试 + docs；验证清单要求「1.230.0 残留 grep 归零（docs-site 除历史记述外）」。
- **实际代码**: `engine_version: Literal["1.230.0"]` 写死在 authority-docs.md 点名的 canonical V1 契约实现（`strategy_execution.py`）；3 份 gateway-contract v1 schema 用 `"const": "1.230.0"`；Crucible 所有的 vendored golden 里 8 处内嵌在 canonical JSON 字符串中。authority-docs.md 明写「Never invent, vendor or pre-register downstream receipts」，CLAUDE.md §First-production V1 明写 custos「must not copy PS or Crucible owner schemas」。
- **影响**: 这是 PS（producer BOM）↔ custos（execution ABI）↔ Crucible（consumer receipt）三方 exact-byte 握手的字段。custos 单方面把 `const` 改成 `2.0.0rc5`，Phase A 那条「producer asset commit / consumer code / receipt」链就断了；而 vendored golden 由 Crucible 生成，custos 改它等于伪造 receipt。「残留归零」在不越权的前提下不可能达成。
- **建议**: Task 2 拆成两段——custos 自有 V1 in place 改动（`strategy_execution.py` / 3 schema / 3 scripts / 受影响测试），以及**明确标注为跨仓协调项**的 PS BOM + Crucible receipt 重签；验收项改为「custos 自有文件残留归零；vendored / 跨仓文件列为 blocked 并指名对端 owner」。

### 🟠 HIGH

#### H1: D2「fork 唯一编译产物是 cp313 .so，物理约束」不成立——那是本机构建产物，fork 支持 3.12；Task 1 Step 4 的失败模式测试断言的是一个不会发生的失败

- **文件**: plan `D2` 行、Task 1 Step 4、偏离日志第 2 行；fork `python/pyproject.toml:25`；fork `Makefile:327,337,404`
- **计划定义**: 「物理约束，非偏好：fork 唯一编译产物是 `_libnautilus.cpython-313-darwin.so`」；Task 1 Step 4「强制 3.12 解释器安装须 fail closed，断言错误信息指向 cp313」。
- **实际代码**: `git -C <fork> ls-files 'python/nautilus_trader/_libnautilus*'` 返回 **0 条**——`.so` 未被跟踪；fork `Makefile:327` `maturin develop --release` 在 fork 自己的 venv 解释器下生成它，`:404` 的 clean 目标会把它删掉。fork `requires-python = ">=3.12,<3.15"`。
- **影响**: 3.13 是「本机 venv 恰好是 3.13」的结果，不是 fork 的约束。以此为由改 `.python-version`、改 `tech-stack.md`「固定 3.12」、放宽 toolkit-nautilus `requires-python`，三处偏离的理由都是空的。Task 1 Step 4 在 path 源下的真实行为是：3.12 解释器会**成功编出 cp312 的 `.so`**（有 Rust toolchain时），测试断言的 fail closed 不会出现；没有 toolchain 时失败信息指向 cargo/maturin，也不指向 cp313。
- **建议**: 二选一并写明——(a) 保持 3.12，在 fork 侧以 3.12 构建，D2 整条删除，`tech-stack.md` 不动；(b) 确实要升 3.13，则理由改为「owner 选择」并单独论证（例如 fork CI 矩阵 / 未来 `<3.15` 上限），Task 1 Step 4 改为断言 wheel tag 与解释器不匹配时 `uv` 的真实拒绝信息。

#### H2: PS 侧如何拿到 2.0.0rc5 完全缺失，且 PS 自己的规则禁止 path 源

- **文件**: plan Slice E / 文件清单 PS 两行；PS `pyproject.toml:21,74-80`
- **计划定义**: Slice E 只列策略 8 文件与测试 25 文件；D1 引用 Plan 60「install the toolkit by command, so the lock stops naming a path」作为「同型问题那边选了相反方向」的对照。
- **实际代码**: PS `pyproject.toml:21` 直接钉 `"nautilus-trader==1.230.0; python_version >= '3.12'"`（PyPI 版本号，不经 custos toolkit 传递）；`:74` 「There is deliberately no [tool.uv.sources] here」。
- **影响**: Task 12 Step 3「4 个策略均可 import 且经 registry 解析」在 PS 仓需要 2.0.0rc5 可导入，而 PS 既不能 path 指 fork（自身规则），PyPI 上又没有这个版本。计划没有回答这个问题，也没把 PS `pyproject.toml` / `uv.lock` / `Makefile`（`toolkit-dev`）列入清单。这不是「PS 选了相反方向」的对照注脚，而是 Slice E 的前置阻塞。
- **建议**: 与 C1 一并决策：fork 产 wheel，两仓都以命令式安装消费；Slice E 文件清单补 PS `pyproject.toml` + `uv.lock` + `Makefile`。

#### H3: Task 5 缺口比「frozen=True 无效」大——根类失去的是整套 msgspec Struct 语义，而 D5/偏离日志把它写成一个待定项

- **文件**: `adapter/trading_config.py:64`；`adapter/signal_processor.py:21`；`adapter/trading_strategy.py:197`；`adapter/filter_manager.py:172,211,216`；`adapter/registry.py:326-329`；PS `trend/*/refinement/nautilus/strategy.py:179,91` 等；fork `crates/trading/src/strategy/config.rs:39-43`；fork `examples/live/sodex/adaptive_martingale.py:207-244`
- **计划定义**: Task 5 Step 2「改自写 `__init__(self, *, ..., **_kwargs)` + `super().__init__()`……`frozen=True` 对 pyclass 无效，冻结语义须另行实现或显式放弃并登记」；偏离日志「D5 `frozen=True` 语义可能丢失……⏳ 待实施时定」。
- **实际代码**: fork 的 `StrategyConfig` 带 `pyo3::pyclass(..., subclass, from_py_object)`（`config.rs:42`），子类化可行；fork 示例 `adaptive_martingale.py:207` `class AdaptiveMartingaleConfig(StrategyConfig)`、`:239` `**_kwargs: Any`、`:244` `super().__init__()` 正是 Task 5 要的形态。但 custos 现状 `NautilusTradingStrategyConfig(StrategyConfig, frozen=True)` 是 **msgspec Struct**：7 个带类型的 section 字段、kw-only 构造、结构化 `__eq__`/`__hash__`、`frozen`。下游 `registry.py:329` `config_factory(parameters=parameters, **base_sections)` 靠 Struct 的 kw 构造；`trading_strategy.py:197` / `filter_manager.py` 对 **子 section**（`RiskConfig` 等）调 `msgspec.structs.asdict`——子 section 可继续是 Struct，但根类的字段声明、校验、相等性、可哈希性都随基类切换消失。PS 三个策略的 Config 子类也都写 `frozen=True`。
- **影响**: 这是 deviation-protocol 里的「Pydantic/模型结构变更」中风险项，计划却把决策推到实施期，且 Task 12 又说 PS Config「随 Slice B Task 5 的根类形态调整」——执行者会在两个仓各自即兴决定。另外 Task 5 Step 1「实例化报冲突」写错了阶段：`class X(StrategyConfig, frozen=True)` 在**类定义时**就会因 `__init_subclass__` 不接受 kwargs 而抛 `TypeError`，失败在 import，不在实例化。
- **建议**: 现在就定：根类改为手写类，字段仍持有 msgspec 子 Struct；冻结用 `__setattr__` 守卫实现（或明确放弃并写出影响面：哪些调用点依赖了根类不可变 / 可哈希）；`__eq__` 是否需要；`_kwargs` 是否透传。把结论写进 D5，偏离日志状态由 ⏳ 改为已定。Step 1 证伪改为「import 时 TypeError」。

#### H4: D4 与 Task 8 冲突——D4 论证「入白名单 = 声明可跑 live」，Task 8 又把 SoDEX 入同一白名单，而本 plan 只验 sandbox/testnet

- **文件**: plan `D4` 行、Task 8 Step 2-4；`src/custos/engines/nautilus/host.py:92,211,346,349`；`src/custos/engines/nautilus/venue_binance.py:68,91`
- **计划定义**: D4「填入未接线 venue 会让 G6 判定入口宽于实际能力，红线 0.2 降为空门」；Task 8 Step 2「白名单新增 SoDEX 两个 connector……比照 `venue_binance.py` 补齐 live 侧」；Step 3 只证实「SoDEX sandbox/testnet 可装配」；D5 与「目标」段都说本次「只做能跑」。
- **实际代码**: `_SUPPORTED_VENUES`（`host.py:92`）是单一 frozenset，`supports_venue()`（`:211`/`:349`）不区分 mode，`:346` 允许 `live`。Binance 的「可跑 live」由 `venue_binance.py:68` `_LIVE_MIN_APPROVERS = 2` 与 `:91` `require_live_owner_evidence()` 等 293 行逐项落实。
- **影响**: 除非本 plan 真的交付 SoDEX **live** 的整套 approvers / owner evidence / 凭据处理并有真机证据（教训 C11：完成判据是对端接受过），否则 Task 8 一落地，G6 入口就对一个只验过 sandbox 的 venue 放开了 live——这正是 D4 自己描述的失败形态。Task 8 Step 4 的两条红线测试（未过 G6 deny / NoopHost deny）测的是 G6 本身，测不到「SoDEX live 能力是否真实存在」。
- **建议**: 明确本 plan 是否交付 SoDEX live。不交付（更可能）→ 把白名单拆成按 mode 的能力表（sandbox/testnet 集合 与 live 集合），SoDEX 只进前者，drift-guard 相应泛化；交付 → Task 8 必须列出 live 侧全部落实项与真机验收，并按 deviation-protocol 高风险（触及红线 0.2）走审议。

### 🟡 MEDIUM

#### M1: mypy 基线判据合理，但「同一批」按行号比对在本 plan 下必然失效；且更便宜的做法是先修掉那 4 处让 `verify` 变绿

- **文件**: plan「基线」段、Task 11 Step 4、验证清单第 3 条；`Makefile:32-35,231,233`；`adapter/orders.py:587,742,853`；`adapter/coordinators/trade_event_handler.py:279`
- **计划定义**: 「4 个错误不增加、不被掩盖，不是 typecheck 全绿」；Task 11「错误数 ≤ 基线 4 个，逐条比对是否同一批」。
- **实际代码**: 实跑确认 4 错误 / 2 文件 / exit 2，`make verify` 因 `Makefile:233` 并入 `toolkit-typecheck` 而在干净主干上红。`orders.py` 与 `trade_event_handler.py` 都在 Slice B 的 38 个待改文件里，Task 3 的路径拍平会移动行号。另外 `make check-authority` 里 `check-toolkit-typing-closure.py` 输出「strict zero, READY_TYPING_CLOSURE」，那只覆盖 base toolkit 41 文件（mypy 第一段 `Success: no issues found in 41 source files`），与 nautilus 包的 4 错误并存——计划应点明这两个「typing」口径不是一回事，免得 close-out 引用错。
- **影响**: 判据本身对（教训 #50：先钉基线），但比对键必须是 `(文件, 错误码, 消息)` 而非行号；否则 Task 11 要么误报「新增」，要么执行者放弃比对。4 处都是 `arg-type`、修法明确（Quantity 归一化 + Optional 收窄），在 plan 前单独一个 commit 修掉，验收就能回到「全绿」，也顺带消灭 `make verify` 恒红这个会被下一个人误以为自己弄坏的状态（教训 C6 known-red 记录义务）。
- **建议**: 优先「前置修复 4 处 → 判据改全绿」；不修则把基线输出落盘到 `.forge/reviews/2026-09/` 并规定比对键，且在 plan 里显式登记 `make verify` known-red 及其原因。

#### M2: task_dag 说 Slice E 只依赖 A，Task 12 又说 PS Config 子类「随 Slice B Task 5 的根类形态调整」

- **文件**: plan Execution Contract `task_dag` 行；Task 12 Step 2；Slice E 标题「仅依赖 Slice A」
- **影响**: E 若真与 B 并行，PS 的 `frozen=True` 子类会在 B 定形前被改一次、定形后再改一次。与 H3 同源。
- **建议**: DAG 改为 E 依赖 A + Task 5，或把根类形态在 plan 里定死（H3）后 E 才真正只依赖 A。

### 🔵 LOW

#### L1: 指标桥接锚点指向了 `handle_quote_tick`，Task 4 用到的是 `handle_bar`
- **文件**: plan 契约证据表「2.0 指标鸭子桥接」行；fork `crates/common/src/python/indicators.rs:62-64,96-99`
- **实际代码**: `:64` 取 `initialized` ✓；`:80` 调 `handle_quote_tick` ✓；但 Task 4 Step 3 经 `register_indicator_for_bars` 走的是 `:96-99` `call_method1(py, "handle_bar", (bar,))`。锚点没错，只是不是这个 plan 要用的那条。

#### L2: Slice 验收「`make check-authority` exit 0」对 B / C 是惰性门
- **文件**: plan Execution Owner `acceptance` 行；plan「基线」段自述「已扰动验证：改 `adapter/utils.py` 后仍 exit 0」
- **影响**: 计划自己证明了这道门对 adapter 改动不敏感，又拿它当 Slice B/C 的验收。B/C 的验收应是各自的失败模式测试 + `pytest --collect-only` 逐文件比对，`check-authority` 只对 Slice A（authority json）有判别力。

#### L3: Task 2「10 个测试文件」实测为 8
- **文件**: plan Task 2 `Files` 行
- **实际代码**: `grep -rln '1\.230\.0' tests/` 命中 8 个（7 个 `test_toolkit_*` + `test_runner_material_authority.py`）。教训 #41：数字类声明附 grep 命令。

#### L4: fork as-of 已漂一格；工作树里有一份未跟踪的 LiveNode 装配范例
- **文件**: fork HEAD `ae98c5c202`（计划记 `cda7fd412e`）；fork 工作树 `examples/live/sodex/paper_trading.py`（`??`，`:109` `LiveNode.builder(..., Environment.SANDBOX)`）
- **影响**: 31 条锚点在新 HEAD 全部仍成立，无需改；但 Task 7 可以直接引用这份范例作为 `LiveNode.builder()` 装配模板，前提是它先被 fork 提交——现在它随时可能消失。

## 正向偏离（改进）

| # | 位置 | 描述 | 理由 |
|---|---|---|---|
| 1 | 契约证据表 | 31 条锚点全部 `file:line` + 实读内容，且在 fork HEAD 前进一格后依然成立 | 教训 #14/#43 落实到位 |
| 2 | 「基线」段 | 起 plan 时就实跑并钉住 mypy 基线与 check-authority 的扰动灵敏度 | 教训 #50 的正确应用 |
| 3 | D6 | 批量 import 改写强制 AST/token 级 + 整行唯一匹配 + 看 diff | 教训 C10 直接内化 |
| 4 | 失败模式覆盖契约 + 红线 gate 表 | 起草时就留出 9 条失败模式与四红线的 code/runtime 分离表 | 教训 #17/#40 |
| 5 | D3 | 「4 个 nautilus 策略无一 import `shared`」经复核成立：5 个 `shared` 导入者全在 `refinement/hummingbot/` | 实证充分 |

## 逐 Slice 匹配率（计划文本 vs 代码事实）

| Slice | 判定 | 关键问题 |
|---|---|---|
| A 依赖与契约 | ❌ 需重写 | C1 / C2 / H1：依赖机制、跨仓字段、解释器前提三处都不成立 |
| B adapter | ⚠️ 需补决策 | H3：根类形态待定；M1：mypy 比对键 |
| C engine host | ⚠️ 需补决策 | H4：白名单是否按 mode 拆 |
| D 测试面 | ✅ | 无新问题；`test_kit` 5 文件、`Currency.from_str(value, strict=False)` 在 fork 存在 |
| E PS | ❌ 阻塞 | H2：PS 如何获得 2.0.0rc5 未回答；M2：依赖关系写错 |
| F 收尾 | ✅ | 结构完整 |

## 优先修复建议

1. **C1 + H1 + H2 合并决策**：fork 是否产 wheel、两仓都命令式安装；解释器是否真的要升 3.13。这三条共用一个根——「fork 以什么形态进入下游」——分开修会互相打架。
2. **C2**：把 `engine_version` 列为跨仓契约变更，补 mandatory-rules §3 影响分析，验收项改为「自有文件归零 + 跨仓项指名 owner」。
3. **H4**：决定本 plan 是否交付 SoDEX live；不交付就按 mode 拆白名单。
4. **H3 + M2**：现在定根类形态并同步 DAG。
5. **M1**：前置修掉 4 处 mypy，判据改全绿；或落盘基线并规定比对键。
6. L1–L4 顺手改。
