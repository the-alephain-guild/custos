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

fork as-of：`cda7fd412e953d37f65f0c8166de772310fde883`（2026-09-11 实读；**fork 在活跃开发中**，
本会话内 HEAD 由 `a27d9a3569` 前进到此，引用时须重新核对）。

| 契约 | 锚点 | 实读内容 |
|---|---|---|
| fork 版本 | `python/pyproject.toml:3` | `version = "2.0.0rc5"` |
| fork 解释器 |  `python/pyproject.toml:25` | `requires-python = ">=3.12,<3.15"` |
| fork 核心依赖 |  `python/pyproject.toml:26` | `dependencies = []`（`:30` 的 pandas 在 `visualization` extra，**非核心**） |
| fork 编译产物 | `python/nautilus_trader/` | 仅 `_libnautilus.cpython-313-darwin.so`（**Python 3.13 专用**） |
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
| 当前解释器 | `.python-version` | `3.12`（venv 实为 3.12.1，装 PyPI 1.230.0） |

**plan-to-plan 引用**：

| plan-id | commit-hash | 引用的文件/章节 |
|---|---|---|
| PS 60 | `6dd552d` | `.forge/plans/2026-07/60-retire-ps-shared-adopt-custos-toolkit.md`「Slice E：Owner 决定 defer（2026-07-30）」 |

### 基线：`make verify` 当前在干净主干上就是红的

`make toolkit-typecheck` 有 4 个**既有** mypy 错误，与本 plan 无关，实测（2026-09-11，
worktree 干净）：

```
adapter/orders.py:587,742,853  Argument "quantity" ... incompatible type "Any | Quantity | Decimal"; expected "Quantity"
adapter/coordinators/trade_event_handler.py:279  Argument 1 to "set_pending_signal" ... "Signal | None"; expected "Signal"
Found 4 errors in 2 files (checked 60 source files)
```

**本 plan 的验收判据是「这 4 个错误不增加、不被掩盖」，不是「typecheck 全绿」。** 不先钉住它，
升级后看到红会误判成自己弄坏的（教训 #50）。

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
| task_dag | Slice A 串行前置；B / C 可并行；D 依赖 B+C；E 依赖 A；F 最后 |
| host_context | orchestrator=custos worktree；workspace_owner=custos；nested_workspace_allowed=false |
| dispatch_authority | user |

## Execution Owner

- owner: `runtime-resolved execution surface`
- acceptance: 每个 Slice 以 `make check-authority` exit 0 + 该 Slice 的失败模式测试通过为准
- concurrency: 单一 owner；Slice B 与 C 若并行须各自 worktree，禁止并发写同一文件

## Execution Handoff

| Step | Source | Target | Artifact | Status |
|---|---|---|---|---|
| 1 | planning surface | runtime-resolved execution surface | plan commit | pending |
| 2 | execution surface | planning surface | Slice close-out commit | append-only |

## 关键设计决策 (Key Design Decisions)

| 问题 | 决策 | 理由 |
|---|---|---|
| **D1** fork 如何进依赖链 | `[tool.uv.sources]` path 依赖直指 `external/backtest-engines/nautilus-trader/python`，`.python-version` 改 3.13 | owner 2026-09-11 定案。**已知代价**：单仓 clone 装不起来，与 custos「外部审计员 clone 单仓可验证」的自足纪律冲突；PS Plan 60 commit `6dd552d` 的 message 正是「install the toolkit by command, so the lock stops naming a path」，同型问题那边选了相反方向。本 plan 接受该代价并登记为遗留项，wheel/OCI 化另起 follow-up |
| **D2** Python 版本 | 3.12 → 3.13 | 物理约束，非偏好：fork 唯一编译产物是 `_libnautilus.cpython-313-darwin.so`。连带放宽 `toolkit-nautilus` 的 `requires-python` 到 `>=3.12,<3.15`，并改 `tech-stack.md`「workspace interpreter 固定 3.12」 |
| **D3** PS `shared/nautilus` 是否拉进来 | 不拉进来，维持 PS Plan 60 Slice E 的 defer | owner 2026-09-11 定案。实证：4 个 nautilus 策略本体已全部 import `custos_toolkit`，无一 import `shared`；`shared/nautilus` 现仅服务 crucible 两个 Dockerfile 与 hummingbot 侧，与 NT 2.0 正交。其删除前置（arx 上线、custos Plan 24）与本次无关 |
| **D4** G6 venue 白名单 | 保留 `binance` / `binance_perpetual`，**新增** SoDEX 两个 connector；**不改为「NT 支持的全部交易所」** | 该集合的契约是「custos 已接线的 connector」而非「NT 支持什么」——`host.py:211`/`:349` 的 `supports_venue()` 消费它，而同 host `:346` 允许 `live`，故列入即等于声明该 venue 可跑 live。而「可跑 live」在 custos 是 `venue_binance.py` 293 行逐项落实的（`_LIVE_MIN_APPROVERS = 2`、`require_live_owner_evidence()`、三套 exec config 构建、凭据处理），每 venue 各一套、无通用实现。填入未接线 venue 会让 G6 判定入口宽于实际能力，红线 0.2 降为空门（教训 #22 同型）。drift-guard `tests/test_nt_binance_venue.py:53` 现断言 `_SUPPORTED_VENUES == frozenset(_BINANCE_CONNECTORS)`（**等号**），本次须改为「等于所有已接线 connector 的并集」并将该测试泛化改名 |
| **D5** 4 个指标薄包装是否删 | **保留**，仅去掉 `Indicator` 基类 | 2.0 确有 `MovingAverageConvergenceDivergence` / `DirectionalMovement` / `AverageTrueRange` / `RelativeStrengthIndex`，但删包装会改变策略侧 import 面并牵动 PS。本次只做「能跑」，收敛到引擎原生另起 follow-up。SuperTrend（418 行）2.0 无对应实现，必须保留 |
| **D6** 批量改 import 的手法 | AST/token 级替换，逐条断言「整行唯一匹配」，改后 `ast.parse` + 看 diff | 教训 C10：行级正则曾把 65 处追踪号清理做成 9 文件语法错 + 更多文件语义静默损坏，而测试照常通过 |

## 承载决策 (Capability Hosting Decision)

不新增能力，纯升级现有代码，无需 skill / hook / plan mode 承载。

## 文件清单 (File Inventory)

| 文件路径 | 操作 | 描述 |
|---|---|---|
| `.python-version` | Modify | 3.12 → 3.13 |
| `pyproject.toml` | Modify | `[tool.uv.sources]` 加 fork path；pandas 显式声明 |
| `packages/custos-strategy-toolkit-nautilus/pyproject.toml` | Modify | `requires-python` 放宽；NT pin 改 fork |
| `packages/custos-strategy-toolkit/src/custos_toolkit/contracts/toolkit_rc.py` | Modify | `:113-118` 版本契约改 2.0.0rc5（V1 in place，不留兼容别名） |
| `docs/authority/ecosystem-authority.json` | Modify | `:78` nautilus_trader 版本 |
| `docs/authority/strategy-artifact-ref-v1.golden.json` | Modify | `:17` engine_version |
| `docs/authority/strategy-artifact-pre-import-verification-v1.golden.json` | Modify | `:23,:77` engine_version |
| `packages/.../adapter/**` (38 文件) | Modify | 40 个符号对路径拍平 |
| `packages/.../adapter/indicators/{macd,adx,atr,rsi,supertrend}.py` | Modify | 去 `Indicator` 基类，逻辑不动 |
| `packages/.../adapter/trading_config.py` | Modify | `:64` `NautilusTradingStrategyConfig` 改 pyclass 子类形态 |
| `packages/.../adapter/signal_processor.py` | Modify | `:21` 随根类改 |
| `packages/.../adapter/{runtime_types,sizing}.py` | Modify | `Instrument` 类型注解改 2.0 具体类型 |
| `src/custos/engines/nautilus/host.py` | Modify | `TradingNode` → `LiveNode`；`:92` 白名单加 SoDEX |
| `src/custos/engines/nautilus/venue_binance.py` | Modify | Binance adapter 符号路径适配 |
| `src/custos/engines/nautilus/venue_sodex.py` | **Create** | SoDEX 连接器装配（对照 `venue_binance.py` 形态） |
| `src/custos/engines/nautilus/runner_safety.py` | Modify | `PriceType` 等路径适配 |
| `tests/**` (69 文件) | Modify | 路径拍平 + testkit 改名 |
| `tests/support/nt_stubs.py` | **Create** | 手写 4 个 stubs 的替代 fixture（2.0 无 `test_kit.stubs`） |
| `tests/test_nt_binance_venue.py` → `tests/test_nt_venue_wiring.py` | **Rename** + Modify | drift-guard 泛化：断言白名单 == 所有已接线 connector 并集，覆盖 Binance + SoDEX |
| `.claude/rules/tech-stack.md` | Modify | 解释器与 NT 版本约束 |
| `docs-site/docs/08-toolkit/overview.md` + zh-Hans 对应 | Modify | `:106` / `:69` 版本表 |
| PS `{trend,momentum,portfolio,_template,fixtures}/**` (8 文件) | Modify | 10 个符号对路径拍平 |
| PS `tests/**` (25 文件) | Modify | 随策略改动适配 |

## 失败模式覆盖契约 (Failure-Mode Coverage)

教训 #17：happy-path 全绿 ≠ 失败模式覆盖。本 plan 必须覆盖以下场景，每条至少 1 个测试：

| 失败模式 | 为什么必须测 | 归属 Slice |
|---|---|---|
| 解释器为 3.12 时装 fork | fork 的 .so 只有 cp313，须 fail closed 而非静默降级 | A |
| `toolkit_rc` 收到 1.230.0 | 旧版本号必须被契约拒绝（证明改的是 V1 而非加了别名） | A |
| Python 指标缺 `initialized` | 2.0 鸭子类型桥接靠 getattr，缺属性时须报错而非静默不更新 | B |
| `StrategyConfig` 子类漏 `super().__init__()` | pyclass 未初始化的失败必须可诊断 | B |
| SoDEX live 未过 G6 | 红线 0.2：`NoopHost` 或未授权时 live 必须 deny | C |
| 非白名单 venue 请求 live | `_SUPPORTED_VENUES` 之外必须拒 | C |
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

#### Task 1: 解释器与 fork path 依赖
**Files**: `.python-version`, `pyproject.toml`, `packages/custos-strategy-toolkit-nautilus/pyproject.toml`
**Step 1（证伪）**: 在当前 3.12 venv 跑 `uv run python -c "import nautilus_trader; print(nautilus_trader.__version__)"`，记录输出为 `1.230.0`
**Step 2（实现）**: `.python-version` 改 3.13；`[tool.uv.sources]` 指 fork `python/` 目录；`requires-python` 放宽 `>=3.12,<3.15`；pandas 显式进依赖（fork 核心依赖为空）
**Step 3（证实）**: 同一命令输出 `2.0.0rc5`，且 `python/nautilus_trader/adapters/sodex` 可 import
**Step 4（失败模式）**: 强制 3.12 解释器安装须 fail closed，断言错误信息指向 cp313
**Step 5**: commit

#### Task 2: 契约层版本号原地改 V1
**Files**: `toolkit_rc.py`, 3 个 authority json, 10 个测试文件, `tech-stack.md`, docs-site 中英
**Step 1（证伪）**: 跑现有断言 1.230.0 的测试，确认全绿（证明它们真的在断言）
**Step 2（实现）**: 逐处改 2.0.0rc5 + `python_requires` 放宽。**按 CLAUDE.md first-production V1 规则原地改，禁止加兼容别名或 predecessor parser**
**Step 3（证实）**: `make check-authority` exit 0
**Step 4（失败模式）**: 构造 `nautilus_version="1.230.0"` 的 ToolkitRc，断言 `ValueError`
**Step 5**: commit

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

#### Task 5: StrategyConfig 改 pyclass 子类
**Files**: `adapter/trading_config.py:64`, `adapter/signal_processor.py:21`, `adapter/registry.py`, `adapter/trading_strategy.py`, `adapter/strategy_core.py`
**Step 1（证伪）**: 实例化 `NautilusTradingStrategyConfig` 报 msgspec/pyclass 冲突
**Step 2（实现）**: 改自写 `__init__(self, *, ..., **_kwargs)` + `super().__init__()` 形态。**注意 `frozen=True` 对 pyclass 无效，冻结语义须另行实现或显式放弃并登记**
**Step 3（证实）**: 构造 + registry 解析 + 策略装配走通
**Step 4（失败模式）**: 漏 `super().__init__()` 的变体须给出可诊断错误
**Step 5**: commit

#### Task 6: Instrument 类型注解
**Files**: `adapter/runtime_types.py:20`, `adapter/sizing.py:12,18`
**Step 1-4**: 改为 2.0 具体类型或 Protocol；mypy 错误数不超过基线 4 个
**Step 5**: commit

### Slice C — daemon engine host（2,091 行，红线所在，可与 B 并行）

#### Task 7: TradingNode → LiveNode
**Files**: `src/custos/engines/nautilus/host.py`
**Step 1（证伪）**: `import nautilus_trader.live.node` 报 ImportError
**Step 2（实现）**: 换 `LiveNode.build()` / `LiveNode.builder()`；`TradingNodeConfig` / `LoggingConfig` / `LiveExecEngineConfig` 换 2.0 对应物
**Step 3（证实）**: sandbox 模式起停一轮
**Step 4（失败模式）**: 启动失败须传播，不留半启动状态
**Step 5**: commit

#### Task 8: SoDEX 接入与 G6 白名单
**Files**: `src/custos/engines/nautilus/venue_sodex.py`(新), `host.py:92`, `tests/test_nt_binance_venue.py`
**Step 1（证伪）**: 请求 SoDEX venue 被 `_SUPPORTED_VENUES` 拒
**Step 2（实现）**: 新增 `venue_sodex.py` 装配 `SodexDataClientFactory` / `SodexExecutionClientFactory`，并比照 `venue_binance.py` 补齐 live 侧的 `_LIVE_MIN_APPROVERS` 与 owner evidence 校验（缺这套就不得进白名单）；白名单**新增**而非替换；drift-guard 断言由 `== frozenset(_BINANCE_CONNECTORS)` 改为「等于所有已接线 connector 的并集」，测试由 `test_nt_binance_venue.py` 泛化改名为 `test_nt_venue_wiring.py`
**Step 3（证实）**: SoDEX sandbox/testnet 可装配
**Step 4（失败模式· 红线 0.2）**: SoDEX **live** 在未过 G6 时必须 deny；`NoopHost` 下 live 必须拒。**这两条是红线测试，不得跳过**
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
**Step 4**: `make toolkit-typecheck` 错误数 **≤ 基线 4 个**，逐条比对是否同一批
**Step 5**: commit

### Slice E — PS 策略本体（8 文件，仅依赖 Slice A）

#### Task 12: PS 策略 import 拍平
**Files**: PS `{trend,momentum,portfolio,_template,fixtures}/**/refinement/nautilus/*.py`
**Step 1（证伪）**: 策略 import 失败
**Step 2（实现）**: 10 个符号对路径拍平（零重写）；策略的 Config 子类随 Slice B Task 5 的根类形态调整
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
6. **登记遗留项**：D1 的 path 依赖使单仓 clone 不可装，wheel/OCI 化另起 follow-up；D5 的 4 个指标薄包装未收敛到引擎原生
7. `git add` + `git commit -m "docs(custos): mark plan 01 as completed"`

## 验证清单 (Verification)

- [ ] `make check-authority`: exit 0
- [ ] `make test-baseline`: PASS
- [ ] `make toolkit-typecheck`: 错误数 ≤ 基线 4 个，且为同一批（**非全绿**）
- [ ] `pytest --collect-only` 收集数不低于改前，skip/uncollectable 文件逐个点名
- [ ] 红线专项 grep（`verification.md` §红线专项检查）全过
- [ ] 失败模式覆盖契约 9 条逐条有测试
- [ ] 所有引用的前置契约均有 `file:line` 证据锚（Step 1.5 gate）
- [ ] 无死代码：1.230.0 残留 grep 归零（docs-site 除历史记述外）
- [ ] PS 侧 `make verify` 达到其自身基线

## 进度追踪 (Progress)

| Task | Status | Completed | Notes |
|---|---|---|---|
| 1 | 🔲 | | |
| 2 | 🔲 | | |
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
| DEV | `pyproject.toml` | **D1 path 依赖破坏单仓自足**：custos 的 `CLAUDE.md` §8 声明外部审计员 clone 单仓即可验证；path 依赖指向 workspace 外，clone 后装不起来。owner 2026-09-11 明示接受，wheel/OCI 化另起 follow-up | ✅ owner |
| DEV | `.python-version` / `tech-stack.md` | **D2 解释器 3.12 → 3.13**：`tech-stack.md` 现声明「workspace interpreter 固定为 Python 3.12」，本 plan 改之。物理约束（fork 仅 cp313 .so），非偏好 | ✅ owner |
| DEV | PS `shared/nautilus` | **D3 维持 Plan 60 Slice E defer**：不删不升，与 NT 2.0 正交 | ✅ owner |
| DEV | `adapter/trading_config.py` | **D5 `frozen=True` 语义可能丢失**：msgspec 的 frozen 对 Rust pyclass 无效。若无法等价实现，须显式放弃并在 close-out 登记影响面 | ⏳ 待实施时定 |
