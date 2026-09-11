# 01 - NautilusTrader 1.230.0 → fork 2.0.0rc5 升级

> **Status**: 🔲 Not started
> **Created**: 2026-09-11
> **Project**: custos（跨仓：philosophers-stone）
> **multi_session_scope**: **true**（6 个 Slice、跨 2 仓库、涉及红线 0.1/0.2/0.4）
> **For Claude**: 按 Slice 派工，**不要单 session 硬推**（教训 #31）

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
| fork 仓位置 | `git -C <fork> remote -v` | origin `wukai9203/nautilus_trader`（public fork）；`the-alephain-guild/nautilus_trader` 2026-09-12 尚不存在 |
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
| task_dag | Slice A 串行前置（Task 0 → 1a → 2a；1b 在 Slice F 前；2b Blocked 不阻塞后续）；B / C 可并行；D 依赖 B+C；**E 依赖 A + Task 5**（PS Config 子类随 D5b 根类形态）；F 最后 |
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
| 3.11 base 无 nautilus extra 安装 | `pyproject.toml:29-32` 的 base 承诺在 lock 含 git 源 / wheel 后仍成立（path 源会击穿它；git 源实测不带 extra 导出 0 行） | A |
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
**Step 0（前置，fork 仓）**: (a) fork 迁到 `the-alephain-guild/nautilus_trader`（public），根仓库 `CLAUDE.md` §8 登记表的 origin 同步改（跨仓项）；(b) fork `python/pyproject.toml` 版本改 `2.0.0rc5+sodex.<n>`；(c) 记下要钉的 sha。**没有 (a)(b) 本 Task 不能开始**——URL 与版本 label 都会进 lock 与三方契约，事后改要牵动 PS
**Step 1（证伪）**: 当前 venv `uv run python -c "import nautilus_trader; print(nautilus_trader.__version__)"` 输出 `1.230.0`；`make check-runtime-lock` 在加 git 源后 diff 非空（证明这道门确实会因 git 源变红，而不是本来就不看它）
**Step 2（实现）**: `[tool.uv.sources]` 加 git 源（`rev` 写精确 sha，`subdirectory = "python"`）；`toolkit-nautilus` 的 NT pin 改新版本号；pandas 显式进依赖；`uv lock`；`Makefile` 两处 `uv export` 加 `--no-emit-package nautilus-trader` 并重生成 runtime lock；`Dockerfile` 加 `nt-builder` 阶段（sha 从 `uv.lock` 推导，不手写）；`test_docker_runtime_contract.py` 加 label 对账断言
**Step 3（证实）**: 同一命令输出 `2.0.0rc5+sodex.<n>`；`python/nautilus_trader/adapters/sodex` 可 import；`grep -c 'path = ' uv.lock` 对 nautilus-trader 为 0 且有 `git+…@<sha>`；`make check-runtime-lock` exit 0；`make test-docker` 全绿（含新对账断言）
**Step 4（失败模式）**: (a) 3.11 解释器下 `uv sync --package custos-runner --extra dev`（无 nautilus extra）必须成功；(b) 把 `uv.lock` 里的 sha 改一位后不重建镜像，对账断言必须变红（证明它是 live guard）；(c) `nt-builder` 里 `maturin` 版本与 fork `[build-system]` 不一致时构建必须失败而非静默用别的版本
**Step 5**: commit

#### Task 1b: 切换到 wheel（D1 阶段二，Slice F 收尾前的门）
**Files**: `pyproject.toml`, `uv.lock`, `docker/runtime-requirements.lock`, `Makefile`, `Dockerfile`, PS `pyproject.toml` / `uv.lock`
**触发判据（三条齐才开始）**: fork 打 tag；fork CI 产 cp312 manylinux + macosx wheel 并挂 Release（fork `build.yml` 已引用 maturin，可作起点）；Task 8 的 SoDEX sandbox / testnet 起停通过（adapter 的 Python 面冻结）
**Step 1（证伪）**: `uv.lock` 仍含 `git+`；`Dockerfile` 仍含 `nt-builder`
**Step 2（实现）**: git 源改带 sha256 的 wheel 引用（uv `url` 源按平台各一，或 `find-links` 指向 Release 资产页，实测定一种）；删 `--no-emit-package` 与 `nt-builder`；runtime lock 重生成后含带 hash 的 nautilus-trader；PS 同步改为命令式安装该 wheel
**Step 3（证实）**: `uv.lock` 无 `git+`；`docker/runtime-requirements.lock` 中 nautilus-trader 带 `--hash=sha256:`；`Dockerfile` 回到无 `nt-builder` 的形状；`make test-docker` 全绿
**Step 4（失败模式）**: 篡改 lock 里 wheel 的 hash 后 `uv sync --frozen` 必须拒绝
**Step 5**: commit。**本 Task 未完成不得 close-out**：1a 的 `nt-builder` 是过渡方案，留着就是永久债务

#### Task 2a: 契约层版本号原地改 V1（custos 自有部分）
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
**Step 1（证伪）**: `uv run python -c "import custos_toolkit_nautilus.adapter"` 报 ImportError
**Step 2（实现）**: 执行替换
**Step 3（证实）**: 同一 import 成功；`pytest --collect-only tests/toolkit` 收集数不低于改前（教训 C10 同批第二条：全绿不含「有没有在跑」）
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
| Task 8 | `tests/test_nt_binance_venue.py:232,243` | 两条装饰分隔线去掉，叙述改为该 test 的 docstring |
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
| 0 | 🔲 | | mypy 基线归零 |
| 1a | 🔲 | | 前置：fork 迁组织 + 版本 label |
| 1b | 🔲 | | 切换门：fork tag + CI wheel + Task 8 通过 |
| 2a | 🔲 | | |
| 2b | ❌ | | Blocked：PS / Crucible 重签 |
| 3 | 🔲 | | |
| 4 | 🔲 | | |
| 5 | 🔲 | | |
| 6 | 🔲 | | |
| 7 | 🔲 | | |
| 8 | 🔲 | | 红线 0.2 |
| 9 | 🔲 | | 红线 0.3/0.4 |
| 10 | 🔲 | | |
| 11 | 🔲 | | |
| 12 | 🔲 | | PS 仓 |
| 13 | 🔲 | | PS 仓 |
| 14 | 🔲 | | |

## 偏离与改进日志 (Deviations & Improvements)

| 类型 | 位置 | 描述 | 已批准 |
|---|---|---|---|
| DEV | `pyproject.toml` | **D1 改向：fork 发布 wheel，不用 path 源**。起草版接受「单仓 clone 装不起来」的代价，审查（`b0ff52a` C1/H1/H2）实证代价范围写窄：path 源还会击穿 3.11 base 承诺、`verify-base-clean` 与 Docker runtime lock，且 PS 规则禁 path 源。owner 2026-09-11 审查会话改向 wheel | ✅ owner（会话中口头，本 fix 落文）|
| DEV | `pyproject.toml` / `Dockerfile` | **D1 再改为两阶段**：fork 活跃期用 git 源（1a），稳定后切 wheel（1b，有触发判据、未完成不得 close-out）。Docker 链的 git 阶段红实测根因是 `uv export` 对 git 源无 `--hash` 而 `Dockerfile:22-27` `--require-hashes`；解法走 custos 自有 wheel 已有的 `--no-deps` 路径 + `nt-builder` + sha 对账，**不登记 known-red**。多出的 `Dockerfile` / `Makefile:96-103` / `test_docker_runtime_contract.py` 三处已进清单 | ✅ owner 2026-09-12 |
| DEV | `.python-version` / `tech-stack.md` | **D2 撤回**：「fork 仅 cp313 .so」是本机构建产物而非约束（`git ls-files` 0 条；fork `requires-python >=3.12`）。解释器保持 3.12，三处偏离取消。**假设**：owner 若仍要升 3.13 须另给理由 | ⏳ 待 owner 确认撤回 |
| DEV | PS `shared/nautilus` | **D3 维持 Plan 60 Slice E defer**：不删不升，与 NT 2.0 正交 | ✅ owner |
| DEV | `adapter/trading_config.py` | **D5b 根类形态已定**：普通子类 + kw-only `__init__` + `__setattr__` 冻结守卫 + `__eq__`，无 `__hash__`；失去的 msgspec 语义逐项列于 D5b。中风险模型结构变更，起草期定稿 | ✅ 本 fix 定稿（`68eab22` Fix 4）|
| DEV | `host.py:92` / Task 8 | **D4 假设：本 plan 不交付 SoDEX live**。依据 plan 自述「只做能跑」与 Task 8 Step 3 只验 sandbox / testnet；白名单按 mode 拆分后 SoDEX 只进 sandbox / testnet 集合。owner 若要交付 live，另按高风险偏离审议 | ⏳ 待 owner 确认 |
| DEV | Task 2b | **`engine_version` 是跨仓契约字段**（mandatory-rules §3）：custos 只改自有 V1 文件，vendored golden 与 PS / Crucible 侧列为 Blocked，不得自行改写 | ✅ 规则约束，无需批准 |
