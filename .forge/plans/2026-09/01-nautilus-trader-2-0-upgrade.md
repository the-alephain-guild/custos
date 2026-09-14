# 01 - NautilusTrader 1.230.0 → fork 2.0.0rc5 升级

> **Status**: ⏳ In Progress（19 行进度表中 15 行 ✅，2026-09-14。**Task 14 已做完除「翻 ✅」以外的全部内容**，Status 不翻——本 plan 自己的 Task 14 第 6 条禁止在 Task 2b 未完成时 close-out；剩余契约协调为 2a / 2b。Task 1b 已发布并切换三平台 hash-pinned wheel，PS `make verify` 在该切换后全绿 998 passed / 18 skipped）
> **Created**: 2026-09-11
> **Project**: custos（跨仓：philosophers-stone）
> **multi_session_scope**: **true**（6 个 Slice、跨 2 仓库、涉及红线 0.1/0.2/0.4）
> **For Claude**: 按 Slice 派工，**不要单 session 硬推**（教训 #31）
> **接手请先读文末「交接 (Handoff) — Slice C 接手说明」**

## 上下文 (Context)

### 为什么做这件事

公会 fork 已自带 Rust SoDEX 场所适配器（`crates/adapters/sodex/`，Python 面
`python/nautilus_trader/adapters/sodex/__init__.pyi` 已由 pyo3_stub_gen 生成）。custos 与 PS
当前钉在官方 NautilusTrader 1.230.0，要消费该适配器只有两条路：为 1.x 另写一个 Python 版
SoDEX 适配器，或把两仓升到 fork 的 2.0.0rc5。本 plan 走后者。

### 现状（2026-09-11 实地扫描）

扫描口径：AST 解析 `import` / `from ... import`，排除 `_vendor` / `.venv` / `.worktrees`。

| 区块 | 位置 | 文件 | 行数 | 符号对 | 2.0 缺失 |
|---|---|---|---|---|---|
| vendored pandas_ta | `packages/custos-strategy-toolkit-nautilus/**/_vendor/` | 149 | 13,924 | 0 | 0 |
| 引擎无关层 | `packages/custos-strategy-toolkit/` | 41 | 6,524 | 0 | 0 |
| adapter | `packages/custos-strategy-toolkit-nautilus/**/adapter/` | 60 | 13,705 | 42 | 2 |
| daemon engine host | `src/custos/engines/nautilus/` | 3 | 2,091 | 20 | 13 |
| custos 测试 | `tests/` | 69 | 16,030 | 57 | 18 |
| PS 策略本体 | PS `{trend,momentum,portfolio,_template,fixtures}/` | 8 | 3,571 | 10 | **0** |

**PS `shared/nautilus`（8,320 行）不在本 plan 范围**——见「关键设计决策」D3。

### 契约证据（Step 1.5 gate）

fork as-of：`ae98c5c2024e818e1f6e7579002dd722e0ff196a`（2026-09-11 审查时实读；起草时为 `cda7fd412e`，
**fork 在活跃开发中**，本会话内 HEAD 已前进两次，下表锚点在 `ae98c5c202` 上逐条复核仍成立，引用时须再核对）。
fork 工作树另有一份**未跟踪**的 `examples/live/sodex/paper_trading.py`（`:109` `LiveNode.builder(..., Environment.SANDBOX)`），
是 Task 7 可用的装配范例，但引用前须先在 fork 提交，否则它随时可能消失。

| 契约 | 锚点 | 实读内容 |
|---|---|---|
| fork 版本 | `python/pyproject.toml:3` | `version = "2.0.0rc5"` |
| fork 解释器 |  `python/pyproject.toml:25` | `requires-python = ">=3.12,<3.15"` |
| fork 核心依赖 |  `python/pyproject.toml:26` | `dependencies = []`（`:30` 的 pandas 在 `visualization` extra，**非核心**） |
| fork 编译产物 | `python/nautilus_trader/_libnautilus.cpython-313-darwin.so` | **本机构建产物，非约束**：`git ls-files` 未跟踪，由 fork `Makefile:327` `maturin develop` 在本机 venv 下生成、`:404` clean 会删；fork `requires-python = ">=3.12,<3.15"`，3.12 可编 |
| 2.0 StrategyConfig | `python/nautilus_trader/trading/__init__.pyi:981` | Rust pyclass，固定签名 + `_kwargs: dict \| None` |
| 2.0 config re-export | `python/nautilus_trader/config/__init__.pyi:38` | `from nautilus_trader.trading import StrategyConfig as StrategyConfig` |
| 2.0 LiveNode | `python/nautilus_trader/live/__init__.pyi:358` | `class LiveNode`，`:372 build()` / `:374 builder()` 静态工厂 |
| 2.0 LiveNodeBuilder | `python/nautilus_trader/live/__init__.pyi:402` | `class LiveNodeBuilder` |
| 2.0 指标鸭子桥接 | `crates/common/src/python/indicators.rs:53` | `impl ActorIndicator for PyActorIndicator`；`:64` 取 `initialized`（属性或可调用皆接受），`:80` 调 `handle_quote_tick` |
| 2.0 指标注册 | `python/nautilus_trader/trading/__init__.pyi:525` | `register_indicator_for_bars` |
| 2.0 testkit | `python/nautilus_trader/testkit/providers.py:123` | `class TestInstrumentProvider`（包名由 `test_kit` 改为 `testkit`） |
| 2.0 无 stubs | `python/nautilus_trader/testkit/` | 仅 `providers.py`，**无 `stubs/` 子模块** |
| 2.0 无 SuperTrend | `python/nautilus_trader/indicators/__init__.pyi` | 导出清单 46 项，无 SuperTrend |
| 硬编码版本契约 | `packages/custos-strategy-toolkit/src/custos_toolkit/contracts/toolkit_rc.py:113-118` | `distribution_name != "..." or python_requires != ">=3.12,<3.13" or nautilus_version != "1.230.0"` → `raise ValueError` |
| toolkit 依赖 pin | `packages/custos-strategy-toolkit-nautilus/pyproject.toml:5,8` | `requires-python = ">=3.12,<3.13"`；`nautilus-trader==1.230.0` |
| G6 venue 白名单 | `src/custos/engines/nautilus/host.py:92` | `_SUPPORTED_VENUES = frozenset({"binance", "binance_perpetual"})` |
| drift-guard | `tests/test_nt_binance_venue.py` | 锁 `_SUPPORTED_VENUES` 与 venue-config 模块一致 |
| 当前解释器 | `.python-version` | `3.12`（venv 实为 3.12.1，装 PyPI 1.230.0）；**本 plan 不改** |
| fork build backend | fork `python/pyproject.toml` `[build-system]` | `requires = ["maturin==1.15.0", "patchelf"]`，`build-backend = "maturin"`——path 源意味着每次 `uv sync` 现场编 Rust |
| fork wheel 产出口 | fork `Makefile:337` | `maturin build --release --out ../dist` |
| uv path 源行为 | PS `pyproject.toml:74-80`（commit `6dd552d`） | 「uv reads metadata for every path source in the lock even when its extra is not requested」——path 源会击穿不带 extra 的 3.11 base 安装 |
| base 3.11 承诺 | `pyproject.toml:29-32` | 「package stays installable on Python 3.11 (audit / paper)」 |
| Docker runtime lock | `docker/runtime-requirements.lock:175`；`Makefile:96-97,99-103` | `nautilus-trader==1.230.0`，由 `uv export --frozen --extra nautilus` 生成，`Dockerfile:22-27` 以 `--require-hashes` 安装；custos 自有三个 wheel 走 `Dockerfile:28-44` 的 `COPY dist/*.whl` + `--no-deps`，**不经 hash 清单** |
| uv git 源导出形态 | 2026-09-12 一次性项目实测（uv 0.12.13） | `uv export --frozen` 对 git 源输出 `pkg @ git+https://…@<sha>`，**无 `--hash`**；不带 extra 导出时该包 0 行（git 源不污染 3.11 base） |
| uv 导出排除开关 | `uv export --help`（uv 0.12.13） | `--no-emit-package <NAME>` 存在 |
| fork 仓位置 | `git -C <fork> remote -v`（2026-09-12） | origin `the-alephain-guild/nautilus_trader`（public，`gh repo fork` 自 nautechsystems 建于 2026-09-12）；upstream `nautechsystems/nautilus_trader`；旧个人 fork 保留为 remote `wukai9203` |
| fork 版本 label | fork `python/pyproject.toml:3`，commit `3345ad4c1c` | `version = "2.0.0rc5+sodex.1"`；`python/uv.lock:548` 同步；`crates/core/build.rs:30` 硬编码 `2.0.0rc5` 不改（断言是 `starts_with` 前缀匹配） |
| V1 契约实现 | `packages/custos-strategy-toolkit/src/custos_toolkit/contracts/strategy_execution.py:140,197` | `engine_version: Literal["1.230.0"]` |
| gateway schema const | `docs/gateway-contract/v1/strategy_artifact_ref_v1.schema.json:130`；`strategy_manifest_v1.schema.json:94`；`strategy_artifact_pre_import_verification_receipt_v1.schema.json:207` | `"const": "1.230.0"` |
| Crucible vendored golden | `docs/authority/vendor/crucible-runner-strategy-release-resolution-v1.golden.json:36,95,114,132,148,150,158,203,234` | `engine_version` 8 处内嵌于 canonical JSON；**Crucible 所有，custos 不得改** |
| 版本号脚本 | `scripts/toolkit_rc_build.py:177`；`scripts/toolkit_rc_release_readiness.py:755`；`scripts/generate_strategy_contract_assets.py:180` | 三处硬编码 `1.230.0` |
| 2.0 StrategyConfig 可子类化 | fork `crates/trading/src/strategy/config.rs:42` | `pyo3::pyclass(module = "nautilus_trader.trading", subclass, from_py_object)` |
| 2.0 子类范例 | fork `examples/live/sodex/adaptive_martingale.py:207,239,244` | `class AdaptiveMartingaleConfig(StrategyConfig)`；`**_kwargs: Any`；`super().__init__()` |
| 2.0 指标 bar 桥接 | `crates/common/src/python/indicators.rs:96-99` | `fn handle_bar` → `call_method1(py, "handle_bar", (bar,))`（Task 4 走的是这条，不是 `:80` 的 quote 路径） |

**plan-to-plan 引用**：

| plan-id | commit-hash | 引用的文件/章节 |
|---|---|---|
| PS 60 | `6dd552d` | `.forge/plans/2026-07/60-retire-ps-shared-adopt-custos-toolkit.md`「Slice E：Owner 决定 defer（2026-07-30）」 |

### 基线：`make verify` 起草时在干净主干上是红的，由 Task 0 归零

`make toolkit-typecheck` 有 4 个**既有** mypy 错误，与本 plan 无关，实测（2026-09-11，
worktree 干净）：

```
adapter/orders.py:587,742,853  Argument "quantity" ... incompatible type "Any | Quantity | Decimal"; expected "Quantity"
adapter/coordinators/trade_event_handler.py:279  Argument 1 to "set_pending_signal" ... "Signal | None"; expected "Signal"
Found 4 errors in 2 files (checked 60 source files)
```

这 4 处都是 `arg-type`（`Quantity` 归一化 + `Optional` 收窄），且两个文件都在 Slice B 的路径拍平范围内——
升级后行号必移，「按行号比对是否同一批」不可判定。**因此 Slice A 前置 Task 0 先把它们修到全绿并单独
commit，本 plan 的 typecheck 验收判据是「全绿」。** 不先钉住基线，升级后看到红会误判成自己弄坏的
（教训 #50）；不先归零，close-out 就得靠一份会漂的对照表。另注意 `make check-authority` 输出的
「strict zero, READY_TYPING_CLOSURE」只覆盖 base toolkit 41 文件，与 nautilus 包的 mypy 结果是两个口径。

`make check-authority` 基线为绿（exit 0）。已扰动验证：改 `adapter/utils.py` 后仍 exit 0，
说明 `docs/authority/` 下 101 条 `packages/` 记录是**历史 receipt 而非当前字节约束**
（commit `f74c0b4 test(custos): stop pinning active contract bytes` + `authority-docs.md`
「historical evidence for that recorded revision, not a permanent byte constraint」）。
教训 C6 在本区域**已失效**，改 adapter 不需重签 receipt。

### 中间态基线（2026-09-12，Task 1a-1 之后）

依赖已切到 2.0 而应用代码仍是 1.x 路径，这是 task_dag 决定的必经中间态。**Slice B / C 的判据
以本快照为参照**，不是「test-baseline 绿」——那要到 Slice D 才可能。

`uv run pytest tests/ -q`：**45 个 collection error，收集期即 Interrupted，零测试执行**
（对照干净基线 `2418 passed`）。运行期失败数为 0 不是好消息，是根本没跑到（教训 #50）。

| 目录 | collection error | 归属 |
|---|---|---|
| `tests/toolkit/` | 35 | Slice B |
| `tests/engines/` | 3 | Slice C |
| `tests/` 根（7 个文件） | 7 | Slice C（host / venue / runner_safety 相关） |

收敛判据分两层，逐层验、不合并：

1. **collection 归零**——该 Slice 范围内的 error 数降到 0（`pytest --collect-only` 逐文件比对，
   被 skip 或 uncollectable 的文件点名报告，不得静默豁免）
2. **运行期回到基线**——全部 collection 修好后，passed 数不低于 2418

完整文件清单见实施期 scratchpad；关键是**每个 Slice 只对自己那一栏负责**，跨栏的红留给对应 Slice，
不在本 Slice 里顺手改、也不算本 Slice 的失败。

## 目标 (Goal)

把 custos 的 toolkit、daemon engine host 与 PS 策略本体从 NautilusTrader 1.230.0 升到公会 fork
2.0.0rc5，使 SoDEX Rust 适配器可被直接消费，且 Non-Custodial 4 红线逐条仍然咬得住。

## 架构 (Architecture)

升级分三种改动性质，按性质而非按目录切片。**路径迁移**是 2.0 把子模块拍平到包顶层
（`model.data|Bar` → `model|Bar`），占 42+20+57 个符号对中的绝大多数，可机械化但必须在 AST/token
层面做（教训 C10：行级正则会把 `f.is_ready()` 改成属性访问且测试照样绿）。**类型语义重写**有三处：
`StrategyConfig` 由 msgspec Struct 变 Rust pyclass、`Indicator` 基类在 2.0 的 Python 面消失、
`Instrument` 无同名抽象基类。**节点装配重写**只在 daemon engine host，`TradingNode` 体系整体
换成 `LiveNode`，同时把 SoDEX 接进 G6 白名单。

## Execution Contract

| Field | Value |
|---|---|
| surface_identity | runtime-resolved by Resolution Authority from execution-time host evidence；planning host is not execution authority |
| required_capabilities | `vcs.commit@native`、`shell.exec@native`、`fs.write@native` |
| preferred_capabilities | `test.run@native`（缺失时降级为人工复跑并记录命令） |
| fallback_policy | native-first；`test.run` 不可用时显式降级，其余 capability 缺失即 blocked |
| task_dag | Slice A 串行前置（Task 0 → 1a-1）；B / C 可并行；D 依赖 B+C；**E 依赖 A + Task 5**；**2a 与 2b 相邻、排在 D 之后**（2a 会让 generator 读 Crucible vendored golden 失败，在 2b 交付前必红，故不占前置位）；1b 在 F 前；F 最后 |
| host_context | orchestrator=custos worktree；workspace_owner=custos；nested_workspace_allowed=false |
| dispatch_authority | user |

## Execution Owner

- owner: `runtime-resolved execution surface`
- acceptance: Slice A 以 `make check-authority` exit 0 + 失败模式测试为准；Slice B / C / D 以该 Slice 的失败模式测试 + `pytest --collect-only` 逐文件比对为准（`check-authority` 对 adapter / host 改动已实测不敏感，对这三个 Slice 是惰性门，不算验收）
- concurrency: 单一 owner；Slice B 与 C 若并行须各自 worktree，禁止并发写同一文件

## Execution Handoff

| Step | Source | Target | Artifact | Status |
|---|---|---|---|---|
| 1 | planning surface | runtime-resolved execution surface | plan commit | pending |
| 2 | execution surface | planning surface | Slice close-out commit | append-only |

## 关键设计决策 (Key Design Decisions)

| 问题 | 决策 | 理由 |
|---|---|---|
| **D1** fork 如何进依赖链 | **两阶段。1a（本 plan 主体，fork 活跃开发期）**：uv git 源 `{ git = "https://github.com/the-alephain-guild/nautilus_trader", rev = "<sha>", subdirectory = "python" }`，lock 钉精确 sha；Docker 链把 nautilus-trader 从「带 hash 的第三方包」改归为「本地构建 wheel」——`uv export` 加 `--no-emit-package nautilus-trader`，`Dockerfile` 加 `nt-builder` 阶段在 lock 钉的 sha 上 `maturin build`，与三个 custos wheel 同路 `--no-deps` 安装，并加一道「lock sha == 镜像内版本 label 里的 sha」对账。**1b（切换门，Slice F 前）**：fork 打 tag、CI 产 cp312 manylinux + macosx wheel、Release 挂载后，custos 与 PS 改为带 sha256 的 wheel 引用，删 `--no-emit-package` 与 `nt-builder`，Docker 回到今天的形状。**不用 path 源** | owner 2026-09-12 定案。git 源实测不污染 3.11 base（不带 extra 导出 0 行）、lock 自足、PS 规则不违反，省掉 fork 活跃期每个 commit 走发布的负担；代价是每台机器每个 sha 编一次 Rust（uv 按 sha 缓存）。Docker 红的根因是 `--require-hashes` 要求字节固定产物而 git 引用指向源码（实测导出无 hash），解法是走 custos 自有 wheel 已有的 `--no-deps` 路径而不是登记 known-red。**版本 label 与源形式无关，现在就要**：fork `pyproject.toml` 版本改 `2.0.0rc5+sodex.<n>`（API 变动递增 n），否则 `toolkit_rc` 按 `importlib.metadata` 比对时与上游 `2.0.0rc5` 无法区分（审查 C2）。**仓库位置先迁后钉**：lock 与三方契约 provenance 都记 URL，改一次牵动 PS lock，故先把 fork 迁到 `the-alephain-guild` 组织（保持 public，LGPL-3.0-only 不变）再钉。path 源的真实代价见审查 `b0ff52a` C1：maturin 现场编 Rust + PS `pyproject.toml:74-80` 实测 uv 读 lock 里每个 path 源元数据、击穿 `pyproject.toml:29-32` 的 3.11 base 承诺与 `Makefile:76-77` `verify-base-clean` |
| **D2** Python 版本 | **保持 3.12，撤回升 3.13** | 起草时的理由「fork 唯一编译产物是 cp313 .so，物理约束」不成立：那份 `.so` 未被 git 跟踪，是本机 `maturin develop` 的产物；fork `requires-python = ">=3.12,<3.15"`，以 3.12 构建即得 cp312 wheel。于是 `.python-version`、`tech-stack.md`「固定 3.12」、`toolkit-nautilus` 的 `requires-python` 三处都不动，少三条偏离。Docker 基础镜像 `python:3.12.13-slim` 亦无需变 |
| **D3** PS `shared/nautilus` 是否拉进来 | 不拉进来，维持 PS Plan 60 Slice E 的 defer | owner 2026-09-11 定案。实证：4 个 nautilus 策略本体已全部 import `custos_toolkit`，无一 import `shared`；`shared/nautilus` 现仅服务 crucible 两个 Dockerfile 与 hummingbot 侧，与 NT 2.0 正交。其删除前置（arx 上线、custos Plan 24）与本次无关 |
| **D4** G6 venue 白名单 | **白名单改为按 mode 的能力表**：`sandbox` / `testnet` / `live` 三个集合，`supports_venue(venue, mode)`。SoDEX 两个 connector 本 plan **只进 sandbox 与 testnet**；`live` 集合维持 `binance` / `binance_perpetual` 不变。**不改为「NT 支持的全部交易所」** | 该集合的契约是「custos 已接线的 connector」而非「NT 支持什么」——`host.py:211`/`:349` 的 `supports_venue()` 消费它，而同 host `:346` 允许 `live`，单一集合意味着列入即等于声明该 venue 可跑 live。「可跑 live」在 custos 是 `venue_binance.py` 293 行逐项落实的（`_LIVE_MIN_APPROVERS = 2`、`require_live_owner_evidence()`、三套 exec config 构建、凭据处理），每 venue 各一套、无通用实现。本 plan 只做「能跑」、Task 8 只验 sandbox / testnet，若仍用单一集合就是 D4 自己描述的失败形态（教训 #22 同型）。按 mode 拆开后，SoDEX 请求 live 在 host gate 因不在 live 集合被拒——这是红线 0.2 的真实测试。**假设：本 plan 不交付 SoDEX live**；要交付须另按 deviation-protocol 高风险审议并补齐 live 侧全部落实项与真机证据（教训 C11）。drift-guard `tests/test_nt_binance_venue.py:53` 现断言 `_SUPPORTED_VENUES == frozenset(_BINANCE_CONNECTORS)`（**等号**），本次改为「三个 mode 集合分别等于该 mode 已接线 connector 的并集」并将该测试泛化改名 |
| **D5** 4 个指标薄包装是否删 | **保留**，仅去掉 `Indicator` 基类 | 2.0 确有 `MovingAverageConvergenceDivergence` / `DirectionalMovement` / `AverageTrueRange` / `RelativeStrengthIndex`，但删包装会改变策略侧 import 面并牵动 PS。本次只做「能跑」，收敛到引擎原生另起 follow-up。SuperTrend（418 行）2.0 无对应实现，必须保留；其 `update_raw` 内 `:203-215` 的列名前缀搜索与 `:22` `_supertrend_column_names()` 是两套并存机制，Task 4 顺手二选一（见「品味收尾项」） |
| **D5b** `StrategyConfig` 根类形态 | `NautilusTradingStrategyConfig(StrategyConfig)` 为**普通子类**（fork `config.rs:42` 带 `subclass`）：kw-only `__init__` 接收 7 个 section（仍是 msgspec 子 Struct，`msgspec.structs.asdict` 消费点不变）+ `**_kwargs` 透传 `super().__init__()`；冻结用 `__setattr__` 守卫在 `__init__` 完成后启用（`object.__setattr__` 赋值）；`__eq__` 按 section 元组；**不提供 `__hash__`**（grep 无消费点）。PS 三个 Config 子类同形态、去 `frozen=True` | 形态即 fork 示例 `examples/live/sodex/adaptive_martingale.py:207-244`。现状根类是 msgspec Struct（`trading_config.py:64`），切换基类后失去的不只是 frozen，还有字段声明 / kw 构造 / 结构化相等——这是 deviation-protocol 的中风险模型结构变更，必须在起草期定而不是留给两个仓的执行者各自即兴。`registry.py:329` 的 `config_factory(parameters=..., **base_sections)` 与 kw-only `__init__` 兼容 |
| **D6** 批量改 import 的手法 | AST/token 级替换，逐条断言「整行唯一匹配」，改后 `ast.parse` + 看 diff | 教训 C10：行级正则曾把 65 处追踪号清理做成 9 文件语法错 + 更多文件语义静默损坏，而测试照常通过 |

## 承载决策 (Capability Hosting Decision)

不新增能力，纯升级现有代码，无需 skill / hook / plan mode 承载。

## 文件清单 (File Inventory)

| 文件路径 | 操作 | 描述 |
|---|---|---|
| `pyproject.toml` | Modify | 1a：`[tool.uv.sources]` git 源钉 sha；1b：改带 sha256 的 wheel 引用；pandas 显式声明（fork 核心依赖为空） |
| `uv.lock` | Modify | 重锁；验收：不含 path 源；1a 为 `git+…@<sha>`，1b 为带 hash 的 wheel |
| `docker/runtime-requirements.lock` | Modify | 1a：`--no-emit-package nautilus-trader` 后重生成，不含该包；1b：恢复为带 sha256 的条目 |
| `Dockerfile` | Modify | 1a：加 `nt-builder` 阶段（`FROM rust:<pin>`，clone fork@sha，`maturin==1.15.0` 与 fork `[build-system]` 同 pin，`maturin build --release`），builder 阶段 `COPY --from=nt-builder` 后与 custos wheel 同路 `--no-deps` 安装；sha 由 `uv.lock` 推导而非手写；1b：删该阶段 |
| `tests/test_docker_runtime_contract.py` | Modify | 1a：断言镜像内 `importlib.metadata.version("nautilus-trader")` 的 local label 与 `uv.lock` 钉的 sha 一致（教训 C7：清单从权威源推导） |
| `Makefile` | Modify | 1a：`:96-97` `runtime-lock` 与 `:99-103` `check-runtime-lock` 加 `--no-emit-package nautilus-trader`，并加「lock sha vs Dockerfile 推导 sha」对账；1b：去掉排除，加与 `toolkit-dev` 同型的 wheel 安装 target |
| `packages/custos-strategy-toolkit-nautilus/pyproject.toml` | Modify | NT pin 改 `2.0.0rc5+sodex.<n>`；`requires-python` **不动** |
| `packages/custos-strategy-toolkit/src/custos_toolkit/contracts/toolkit_rc.py` | Modify | `:113-118` 版本契约改 `2.0.0rc5+sodex.<n>`（V1 in place，不留兼容别名） |
| `packages/custos-strategy-toolkit/src/custos_toolkit/contracts/strategy_execution.py` | Modify | `:140,197` `engine_version` Literal（V1 in place） |
| `docs/gateway-contract/v1/{strategy_artifact_ref_v1,strategy_manifest_v1,strategy_artifact_pre_import_verification_receipt_v1}.schema.json` | Modify | `const` 改新值（**跨仓契约，见 Task 2b**） |
| `scripts/{toolkit_rc_build,toolkit_rc_release_readiness,generate_strategy_contract_assets}.py` | Modify | 三处硬编码版本号 |
| `docs/authority/vendor/crucible-runner-strategy-release-resolution-v1.golden.json` | **Blocked** | Crucible 所有，由 Crucible 重新提供；custos 不改 |
| `packages/custos-strategy-toolkit-nautilus/src/custos_toolkit_nautilus/adapter/{orders.py,coordinators/trade_event_handler.py}` | Modify | Task 0：4 处 `arg-type` 归零 |
| `docs/authority/ecosystem-authority.json` | Modify | `:78` nautilus_trader 版本 |
| `docs/authority/strategy-artifact-ref-v1.golden.json` | Modify | `:17` engine_version |
| `docs/authority/strategy-artifact-pre-import-verification-v1.golden.json` | Modify | `:23,:77` engine_version |
| `packages/.../adapter/**` (38 文件) | Modify | 40 个符号对路径拍平 |
| `packages/.../adapter/indicators/{macd,adx,atr,rsi,supertrend}.py` | Modify | 去 `Indicator` 基类，逻辑不动 |
| `packages/.../adapter/trading_config.py` | Modify | `:64` `NautilusTradingStrategyConfig` 改 pyclass 子类形态 |
| `packages/.../adapter/signal_processor.py` | Modify | `:21` 随根类改 |
| `packages/.../adapter/{runtime_types,sizing}.py` | Modify | `Instrument` 类型注解改 2.0 具体类型 |
| `src/custos/engines/nautilus/host.py` | Modify | `TradingNode` → `LiveNode`（重写前先 typed 化 spec 与 NT 节点能力，见 Task 7 约束）；`:92` 白名单改为按 mode 的三个集合，`:211` / `:349` `supports_venue(venue, mode)` |
| `src/custos/engines/nautilus/venue_binance.py` | Modify | Binance adapter 符号路径适配 |
| `src/custos/engines/nautilus/venue_sodex.py` | **Create** | SoDEX 连接器装配（对照 `venue_binance.py` 形态） |
| `src/custos/engines/nautilus/runner_safety.py` | Modify | `PriceType` 等路径适配 |
| `tests/**` (69 文件) | Modify | 路径拍平 + testkit 改名 |
| `tests/support/nt_stubs.py` | **Create** | 手写 4 个 stubs 的替代 fixture（2.0 无 `test_kit.stubs`） |
| `tests/test_nt_binance_venue.py` → `tests/test_nt_venue_wiring.py` | **Rename** + Modify | drift-guard 泛化：三个 mode 集合分别 == 该 mode 已接线 connector 并集；SoDEX 只在 sandbox / testnet 集合 |
| `.claude/rules/tech-stack.md` | Modify | 仅 NT 版本 pin（`:15` `:60`）；「固定 3.12」**不动** |
| `docs-site/docs/08-toolkit/overview.md` + zh-Hans 对应 | Modify | `:106` / `:69` 版本表 |
| PS `{trend,momentum,portfolio,_template,fixtures}/**` (8 文件) | Modify | 10 个符号对路径拍平 |
| PS `tests/**` (25 文件) | Modify | 随策略改动适配 |
| PS `pyproject.toml` / `uv.lock` / `Makefile` | Modify | `:21` NT pin 改 fork wheel（PS 规则禁 path 源，`pyproject.toml:74`）；`toolkit-dev` 同型的命令式安装 |

## 失败模式覆盖契约 (Failure-Mode Coverage)

教训 #17：happy-path 全绿 ≠ 失败模式覆盖。本 plan 必须覆盖以下场景，每条至少 1 个测试：

| 失败模式 | 为什么必须测 | 归属 Slice |
|---|---|---|
| lock 钉的 fork sha 与镜像内编的 sha 不一致 | 1a 把 nautilus-trader 移出 hash 清单后，唯一能证明「镜像装的就是 lock 钉的」是这道对账；缺了它「lock 钉 A、镜像编 B」不会变红 | A |
| 1b：fork wheel hash 被篡改 | `uv sync --frozen` 必须拒绝，证明 lock 真的钉住了字节 | F |
| base 安装不被击穿 | `pyproject.toml:26-32` 的承诺是「发布的包在 3.11 可装」，仓库的门是 `Makefile:76-78` `verify-base-clean`（`uv sync --package custos-runner --extra dev`，**不指定解释器**）。**原措辞「3.11 解释器下 uv sync」在 uv workspace 下不可能成立**——toolkit-nautilus 自己的 `requires-python` 把整个 workspace 解析为 `==3.12.*`（实测报错原文如此）。已改为验真实的门 | A ✅ 已验 |
| `toolkit_rc` 收到 1.230.0 | 旧版本号必须被契约拒绝（证明改的是 V1 而非加了别名） | A |
| Python 指标缺 `initialized` | 2.0 鸭子类型桥接靠 getattr，缺属性时须报错而非静默不更新 | B |
| `StrategyConfig` 子类漏 `super().__init__()` | pyclass 未初始化的失败必须可诊断 | B |
| SoDEX 请求 live | 红线 0.2：SoDEX 不在 live 集合，host gate 必须 deny；`NoopHost` 下 live 必须 deny | C |
| 非白名单 venue / 错 mode | 三个 mode 集合之外必须拒；Binance 的 live 集合不因本次改动缩小 | C |
| LiveNode 启动失败 | 失败须传播，不得静默留下半启动状态 | C |
| 2.0 `Price`/`Quantity` 参与 money math | 红线 0.4：Decimal 不得退化为 float | B+C |
| 云端失联 | 红线 0.3：本地 breaker 与 cap 仍生效（2.0 下回归） | C |

## 红线 gate 满足度 (Red-Line Gate)

教训 #40：close-out 必须逐条区分 code 覆盖与 runtime 接线，不得承袭红线名当兑现声明。本表在
每个 Slice close-out 时填，起草时留空。

| 红线 | code_coverage | runtime_wire | defer_status | follow_up |
|---|---|---|---|---|
| 0.1 Key/KEK 不出进程 | TBD | TBD | — | — |
| 0.2 G6 host gate 不绕过 | TBD | TBD | — | — |
| 0.3 失联 ≠ 停止 | TBD | TBD | — | — |
| 0.4 Decimal money math | TBD | TBD | — | — |

## 实现任务 (Tasks)

### Slice A — 依赖装配与契约版本（串行前置，其余 Slice 全部依赖）

#### Task 0: mypy 基线归零（Slice A 最前，单独 commit）
**Files**: `adapter/orders.py:587,742,853`, `adapter/coordinators/trade_event_handler.py:279`
**Step 1（证伪）**: `make toolkit-typecheck` 报 `Found 4 errors in 2 files`，exit 2
**Step 2（实现）**: 三处 `quantity` 归一为 `Quantity`；一处 `Signal | None` 在调用前收窄
**Step 3（证实）**: `make toolkit-typecheck` exit 0；`make verify` 在主干转绿
**Step 4**: commit（`fix(custos): zero the toolkit mypy baseline before the nautilus 2.0 upgrade`）

#### Task 1a: fork git 源 + Docker 内构建（D1 阶段一）
**Files**: `pyproject.toml`, `uv.lock`, `docker/runtime-requirements.lock`, `Makefile:96-103`, `Dockerfile`, `tests/test_docker_runtime_contract.py`, `packages/custos-strategy-toolkit-nautilus/pyproject.toml`
**Step 0（前置，fork 仓）**: (a) fork 迁到 `the-alephain-guild/nautilus_trader`（public），根仓库 `CLAUDE.md` §8 登记表的 origin 同步改（跨仓项）；(b) fork `python/pyproject.toml` 版本改 `2.0.0rc5+sodex.<n>`；(c) 记下要钉的 sha。**状态（2026-09-12）**：(a) 完成——组织仓已建、本地 remotes 已改（origin=组织仓 / upstream=nautechsystems / 旧个人 fork 保留为 `wukai9203`）、根 `CLAUDE.md` §8 已登记；owner 选择先合并上游再推：merge commit `3fe857a351` 把上游 23 个 commit 合入（`git merge-tree` 无冲突，`python/uv.lock` 重解析无变化，`Cargo.lock` 经 `cargo metadata --locked` 联网核验通过），develop 已推送并跟踪 `origin/develop`；(b) 完成——`3345ad4c1c`（`+sodex.1`）；(c) **钉 `3fe857a351`**（含 (b) 与上游合并；fork 工作区仍有 4 个 sodex 文件未提交，不在此 sha 内）。**没有 (a)(b) 本 Task 不能开始**——URL 与版本 label 都会进 lock 与三方契约，事后改要牵动 PS
**Step 1（证伪）**: 当前 venv `uv run python -c "import nautilus_trader; print(nautilus_trader.__version__)"` 输出 `1.230.0`；`make check-runtime-lock` 在加 git 源后 diff 非空（证明这道门确实会因 git 源变红，而不是本来就不看它）
**Step 2（实现）**: `[tool.uv.sources]` 加 git 源（`rev` 写精确 sha，`subdirectory = "python"`）；`toolkit-nautilus` 的 NT pin 改新版本号；pandas 显式进依赖；`uv lock`；`Makefile` 两处 `uv export` 加 `--no-emit-package nautilus-trader` 并重生成 runtime lock；`Dockerfile` 加 `nt-builder` 阶段（sha 从 `uv.lock` 推导，不手写）；`test_docker_runtime_contract.py` 加 label 对账断言
**Step 3（证实）**: 同一命令输出 `2.0.0rc5+sodex.<n>`；`python/nautilus_trader/adapters/sodex` 可 import；`grep -c 'path = ' uv.lock` 对 nautilus-trader 为 0 且有 `git+…@<sha>`；`make check-runtime-lock` exit 0；`make test-docker` 全绿（含新对账断言）
**Step 4（失败模式）**: (a) 3.11 解释器下 `uv sync --package custos-runner --extra dev`（无 nautilus extra）必须成功；(b) 把 `uv.lock` 里的 sha 改一位后不重建镜像，对账断言必须变红（证明它是 live guard）；(c) `nt-builder` 里 `maturin` 版本与 fork `[build-system]` 不一致时构建必须失败而非静默用别的版本
**Step 5**: commit

#### Task 1b: 切换到 wheel（D1 阶段二，Slice F 收尾前的门）
**Files**: `pyproject.toml`, `uv.lock`, `docker/runtime-requirements.lock`, `Makefile`, `Dockerfile`, PS `pyproject.toml` / `uv.lock`
**触发判据（2026-09-13 重写，原判据不可满足——见偏离日志）**: 原文要求 fork CI 产 wheel 并挂 Release，实测该 fork 从未运行过任何 workflow（`actions/runs` `total_count = 0`）且注册了 0 个 self-hosted runner，而产 wheel 的 job 在 push 事件下全部要求 `["self-hosted","Linux","X64","build"]`。现判据三条：(1) 三平台 wheel 就位并各记 sha256——darwin-arm64 / linux-aarch64 / linux-x86_64，均由 fork `3fe857a351` 构建，版本 `2.0.0rc5+sodex.1`；(2) Release 已由 `gh release create` 发布且各资产 URL 可取（**不经 CI**）；(3) Task 8 的 SoDEX sandbox / testnet 起停通过（已满足，adapter 的 Python 面冻结）
**Step 1（证伪）**: `uv.lock` 仍含 `git+`；`Dockerfile` 仍含 `nt-builder`
**Step 2（实现）**: **2026-09-13 实测定案，取 `url` 源按平台各一 + `marker`**（不取 `find-links`）。实测依据：`file://` 被 uv 拒（`URL scheme is not allowed`）；http(s) 源下 lock 为每个平台各记一条 `[[package]]`，含 `source.url` / `resolution-markers` / `wheels[].hash`，uv 自算的 sha256 与独立 `shasum -a 256` 一致；`uv export` 随之吐出带 marker 且带 `--hash=sha256:` 的行，正是 `--require-hashes` 要的形态。配套：`pyproject.toml` `[tool.uv]` 新增 `environments` 列三平台（否则全平台解析在无 wheel 的环境上解不开）；删 `--no-emit-package`；`Dockerfile` 保持无 `nt-builder` 的形状；退役 `docker/nautilus-wheel.dockerfile`、`make nautilus-wheel` 与 `scripts/nautilus_source_pin.py`（它们只服务 git-源阶段）；runtime lock 重生成后含带 hash 的 nautilus-trader；PS 同步改为同一组 wheel 引用
**Step 3（证实）**: `uv.lock` 无 `git+`；`docker/runtime-requirements.lock` 中 nautilus-trader 带 `--hash=sha256:`；`Dockerfile` 回到无 `nt-builder` 的形状；`make test-docker` 全绿
**Step 4（失败模式）**: 篡改 lock 里 wheel 的 hash 后 `uv sync --frozen` 必须拒绝
**Step 5**: commit。**本 Task 未完成不得 close-out**：1a 的 `nt-builder` 是过渡方案，留着就是永久债务

#### Task 2a: 契约层版本号原地改 V1（custos 自有部分）

> **⚠ 已改排到 Slice F 前，与 2b 相邻（2026-09-12 实测改排）**。原置于 Slice A 末尾，实测**不可行**：
> `scripts/generate_strategy_contract_assets.py:214-223` 从 `docs/authority/vendor/crucible-runner-strategy-release-resolution-v1.golden.json`
> 读 `artifact_binding.artifact_ref`，再用 `StrategyArtifactRefV1.model_validate()` 校验。一旦把
> `strategy_execution.py` 的 `Literal` 改成新值，那份 golden 里的 `engine_version = 1.230.0` 立刻不过，
> generator 失败 → `make check-authority` 红 → `make verify` 红。而**那份 golden 归 Crucible，custos 不得改**
> （`authority-docs.md`「Never invent, vendor or pre-register downstream receipts」），即 Task 2b。
> 也就是说 **2a 的验收项「`make check-authority` exit 0」在 2b 交付前不可满足**——正是教训 C14 说的
> 「写出一条自己无权满足的门」，这次出现在执行顺序上。
>
> **Slice B / C / D 不依赖版本号常量**（它们改 import 路径），故把 2a 推到它们之后、与 2b 相邻，
> 让中间期 `check-authority` 保持绿、可继续充当判据。2a 的改动本身已验证过一遍并回退，
> 落地时直接重做即可：源头是 `strategy_execution.py` 的 2 处 `Literal`、`toolkit_rc.py` 1 处、
> `generate_strategy_contract_assets.py:180` 1 处，schema 与 golden 由 `make strategy-contract-assets`
> **重新生成而非手改**（C7：清单从权威源推导）；另有 `toolkit_rc_build.py:177`（`"nautilus-trader==1.230.0"`
> 依赖字符串形态）与 `toolkit_rc_release_readiness.py:755`，以及 7 个测试文件（实测 7 个，非 plan 原写的 8 个）。

**Files**: `toolkit_rc.py`, `strategy_execution.py:140,197`, 3 个 gateway schema 的 `const`, 3 个 authority json, 3 个 scripts, 8 个测试文件（`grep -rln '1\.230\.0' tests/`：7 个 `test_toolkit_*` + `test_runner_material_authority.py`）, `tech-stack.md`, docs-site 中英
**Step 1（证伪）**: 跑现有断言 1.230.0 的测试，确认全绿（证明它们真的在断言）
**Step 2（实现）**: 逐处改 `2.0.0rc5+sodex.<n>`。**按 CLAUDE.md first-production V1 规则原地改，禁止加兼容别名或 predecessor parser**；`python_requires` 不动
**Step 3（证实）**: `make check-authority` exit 0；custos 自有文件 `grep -rn '1\.230\.0'` 只剩 `docs/authority/vendor/**`、历史 plan / marker / receipts 与 docs-site 历史记述
**Step 4（失败模式）**: 构造 `nautilus_version="1.230.0"` 的 ToolkitRc，断言 `ValueError`
**Step 5**: commit

#### Task 2b: 跨仓契约协调（❌ Blocked 直到对端交付）
**Files**: `docs/authority/vendor/crucible-runner-strategy-release-resolution-v1.golden.json`（Crucible 所有）；PS producer BOM；Crucible consumer receipt
**说明**: `engine_version` 是 PS（producer BOM）↔ custos（execution ABI）↔ Crucible（consumer receipt）三方 exact-byte 握手的字段。custos 改完 2a 后，Phase A 那条「producer asset commit / consumer code / receipt」链在对端重签前是断的。本 Task 只做三件事：(1) 在 `.forge/README.md` 索引行标 Blocks → PS / Crucible 并指名 owner；(2) 向对端提交精确的新值与 custos 侧 2a 的 commit；(3) 对端交付后把 vendored golden 换成 Crucible 重新提供的版本，跑 `make check-authority`。**custos 不得自行改写 vendored golden**（authority-docs.md「Never invent, vendor or pre-register downstream receipts」）
**Step 4**: 对端交付后 commit

### Slice B — adapter 层（13,705 行，可与 C 并行）

#### Task 3: import 路径拍平（40 符号对 / 38 文件）
**Files**: `packages/custos-strategy-toolkit-nautilus/src/**/adapter/**`
**手法约束（D6）**: AST/token 级替换器，每处要求整行唯一匹配（匹配数 ≠ 1 即拒绝，不猜），写前 `ast.parse`，改完**必须看 diff 而非只看 pytest**
**⚠ 判据失效更正（2026-09-12 实测）**: 原写的证伪点**不成立**。`adapter/__init__.py:7` 是一个大 `try:`，`:96` `except ImportError: pass`——在 NT 2.0 下包级 `import custos_toolkit_nautilus.adapter` **照常成功**，而 `orders` / `trading_config` / `strategy_core` / `indicators.supertrend` / `coordinators.execution` 逐个 import 全部 `ModuleNotFoundError`。`__all__` 仍列出那些并不存在的名字。用包级 import 当判据，Slice B 漏改几个文件也不会红。
**Step 1（证伪）**: 逐个 import 具体子模块（至少 `orders` / `trading_config` / `strategy_core` / `indicators.supertrend` / `coordinators.execution`）确认 `ModuleNotFoundError`；并断言 `__all__` 中每个名字当前**不可达**
**Step 2（实现）**: 执行替换；**顺手把 `__init__.py:7-97` 的静默 `except ImportError: pass` 处理掉**——它让「包能 import」与「内容可用」脱钩，是教训 #21 的形态
**Step 3（证实）**: `__all__` 中每个名字逐个可达（不是包能 import 就算）；`pytest --collect-only tests/toolkit` 收集数不低于改前（教训 C10 同批第二条：全绿不含「有没有在跑」）
**Step 4**: commit

#### Task 4: 5 个指标类去 Indicator 基类
**Files**: `adapter/indicators/{macd,adx,atr,rsi,supertrend}.py`
**Step 1（证伪）**: 断言 5 个类当前继承 `Indicator`
**Step 2（实现）**: 去基类，**逻辑一行不动**；确认各自暴露 `initialized` 与 `handle_bar`
**Step 3（证实）**: 经 `register_indicator_for_bars` 注册后能收到 bar 并更新
**Step 4（失败模式）**: 去掉 `initialized` 的变体须报错，不得静默不更新
**Step 5**: commit

#### Task 5: StrategyConfig 改 pyclass 子类（形态见 D5b）
**Files**: `adapter/trading_config.py:64`, `adapter/signal_processor.py:21`, `adapter/registry.py`, `adapter/trading_strategy.py`, `adapter/strategy_core.py`
**Step 1（证伪）**: `class NautilusTradingStrategyConfig(StrategyConfig, frozen=True)` 在 **import 时**抛 `TypeError`（pyclass 的 `__init_subclass__` 不接受 kwargs；失败在类定义，不在实例化）
**Step 2（实现）**: 按 D5b：普通子类 + kw-only `__init__` + `super().__init__()` + `__setattr__` 冻结守卫 + `__eq__`；顺手把 `:265,273` 对自有 `config_wrapper` 的 `getattr(..., None)` 收口到 `load_config` 边界（见「品味收尾项」）
**Step 3（证实）**: 构造 + registry 解析 + 策略装配走通；`msgspec.structs.asdict(config.position)` 等消费点不变
**Step 4（失败模式）**: (a) 漏 `super().__init__()` 的变体须给出可诊断错误；(b) `__init__` 完成后对任一 section 赋值必须抛错（冻结守卫是 live guard，不是注释）
**Step 5**: commit

#### Task 6: Instrument 类型注解
**Files**: `adapter/runtime_types.py:20`, `adapter/sizing.py:12,18`
**Step 1-4**: 改为 2.0 具体类型或 Protocol；`make toolkit-typecheck` 全绿（基线已由 Task 0 归零）
**Step 5**: commit

### Slice C — daemon engine host（2,091 行，红线所在，可与 B 并行）

#### Task 7 前置调查（2026-09-12 实测，Slice C 的实际起点）

**结论：这不是「换 API 名」，2.0 取消了 Python 侧对内部消息总线的访问。** custos 有两个桥接挂在
`node.kernel.msgbus` 上（`host.py:595`），都订阅 `events.order.*` 通配符：

| 桥接 | 订阅 | 承载 |
|---|---|---|
| `RunnerFactMessageBusBridge` | `events.order.*` + `events.position.*`（`runner_fact_producer.py:298-299`）| 签名 RunnerFact 发射，「对账不静默」|
| `OrderReservationBoundary` | `events.order.*`（`order_reservation_boundary.py:121`）| runner safety 边界，红线 0.2 / 0.3 |

2.0 的 `LiveNode` 只暴露 `environment` / `trader_id` / `instance_id` / `is_running` / `cache` /
`portfolio`（`live/__init__.pyi:358-371`）。逐条排除的替代：

- `add_stream_processor`——**不适用**。`crates/live/src/node/mod.rs:355-362` 写明它处理
  「supported typed **external** messages」的 inbound streaming，是外部入站通道而非内部事件总线。
- `with_external_msgbus_factory`——**不适用**，注入外部后端，不读内部流。
- 2.0 `Actor`——**无事件回调**。`crates/common/src/python/actor.rs` 只有 `subscribe_data` /
  `subscribe_signal` / `subscribe_instruments` 等数据订阅，order/position 事件回调 grep 零命中。

**迁移路径（owner 2026-09-12 定案：由 Strategy 回调转发）**：2.0 把事件分发移到 Strategy 的
typed 回调，`trading/__init__.pyi:442-463` 提供 `on_order_event` / `on_order_submitted` /
`on_order_rejected` / `on_order_accepted` … 与 `on_position_event` / `on_position_opened` /
`on_position_changed` / `on_position_closed`，覆盖两个桥接所需的全部事件。

**必须由 host 强制，不能靠策略自觉**：`host.py:405-406` 的策略来自签名 artifact
（`create_strategy()` 或 `artifact.strategy`），**全仓 `issubclass` grep 零命中**——当前没有任何
基类约束。若转发只写在 toolkit 基类里，一个不继承它的 artifact 就会让两条红线静默失效。因此
host 必须在 `add_strategy` 前校验策略具备转发契约，否则拒绝（教训 #22：多层 fail-fast，不靠
任一层自觉）。

**类型映射（实测）**：

| 1.x | 2.0 | 锚点 |
|---|---|---|
| `TradingNodeConfig(**kwargs)` | `LiveNode.builder(name, trader_id, environment)` + `.with_*()` 链 | `live/__init__.pyi:374` |
| `TradingNode(config=...)` + `node.build()` | `builder.build()` | `live/__init__.pyi:445` |
| `node.add_data_client_factory(venue, f)` | `builder.add_data_client(name, factory, config, routing)` | `live/__init__.pyi:425` |
| `node.add_exec_client_factory(venue, f)` | `builder.add_exec_client(...)` / `add_simulated_exec_client(...)` | `live/__init__.pyi:431,440` |
| `node.trader.add_strategy(s)` | `node.add_strategy(s)` | `live/__init__.pyi:385` |
| `node.kernel.msgbus` | **无对应**，见上 | — |
| `node.stop_async()` | `node.stop()` / `handle()` | `live/__init__.pyi:382` |
| `LoggingConfig` | `common.LoggerConfig` | `common/__init__.pyi:184` |
| `LiveExecEngineConfig` | `LiveExecutionEngineConfig` | `live/__init__.pyi:124` |
| `SandboxLiveExecClientFactory` | `SandboxExecutionClientFactory` | `adapters/sandbox/__init__.pyi:96` |

**Environment 映射（2026-09-12 定，有实证）**：`Environment` 只有 `BACKTEST` / `SANDBOX` / `LIVE`
（`common/__init__.pyi:1668-1671`），而 custos 有 sandbox / testnet / live。区分点不是「是不是测试」，
而是**撮合在本地还是在对端**：

| custos trading_mode | 2.0 Environment | exec client 注册方式 |
|---|---|---|
| `sandbox`（本地撮合）| `Environment.SANDBOX` | `builder.add_simulated_exec_client(...)`（收 `SimulatedExecutionClientFactory`，`crates/live/src/node/builder.rs:527-532`）|
| `testnet`（连测试网端点）| `Environment.LIVE` | `builder.add_exec_client(...)`，测试网由适配器自身的 environment 参数表达 |
| `live` | `Environment.LIVE` | `builder.add_exec_client(...)` |

依据：fork 示例连真实交易所一律 `Environment.LIVE`，**包括**适配器级 sandbox 的那个
（`examples/live/architect_ax/exec_tester.py:67` 用 `Environment.LIVE` 而 `:82` 是
`AxDataClientConfig(environment=AxEnvironment.SANDBOX)`）；`Environment::Sandbox` 在 Rust 侧
（`crates/live/src/node/mod.rs:5749+`）用于本地模拟。所以 custos 现有的
`SandboxLiveExecClientFactory` 路径对应 `SANDBOX` + simulated 注册，Binance testnet 走 `LIVE`。

**Task 7 因此拆为 7a / 7b**：7a = LiveNode API 迁移 + 桥接改回调转发 + host admission 强制
（让 host 能跑，可由 `tests/test_nt_trading_node_host.py` 713 行等验证）；7b = plan 原列的品味
重构（spec 归一 typed 视图 + NT 能力收成 typed adapter，27 处 `getattr` 收口）。

#### Task 7a 实施期补充调查（2026-09-12，fork `3fe857a351` 实读）

上面那段前置调查停在「msgbus 没了、改走 Strategy 回调」。实施期再往下扫一层，发现的不是
API 名而是**语义与能力边界**的五处变化。四维 Foundation Scan 里这属于影响面维（教训 #33b）：
第一层是符号路径，第二层是 msgbus 不存在，第三层（本段）是替代路径自身带着 1.x 没有的条件。

**F1 — 一个 event loop 只能跑一个 LiveNode（架构级，需 owner 决策）**

`crates/live/src/python/node.rs:989` 在 `run_async` 里检查 thread-local `HOSTED_RUN_ACTIVE`
（`:216` 定义），命中即抛 `another LiveNode is already running on this event loop; run one
concurrent LiveNode per process`。`:214-215` 给的理由是硬的：「the runner binds its senders
into thread-local storage and the msgbus is thread-local, so two interleaved hosted nodes would
cross-wire each other's events rather than fail」——不是保守限制，是会串线。

而 custos host 是按多 instance 建的：`_active_nodes` / `_lifecycle_authorities` /
`_runner_fact_contexts` 都是 `dict[instance_id, …]`，`_claim_execution_account_partition`
（`host.py:351`）专门防同一 credential scope 上出现第二个 testnet/live instance——这个守卫的
存在本身就说明「同一 runner 上多个 instance」是设计内的。

本 Task 采取 **fail closed**：deploy 第二个 instance 时在 host 内明确拒绝，理由指名 NT 2.0 的
per-loop 约束，而不是让它走到 `run_async` 去撞一个 NT 的通用错误、更不是让两个 node 串线。
**这是能力缩减，需 owner 定去向**：(a) 维持单 instance（本 Task 的选择）；(b) 每 instance 一个
线程 + 独立 loop；(c) 每 instance 一个进程（NT 自己的建议）。(b)/(c) 都是独立 plan 的量。

**F2 — 事件回调受 `ComponentState::Running` 门控（红线兑现范围）**

`crates/trading/src/strategy/mod.rs:1356`（order）与 `:1468`（position）在分发前一律
`if state != ComponentState::Running { return; }`，`:1354-1355` 的注释写明用意：「Events are
logged unconditionally so residual events received after stop remain observable, but dispatch is
gated on the running state.」

1.x 的 `msgbus.subscribe("events.order.*", …)` 没有这个门，订阅到 dispose 前一直有效。所以
迁移后两个红线桥接的可见窗口从「订阅期间」收窄为「策略 Running 期间」。已核实的两侧边界：

- **停止侧安全**：`host.py:737` `_apply_shutdown_policy` 在 `node.stop_async()` **之前**跑完，
  flatten / preserve 的撤单与平仓都发生在策略仍 Running 时。另外 2.0 的 Strategy **没有
  `pause`**（`.venv` 的 `trading/__init__.pyi:472-` 有 `stop`/`resume`/`degrade`/`fault`，无
  `pause`），所以 `_apply_shutdown_policy` 里 `getattr(strategy, "pause", None)` 那条 fallback
  在 2.0 下恒为 None——它不会把策略提前踢出 Running。
- **残留侧有缺口**：`stop` 之后交易所迟到的终态回报（cancel ack / 迟到 fill）不再进回调。1.x
  下 msgbus 仍会送达。影响：RunnerFact 少一条迟到事实、reservation 少一次释放。
- **启动侧影响小**：reconciliation 期间的事件不属于本进程初始化的订单，`_owned_order_ids`
  （`runner_fact_producer.py:293`）与 `_has_reservation` 本来就会丢弃它们。

close-out 的红线表必须按这个窗口如实写，不得承袭红线名（教训 #40）。

**F3 — Python 回调抛的异常被 Rust 侧丢弃**

`crates/trading/src/python/strategy.rs:973` 是 `let _ = self.dispatch_on_order_event(event);`，
position 侧 `:1045` 同形。也就是说桥接里 `raise` 传不回 NT，什么都不会停。1.x 下 msgbus 回调
的异常同样不保证传播，但当时桥接是唯一订阅者；现在转发器夹在 NT 与桥接之间，**必须自己**把
失败变成可见信号，否则「对账不静默」在 2.0 下自动降级为静默。

**F4 — `subscribe_topic` 不是替代路径（补前置调查的第四条排除）**

前置调查排除了 `add_stream_processor` / `with_external_msgbus_factory` / 2.0 `Actor`，漏了
Strategy 自己的 `subscribe_topic`（`.venv` `trading/__init__.pyi:974`，看起来最像 msgbus 的
替代）。它收不到 order/position 事件，路由表是两张：

- `subscribe_topic` → `crates/common/src/python/msgbus.rs:842` → `msgbus_api::subscribe_any`
  （`crates/common/src/msgbus/api.rs:243`），写进 `MessageBus.topics` / `.subscriptions`
- order/position 事件 → `api.rs:1172 publish_order_event` / `:1184 publish_position_event`
  → `:1285 publish_typed`，只填 typed router（`bus.router_order_events`）与 tls handler buffer，
  **不查** `bus.topics`

`BusTap`（`crates/common/src/msgbus/mod.rs:230`）能看到全部 publish 且早于组件状态门，但它是
Rust-only（`on_publish(topic, &dyn Any)`），没有 Python 面，event_store 自用。

**F6 — 连接状态查询面在 Python 侧整个消失（红线 0.3 的输入）**

`host.py:1232-1233` 的 `check_engine_connected` 读 `node.kernel.data_engine.check_connected()`
与 `.exec_engine.check_connected()`，供两处消费：`ConnectivityState` → `zombie_watchdog`，以及
readiness 的 `data_connectivity_ready` / `execution_connectivity_ready` 两个字段。

2.0 里这两个方法在 Rust 侧仍在（`node/mod.rs:825-826` 的启动超时诊断、`:790-791` 的停机诊断
都在用），但**没有任何 Python 面**：`grep 'pyo3(name = "..." )'` 全 crates 对
`is_connected` / `check_connected` / `connected` 零命中，`LiveNodeHandle` 只有
`is_stopping` / `is_running` / `state` / `stop()`（`live/__init__.pyi:446-453`）。而且 Rust 侧
自己也只在启动与停机两端调它，**运行期不轮询**。

后果分两段，不可混为一谈：

- **启动判定不受损**。进入 `NodeState::Running` 之前 NT 已经跑完 `connect_exec_phase`
  （`node/mod.rs:2114`）与 `await_engines_connected`，所以「已 Running」蕴含「启动时连上了」。
  readiness 的两个字段可以由 `handle().state == NodeState.RUNNING` 共同回答，语义不假。
- **运行期断连检测能力丢失**。1.x 可以在任意时刻问「现在还连着吗」，2.0 问不到。zombie
  watchdog 的输入因此退化为「节点还在跑吗」。红线 0.3 的 close-out 必须按这个如实写。

两条出路，owner 2026-09-12 选 **(a)**：本 Task 用 `handle().state` 顶上并显式降级声明。未选的 (b) 是给公会 fork 加
两个只读 getter 暴露 `check_connected`——fork 是公会自有的，改动本身很小，但它会推进 sha、
牵动 Task 1a 已钉的 `3fe857a351` 与一轮重编译，且是「往 fork 加自有 API」的先例。

**F5 — `kernel` 整体消失，不止 msgbus**

前置调查的映射表只列了 `node.kernel.msgbus` 无对应。实际 host 用到 `node.kernel` 的
`cache` / `trader` / `portfolio` / `exec_engine` / `loop` / `executor` / `dispose` 七处，2.0 的
`LiveNode` 一个 `kernel` 属性都没有（`.venv` `live/__init__.pyi:358-399`）。逐条：

| 1.x | 2.0 | 依据 |
|---|---|---|
| `node.kernel.cache` / `.portfolio` | `node.cache` / `node.portfolio`，**必须在 `run_async()` 之前捕获** | `python/node.rs:798` `node_consumed_err`：run_async 把 node move 进 awaitable，之后取属性抛「use the `cache` and `portfolio` captured before the run」 |
| `node.kernel.trader.strategies()` | 无。host 自己持有它 add 进去的策略 | `live/__init__.pyi:385` 只有 `add_strategy` |
| `node.kernel.trader.is_running` | `node.handle().state == NodeState.RUNNING` | `node/mod.rs:2153 finish_startup_trader` 在 trader 起来后才 `try_set_running`，与 1.x「started trader 即 reconciliation 通过的收据」同义 |
| `node.kernel.exec_engine.reconciliation` | 无读回面（只有 `builder.with_reconciliation()` 写入） | `live/__init__.pyi:412` |
| `node.kernel.loop` + `add_signal_handler` | **不再需要** | Python `run_async` 固定 `NodeRunMode::Hosted`（`python/node.rs:549`），`node/mod.rs:1461` 「A hosted node never installs signal handlers」。`_restore_runner_signal_ownership`（`host.py:527`）的存在理由消失 |
| `node.kernel.dispose` + executor 保护 | `node.dispose()` | 2.0 `dispose` 只做 `close_external_ingress` + `kernel.dispose` + `handle.set_stopped`（`node/mod.rs:562-566`），不碰 Python 的 asyncio loop，所以 `_dispose_node_preserving_runner_loop`（`host.py:763`）那段「NT 会 cancel loop 上所有 task 并 loop.stop()」的理由也消失 |
| `node.stop_async()` | `node.handle().stop()` → await run task → `node.dispose()` | `python/node.rs:897`：handle 在 run_async 期间仍有效，是 hosted run 的受支持停法 |

#### Task 7: TradingNode → LiveNode
**Files**: `src/custos/engines/nautilus/host.py`
**Step 1（证伪）**: `import nautilus_trader.live.node` 报 ImportError
**Step 2（实现）**: 换 `LiveNode.build()` / `LiveNode.builder()`；`TradingNodeConfig` / `LoggingConfig` / `LiveExecEngineConfig` 换 2.0 对应物。**手法约束（品味 ⬆️）**：`host.py` 1,403 行、`deploy` 142 行、`_build_runner_fact_context` 95 行的根因是 `DeploymentSpec` 以裸 `dict` 贯穿 host、NT 对象靠 27 处 `getattr` 逐点校验。重写前先 (1) 在 host 入口把 spec 归一为一个 frozen 的 typed 视图，两个长函数改为直取字段；(2) NT 节点的能力（loop / kernel.dispose / executor / trader.strategies / cache）收成一个 typed adapter，`getattr` 收口为一处。判据是「`RuntimeError("… lost its …")` 类重复校验消失」，**不是文件变短；禁按行数拆 host**。`runner_safety.py:391-394` 那条「NT 1.230.0 injects Sandbox's portfolio argument by factory class name」的 workaround 在 2.0 下须重新实证：仍成立则改注释的版本号，不成立则删
**Step 3（证实）**: sandbox 模式起停一轮
**Step 4（失败模式）**: 启动失败须传播，不留半启动状态
**Step 5**: commit

#### Task 8: SoDEX 接入与按 mode 的 G6 白名单
**Files**: `src/custos/engines/nautilus/venue_sodex.py`(新), `host.py:92,211,349`, `tests/test_nt_binance_venue.py`
**Step 1（证伪）**: 请求 SoDEX venue 在任一 mode 都被 `_SUPPORTED_VENUES` 拒
**Step 2（实现）**: `_SUPPORTED_VENUES` 改为按 mode 的三个集合，`supports_venue(venue, mode)`，`:211` / `:349` 两个调用点随签名改；新增 `venue_sodex.py` 装配 `SodexDataClientFactory` / `SodexExecutionClientFactory`（fork `adapters/sodex/__init__.pyi:49,77`），只做 sandbox / testnet 的 exec config；SoDEX 只加入 `sandbox` 与 `testnet` 集合，`live` 集合不动。**本 Task 不复制 `_LIVE_MIN_APPROVERS` / owner evidence——那是 live 交付的内容，本 plan 不做**；drift-guard 由 `test_nt_binance_venue.py` 泛化改名为 `test_nt_venue_wiring.py`，断言三个集合分别等于该 mode 已接线 connector 的并集
**Step 3（证实）**: SoDEX sandbox / testnet 可装配并起停一轮
**Step 4（失败模式 · 红线 0.2）**: (a) SoDEX 请求 **live** 在 host gate 被拒，且拒绝理由是「不在 live 集合」而非凭据缺失；(b) `NoopHost` 下 live 必须拒；(c) Binance 的 live 集合与改前逐字相等（不因本次改动缩小）。**这三条是红线测试，不得跳过**
**Step 5**: commit

#### Task 9 前置调查（2026-09-12，fork `3fe857a351` + 已装 2.0 实读）

Task 7a 收尾时只知道「guarded exec factory 建在 1.x 基类上，Task 9 要重建」。往下查发现
**重建在原位置是不可能的**，红线 0.2 的承载点必须换地方。

**G1 — 执行客户端层在 2.0 对 Python 是关闭的**

`runner_safety.py:276` 的 `GuardedLiveExecutionClient(LiveExecutionClient)` 与 `:370` 的
`guarded_exec_client_factory(upstream_factory: type[LiveExecClientFactory])` 依赖两个 1.x 基类
（`:11-12` 的 import）。2.0 两个都没有：

- `nautilus_trader.execution` 只导出 fill / fee model，`grep -c 'class.*ExecutionClient\b'`
  在它的 `.pyi` 上是 **0**。全 `.pyi` 里唯一带这个名字的是 `live.ExecutionClientConfig`。
- 更硬的一层：`builder.add_exec_client(name, factory, config)` 不接受任意 Python 对象。
  `crates/system/src/python/registry.rs:175` 的 `extract_exec_factory` 拿 `factory.name()` 去一张
  Rust 注册表里查 extractor，查不到就 `:190` `No execution factory extractor registered for
  '{factory_name}'`。只有 Rust 侧登记过的 adapter（binance / sandbox / sodex …）在表里。

所以「包一层 upstream factory」这个手法在 2.0 无处落脚，不是改几个符号的事。

**G2 — 唯一可用的拦截点是 Strategy 的下单方法，覆盖面因此要单独论证**

toolkit 的下单全部经 `s.submit_order(order)`（`packages/**/adapter/` 下 grep 实测 10 处，分布在
`strategy_core.py` 与 `coordinators/{signal_execution,execution,sltp}.py`），所以策略主动下的单
可以 100% 覆盖 —— 手法与 Task 7a 的事件转发同源：host 在实例上装包装并验证装上了。

**绕过路径有三条，都可以被 host 关掉或看见**，这是它能算数的前提：

| 绕过 | 触发条件 | 是否可拦 |
|---|---|---|
| order manager 代下单（contingent / emulated）| `StrategyConfig.manage_contingent_orders` 等三个开关；命中后走 `strategy/mod.rs:1410 dispatch_manager_actions` → `:1420 SubmitToEmulator` / `:1423 SubmitToRisk`，不经 Python | 三个开关在 `trading/__init__.pyi:988` 起**默认全 `False`**，admission 校验即可 |
| 单笔带 emulation / exec-algorithm | `order.emulation_trigger` / `order.exec_algorithm_id` | 这两条仍经 `submit_order_native`，包装看得见，拒绝即可 |
| `market_exit()` | 策略主动调；`strategy/mod.rs:1692` 置 `is_exiting` 后由 NT 代平仓 | Strategy 的公开方法，toolkit grep 零命中，包装或 admission 可处理 |

**位置本身是弱化**：1.x 守在 exec client（最靠近 venue，任何来源的命令都过），2.0 守在 strategy
（最靠近来源，只有经过它的命令才过）。上面三条堵上之后覆盖面可论证，但「堵上」是一组前提，
不再是结构上的必然。

**G3 — 拒绝的语义变了，而且这次是变干净了；代价在别处**

`crates/trading/src/strategy/mod.rs:2318 submit_order_native` 里，`:2364` 才
`cache.add_order(...)`、`:2367` 才 `publish_order_initialized(order)`。也就是说**在 Python 层拦住
不调 `submit_order`，订单从未进 cache、从未发出任何事件**——它只是一个被丢弃的 Python 对象。
1.x 需要补一个 `generate_order_rejected` 让已经在 cache 里的订单闭合，2.0 不需要，也没法：
Rust 的 `Strategy::deny_order`（`:2044`）没有 pyo3 暴露，exec client 的 `generate_order_*` 随 G1
一起没了。

**代价是拒绝不再产生任何 NT 事件**。1.x 的 OrderRejected 会经 RunnerFact 桥接落进签名事实流；
2.0 拦在这里，NT 什么都不会说。所以「对账不静默」要求 custos 在拒绝时**自己**发 RunnerFact，
否则每一次守卫生效都是静默的——守住了资金，丢掉了记录。

#### Task 9: venue_binance / runner_safety 适配 + 红线 0.3/0.4 回归
**Files**: `venue_binance.py`, `runner_safety.py`
**Step 1-3**: Binance adapter 符号路径 + `PriceType` 适配
**Step 4（失败模式）**: 云端失联时本地 breaker 与 `max_notional_per_runner` cap 仍生效（0.3）；2.0 `Price`/`Quantity` 参与 money 路径无 `float()`（0.4，按 `verification.md` §红线专项检查跑 grep）
**Step 5**: commit

### Slice D — custos 测试面（69 文件 16,030 行，依赖 B + C）

#### Task 10: testkit 迁移与 stubs 替代 fixture
**Files**: `tests/support/nt_stubs.py`(新), 涉及 `test_kit` 的测试
**Step 1（证伪）**: `from nautilus_trader.test_kit.stubs.data import TestDataStubs` 报 ImportError
**Step 2（实现）**: `test_kit.providers` → `testkit.providers`；手写 4 个 stubs（commands / component / data / execution）的替代 fixture；`model.currencies|USDT` → `Currency.from_str("USDT")`；`TestClock` / `InstrumentProvider` 另寻 2.0 对应物
**Step 3（证实）**: `pytest --collect-only tests/` 收集数不低于改前，且**逐文件比对**——被 skip 或 uncollectable 的文件须点名报告，不得静默豁免
**Step 4**: commit

#### Task 11: 其余测试适配与基线复核
**Step 1-3**: 剩余路径拍平；`make test-baseline` 通过
**Step 4**: `make toolkit-typecheck` **全绿**（Task 0 已归零，任何非零都是本 plan 引入的）
**Step 5**: commit

### Slice E — PS 策略本体（8 文件，依赖 Slice A + Task 5）

#### Task 12: PS 策略 import 拍平
**Files**: PS `{trend,momentum,portfolio,_template,fixtures}/**/refinement/nautilus/*.py`
**Step 1（证伪）**: 策略 import 失败
**Step 2（实现）**: 10 个符号对路径拍平（零重写）；三个 Config 子类按 D5b 定稿形态改（去 `frozen=True`，kw-only `__init__` + `super().__init__()`），不得另创形态；PS `pyproject.toml:21` NT pin 改 fork wheel（命令式安装，PS 规则禁 path 源）
**Step 3（证实）**: 4 个策略均可 import 且经 registry 解析
**Step 4**: commit（**PS 仓独立 commit**）

#### Task 13: PS 测试与 `make verify`
**Step 1-3**: PS `tests/` 25 文件适配；PS `make verify` 达到其自身基线
**Step 4**: commit

### Slice F — 收尾

#### Task 14: 文档收尾(close-out)
1. 本 plan 顶部 `Status: ⏳ → ✅ Completed` + `Completed: YYYY-MM-DD`
2. `.forge/README.md` 索引状态 `⏳ → ✅`
3. `.claude/rules/verification.md` 追加 SoDEX 相关红线 grep 与 3.13 相关验证
4. **填写「红线 gate 满足度」表**——逐条区分 code 覆盖与 runtime 接线，defer 项显式标注（教训 #40）
5. **完成报告章节**，含 `### 功能验证（主路径）` 子段（操作者怎么试一遍 SoDEX sandbox 起停）
6. **登记遗留项**：Task 1b 若因 fork 未打 tag 而未完成，plan **不得 close-out**（1a 的 `nt-builder` 是过渡方案）；Task 2b 若对端仍未交付，遗留项写明阻塞方与 owner；D5 的 4 个指标薄包装未收敛到引擎原生；SoDEX live 未交付（D4 假设）
7. `git add` + `git commit -m "docs(custos): mark plan 01 as completed"`

## 品味收尾项（顺手，不另起 plan）

来源 `.forge/reviews/2026-09/taste-plan-01-nautilus-upgrade-scope.md`。只列本 plan 本来就要改的文件；「不碰」的也写明，不留给执行者临场决定。

| 归属 | 位置 | 做什么 |
|---|---|---|
| Task 4 | `adapter/indicators/macd.py:1` vs `:12` | module / class docstring 去重，留一处（其余 4 个指标文件同型即同处理） |
| Task 4 | `adapter/indicators/supertrend.py:203-215` | 列名前缀搜索与 `:22` `_supertrend_column_names()` 二选一：信任精确构造并在不匹配时 `raise`（vendored pandas-ta 版本固定） |
| Task 5 | `adapter/trading_config.py:265,273` | 对自有 `config_wrapper` 的 `getattr(..., None)` / `hasattr` 收口到 `load_config` 边界，此处直取 |
| Task 8 | `tests/test_nt_venue_wiring.py`（原 `test_nt_binance_venue.py`）| ✅ 两块装饰分隔线去掉，叙述移入 `test_testnet_pins_the_declared_leverage` 与 `test_a_supported_key_type_is_accepted` 的 docstring |
| Task 9 | `runner_safety.py:67-73,109-113` | money 路径上的 `getattr(...) or ...` 静默默认链改为 typed 读取器，字段缺失即 `raise`；Step 1 先逐字段实证 2.0 的 command / order 对象形状 |
| Task 9 | `runner_safety.py:203-213` `modify_order` | 补 `_log.warning("runner_order_modification_rejected", reason_code=..., error_type=...)`，与 submit 路径对称（教训 #21） |
| Task 7 | `host.py` | 见 Task 7 Step 2 手法约束（⬆️ 上界） |
| 不碰 | `adapter/indicators/*.py` 的 `val` / `result` | 作用域 5 行内，不值得单独动 |

## 验证清单 (Verification)

- [ ] `make check-authority`: exit 0
- [ ] `make test-baseline`: PASS
- [ ] `make toolkit-typecheck`: 全绿（Task 0 归零后的基线）
- [ ] `pytest --collect-only` 收集数不低于改前，skip/uncollectable 文件逐个点名
- [ ] 红线专项 grep（`verification.md` §红线专项检查）全过
- [ ] 失败模式覆盖契约 10 条逐条有测试
- [ ] 所有引用的前置契约均有 `file:line` 证据锚（Step 1.5 gate）
- [ ] 无死代码：custos 自有文件 `1.230.0` 残留归零（docs-site 历史记述与 `.forge/` 历史产物除外）；`docs/authority/vendor/**` 与 PS / Crucible 侧列为 Task 2b Blocked 并指名 owner，**不得自行改写**
- [ ] `uv.lock` 不含 path 源；`docker/runtime-requirements.lock` 中 fork wheel 带 sha256；3.11 base `uv sync` 成功
- [ ] PS 侧 `make verify` 达到其自身基线

## 进度追踪 (Progress)

| Task | Status | Completed | Notes |
|---|---|---|---|
| 0 | ✅ | 2026-09-11 | `2bf09e8` + `7971bc8`；mypy 4→0、fmt 8→0、lint 2→0，`make verify` exit 0 |
| 1a-1 | ✅ | 2026-09-12 | `2010309`；git 源钉 `3fe857a351`，NT 2.0.0rc5+sodex.1 |
| 1a-2 | ❌ 撤销 | 2026-09-13 | **撤销而非阻塞**（状态表例无此标记，故加字说明）。`nt-builder` 已实现并跑到最后一个 crate，因 `release.yml:155` 的两平台构建装不下而放弃，详见偏离日志。**已落地的 runtime lock 部分保留为过渡**（`--no-emit-package` + 重生成，`make check-runtime-lock` 由红转绿、`make dist` 解锁），随 1b 一并撤 |
| 1b | ✅ | 2026-09-14 | Release `guild-v2.0.0rc5+sodex.1`从fork `3fe857a351`发布三平台wheel；三份摘要分别为`0a06c389…` / `d1e56ae7…` / `b5d0f1e4…`。Custos改为三个互斥marker URL source，`uv.lock`与runtime lock逐资产绑定SHA-256，git-source builder/Make target/parser/tests退役；官方ARM64 Docker runtime 23/23通过。PS `ae04bfee4bbf88486116ef879af9f5fa1e56d316`同步三wheel并完成998 passed / 18 skipped全量验证 |
| 2a | ⏳ | 2026-09-14 | Custos自有Literal、toolkit RC dependency policy、release-readiness与当前authority文档已切`2.0.0rc5+sodex.1`；多平台URL lock parser按同版本、唯一marker/source聚合三份hash。等待2b的Crucible owner golden/receipt后再生成最终schema/golden并翻✅ |
| 2b | ❌ | | Blocked：PS / Crucible 重签 |
| 3 | ✅ | 2026-09-12 | `9fecab3`；37 文件 AST 拍平；判据改为 `__all__` 逐名可达 |
| 4 | ✅ | 2026-09-12 | `261dc7a`；5 指标去基类 + docstring 去重 |
| 5 | ✅ | 2026-09-12 | `8c77d5f`；pyclass 子类 + 冻结守卫 + 移除静默 except |
| 6 | ✅ | 2026-09-12 | `57c3a8b`；Instrument 改 Protocol，解锁 28→41 |
| 7a | ✅ | 2026-09-12 | `65b7f1c` venue → `7506447` forwarder → `83dc8cb` host。含 venue_binance 提前迁移（host 的前置）|
| 7b | ✅ | 2026-09-12 | `c34ffd0`；spec 归一为 `_DeploymentIdentity`；venue 从硬编码改为按 connector 派生（signed facts 曾对 SoDEX 谎报 BINANCE）；新增 NT-free `venues.py` 单一表；client order id 上限改为按 venue；toolkit instrument id 三副本收口；toolkit mypy 84→19。typed adapter 一项经 grep 实证已由 7a 兑现 |
| 8 | ✅ | 2026-09-12 | `00d0e74`；白名单按 mode 拆三集合 + `venue_sodex.py` + host 按 connector 分派；drift-guard 泛化为 `test_nt_venue_wiring.py`（41 条）；红线三条 + SoDEX venue 16 条全部扰动验过 |
| 9 | ✅ | 2026-09-12 | `781ccdd`；执行门迁到 strategy 边缘 + 拒单进签名事实流 + money 路径去 or-chain；红线 grep 四条全过 |
| 10 | ✅ | 2026-09-12 | `3dd7ff6`；27 测试文件拍平 + TestClock→`Clock.new_test()`；22 文件转绿、0 新红 |
| 11 | ✅ | 2026-09-12 | `90eda67`→`fc51c28`；全量 `54 failed/25 err` → `7 failed/3 err`，剩余 4 文件全归 Task 2a（3 个断言 `1.230.0`）与 close-out 计数 |
| 12 | ✅ | 2026-09-12 | PS `ef41c0c` + custos `c8ebc92`；9 个策略模块拍平 + 9 个 config 子类改 D5b 形态 + NT pin 改 fork git 引用；**7 个策略**在 PS 自己的环境（NT 2.0.0rc5+sodex.1）下 import、经 registry 从各自 config.yaml 解析并构造成功 |
| 13 | ✅ | 2026-09-12 | PS `cab51b4`；收集从 9 文件报错→0，`make verify` 全绿（996 passed / 18 skipped）；退役 lane 具名跳过而非移植；另修两处「藏在绿色后面」的东西 |
| 14 | ⏳ | 2026-09-14 | 除「翻 ✅」外全部做完：红线 gate 满足度表、逐文件计数表（探针转绿）、阶段性报告含功能验证主路径、遗留项、`verification.md` 的 SoDEX 红线检查、索引条目。Task 1b已关闭；**Status保持⏳**直到2a/2b跨仓engine-version契约协调完成 |

## 交接 (Handoff) — Slice C 接手说明

> 上一轮单会话做完 Task 0 / 1a-1 / Slice B 后在此交接。plan 顶部写明「按 Slice 派工，不要单
> session 硬推」（教训 #31），而 Slice C 是四条红线所在，故在此断开。本段是接手者的入口。

### 起点

先完整读本文件，重点是 **「Task 7 前置调查（2026-09-12 实测，Slice C 的实际起点）」**——
Slice C 的调查已经做完并落盘（`619e19a` / `b2edde5`），那段是完整输入，**不需要重查**：
2.0 取消 Python 侧 msgbus 访问的实证、三条替代路径的逐条排除、由 Strategy 回调转发的迁移
方案、完整类型映射表、Environment 映射及其依据。

### 交接时的状态

- Slice B 收尾于 `8c77d5f`；调查落盘于 `619e19a`、Environment 映射于 `b2edde5`
- 依赖已切到公会 fork：`nautilus_trader 2.0.0rc5+sodex.1`，uv git 源钉 `3fe857a351`
- adapter **59/59 可 import**，`__all__` 75 名全可达
- ~~**`make verify` 在主干 exit 0** —— 这是新基线，此后任何红都是新引入的~~ **这句不成立**，见下方「接手点实测基线」
- 中间态：`tests/toolkit` 有 16 个 collection error，全部是测试文件自身的 1.x 路径，归
  Slice D，**不是 Slice C 的责任**（见「中间态基线」段的分栏表）

### 接手点实测基线（2026-09-12，Task 7a 接手时在 `f790169` 上跑）

上面划掉的那句是交接时写的，**实测推翻**。在 `f790169` 上 `uv run pytest tests/
--continue-on-collection-errors`：

```
55 failed, 2046 passed, 41 skipped, 1 xfailed, 29 errors
```

`make verify` 的 `test-baseline` 就是 `uv run pytest tests/`（`Makefile:62-64`），所以它在接手时
是红的，不是 exit 0。不带 `--continue-on-collection-errors` 时更红：收集期即 Interrupted。

**这不改变判据，只是把判据换成可用的那个**——「此后任何红都是新引入的」在一个本就红的基线上无法
执行。可执行的判据是**逐文件比对**：Task 7a 完成后同一命令得到

```
54 failed, 2132 passed, 41 skipped, 1 xfailed, 25 errors
```

失败文件集合 `comm` 对照的结果是：接手点红而现在绿的有 1 个（`test_portfolio_snapshot.py`），
现在红而接手点绿的有 **0 个**，两边都红的 12 个（全部是 `tests/toolkit/**` 的 1.x 路径与
`MovingAverageType.WILDER`，归 Slice D；以及断言 `1.230.0` 的两个文件，归 Task 2a）。

**给下一个接手者**：不要引用「基线是绿的」这类状态声明而不复核（生态教训 #49）。要判断自己有没有
弄坏东西，跑上面那条命令并对失败文件集合做 `comm` 比对，不要比对总数——总数会同时被修好的和新
引入的两边推动。

### 本次任务

**Task 7a**：LiveNode API 迁移 + 两个红线桥接改由 Strategy 回调转发 + host 侧 admission 强制。
（Task 7 已按规模拆为 7a / 7b；7b 是品味重构，不在本次范围。）

最要紧的一条，前置调查里写了、这里重复一遍：**转发必须由 host 强制校验，不能靠策略自觉**。
`host.py:405-406` 的策略来自签名 artifact，而全仓 `issubclass` grep 零命中——转发若只写在
toolkit 基类里，一个不继承它的 artifact 会让红线 0.2/0.3 与「对账不静默」静默失效。这是
教训 #22（多层 fail-fast，不靠任一层自觉）的直接适用。

验证手段：`tests/test_nt_trading_node_host.py`（713 行）、`tests/engines/nautilus/` 下多个，
其中两个真的构造 node。

### 本地操作陷阱（上一轮实测，不属于 plan 正文但会真的绊住人）

- `uv` 不在 PATH，命令前加 `export PATH=$HOME/.local/bin:$PATH`
- `uv sync` 会现场编译 fork 的 Rust 扩展；首次约十几分钟，之后按 sha 缓存。放后台跑、输出
  直写文件，**不要接管道**（管道会让退出码变成 `tail` 的——教训 #50 续编二）
- `git checkout --` 被 guardrail 拦（拦得对）。还原单文件用 `git show HEAD:<path> > <path>`
- `tests/integration/runner_fact_publication_process.py` 被 `receipts/custos-runner-fact-local-publication-v1.json`
  按 sha256+size 钉住，已加进 ruff `extend-exclude`。**不要格式化它，也不要跑 `make fmt`**（那会扫全目录）
- fork 在活跃开发中，HEAD 会前进；lock 钉的是 `3fe857a351`，引用 fork 行号前先核对

### 纪律

- 按仓库 `.claude/rules/` 执行；每个 Task 走「证伪 → 实现 → 证实 → 失败模式」
- 批量改源码用 AST 级、整行唯一匹配、写前 `ast.parse`、改完看 diff（教训 C10）
- commit 前先 `git status --short` 核对 index（教训 #27）
- **断言的覆盖面不得大于实证的覆盖面**（教训 #46）——上一轮在这条上栽过一次：8 个待格式化
  文件只扰动验了 1 个就声称「全部可安全格式化」，第一次 `make verify` 即被 authority gate 打回
- plan 是活文档，实施中发现的偏离即时写进偏离日志并 commit

### Slice C 之后还剩什么

Task 8（SoDEX 接入 + 按 mode 的 G6 白名单）、Task 9（venue_binance / runner_safety 适配 +
红线 0.3/0.4 回归）、Slice D（测试面 69 文件）、Slice E（PS 策略 8 文件，10 个符号对纯路径
拍平、**零重写**）、Task 2a/2b（版本号；2a 已改排到 D 之后，因为它在 2b 交付前不可能绿）、
Task 1b（wheel 切换门）、Task 14（close-out）。

**Task 2b 仍是硬阻塞，且不是任何新会话能自己解决的**：`engine_version` 是 PS（producer BOM）
↔ custos（execution ABI）↔ Crucible（consumer receipt）三方 exact-byte 握手字段，vendored
golden 归 Crucible，custos 不得改（`authority-docs.md`「Never invent, vendor or pre-register
downstream receipts」）。这条需要推对端交付。

## 阶段性报告（Task 14 · 本 plan **未** close-out）

- **日期**: 2026-09-12
- **状态**: ⏳ In Progress。**Task 14 第 6 条明文禁止在此 close-out**，四个阻塞项逐条实证如下。
- **实施 commit 范围**: `2bf09e8`..`96e5a92`（47 个 commit）
- **进度**: 19 行进度表中 14 行 ✅；未完 1a-2 / 1b / 2a / 2b 与本条 14

### 为什么不能 close-out（逐条实证，不看表格）

| 阻塞项 | 判据 | 实测 |
|---|---|---|
| **Task 1b** 切换门 | fork 打 tag + CI 产 wheel 挂 Release + Task 8 通过 | fork 最新 tag 是 `v2.0.0rc4`，而本 plan 钉的 `3fe857a351` 是 `git describe` 下的 `v1.222.0-4149-g3fe857a351`——**无 tag 可达**；且 `v2.0.0rc3` / `v2.0.0rc4` 内 `crates/adapters/sodex` 文件数**均为 0**，现有 tag 都不含我们要的适配器；`gh release list` 空。三条判据只有 Task 8 ✅ |
| **Task 1a-2** Docker 侧 | `nt-builder` + runtime lock + sha 对账 | `grep -c nt-builder Dockerfile` = **0** |
| **Task 2a** 版本号 | `engine_version` 常量归零 | `strategy_execution.py:140,197` 仍是 `Literal["1.230.0"]` |
| **Task 2b** 对端重签 | PS / Crucible 交付 exact-byte 收据 | ❌ Blocked，跨仓，非本仓可满足（C14）|

plan 自己写的理由是「1a 的 `nt-builder` 是过渡方案」——git 源加本地构建能跑，但它不是可复现的
发布形态，而 `custos-runner` 是要打镜像发出去的。在 fork 打 tag 之前把 plan 标完成，等于把一个
过渡态记成终态。

### 红线 gate 满足度（教训 #40：code 覆盖 ≠ runtime 接线）

| 红线 | code 覆盖 | runtime 接线 | defer / 缩减 |
|---|---|---|---|
| **0.1** Key/KEK 不出进程 | ✅ venue 层两侧都有：Binance 仅把凭据放进本地 NT config；SoDEX 四个字段全部显式必填，并有一条**把 `SODEX_*` 环境变量全设上**的测试证明不走适配器的 env fallback；`_sanitize_exception` 命中凭据关键词即整段脱敏；`test_the_testnet_exec_config_carries_the_credential_and_reports_it_present` 断言 repr 不泄漏。grep 四条全空 | ✅ 在 `deploy` 主路径上 | — |
| **0.2** G6 host gate 不绕过 | ✅ 白名单按 mode 拆三集合；admission 在 `engine_lifecycle.py:422` 判定；drift guard held 白名单与 venue 模块两侧；三条红线测试全部扰动验过会红 | ⚠️ **部分**：admission 已接线；但**执行门本身从 exec client 移到了 strategy 边缘**（Task 9，G1：2.0 对 Python 关闭了执行客户端层，`extract_exec_factory` 只认 Rust 侧登记的 adapter）。1.x 守在最靠近 venue 的地方、任何来源的命令都过；2.0 守在最靠近来源的地方、**只有经过 strategy 的命令才过**。覆盖面靠三条前提（三个 manage_* 开关默认 False + 单笔 emulation/exec-algorithm 可见可拒 + `market_exit` toolkit 零命中）论证，**不再是结构上的必然** | **SoDEX live 未交付**（D4 假设，owner 2026-09-11 确认）：`_LIVE_VENUES` 只有 binance 两个 connector，venue_sodex 的 live 构建器主动 `NotImplementedError` |
| **0.3** 失联≠停止 | ✅ grep 空（无 `stop_all_strategies` / `force_shutdown`）；本 plan 未改其逻辑 | ✅ `_daemon.py:418` 在生产路径构造 `FallbackBreaker(limits.breaker)` | — |
| **0.4** Money 用 Decimal | ✅ grep 空；Task 9 另把 `runner_safety` money 路径的 `getattr(...) or ...` 默认链改为逐级 `is not None` + 价格必须为正（`or` 会把 0 当缺失，而 0 价算出的 notional 通过任何 cap） | ✅ 在订单闸门路径上 | — |

**三条未被任何 gate 覆盖的事实**，写在这里以免被上表的绿色掩盖：

1. **没有任何订单到过 SoDEX。** Task 8 的装配与起停都在 `FakeLiveNode` 与本机真 `LiveNode.builder().build()` 上验证，**没有真机往返**。按 C11 的判据，一条通道的完成判据是「对端接受过」，不是本仓绿。
2. **策略在 2.0 下还不能启动。** `trading_strategy.on_start` 曾无条件读 `self.msgbus` / `self.id`（2.0 两者都不存在）；该阻塞已随 `EventPublisher` 退役解除（`3d44aed`），但解除之后**仍未在真节点上跑过一次 `on_start`**。
3. **SoDEX 尚无端到端策略路径。** toolkit 侧 instrument id 已按场所原生符号收口（`c34ffd0`），但 RunnerFact 的 signal / TCA 字段缺口需与 Crucible 协商（见偏离日志「RunnerFact 覆盖面缺口」条）。

### 功能验证（主路径）

操作者现在可以试的，就是「装配一个 SoDEX sandbox 部署并起停一轮」——**不含真机成交**：

1. `make install` 后确认引擎版本：
   `uv run python -c "import nautilus_trader; print(nautilus_trader.__version__)"` → `2.0.0rc5+sodex.1`
2. 确认这台 runner 声称能跑哪些 venue（红线 0.2 的能力面）：
   `uv run pytest tests/test_nautilus_host_capability.py tests/test_nt_venue_wiring.py -q`
   → SoDEX 在 sandbox / testnet 为 True、live 为 False；Binance 三个 mode 均 True
3. 装配一个 SoDEX sandbox 部署并起停：
   `uv run pytest tests/test_nt_trading_node_host.py -q -k sodex`
   → 数据客户端与模拟撮合客户端都注册在 `SODEX_PERPS` 下，`deploy` → `stop` 走完
4. 在 PS 侧确认策略能被解析出来：
   `cd ../../alchymia-labs/philosophers-stone && make verify`
   → 996 passed / 18 skipped（18 个跳过的理由都具名，11 个指向退役的 crucible lane）

### 遗留项

| 项 | 状态 | 阻塞方 / 解除条件 |
|---|---|---|
| Task 1a-2 Docker 侧（`nt-builder` + runtime lock + sha 对账）| 🔲 | 本仓可做，未做 |
| Task 1b 切换到 wheel | 🔲 | **fork 未打含 sodex 的 tag**、未产 Release wheel。owner: fork 维护方 |
| Task 2a 版本号常量 | 🔲 | 本仓可做，但须与 2b 同批（改早了 `check-authority` 必红，见偏离日志「Task 2a 排序」条）|
| Task 2b 对端重签 | ❌ | PS 与 Crucible 的 exact-byte 收据。owner: 两仓维护方（C14）|
| D5 的 4 个指标薄包装未收敛到引擎原生 | 🔲 | 本仓可做，非阻塞 |
| SoDEX live 未交付 | 🔲 | D4 假设，owner 2026-09-11 确认。要交付须另按 deviation-protocol 高风险审议并补齐 live 侧全部落实项与真机证据 |
| SoDEX / 策略的真机证据 | 🔲 | 需网络与真实凭据（C11 同类）|
| RunnerFact 的 signal / TCA 字段缺口 | 🔲 | 需与 **Crucible** 协商（不是 arx，见偏离日志）|
| toolkit mypy 16 errors | 🔲 | 本 plan 验证清单要求全绿；84→19→16，剩余 16 条已逐条列于偏离日志 |

## close-out 测试计数

本 plan 实施期（`2bf09e8~1..HEAD`，47 个 commit）改动过的全部 `tests/` 文件，条数取自一次
`pytest --collect-only`，非手写（`progress-management.md` §数字类声明必须来自实跑）。

四个计 0 的是真的没了，各有去向：`test_nautilus_source_pin.py` 随 Task 1b 退役——它读的是
git 源，而 1b 把引擎换成按 sha256 钉住的已发布 wheel，连同 `docker/nautilus-wheel.dockerfile`
与 `make nautilus-wheel` 一起撤，其职责由 `test_nautilus_wheel_sources.py` 接替；
`test_nautilus_runner_safety_adapter.py` 随 Task 9 删除
（它测的 guarded exec-client factory 在 2.0 没有位置，`781ccdd`）；`test_nt_binance_venue.py`
改名为 `test_nt_venue_wiring.py`（Task 8，`00d0e74`）；`test_event_publisher.py` 随
`EventPublisher` 退役（`3d44aed`）。

| 测试文件 | 条数 |
|---|---|
| `tests/cli/test_cli_engine_dispatch.py` | 4 |
| `tests/cli/test_runner_safety_daemon_composition.py` | 7 |
| `tests/core/test_engine_protocol_contract.py` | 3 |
| `tests/core/test_engine_protocol_tier2.py` | 9 |
| `tests/engines/nautilus/test_nautilus_config_extension.py` | 5 |
| `tests/engines/nautilus/test_readiness_checks_what_it_claims.py` | 14 |
| `tests/engines/nautilus/test_runner_safety_execution_boundary.py` | 33 |
| `tests/engines/nautilus/test_runner_safety_host_wiring.py` | 6 |
| `tests/engines/nautilus/test_sandbox_runner_fact_host.py` | 4 |
| `tests/engines/nautilus/test_state_snapshot_nautilus_impl.py` | 11 |
| `tests/engines/nautilus/test_strategy_event_forwarding.py` | 11 |
| `tests/test_cancel_still_reaches_the_venue.py` | 2 |
| `tests/test_client_order_id_length.py` | 5 |
| `tests/test_client_order_id_sandbox_execution.py` | 1 |
| `tests/test_credential_lifecycle.py` | 2 |
| `tests/test_engine_lifecycle.py` | 11 |
| `tests/test_flatten_records_what_it_contained.py` | 4 |
| `tests/test_main_host_selection.py` | 3 |
| `tests/test_nautilus_host_capability.py` | 11 |
| `tests/test_nt_sodex_venue.py` | 16 |
| `tests/test_nt_trading_node_host.py` | 40 |
| `tests/test_nt_venue_wiring.py` | 43 |
| `tests/test_plan_closeout_counts.py` | 20 |
| `tests/test_portfolio_snapshot.py` | 13 |
| `tests/test_pre_import_contract.py` | 6 |
| `tests/test_runtime_candidate_promotion.py` | 7 |
| `tests/test_runtime_candidate_promotion_workflow.py` | 4 |
| `tests/test_strategy_signal_bridge.py` | 8 |
| `tests/test_toolkit_inventory.py` | 5 |
| `tests/test_toolkit_zero_rewrite.py` | 5 |
| `tests/toolkit/test_base_strategy_filters.py` | 13 |
| `tests/toolkit/test_cancels_are_countable.py` | 11 |
| `tests/toolkit/test_capital_allocator.py` | 15 |
| `tests/toolkit/test_config_self_validation.py` | 32 |
| `tests/toolkit/test_every_close_path_shares_one_decision.py` | 13 |
| `tests/toolkit/test_execution_manager.py` | 10 |
| `tests/toolkit/test_filter_direction.py` | 8 |
| `tests/toolkit/test_fixed_risk_sizing.py` | 9 |
| `tests/toolkit/test_multi_pair_integration.py` | 8 |
| `tests/toolkit/test_native_trailing_mode.py` | 35 |
| `tests/toolkit/test_native_trailing_submitter.py` | 16 |
| `tests/toolkit/test_nautilus_filter_momentum.py` | 12 |
| `tests/toolkit/test_nautilus_filter_regime.py` | 10 |
| `tests/toolkit/test_nautilus_filter_volatility.py` | 10 |
| `tests/toolkit/test_nautilus_filter_volume.py` | 10 |
| `tests/toolkit/test_nautilus_startup_validator.py` | 7 |
| `tests/toolkit/test_nautilus_utils.py` | 26 |
| `tests/toolkit/test_order_side_regression.py` | 7 |
| `tests/toolkit/test_order_submitters.py` | 20 |
| `tests/toolkit/test_order_tracker_close_guard.py` | 15 |
| `tests/toolkit/test_pair_context.py` | 16 |
| `tests/toolkit/test_shutdown_position_policy.py` | 3 |
| `tests/toolkit/test_signal_execution_coordinator.py` | 14 |
| `tests/toolkit/test_strategy_core.py` | 12 |
| `tests/toolkit/test_tick_exit_close_position.py` | 4 |
| `tests/toolkit/test_trade_event_handler.py` | 12 |
| `tests/toolkit/test_trailing_behavioral_equivalence.py` | 4 |
| `tests/test_nautilus_wheel_sources.py` | 3 |
| `tests/test_docker_runtime_contract.py` | 20 |
| `tests/test_nautilus_source_pin.py` | 0 |  (已删除)
| `tests/test_nautilus_runner_safety_adapter.py` | 0 |  (已删除)
| `tests/test_nt_binance_venue.py` | 0 |  (已删除)
| `tests/toolkit/test_event_publisher.py` | 0 |  (已删除)

## 偏离与改进日志 (Deviations & Improvements)

| 类型 | 位置 | 描述 | 已批准 |
|---|---|---|---|
| DEV | `pyproject.toml` | **D1 改向：fork 发布 wheel，不用 path 源**。起草版接受「单仓 clone 装不起来」的代价，审查（`b0ff52a` C1/H1/H2）实证代价范围写窄：path 源还会击穿 3.11 base 承诺、`verify-base-clean` 与 Docker runtime lock，且 PS 规则禁 path 源。owner 2026-09-11 审查会话改向 wheel | ✅ owner（会话中口头，本 fix 落文）|
| DEV | `pyproject.toml` / `Dockerfile` | **D1 再改为两阶段**：fork 活跃期用 git 源（1a），稳定后切 wheel（1b，有触发判据、未完成不得 close-out）。Docker 链的 git 阶段红实测根因是 `uv export` 对 git 源无 `--hash` 而 `Dockerfile:22-27` `--require-hashes`；解法走 custos 自有 wheel 已有的 `--no-deps` 路径 + `nt-builder` + sha 对账，**不登记 known-red**。多出的 `Dockerfile` / `Makefile:96-103` / `test_docker_runtime_contract.py` 三处已进清单 | ✅ owner 2026-09-12 |
| DEV | `.python-version` / `tech-stack.md` | **D2 撤回**：「fork 仅 cp313 .so」是本机构建产物而非约束（`git ls-files` 0 条；fork `requires-python >=3.12`）。解释器保持 3.12，三处偏离取消 | ✅ owner 2026-09-11 确认撤回 |
| DEV | PS `shared/nautilus` | **D3 维持 Plan 60 Slice E defer**：不删不升，与 NT 2.0 正交 | ✅ owner |
| DEV | `adapter/trading_config.py` | **D5b 根类形态已定**：普通子类 + kw-only `__init__` + `__setattr__` 冻结守卫 + `__eq__`，无 `__hash__`；失去的 msgspec 语义逐项列于 D5b。中风险模型结构变更，起草期定稿 | ✅ 本 fix 定稿（`68eab22` Fix 4）|
| DEV | `host.py:92` / Task 8 | **D4 假设：本 plan 不交付 SoDEX live**。依据 plan 自述「只做能跑」与 Task 8 Step 3 只验 sandbox / testnet；白名单按 mode 拆分后 SoDEX 只进 sandbox / testnet 集合。owner 若要交付 live，另按高风险偏离审议 | ✅ owner 2026-09-11 确认 |
| DEV | `tests/toolkit/test_native_trailing_submitter.py` | **Task 0 Files 清单漏了测试文件**：三处 `make_qty` 的 `hasattr` 守卫实为迁就一个不忠实的 test double——4 个 `MockInstrument` 中 3 个已定义 `make_qty`，只有 trailing 那个漏了。补齐 mock 而非在生产代码保留守卫（教训 C4）。commit `2bf09e8` | ✅ 实施中发现 |
| DEV | `pyproject.toml` / 8 个既有红文件 | **Task 0 扩展：主干三层既有红一并归零**（owner 2026-09-11 批准）。`verify` 红有三个独立原因，plan 只记了 typecheck 一个：另有 `fmt-check` 8 文件、`lint` 2 处 I001（后者此前被 fmt-check 短路挡住）。七个文件已格式化；第八个 `tests/integration/runner_fact_publication_process.py` 被 `receipts/custos-runner-fact-local-publication-v1.json` 按 sha256+size 钉住，格式化即 `check-authority` 报 source drift（实测），故加入 ruff `extend-exclude`，与 inventory-extracted 包同一处理方式——formatter 不碰、authority gate 验字节，条目注明解除条件。commit `7971bc8`，`make verify` 现 exit 0。**过程教训**：只扰动验了 8 个里的 1 个就声称「全部可安全格式化」，第一次尝试即被 gate 打回——断言宽于实证（生态 #46） | ✅ owner |
| DEV | `toolkit-nautilus/pyproject.toml` | **切换丢的不止 pandas**：fork 核心依赖为空，lock 移除 9 个原由 1.230.0 传递的包（click / fsspec / msgspec / portion / pyarrow / pytz / sortedcontainers / tqdm / uvloop）。逐个核对自有 import：7 个无人使用，**msgspec 被 adapter 15 源文件 + 3 测试使用**（全部 config struct 的基础），与 pandas 一并显式声明。plan 原只预见 pandas | ✅ 实施中发现 |
| DEV | 失败模式契约「3.11 base」 | **原措辞不可能成立**：uv workspace 下 toolkit-nautilus 的 `requires-python` 把整个 workspace 解析为 `==3.12.*`，任何 3.11 sync 必失败，与 git 源无关。真实的门是 `Makefile:76-78` `verify-base-clean`（不指定解释器），已按它验证并通过 | ✅ 实施中更正 |
| DEV | Task 3 判据 | **证伪点失效**：`adapter/__init__.py:7-97` 的 `except ImportError: pass` 使包级 import 在 2.0 下照常成功。判据改为「`__all__` 逐名可达」，并把该静默吞没一并处理（教训 #21） | ✅ 实施中发现 |
| DEV | Task 1a | **拆为 1a-1（Python 侧）/ 1a-2（Docker 侧）**：两条链路各自可独立验收，合并会让第一个绿等到两轮完整 Rust 编译（本地 + 容器）之后 | ✅ owner 2026-09-12 |
| DEV | Task 2a 排序 | **2a 从 Slice A 末尾改排到 D 之后**：实测其 `Literal` 改动使 generator 校验 Crucible vendored golden 失败，`check-authority` 在 2b 交付前不可能绿；而 B/C/D 不依赖版本号常量。原顺序等于让整条中间期失去绿判据（C14 在执行顺序上的形态） | ✅ 实施中改排 |
| DEV | `host.py` / Task 7a | **2.0 一个 event loop 只能跑一个 LiveNode**（`python/node.rs:989` 的 thread-local `HOSTED_RUN_ACTIVE`，理由是 runner senders 与 msgbus 都是 thread-local、两个交错的 hosted node 会串线）。custos host 按多 instance 建（三个 `dict[instance_id, …]` + `_claim_execution_account_partition`）。本 Task 采 fail closed：第二个 instance 在 host 内被明确拒绝。**这是能力缩减**，去向已定（见偏离日志）：维持单 instance / 每 instance 一线程 / 每 instance 一进程。**owner 2026-09-12 定：维持单 instance，host 内 fail closed**；另两条各是独立 plan 的量，在那之前这道守卫都得在 | ✅ owner 2026-09-12 |
| DEV | 两个红线桥接 | **事件可见窗口收窄为「策略 Running 期间」**（`strategy/mod.rs:1356` order、`:1468` position 的状态门；1.x 的 msgbus 订阅无此门）。停止侧安全（shutdown policy 跑在 `stop_async` 之前，且 2.0 无 `pause`），残留侧有缺口（stop 后迟到的终态回报不再进回调）。close-out 红线表按此如实降级（教训 #40） | ✅ 实施中发现 |
| DEV | 转发器 | **Python 回调的异常被 Rust 丢弃**（`strategy/python/strategy.rs:973` / `:1045` 的 `let _ =`）。转发器必须自己把桥接失败变成可见信号，否则「对账不静默」在 2.0 下自动降级为静默 | ✅ 实施中发现 |
| DEV | Task 7 前置调查 | **补第四条排除：`Strategy.subscribe_topic` 不是 msgbus 替代**。它走 `subscribe_any` → `bus.topics`，而 order/position 走 `publish_typed` → typed router，两张表不相交（`api.rs:243` vs `:1285`）。`BusTap` 能看到全部 publish但是 Rust-only、无 Python 面 | ✅ 实施中发现 |
| DEV | `venue_binance.py` | **Task 9 的 venue 部分提前到 7a**：host 的 `deploy` import 该模块，其 1.x import 让 Slice C 的每个测试都收集失败——7a 无法自证。改动不止符号路径：`account_type`→`product_type`、sandbox 的 per-instrument `leverages`→单个 `default_leverage`、`futures_leverages` 键改交易所符号（要自己剥 `-PERP`，1.x 由 `BinanceSymbol` 构造时剥）、exec config 新增必填 `account_id`。Task 9 保留 runner_safety 与红线 0.3/0.4 回归 | ✅ 实施中改排 |
| DEV | `venue_binance.py` | **2.0 只认 HMAC 与 Ed25519（读密钥材料自动判别），无 RSA**；而 `binance_ledger.py:70` 的 credential 契约允许 RSA。key-type 字段消失意味着声明值不再传给任何人，一个 RSA 凭据会被静默接受、到交易所才失败，且错误指向凭据而非类型。已在 venue 层 fail closed | ✅ 实施中发现 |
| DEV | `runner_safety.py` / Task 9 | **guarded exec factory 建在 1.x 的 `LiveExecClientFactory` / `LiveExecutionClient` 上，2.0 无这两个基类**，整段需重建。7a 下配了 `runner_safety_boundary_factory` 的 deploy 会在 lazy import 处失败——即 fail closed，不会出现「无守卫照跑」。Task 9 的实质工作，6 个测试文件（`test_runner_safety_*` / `test_client_order_id_*` / `test_cancel_still_reaches_the_venue` / `test_nautilus_runner_safety_adapter`）随之仍红 | ✅ 范围界定 |
| DEV | `_daemon.py` / `host.py` | **`process_shutdown_requested` 随两个 workaround 一起删**：它唯一的消费者是 `_restore_runner_signal_ownership`。删前实证两件事——`_daemon.py:790-791` daemon 自己在 loop 上装 SIGINT/SIGTERM，以及 `_daemon.py` **未被** authority gate 按字节固定（改一行后 `make check-authority` 仍 exit 0，C6 的 pin 清单不含它）| ✅ 实施中发现 |
| DEV | 扰动验证流程 | **我的验证工具骗了我一次**：改文件后在同一秒内 `cp` 还原，且扰动只是重排行、字节数不变，于是 `.pyc` 的 (mtime 秒, size) 判据认为缓存仍有效，pytest 跑的是扰动版字节码而磁盘是正确版。表现为「还原后测试仍红」，一度让我以为提交了坏代码。**此后所有扰动一律 `PYTHONPYCACHEPREFIX` 指向每次唯一的目录**，先前四次结论作废并重做（七处全部重验会红）。教训 #50 家族，已登记为 custos lesson C15 | ✅ 实施中发现 |
| DEV | `host.py` / `venue_binance.py` / Task 8 | **分派面比 plan 清单大一圈**。plan 只列 `venue_sodex.py` 新建 + `host.py` 三处，但 `deploy()` 与 `_build_exec_plan()` 原先硬编码 `venue_binance` 与 `Binance*ClientFactory`——只改白名单会让 SoDEX 通过 admission 之后在 Binance 的 config builder 里炸（admission 说支持、装配说不支持，正是白名单该防的形态）。故加 `_VENUE_MODULE_BY_CONNECTOR` 分派表，并给两个 venue 模块统一面：`CONNECTORS_BY_MODE` / `client_name` / `data_client_factory` / `exec_client_factory` / `build_data_client_config_for_mode`。低风险：模块内部，无 wire 契约变更 | ✅ 实施中发现 |
| DEV | DeploymentSpec | **SoDEX 执行需要两个 spec 新字段**：`wallet_address` 与 `sodex_account_id`。四个凭据里只有 API key 名与私钥是秘密（来自 vault），钱包地址与数字账户 id 是公开标识符（适配器 `config.rs:167` 原文「Public information, not a secret」），归属 owner 签名的 spec。**不可省**：适配器对四个字段各有 env fallback（`SODEX_*`），任一留空即静默改用宿主环境变量里的凭据，故 venue 层四个字段全部显式必填、缺一即 raise，并有一条把 env 全设上的测试证明不会走 fallback。中风险（spec 字段新增），仅 venue_sodex 消费 | ✅ 实施中发现 |
| DEV | `venue_sodex.build_instrument_id_strings` / toolkit | **SoDEX 的 `pairs` 是场所原生符号，不做 BASE-QUOTE 翻译**：现货列 `vBTC_vUSDC`（场所自有 v 前缀代币、下划线分隔），永续列 `BTC-USD`（两个引擎报价资产就不同）。从 `BTC-USDT` 推导等于编造报价资产，而符号错了不报错、只是加载出空的 instrument 集（教训 #52 同型）。**遗留缺口（本 Task 不修）**：toolkit `adapter/trading_config.py:320-325` 的 `external_order_claims` 仍按 Binance 形状构造（`pair.replace("-","")` + 由 connector 取 venue），SoDEX 策略配置会拿到错的 instrument id。归 toolkit adapter，随 Task 7b / 12 处理；在那之前 SoDEX 不具备端到端策略运行能力 | ✅ 遗留登记 |
| DEV | Task 8 Step 4(b) | **`NoopHost` 在本仓已不存在**（grep 0 命中）——plan 该条措辞来自 `CLAUDE.md` 红线 0.2 的历史表述。当下等价物是 `SandboxSimulationHost`，且它与真 host 共用同一张 venue 表，真正拦住它的是 mode 门（admission 先问 mode 后问 venue）。断言因此落在 `supports_trading_mode`：sandbox True、testnet / live 均 False。**初稿曾写成 `supports_venue(...) is False or not supports_trading_mode("live")` 的析取——那是恒真、扰动不会红**，改为直接断言 mode 门（教训 #46） | ✅ 实施中更正 |
| DEV | Task 8 Step 3 | **「起停一轮」的兑现范围**：sandbox 与 testnet 两条都经 host 的 `deploy` → `stop` 走通（`FakeLiveNode` 装配面，断言客户端注册名 `SODEX_PERPS` 与工厂类型），另在本机对**真** `LiveNode.builder(...).build()` 实跑过两次（sandbox 模拟撮合 + testnet 真 exec 客户端，NT 2.0.0rc5+sodex.1，日志见会话）。**未做且不声称**：对 SoDEX 的真机往返——那需要网络与真实凭据，属 C11 同一类证据 | ✅ 范围声明 |
| DEV | close-out 计数 | **Task 8 改动的测试文件条数**：`test_nt_trading_node_host.py` 33→36、`test_nt_binance_venue.py` 改名为 `test_nt_venue_wiring.py` 且 34→41、新增 `test_nt_sodex_venue.py` 16、`test_nautilus_host_capability.py` →11、`test_engine_lifecycle.py` →11。按 `progress-management.md` §数字类声明必须来自实跑，close-out 逐文件表须重数 | ✅ 遗留登记 |
| DEV | Task 7b 范围 | **两项里有一项已经做完了**。plan 的 7b 是「spec 归一 typed 视图 + NT 能力收成 typed adapter，27 处 `getattr` 收口」。第二项经 grep 实证**已由 7a 兑现**：`_NodeRuntime`（`host.py:137`）在 run 开始前捕获 handle / cache / portfolio / strategies，下游一律读它；plan 点名的 `kernel` / `executor` / `trader.strategies` / `loop` 在 host 中零散访问**零命中**（只剩注释）。剩余 15 处 `getattr` 全在 strategy 鸭子类型面与 NT order/position 的真多态上（Task 9 已就后者出具实证），不是同一类问题。故 7b 的实际内容是第一项 | ✅ 实施中实证 |
| DEV | `host.py` / `sandbox_runner_fact_host.py` | **签名事实对 SoDEX 谎报 venue（Task 8 接入后已可达）**。两处 `venue="BINANCE"` 硬编码与 connector 无关；`runner_fact_producer.py:345` 把它绑成 `venue = self._deployment.venue`，此后**每一条 fill 事实**都带它，且 `_scoped_event_id(authority, "execution_fill", venue, ...)` 用它派生事件 id。SoDEX sandbox 走得到（testnet 更早被 Binance ledger 的 connector 检查拦下）。改为由 connector 派生。**这条是 7b 的实际收益**：它不是「读法不一致」的风格问题，是一个读者把另一个读者知道为假的东西签了名 | ✅ 实施中发现 |
| DEV | `src/custos/engines/nautilus/venues.py`（新）| **connector → venue / connector → 模块 收进一张 NT-free 表**。三个消费者需要在没装 nautilus extra 的 base install 上回答：admission 问 connector 是否支持、sandbox fact host 要给事实命名 venue、host 要先解析模块再 import 它。表中的 venue 名与适配器常量（`BINANCE_VENUE` / `SODEX_SPOT` / `SODEX_PERPS`）重复，这是保持可 import 的代价，由 `test_the_nt_free_venue_names_match_the_adapters_own_constants` 守住而不是靠自觉 | ✅ 实施中决定 |
| DEV | `host.py` 结算币种 | **同一规则两份实现，且已经漂移**。`sandbox_runner_fact_host.py:188` 的注释写着「One derivation, shared with the guard paths in host.py」，而 `host.py` 的 `_build_runner_fact_context` 里内联了第二份——且它不 discard 空 quote：`pairs=["BTC-"]` 在共享函数是「settle in none」，在内联副本是「currency '' is outside RunnerFact v1」，把读者引向币种而不是格式错误的 pair。现由 `_DeploymentIdentity` 单点派生，那句注释重新为真 | ✅ 实施中发现 |
| DEV | `host.py` trading_mode 归一 | **`.lower()` 与 `or "sandbox"` 是死代码**。`deploy` 第一行的 `EngineLifecycleAuthority.from_spec`（`engine_protocol.py:214-221`）要求 trading_mode 存在且**恰是**三个小写名之一，此后 `:475` / `:552` 的归一描述的是到不了的情形，而 `:849` / `:880` 直接裸比——读者无从判断哪个是有意的。改为一律读已校验过的 authority。**没有可利用的缺陷**：实测非法 mode 在 deploy 第一行即 `ValueError`，已用一条测试钉住这个事实 | ✅ 实施中实证 |
| DEV | `runner_safety.py` client order id 上限 | **原注释自己预告了这次**：「Wiring a second venue means giving this a per-venue limit rather than leaving it to guess」。Task 8 接了第二个 venue，于是 SoDEX 的订单在按 Binance 的 36 判。改为由 venue 模块声明：Binance 给实测值，**SoDEX 声明 `None`**——fork 适配器无此常量、本仓从未向该场所下过单，照搬 36 是对另一个交易所的声明，而若它的真实上限更短，这个猜测反而给出它支撑不了的保证（C11）。代价写在代码里：过长 id 由场所拒而非本地拒，多一次往返；runner 自己生成的 id 固定 32 字符，不会是来源。解除条件是该场所首次真机会话实测 | ✅ 实施中发现 |
| DEV | toolkit instrument id | **Task 8 登记的遗留项在此关闭**。`pair.replace("-","")` + `-PERP` 的 Binance 约定原有**三份副本**（`utils.build_instrument_id` / `trading_config.build_nautilus_base_config` 的 `external_order_claims` / `trading_strategy._build_instrument_id_for_pair`），收口为 `utils.instrument_id_str`，并加 `PAIRS_ARE_VENUE_SYMBOLS`：SoDEX 的 pairs 已是场所原生符号，逐字使用。**未改**的是 `VENUE_MAP.get(connector, "BINANCE")` 这个静默默认——把未知 connector 变成 BINANCE 同属「默认即假声明」，但它是 toolkit 公开 API 的既有行为、有测试钉着（`test_nautilus_utils.py:118`），改它是中风险契约变更，需单独决策 | ✅ 实施中关闭 + 遗留登记 |
| DEV | `make toolkit-typecheck` | **本 plan 的验证清单要求它全绿（`:603` 写「任何非零都是本 plan 引入的」），实测此前是 84 errors**，NT 2.0 切换引入、此前无人登记。7b 修掉其中 65 条：`NautilusTradingStrategyConfig` 的九个 section 经 `object.__setattr__` 赋值，类上没有声明，于是每个读 `config.trading` 的地方都报 `attr-defined`。补类级注解（只声明、运行期不产生任何东西）后 **84 → 19**。剩余 19 条列在下一条 | ✅ 实施中发现 |
| DEV 🔴 | `trading_strategy.py:466,471` | **阻塞级发现（不属 7b，需 owner 决策）：策略基类的 `on_start` 在 2.0 下必抛 `AttributeError`**。它无条件构造 `EventPublisher(msgbus=self.msgbus, strategy_id=... self.id ...)`，而实测 `dir(Strategy)` 中 **`msgbus` 与 `id` 都不存在**（2.0 有 `strategy_id` / `publish_data` / `publish_signal` / `subscribe_topic`，没有 Python msgbus 面——正是本 plan 的中心发现）。现有测试全部用 strategy double 或不跑 `on_start`，所以一直不可见。`EventPublisher` 的存在理由是 Crucible SSE 持久化，2.0 下要么改走 `publish_*`、要么下线，这是一个交付决策而非重构。**在它落地前，没有任何 PS 策略能在 2.0 下启动** | 🔲 待 owner 决策 |
| DEV | close-out 计数 | **Task 7b 改动的测试文件条数**：`test_nt_trading_node_host.py` 36→40、`test_nt_venue_wiring.py` 41→43、`test_sandbox_runner_fact_host.py` →4、`test_runner_safety_execution_boundary.py` →33、`test_nautilus_utils.py` →25、`test_nautilus_config_extension.py` →5。按 `progress-management.md` §数字类声明必须来自实跑，close-out 逐文件表须重数 | ✅ 遗留登记 |
| DEV | Slice E 现状 | **plan 的「8 文件 / 4 个策略 / 10 个符号对」写于起草时，实测都不对**。策略是 **7 个**（plan 漏了 `fixtures/scale_in_probe`、`portfolio/rebalancing`、`_template`），改动文件是 **9 个 .py**（7 个 `strategy.py` + 2 个 `strategy_core.py`）。符号对实测只有 **4 组**：`model.data` / `model.enums` / `model.identifiers` 三者并入 `nautilus_trader.model`，`config.StrategyConfig` 移到 `nautilus_trader.trading`；**`nautilus_trader.indicators` 不变**（`AverageTrueRange` / `ExponentialMovingAverage` 实测仍在原处，未动那一行）| ✅ 实施中实证 |
| DEV | PS `pyproject.toml` NT pin | **写的是「改 fork wheel」，实际改成 git 直接引用**，与 custos Task 1a-1 的两阶段决定一致：fork 尚未打 tag（那正是 Task 1b 的触发判据），没有版本号可写。PS 禁的是**路径源**（理由是 lock 里的路径只在写它的那台机器上解析得开），git 直接引用没有这个问题，且 `pandas_ta` 已有同机制先例，故不动 `[tool.uv.sources]`。lock 实测由 `1.230.0` 解析到 `2.0.0rc5+sodex.1 (3fe857a3)`，随之移除 9 个原由 1.230.0 传递的包——grep 实证 PS 只用其中的 `msgspec`，而 `nautilus` extra 本就显式声明它 | ✅ 实施中改向 |
| DEV | config 子类形态 | **9 个而非 plan 说的 3 个**：7 个只带 `parameters` 的 `NautilusTradingStrategyConfig` 子类，加 2 个带 4 个标量字段的 `StrategyConfig` 子类（两个 `strategy_core.py`）。一律按 D5b：去掉 `frozen=True` 类关键字、kw-only `__init__`、字段在 `super().__init__()` **之前**用 `object.__setattr__` 落位。**msgspec 参数 struct 不动**——它们是纯数据、从不继承 nautilus，`frozen=True` 在那里仍然成立 | ✅ 实施中实证 |
| DEV | `external_order_claims` | **Task 12 暴露了一个让每个策略「什么都不认领」的缺陷**（custos 侧修，`c8ebc92`）。2.0 把该字段改名为 `external_order_instrument_ids`，而 toolkit 仍发旧名；`StrategyConfig` 的构造以 `**_kwargs` 收尾，不认识的关键字**被接受然后丢弃**，于是列表照建、照传、照丢，属性留在 `None`，**不抛任何异常**。守卫写成通用形态：从构造签名读取可接受名集合，要求 base config 发出的每个键都有去处——下次改名它还会答话（教训 #35 + #21） | ✅ 实施中发现 |
| DEV | `deploy/nautilus/runner.py` | **PS 侧还有一处 1.x 装配未动**：它 import `TradingNode` / `BinanceAccountType` / `SandboxLiveExecClientFactory` 等 1.x 面，被 `deploy/nautilus/__init__.py` 与多个 `test_runner_*` 消费。不在 Task 12 的 Files 范围（那里只列 `refinement/nautilus/*.py`），归 Task 13。**Task 13 的起点已实测**：PS `pytest tests` 现有 **9 个文件收集期报错** | ✅ 遗留登记 |
| DEV 🔴→✅ | `EventPublisher` 下线（owner 2026-09-12 定 B）| **7b 登记的阻塞发现已解决，方式是删掉它而不是修它**。owner 确认 sidecar 那条线（msgbus → Redis → sidecar SSE → Crucible EventPersister）已退役，故删 `event_publisher.py`、六个源文件的调用点、`on_order_accepted` / `on_position_opened` 两个只为发布而存在的覆写，以及 `test_event_publisher.py`。`trading_strategy.on_start` 里那句必炸的 `self.msgbus` / `self.id` 随之消失；toolkit mypy 19→16。**保留** signal-id 关联机制（`generate_signal_id` / `make_signal_tag` / `extract_signal_id_from_tags` / `_order_signal_map` / `PairContext.active_signal_id`），移到中性命名的 `adapter/signal_correlation.py`——它正是补齐工作要的那条链（见下一条）。当前**无人读取**它，模块 docstring 写明原因，防止下一个读者当死代码删掉 | ✅ owner 2026-09-12 |
| DEV | `_order_signal_map` 清理 | **删除时发现一个既有泄漏**：两处 `pop` 原本写在 `if s._event_publisher.enabled:` 块内（`trade_event_handler` 成交路径、`order_reconciler` 拒单路径），而发布默认关闭——也就是说**默认配置下这张按订单键的 map 从不收缩**。现在 pop 无条件执行，并在注释里写明它不是可选项。副作用：两个 stub 测试因此必须带 `_order_signal_map`，那正是"这段清理真的跑了"的证据 | ✅ 实施中发现 |
| DEV | 抽取清单的断言越界 | **`strategy-toolkit-inventory-v1.json` / `strategy-toolkit-extraction-v1.json` 是 commit 快照收据**（`typing-closure` 的 `verification_mode: exact_commit_snapshot`、`checkout_head: b5ff7ee9`，并按 sha256 钉住这两份文件），所以**不改它们**。越界的是测试：`assert all(_target(path).is_file())` 把「b5ff7ee9 时 241 个文件 1:1 抽取完成」变成了「这 241 个文件必须永远存在」——`authority-docs.md` 明文禁止用历史收据永久固定当前源码。改为「仍在，或在 `_RETIRED_SINCE_EXTRACTION` 里具名退役」，加一条即是一次可审的动作。扰动实测：另删一个未具名文件，两条断言仍红 | ✅ 实施中判定 |
| DEV | close-out 计数探针 | **同一条「最新认领说了算」规则原本没覆盖删除**：`test_a_close_out_counts_no_test_file_that_has_since_been_deleted` 把**每一份** plan 的行都按今天的文件系统查，而该文件自己的 docstring 写的是「最新认领该文件的 close-out 才对今天负责，旧行是那一刻的记录」。现按同一规则处理：最新认领计 0 = 该 plan 删了它；并补一条反向断言（计 0 却还在 = 红），以免 0 变成消音开关 | ✅ 实施中判定 |
| DEV | 补齐的对接方是 Crucible 不是 arx | **调查结论（owner 2026-09-12 提问）**：arx `backend/crates/coordination/src/execution_analytics_gateway.rs:1-4` 自述「Signed typed client for **Crucible-owned** execution analytics … does not persist or recompute execution facts owned by Crucible」，`coordination/src/lib.rs:3` 再述「ARX owns neither downstream business semantics nor the RunnerFact data plane」。arx **已有**整套 V1 契约（`ExecutionSignalV1` / `ExecutionOrderV1` / `ExecutionPositionV1` / **`ExecutionTcaV1`（含 `slippage_bps` / `latency_ms` / `benchmark_price` / `benchmark_source` / `completeness`）** / `ExecutionFlowV1`）与 `web/components/pages/tca/TCAPage.tsx`。所以要谈的是 Crucible 能否从 custos 的事实派生出这些，不是给 arx 加字段 | ✅ 实施中调查 |
| DEV | RunnerFact 覆盖面缺口（四项，待跨仓协商）| **① signal 语义不是同一件事**：custos 的 `emit_strategy_signal_sync` 触发点在 `_on_order_initialized` 内（`runner_fact_producer.py:490`），`occurred_at` 取订单事件的 `ts_event`——它实际是「一张单被初始化了，方向是 X」，不是「策略在某时刻产出强度 0.8 的信号」。`strength` / `metadata` / `stop_loss` / `take_profit` / `price` 三方都没有。**② order 用日志事实承载结构化状态**：生命周期走 `RunnerRuntimeLogFact`（`component="custos.execution.order"`），Crucible 要投影成 `ExecutionOrderV1` 得解析 `structured_fields`，且缺 typed 的 `requested_at` 与 `venue_order_id`。**③ TCA 两个输入都没有**：`latency_ms` 缺 `requested_at`，`slippage_bps` 缺 benchmark。**已实证可得**：2.0 的 `MarketOrder` / `LimitOrder` 都有 `ts_submitted` / `ts_accepted` / `ts_closed`（删掉的 `_compute_fill_latency_ms` 正是用 `ts_submitted` 作提交锚、缺失则退到 `ts_init`），benchmark 可由 `NautilusPortfolioSnapshotProvider` 的 mark price 产出——都是「能拿到但没接线」。**④ 入场↔保护单的关联**：custos 按 `client_order_id` 做确定性关联，而保护单的 id 与入场单不同，所以它知道 `order_role` 却不知道某张保护单保护的是哪次入场——`ExecutionOrderV1.signal_fact_id` / `ExecutionTcaV1.signal_fact_id` 要的正是这个，也正是保留 `signal_correlation` 的理由 | 🔲 待跨仓协商 |
| DEV | crucible lane 不移植 | **Task 13 没有把 `deploy/nautilus/runner.py` 升到 2.0**，因为有既有 owner 决定（PS `CLAUDE.md:54-64`，2026-07-30）：该 lane「不修、不删」，等 arx 整套上线后随 crucible 一并清理。移植它等于推翻那个决定，删掉它的测试又会在有人真退役它之前先抹掉它做过什么的记录。故新增 `tests/_crucible_lane.py`：**具名跳过**，探针是 1.x 模块布局本身（`find_spec("nautilus_trader.model.enums")`），lane 被移植或有人重新钉 1.x 时这些测试会自己回来。两个文件按类拆分而非整文件跳过——它们大部分覆盖的是 toolkit，只有 runner 那一半站下（`test_nautilus_ipc.py` 6 跑 5 跳、`test_runner_integration.py` 20 跑 5 跳）| ✅ 依既有 owner 决定 |
| DEV | `tests/test_msgbus_stream_e2e.py` | **删除而非跳过**：它是 msgbus → Redis 落盘格式对 sidecar consumer 的 e2e gate，**两端都没了**（发布器已随 B 从 toolkit 删除、sidecar lane 已退役）。条件跳过会在探针翻转的那天说谎——lane 被移植回来时它仍然 import 不到 `event_publisher`。与 custos 侧删 `test_event_publisher.py` 同一判据 | ✅ 实施中判定 |
| DEV | 三处**陈旧而非损坏**的期望 | 都是跨仓改动后本仓没跟上，且此后文件一直收集失败所以无人发现（C8 家族）。**①** `test_stale_order_sweep.py` 断言「同侧第二张保护单按重复单取消」——custos `e200838`（2026-08-14）有意反转为「保留」，理由是场所订单流是账户级的，那张「重复单」可能是另一个部署唯一的保护。**②** 同一 commit 让 tracker 记录被保护的数量，`set_/add_*_sl_order` 多一个参数。**③** `test_core_contract.py` 的 AST 断言读的是**本仓自己那份已 defer 的** `shared/nautilus/strategy_core.py`，而运行时导入的是 toolkit——`on_bar` 两边都有，所以只有 tick 模板方法暴露了这件事；改为从 `inspect.getsourcefile(NautilusStrategyCore)` 取，不会再漂 | ✅ 实施中发现 |
| DEV 🔴 | `test_base_strategy.py` 的假跳过 | **118 条测试带着一句不实的理由静默跳过**：`_can_import_nautilus()` import 的是 `nautilus_trader.trading.strategy`（1.x 子模块），2.0 没有，于是判定「nautilus_trader not installed」——而它装着。改用 2.0 位置后，其中 **25 条真的红**，本 Task 逐条修完。修的过程中又发现两条 reversal 测试**从来没跑到断言**（`pending_signal` 非空且不是被跟踪入场单时 handler 直接 return），所以其中的否定断言一直因为错误的原因在绿。教训 #50 + C10 同族：探针本身会骗人，而「全绿」不含「有没有在跑」 | ✅ 实施中发现 |
| DEV 🔴 | `deploy/custos/scripts/bootstrap_vault.sh` | **该脚本接受了它存在的意义就是要拒绝的输入**。`case "$SCOPE_DIGEST" in *[!0-9a-f]*)` 里的 `a-f` 是**排序区间**，在 C 以外的 locale 下同时覆盖 A-F，于是全大写的 digest 通过校验——而这道校验的位置就在读取密钥之前。zh_CN.UTF-8 下实测复现（`[!0-9a-f]` 不拒 `AAAA`，`LC_ALL=C` 下拒）。改为逐字符枚举。测试早就写着，只是一直跑不起来 | ✅ 实施中发现 |
| DEV | close-out 计数 | **Task 13 改动的 PS 测试文件**：PS 不在 `tests/test_plan_closeout_counts.py` 的扫描域内（那个探针只读 custos 的 `.forge/plans`），故不入逐文件表；PS 侧的数字以 `make verify` 的 996 passed / 18 skipped 为准 | ✅ 说明 |
| DEV | close-out 计数 | **Task 7a 改动了若干测试文件的条数**（`test_nt_trading_node_host` 27→33、`test_nt_binance_venue` →34、新增 `test_strategy_event_forwarding` 11 等）。按 `progress-management.md` §数字类声明必须来自实跑，plan close-out 的逐文件表格须在 Slice D 之后按 `pytest --collect-only` 重数，不得沿用旧行 | ✅ 遗留登记 |
| DEV | `runner_safety.py` | **执行门从 exec client 迁到 strategy 边缘**（owner 2026-09-12 定，选项 (a)）。1.x 的 `GuardedLiveExecutionClient` 与 `guarded_exec_client_factory` 删除而非移植——2.0 无 Python 可继承的执行客户端基类，且 `add_exec_client` 的 factory 走 Rust 注册表按名查 extractor。新位置的覆盖面靠三条前提成立：三个 config 开关在 admission 被拒、带 `emulation_trigger`/`exec_algorithm_id` 的单在 gate 被拒、`market_exit()` 被拒。**位置比 1.x 靠上，这是红线 0.2 的实质弱化**，close-out 的红线表按此写 | ✅ owner 2026-09-12 |
| DEV | `runner_fact_producer.py` | **拒单自己发 RunnerFact**（owner 2026-09-12 定，选项 (a)）。2.0 只在 submit 内部把订单入 cache 并发 OrderInitialized，所以在 gate 拒掉的单从未存在、NT 不会有任何事件说这件事。新增 `record_local_refusal`，走既有 runtime-log 事实通道（`lifecycle="refused"`），不改 schema。**没有事实通道的 gate 在 deploy 时被拒**——「守住了资金、丢掉了记录」不接受 | ✅ owner 2026-09-12 |
| DEV | `strategy_hooks.py` | **新增**：Task 7a 的「在实例上安装包装并验证生效」与 Task 9 的 gate 安装是同一件事，提取为 `install_hook`。`StrategyForwardingUnsupported` 随之泛化为 `StrategyHookUnsupported`——转发装不上与守卫装不上是同一类拒绝理由 | ✅ 实施中发现 |
| DEV | `runner_safety.py` money 路径 | **品味项落地**：`getattr(...) or ...` 默认链改为逐级 `is not None` + 价格必须为正。`or` 把 0 当作缺失，于是零价格会静默换用下一个价源；而零价格算出的 notional 是 0，预留 0 通过任何 cap。保留的 `getattr(order, "price"/"trigger_price")` 是**实测过的**真多态（2.0 MarketOrder 两者皆无、LimitOrder 只有 price、StopMarketOrder 只有 trigger_price），不是防御性冗余。`_truthy_attr` 的 callable 分支删除——实测 2.0 这些字段全是 getset_descriptor，不是方法 | ✅ 实施中发现 |
| DEV | `tests/test_nautilus_runner_safety_adapter.py` | **整个文件删除**：它测的是 `GuardedLiveExecutionClient` 的 connect/disconnect 生命周期与 Cython descriptor 代理问题，那个类在 2.0 不存在，测试对象随之消失。`test_runner_safety_host_wiring.py` 是重写而非删除——「host 要把守卫接上」这个语义仍在 | ✅ 实施中发现 |
| DEV | 多 agent 并行 | **另一个 agent 并行在做 Task 10**，其 `3dd7ff6` 把我当时正在重写的 `test_runner_safety_host_wiring.py` 一并 commit 了（commit message 只说 toolkit/host 测试迁 2.0 import layout，实际含 Task 9 的重写）。内容没丢，但期间两次表现为「文件变回旧内容」，我误判为工具写入未落地、排查了两轮。已登记为 lesson C16 | ✅ 实施中发现 |
| DEV | `host.py:1232-1233` | **连接状态查询面在 Python 侧整个消失**（F6）：2.0 对 `check_connected` 无 Python 面，Rust 侧也只在启动与停机两端调它。启动判定可由 `handle().state == RUNNING` 顶上（进入 Running 蕴含启动时已连），**运行期断连检测能力丢失**，红线 0.3 按此如实降级。**owner 2026-09-12 定：(a) 降级声明**，用 `handle().state` 顶上，不给 fork 加自有 API；运行期断连检测登记为 follow-up | ✅ owner 2026-09-12 |
| DEV | `host.py:527` / `:763` | **两处 1.x workaround 的理由在 2.0 下消失**：`run_async` 固定 `NodeRunMode::Hosted`、不装 signal handler（`python/node.rs:549` + `node/mod.rs:1461`），故 `_restore_runner_signal_ownership` 无对象；2.0 `dispose` 不碰 Python asyncio loop（`node/mod.rs:562-566`），故 `_dispose_node_preserving_runner_loop` 的 loop 保护无对象。两处删除而非改写 | ✅ 实施中发现 |
| DEV | `strategy_core.py` 数据回调 | **2.0 下 toolkit 策略收不到任何 tick**：回调去掉了后缀（`on_trade_tick` → `on_trade`、`on_quote_tick` → `on_quote`），toolkit 仍定义旧名，于是那两个方法**从未被调用**。`on_core_*` 是 toolkit 对策略的契约、不改。Slice B 只改了 import 路径，这层没覆盖到；单测全绿，是 Slice D 的真引擎测试逼出来的 | ✅ 实施中发现 |
| DEV | `strategy_core.py` 单笔 cancel | **2.0 的 `cancel_order` 收 `ClientOrderId` 而非 order 对象**，而它的 stub 写 `order: Any`——传 order 能通过类型检查，运行时才在回调内抛 `TypeError`，被外层 handler 吞成一行日志。可观测结果是 cancel 永不到达 venue 而无人变红，正是该测试 docstring 预言的「交易所留着一个活着的 stop-loss，日志还愉快地记着请求」。另：`cancel_all_orders` 仍收 `params` 而 `cancel_order` 不再收，传了就显式拒绝、不静默丢 | ✅ 实施中发现 |
| DEV | Slice D 其余 2.0 改名 | `AggressorSide.BUYER`→`BUY`、`MovingAverageType.WILDER`→`Wilder`、`oms_type` 需 `OmsType` 枚举、`LoggingConfig`→`LoggerConfig`、`Cache(database=)`→`Cache(config=)`、`OrderFactory` 不再收 cache 且 id-shape flags 移到 config、`model.currencies` 删除（改 `Currency.from_str`）、`test_kit` 删除（两个 tick 工厂重建于 `tests/fixtures/nt_data_stubs.py`）。**2.0 的 risk engine 还强制 instrument 最小名义值**，测试的挂单需自带足额 notional | ✅ 实施中发现 |
| DEV | `set_client_order_id_count` | **能力降级**：2.0 移除该 setter，计数器无法从测试驱动到 `2**31-2` / `2**31-1` 两个最坏点（C11 当初直接钉住的正是它们）。改为断言「UUID 形态下计数器不参与 id，故重复生成长度恒定」，证据强度低于原来，已写进该模块注释 | ✅ 实施中发现 |
| DEV | 拍平工具（Slice D） | **两个假设在 tests/ 才暴露，adapter 从未触发**：(1) **函数级 import 的作用域**——按文件合并把 `test_pair_context.py` 16 处 per-method import 并到第一个方法、删掉其余 15 处，`BarType`/`InstrumentId` 在那 15 个方法里未定义（47 个 F821）。改为按 enclosing scope 合并，归属用「自 scope 向下走、遇嵌套 scope 即停」。(2) **`# noqa` 承载信息**——`E402`（`pytest.importorskip` 之后的 import）与 `F401`（仅用于可用性探测）；合并时静默丢了 35 个中的 26 个。现按被替换语句的 noqa 并集附加到合并行。两处都是**跑完读 diff/lint 才发现**，不是工具报的错（教训 C10 同族） | ✅ 实施中发现并修 |
| DEV | Task 2b | **`engine_version` 是跨仓契约字段**（mandatory-rules §3）：custos 只改自有 V1 文件，vendored golden 与 PS / Crucible 侧列为 Blocked，不得自行改写 | ✅ 规则约束，无需批准 |
| DEV | `Dockerfile` / Task 1a-2 | **`nt-builder` 放弃，D1 阶段一取消，直接做阶段二（Task 1b）**。实施到底后否决，理由两条且第二条才是决定性的：(1) 本机 Docker VM 只有 7.7 GiB，`nautilus-pyo3` 编到 4845s 被 `SIGKILL`，BuildKit 报 `ResourceExhausted: cannot allocate memory`——根因是 fork `[profile.release]` 的 `lto = "fat"` + `codegen-units = 1`；宿主机 64 GiB，属配置问题，可修。(2) **决定性的是它装不进 custos 自己的发布流水线**：`release.yml:155` 以 `platforms: linux/amd64,linux/arm64` 在 `ubuntu-24.04`（16 GiB / 4 核）构建镜像，带上 `nt-builder` 等于每次发布把 NT 整个 Rust 工作区编两遍、其中一遍走 QEMU 模拟；参照系是本机 10 核原生 arm64 编到 81 分钟仍未完成。另：`release.yml` 只传 `labels:` 未传 `build-args:`，新 Dockerfile 的参数校验会让该构建直接失败，而计划 File Inventory **未列 `release.yml`**——这是计划的覆盖缺口，不是实施偏离 | ✅ owner 2026-09-13 选 B |
| DEV | Task 1b 触发判据 | **原判据不可满足，按实测重写**。原文要求「fork 打 tag；fork CI 产 cp312 manylinux + macosx wheel 并挂 Release」。实测 fork 的 `actions/runs` `total_count = 0`（该仓从未运行过任何 workflow）、`actions/runners` `total_count = 0`、environments / secrets / variables 均为 0，而产 wheel 的 job 及其全部前置在 push 事件下都要求 `["self-hosted","Linux","X64","build"]`（`build.yml:122` / `:198` / `:299` / `:444`）。**CI 造不出 wheel，也造不出 tag**（`tag-release` 门在 `refs/heads/master`）。改为：wheel 本地构建、`gh release create` 上传（不经 CI）；判据改为 (1) 三平台 wheel 就位并各记 sha256 (2) Release 已发布且 URL 可取 (3) Task 8 通过（已满足） | ✅ owner 2026-09-13 |
| DEV | 发布版本的选择 | **发 `2.0.0rc5+sodex.1`（fork `3fe857a351`），不发 develop HEAD**。Task 8 / 12 / 13 验证的正是该 commit；develop 已多 16 个 commit（+566 行 adapter）无人复验，跟着发等于把未验代码推给 PS 与 custos。同时 fork `python/pyproject.toml` 已 bump 到 `+sodex.2`（fork commit `7ef69a64a4`，**未推送**），使 `sodex.1` 从此永久指 `3fe857a351`、`sodex.2` 指 develop 新代码，消除「一个标签指两套字节」 | ✅ owner 2026-09-13 |
| DEV | Task 1a Step 4(a) 验收措辞 | **写了一条机制上不成立的门**。原文「3.11 解释器下 `uv sync --package custos-runner --extra dev` 必须成功」——实测 workspace 在 3.11 上根本无法解析，`custos-strategy-toolkit-nautilus` 的 `requires-python == 3.12.*` 直接拒绝，与本任务改动无关；`tech-stack.md` 本就写明 3.11 兼容性「由独立 wheel/import gate 验证，不能从 workspace interpreter 推断」。改为可满足且同义的判据：base 导出（不带 `--extra nautilus`）不含 NautilusTrader——实测 45 个包、0 处 nautilus 引用。教训 C14 同型，这次出现在验收项措辞上 | ✅ 实施中发现 |
| DEV | `pyproject.toml` `[tool.uv]` | **切 wheel 源强制声明支持平台集**。实测 uv 默认做全平台解析，wheel 源下没有对应 wheel 的环境会让 lock 解不开（uv 自己提示 `consider limiting the environments with tool.uv.environments`）。custos `pyproject.toml:80` 的 `[tool.uv]` 需新增 `environments`，列出 darwin-arm64 / linux-aarch64 / linux-x86_64。不影响 base 的 3.11 可安装性——base wheel 是 `py3-none-any`，其兼容性不由 lock 的平台集决定 | ✅ 实施中发现 |
| DEV | 发布资产命名 | **uv 不校验 wheel 内部 tag 与文件名是否一致**（实测：把 macOS wheel 改名成 `manylinux_2_34_x86_64` 后 uv 照常接受并记入 lock）。故 Release 资产的平台 tag 必须由构建产出、不得手工改名；并保留「镜像内 NT 能 import 且版本与 lock 一致」的断言作为兜底——命名错误只会在运行期暴露 | ✅ 实施中发现 |
| DEV | `docker/nautilus-wheel.dockerfile` | **引擎 wheel 的构建内存实测：峰值 41.81 GiB，不反常**。三轮对照把假峰与真需求分开：VM 23.4 GiB（构建可用 ~18.5）峰值停在 18.29 后被杀；VM 31.3 GiB（可用 ~29）峰值 29.81 仍在爬升时被杀；VM 46.7 GiB（可用 ~45.9）峰值 41.81 跑完。前两个「峰值」都是被可用内存截断的假值。**合理性判据**：fat LTO 的输入是 743 个 `.rlib` 共 2.42 GiB（实测自 uv 本机构建留下的 target 目录），41.81 / 2.42 ≈ 17 倍，落在 LLVM full LTO 的常见区间（5–15 倍，且 rlib 含非 bitcode 部分故实际更高）。**先前记为「220 倍可疑」的说法作废**——那是拿链接**输出**（135.4 MiB strip 后的 `.so`）当分母，用错了对象。排查同时确认：fork `.cargo/config.toml` 给 Linux 注入的全是链接期参数、`[tool.maturin] features` 未多开、上游的 wheel job 一律跑在 Depot 8-vCPU 或 self-hosted 上。结论是输入体量决定，非泄漏；降内存的唯一无损办法是砍 feature（前十大 rlib 里六个是 custos 不用的场所适配器） | ✅ 实施中实测 |
| DEV | `docker/nautilus-wheel.dockerfile` 文件名断言 | **守卫赌了一个未经核实的约定，代价是一轮 38 分钟的构建**。原断言按 PEP 427 假设 maturin 会把本地版本里的 `+` 转义成 `_`，于是去匹配 `nautilus_trader-2.0.0rc5_sodex.1-*.whl`；maturin 实际**原样保留 `+`**。证伪材料当时就在手上——已核验过的 macOS wheel 文件名一直写着 `2.0.0rc5+sodex.1`。改为：按版本无关的 glob 取文件，再断言文件名含版本（两种拼法都接受），已按五种输入证伪（两种拼法接受、`sodex.2` 与 `rc4` 拒绝）。**另一处值得留存的观察**：`test "$#" -eq 1` 当时是**通过**的——glob 不匹配时 shell 留下字面模式、恰好一个参数——真正拦住空产出的是随后的 `test -f "$1"`。两道必须并存 | ✅ 实施中发现 |
| DEV | `docker/nautilus-wheel.dockerfile` / `Makefile` | **x86_64 wheel 可在 Apple Silicon 上构建，砍 feature 的路被数据否决**。先前判定「本机只能 QEMU，不现实」是未经测量的推断：实测 Docker 的 `linux/amd64` 走 **Rosetta**（Python 微基准 1.83s vs 原生 1.60s，1.14 倍；QEMU 会是 5–20 倍），实际编译折算约 **2 倍**（72.2 分钟 vs 原生 37 分钟）——微基准只够判断「不是 QEMU 量级」，不足以估编译耗时。据此 `make nautilus-wheel` 增 `NAUTILUS_WHEEL_PLATFORM` 参数。**砍 feature 的量化否决**：`crates/pyo3/Cargo.toml` `default = []` 且全 crate 仅三个可选依赖（`mimalloc` / `nautilus-betfair` / `nautilus-blockchain`），owner 想砍的 kraken/bybit/dydx/coinbase/bitmex/deribit/lighter/databento/IB 在 `extension-module` 中**不带 `?`**、是硬依赖。实测占比：可经 feature 关闭仅 **5.1%**（0.124 GiB），arrow/postgres 相关 21.1%，硬依赖场所 16.2%；**三者全砍（含改 fork 源码）剩余输入 1.39 GiB、估算峰值仍 23.7 GiB**，超过免费 runner 的 16 GB。要压进 14 GiB 需砍掉 66%。故保留全 feature 与 fat LTO，上游 `[profile.bench-lto]` 注释亦表明 fat 是其对外基准数字的前提 | ✅ owner 2026-09-14 |
| DEV | 构建峰值随架构变化 | **同源同 profile，x86_64 峰值 24.18 GiB、aarch64 41.81 GiB，差 73%**。不得把单一架构的实测值当作通用内存需求——重建 wheel 时须按目标架构各自预留。另两处健壮性修补：(a) rustup 下载偶发 `tls handshake eof`（同一 URL 宿主机 `HTTP 200`/0.18s），加三次有界重试，连续失败仍然停；(b) cargo 缓存按 `TARGETPLATFORM` 分 id——容器内 host triple 即目标 triple，两架构原本写同一 `target/release` 路径、会互相判为失效 | ✅ 实施中实测 |
| DEV | `toolkit_rc_release_readiness.py` lock parser | Task 1b 的三个互斥marker URL会让`uv.lock`为同一name/version生成三条record；旧parser把任何重复name都判为ambiguous。现只在版本不同、source非纯URL、marker缺失/重复或URL重复时拒绝；合法三平台record聚合全部URL、marker与SHA-256进入dependency evidence。单版本registry依赖语义不变。 | ✅ Task 2a RED/GREEN |
