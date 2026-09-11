# 品味审查报告 — plan 01 文件清单（NautilusTrader 2.0 升级触碰面）

- 扫描范围: `--files` 14 个现存代码文件（plan 01 File Inventory 中已存在的 `.py`）：`src/custos/engines/nautilus/{host,venue_binance,runner_safety}.py`、adapter 的 `trading_config.py` / `signal_processor.py` / `runtime_types.py` / `sizing.py` / `indicators/{macd,adx,atr,rsi,supertrend}.py`、`custos_toolkit/contracts/toolkit_rc.py`、`tests/test_nt_binance_venue.py`。合计 4,613 行。
- 宪法版本: 生态级 `the-alephain-guild/.claude/rules/coding-taste.md`（核心条款 + 8 收尾条）。custos 仓自身无 `coding-taste.md`，按 Step 2 上溯 workspace 根命中。子系统特化取 custos `code-style.md`（脱敏日志 / Decimal / structlog kwargs）。
- 检测: grep 机械层（§3.0 清单模式，NUL + `xargs -0 rg -H --`）+ Claude 语义层。未传 `--dim`，全维度。未调 codex，未开 `--adversarial`。
- 触发来源: `/forge:review --chain` Step 4.5 默认品味审查。**这份 plan 尚未执行**，本报告审的是它将要触碰的存量代码，findings 供 plan 起草者决定「顺手收尾 / 明确不碰」，并供下游 `/forge:fix` 与本 plan 审计报告并列消费。

## 🔴 高危害 (0)

无。8 处 `except Exception` 全部实读：`runner_safety.py:155/183` 记 structlog 后拒单、`:166/196/218` 回滚后 `raise`、`host.py:484/496` 清理后 `raise`，都是注明或自明的 fail-closed；唯一未记日志的一处降为 🟡（见下）。

## 🟡 中 (5)

| 宪法 | 位置 | 现象 | 建议（不改，留给 /forge:fix） |
|------|------|------|------|
| 二 类型 | `adapter/trading_config.py:265,273` | `getattr(config_wrapper, "snapshot", None)` / `getattr(config_wrapper, "signal", None)`，`config_wrapper` 是 custos 自己 `load_config` 产出的强类型对象，却当 dict 防御读；同函数 `:269` 又用 `hasattr` 再防一次 | 在 `load_config` 边界把两个 section 归一为确定字段（缺省 `None`），此处直取。本 plan Task 5 正要重写这个文件的根类，是顺手收口的时机 |
| 二 类型 | `runner_safety.py:67-73,109-113`（共 10 处） | money 路径上的静默默认链：`getattr(command, "quantity", None) or order.quantity`、`getattr(command, "price", None) or getattr(order, "price", None) or getattr(order, "trigger_price", None) or cache.price(...)`、`getattr(mark, "value", mark)`。任一字段在 2.0 改名，`or` 链会无声滑到下一个来源而不是报错 | 与 NT 对话的边界收成一个小型 typed 读取器（Protocol 或显式 `match` 分支），字段缺失时 `raise`。本 plan Task 9 触碰本文件且承担红线 0.4 回归，应把「字段名在 2.0 下逐个实证」写进 Step 1 |
| 五 错误处理 | `runner_safety.py:203-213` `modify_order` | `except Exception:` 后只 `generate_order_modify_rejected(... _POLICY_REJECTION_REASON ...)` 并 `return`，**没有 structlog**，也没保留 `type(exc).__name__`；同文件 `submit_order` / `submit_order_list` 两条同型路径都记了 `runner_order_reservation_rejected`（`:157-161` / `:185-190`）。拒单事件本身进 NT 审计流，但 reservation 为何失败在日志里不可见 | 补一条 `_log.warning("runner_order_modification_rejected", reason_code=..., error_type=...)`，与 submit 路径对称（教训 #21 零静默） |
| 四 诚实 | `runner_safety.py:391-394` | 注释「NT 1.230.0 injects Sandbox's portfolio argument by factory class name」+ 改写 `__name__` 的 workaround，是对 1.230.0 内部行为的版本耦合声明 | 升级后这句要么被证实仍成立、要么随 `LiveNode` 装配一起删。本 plan Task 7/9 应把它列为「必须重新实证的运行时假设」，不能让注释在 2.0 下变成陈述一个不存在的行为 |
| 核心 · 消除特殊情况 | `adapter/indicators/supertrend.py:203-215` | `update_raw` 内「Fallback: try to find column by prefix if exact match fails」——对 pandas-ta 返回列名做前缀搜索兜底；而同文件 `:22` 已有 `_supertrend_column_names()` 按 pandas-ta 命名约定精确构造列名。两套机制并存，读者无法判断哪套是真的 | 二选一：信任 `_supertrend_column_names` 并在不匹配时 `raise`（vendored pandas-ta 版本固定，列名不会漂），删掉前缀搜索；或把前缀搜索收进 `_supertrend_column_names` 一处。本 plan D5 决定保留 SuperTrend 包装，正好一并收尾 |

## 🟢 低 (3)

| 宪法 | 位置 | 现象 | 建议 |
|------|------|------|------|
| 一.3 分隔条 | `tests/test_nt_binance_venue.py:232,243` | 两条 `# ----` 装饰线框住一段 12 行的测试动机叙述 | 叙述本身有价值（记录了 2026-08-01 testnet 实测），去掉分隔线、改成该 test 的 docstring。本 plan Task 8 要把这个文件改名泛化，顺手 |
| 一.2 一处信息一次 | `adapter/indicators/macd.py:1` vs `:12` | module docstring「MACD … indicator wrapping pandas-ta」与 class docstring「MACD … indicator using pandas-ta」几乎逐字重复（只抽查了 macd.py，其余 4 个指标文件未逐一核对） | 留一处。Task 4 去 `Indicator` 基类时顺手 |
| 核心 · 命名 | `adapter/indicators/{macd,adx,atr,rsi,supertrend}.py` 共 17 处 `result = ta.xxx(...)` / `val = result[col].iloc[-1]` | 局部变量名无领域语义，但作用域都在 5 行内、且紧邻 `self._macd_line = float(val)` 这类有名字的目的地 | 不值得单独动；Task 4 改这些文件时若顺手改名（`series` / `latest`）可以，不改也不构成债 |

## ⬆️ 上界·核心条款 (1) — 数据结构上界待审，非机械修复

| 宪法 | 位置 | 结构 / 职责 / 依赖方向问题 | handoff（数据结构先行，非拆分中心）|
|------|------|------|------|
| 三 尺度 / 核心 · 分解 | `src/custos/engines/nautilus/host.py`（1,403 行；`deploy` `:385-527` 142 行，`_build_runner_fact_context` `:611-706` 95 行） | 症状是巨型文件与两个长函数；根因在数据结构：`DeploymentSpec` 以裸 `dict` 贯穿整个 host，`deploy` 与 `_build_runner_fact_context` 各自 `spec.get("strategy_id")` / `spec["trading_mode"]` / `spec.get("pairs")` 并各自 `raise RuntimeError("validated DeploymentSpec lost its …")`——同一份 spec 在同一进程内被反复重新验证。27 处 `getattr(node/kernel/strategy, …)` 是第二层症状：NT 对象的形状同样没有在边界归一，靠每个调用点自己 `callable()` 再 `raise` | **本 plan Task 7 正要把 `TradingNode` 体系整体换成 `LiveNode`，这是 host 数据结构定型的唯一窗口，不该在裸 dict 上做一次「路径替换式」重写。** 重构须数据结构先行：(1) 在 host 入口把 spec 归一成一个 frozen 的 typed 视图（宪法二「边界归一化，内部直取」），`deploy` / `_build_runner_fact_context` 改为直取字段；(2) NT 节点的 5 个能力（loop / kernel.dispose / executor / trader.strategies / cache）收成一个 typed adapter，27 处 `getattr` 收口为一处；(3) 之后再看行数——判据是「`RuntimeError("… lost its …")` 这类重复校验是否消失」，不是文件是否变短。**禁用「待拆分」措辞**，按行数切 host 只会把裸 dict 复制进更多文件 |

**依赖方向**：本 scope 全部处于 adapter / engine host（IO 边缘）或 contracts（纯 pydantic，无 IO import）。`indicators/*.py` import `nautilus_trader` 与 vendored pandas-ta 是适配层的正方向；`toolkit_rc.py` 无 IO 依赖。**无高置信反向命中**，不进 ⬆️ 计数。

## 🔬 近似探针命中复核 (12) — 命中→复核→结论，禁 silent skip

> 对账：god 函数 Step 3 命中 **11** = 🔬 11 行（finding 2 + 排除 9）；成组裸参数命中 **1** = 🔬 1 行（排除 1）；包扁平度 / 垃圾桶包名清单模式 N/A，0 行。合计 12 行。

| 近似类型 | 位置 | 复核判定 | 理由 |
|------|------|------|------|
| god 函数 | `host.py:210 supports_venue`（~81 行距）| 排除 | 实读 3 行函数；探针的 `^\s*def` 不匹配 `async def`，行距把后面两个 async 方法算了进来 |
| god 函数 | `host.py:382`（~145 行距，实为 `:385 deploy`）| **finding → ⬆️**（并入上界条目）| 实读 142 行：幂等守卫 → artifact 校验 → exec plan → NT 超时旋钮 → 分区认领 → 建节点 → 挂 bridges → `run_async`，每段都是 fail-fast 分支。职责是「一次部署的生命周期」并不散，但长度来自对裸 `dict` spec 与 NT 对象的逐点校验——归数据结构上界，不按行数拆 |
| god 函数 | `host.py:611 _build_runner_fact_context`（~95 行）| **finding → ⬆️**（并入上界条目）| 实读 95 行：8 个 `spec` 键的逐个 `.get` + 校验 + 两处 `raise RuntimeError("validated DeploymentSpec lost its …")`，与 `deploy` 重复校验同一份 spec。同上 |
| god 函数 | `host.py:763 _dispose_node_preserving_runner_loop`（~72 行距）| 排除 | 结构扫描（含 `async def`）显示真实长度 < 60；行距含后续 async 方法 |
| god 函数 | `host.py:850 _instrument_ids`（~114 行距）| 排除 | 实读 8 行 staticmethod；行距吞掉了 `:898 _flatten_and_confirm_shutdown`（66 行，探针本身漏报）。后者实读为单一职责的确认轮询循环：取消挂单 → 平仓 → 等稳定 N 轮 → 超时 `raise`，注释说清了 Binance `-2022` 的顺序约束，不构成 finding |
| god 函数 | `host.py:964 attached`（~146 行距）| 排除 | 实读 < 20 行；行距含多个 async 方法 |
| god 函数 | `host.py:1110 runner_fact_deployments`（~269 行距）| 排除 | 实读 2 行 property；其后是 12 个 `async def`，探针全部跳过 |
| god 函数 | `indicators/supertrend.py:151 update_raw`（~101 行）| 排除（god）；其中 `:203-215` 另记 🟡 | 实读 101 行：缓冲门控 → pandas-ta 调用 → 列名解析 → NaN 校验 → 写 4 个状态字段。单一职责（一步指标更新），长度来自防御分支；真正的味道是列名双机制，已单列 🟡 |
| god 函数 | `toolkit_rc.py:147 validate_member_matrix`（~74 行距）| 排除 | 行距跨过 `:155` `:163` 两个 class 定义；实读 8 行 |
| god 函数 | `toolkit_rc.py:221 validate_sbom_matrix`（~92 行距）| 排除 | 行距跨过 `:233` `:242` 两个 class；实读 12 行 |
| god 函数 | `toolkit_rc.py:313 validate_oci_publication`（~92 行距）| 排除 | 到 `:359` 下一个 class 为止 46 行，未超 60；pydantic `model_validator` 逐字段交叉校验，职责单一 |
| 成组裸参数 | `host.py:547 _build_exec_plan(self, trading_mode, spec, credential, venue)` | 排除 | 探针把 `self` 计入；真实 4 参数，且 `spec` / `credential` 已是两个聚合对象 |

**其他机械命中的定性**（非近似探针，但按铁律留痕）：

- getattr 39 处：`host.py` 27 处实读为 NT 对象鸭子类型 + `if not callable(...): raise RuntimeError(...)` 的 fail-fast 形态，`:1270` 附近有显式理由「Duck-typed, not isinstance: the toolkit is not required of a deployment」——**排除**为单点 finding，症状并入 ⬆️ 上界条目的 (2)；`runner_safety.py` 10 处为静默默认链——**🟡**；`trading_config.py` 2 处对自有类型防御——**🟡**。
- 垃圾变量名 30 处：`bar` 11 处是领域词（K 线）——排除；`val` 10 + `result` 7 ——🟢；`data` 2 处在 `signal_processor.py:186-197 receive_okx_signal(data: dict)`，是外部 OKX 载荷的边界参数，名字泛但语义由 docstring 与类型给出——排除。
- 裸 except 8 处：见 🔴 段说明，7 处排除、1 处 🟡。

## 统计与优先级

- 探针存活证明: **10/10** 条经 `fixtures/mechanical/positive` + 现生成 700 行样本实证会命中（本轮 `--files` 接法：`tr ',' '\0'` → `xargs -0 rg -n -H … --`）：编号注释 7 · 分隔条 4 · getattr 2 · print 4 · 中文日志 4 · 裸 except 2 · 垃圾变量 6 · 成组裸参数 1 · god 函数 1 · 巨型文件 1，与 skill 参考量级一致。存活证明清单末尾因 `paste` 多带一个换行，最后一个假路径报 `No such file`，不影响命中计数（正式扫描用 `printf` producer，无此尾巴）。清单模式下目录尺度探针（垃圾桶包名 / 包扁平度①②）记 **N/A**。本轮对目标 scope 的调用退出码：全部合法（0 或 1）。
- 零命中且已证活、记「干净」: 编号注释（一.1）· print（七）· 中文日志描述（七）· 同文件注释中英跳变（六，`\p{Han}` 全文 0 命中）。
- 各级: 🔴 0 · 🟡 5 · 🟢 3 · ⬆️ 1（上界·核心条款）。按条目分布: 二 类型 ×2 · 五 错误处理 ×1 · 四 诚实 ×1 · 核心·消除特殊情况 ×1 · 一.3 ×1 · 一.2 ×1 · 核心·命名 ×1 · 三/核心·分解 ×1。
- 近似探针复核对账: god 函数 命中 11 = 🔬 finding 2 + 排除 9 ✅；成组裸参数 命中 1 = 🔬 finding 0 + 排除 1 ✅。探针已知漏报一条（`_flatten_and_confirm_shutdown` 66 行，因 `async def`），已在复核表内补记并定性排除。
- 维度范围: all（未传 `--dim`）。
- **与 plan 01 审计报告的衔接**（`01-nautilus-trader-2-0-upgrade-review.md`）: ⬆️ 条目直接约束 Task 7 的做法——不要在裸 dict 上做 `TradingNode → LiveNode` 的替换式重写；🟡 第 2、4 条约束 Task 9 的 Step 1 要逐字段实证 2.0 的对象形状；🟡 第 1、5 条与 🟢 三条都落在本 plan 已要改的文件里，可作 Task 4/5/8 的顺手收尾项，但须在 plan 里明写「顺手」还是「不碰」，不留给执行者临场决定。
- 建议先处理: (1) ⬆️ host 数据结构定型（在 Task 7 之前，不是之后）；(2) 🟡 `runner_safety.py` 静默默认链 + `modify_order` 无日志（红线 0.4 邻接）；(3) 🟡 `runner_safety.py:391` 版本耦合注释纳入 Task 9 实证清单；(4) 🟡 supertrend 列名双机制；(5) 其余顺手。
