# 25 — Binance 拒收本机生成的 client order id（超长，订单一律发不出）

> **Status**: ✅ Completed (2026-07-30) —— 代码侧四个 Task 全绿; 计划自定的**唯一完成判据**(真机接单)
> 已在 PS 侧取得: 三轮 testnet 实跑, 我方 32 字符 client order id 三次被 `OrderAccepted`, `-4015` 0 次
> (证据见 §验证清单末项)
> **Created**: 2026-07-30
> **Project**: custos (`tesseract-trading/custos/`)
> **Depends on**: 无 —— 现有代码即可复现，不依赖任何未落地的能力
> **Blocks**: 离线通道与签名通道在 Binance 上的**任何**下单；PS Plan 61 的 testnet 端到端验收
> **For Claude**: `/forge:execute`；单 session（4 Task，无跨仓库改动）
> **multi_session_scope**: false

## 上下文 (Context)

2026-07-30，philosophers-stone 首次以 `MODE=testnet` 跑离线通道。这是这条通道第一次真正走到
交易所下单路径 —— 策略起来了、行情订阅上了，但**第一次 `submit_order` 就被交易所拒**：

```
ERROR CUSTOS-dcb00e520b45569e83b0.ExecClient-BINANCE: Failed on submit_order
  ClientOrderId('O-20260730-044937-dcb00e520b45569e83b0-000-2'):
  BinanceClientError({'code': -4015, 'msg': 'Client order id length should be less than 36 chars'})
```

该 id 实测 **44 字符**（`printf '…' | wc -c`），Binance 上限 36。**这不是市场原因、不是权限、
不是偶发** —— id 的构造方式决定它每次都超长，所以这条通道在 Binance 上一单也发不出去。

### 根因：trader id 带了 20 字符的 deployment instance id

`src/custos/engines/nautilus/host.py:806-808`：

```python
tag = "".join(ch for ch in deployment_instance_id if ch.isalnum())[:20] or "000"
return f"CUSTOS-{tag}"
```

NautilusTrader 的 client order id 由**策略自己的** OrderFactory 生成
（`nautilus_trader/common/generators.pyx:117-151`），带连字符时格式为：

```
O-{YYYYMMDD}-{HHMMSS}-{trader_id_tag}-{strategy_tag}-{count}
```

`TraderId.get_tag()` 取最后一个连字符之后的部分，即上面那 20 字符。长度算术（实测复核，
`tag=20 / counter=1` 得 44，与观测值逐字符吻合，说明模型正确）：

| tag 长度 | counter 位数 | 带连字符 | 不带连字符 |
|---|---|---|---|
| 20 | 1 | **44** ✗ | 39 ✗ |
| 20 | 5 | 48 ✗ | 43 ✗ |
| 12 | 1 | **36** ✗（上限是「小于 36」）| 31 ✓ |
| 12 | 5 | 40 ✗ | 35 ✓ |
| 8 | 1 | 32 ✓ | 27 ✓ |
| 8 | 5 | **36** ✗ | 31 ✓ |
| 7 | 5 | 35 ✓ | 30 ✓ |
| UUID4 去连字符 | 任意 | — | **32 ✓（固定）** |

两点值得先说清，因为它们决定选型：

1. **上限是「小于 36」**，所以 36 本身就是失败。`tag=12` 配 1 位 counter 恰好 36 —— 一个看起来
   安全的选择实际不安全。
2. **任何结构化 id 的预算都随 counter 位数上涨**。counter 是每策略实例的下单计数，随交易量增长。
   即 A 类修法不是「改一个常数」，而是「把失败阈值推远」。

### 为什么此前不可能被发现

- sandbox 用 `SandboxSimulationHost`，撮合在本地，**从不向交易所提交订单**；
- sandbox 的数据客户端也不认证（`engines/nautilus/venue_binance.py`
  `build_data_client_config` 的 `trading_mode == "sandbox"` 分支把 `api_key` 置 None，
  docstring 明写 bootstrap 凭证「may be deliberately non-functional」）；
- 本仓 grep `4015` 与 36 字符相关处理 **零命中** —— 上游对这个约束没有任何认知或防护。

所以这是一个只有真实 venue 才能暴露的缺口，与本仓 C10 记的「绿的测试套说明不了什么跑过了」
同一族：**测试全绿加 sandbox 全绿，等于零个订单被交易所接受过**。

### 影响面

`_trader_id` 只有一个消费者（`host.py:323` 构造 NT config），且**本仓无任何地方从 trader id
或 client order id 反解**（grep `CUSTOS-` 的其余命中都是签名域分隔符，与此无关；唯一用到
trader id 的测试 `tests/test_credential_lifecycle.py:124` 用的是短值 `TraderId("CUSTOS-CRED")`）。
即改 id 形态在本仓内部没有隐藏消费者。

## 决策 (Decisions)

### 选型：三条路，都落在本仓

`NautilusTradingStrategyConfig` 继承 NT 的 `StrategyConfig`，实测其 `__struct_fields__`
**已含** `use_uuid_client_order_ids` 与 `use_hyphens_in_client_order_ids`
（`nautilus_trader/trading/config.py:88-89`，由 `trading/strategy.pyx:151-152,303` 传进
OrderFactory）。所以三条路都不需要新铺管道：

- **A — 缩短 trader id tag。** 保留「一眼看出是哪个 deployment」的可读性。代价：tag ≤ 7 才能
  扛住 5 位 counter；预算随交易量变化，是把阈值推远而非消除。
- **B — `use_uuid_client_order_ids=True` + `use_hyphens_in_client_order_ids=False`。**
  固定 32 字符，**与 tag 长度和 counter 位数都无关**。代价：id 本身不再携带归属信息。
- **C — tag 缩到 12 且关闭连字符。** 保留 48 bit 归属，5 位 counter 下 35 ✓，但 6 位即 36 ✗ ——
  仍有天花板，只是更远。

**推荐 B。** 理由不是「uuid 更时髦」，而是本 plan 的目的是让订单**能发出去**，而 A/C 都只是把
同一类失败的阈值往后推 —— 在一个已经因为「今天成立、上量后不成立」吃过亏的系统里，选固定宽度
比选更大的常数更值。

**归属信息不会因此丢失**：NT 的日志行本身就带 trader id 前缀
（`CUSTOS-dcb00e520b45569e83b0.ExecClient-BINANCE`，见上文报错原文），且本仓在下单路径上记
`deployment_instance_id`。要把一笔订单对回某个 deployment，应该查本仓日志而不是靠肉眼读交易所
界面上的 id —— 后者本来也只有截断后的 20 字符。

若 owner 认为交易所界面上的肉眼归属不可放弃，则选 C 并接受天花板，**但 Task 3 的最坏情况守护
不可省**。

### 上限值必须实证，不能沿用报错文本

报错说的是 Binance USDT 期货的 36。本 plan 不假设 spot、其他 venue、或未来的 Binance 版本
同值 —— Task 1 要把这个数字连同来源（适配器常量 / 官方文档 / 实测）一起钉下来，而不是把
`36` 直接散进代码。

## 目标 (Goal)

Binance 上的下单不再因 id 长度被拒，且这个约束有一道守护 —— 它在 counter 涨到最坏情况时也
必须仍然成立，而不是「今天量小所以够用」。

## 非目标 (Non-goals)

- **不修 PS 侧的 `make stop`** —— 那是 PS Plan 61 Task 1。
- **不追 `portfolio_equity_ambiguous`** —— 见下方 follow-up hook，先给 testnet 账户入金再判。
- **不改 sandbox 的执行路径** —— 但 Task 4 必须确认 sandbox 没被这次改动影响（它走同一份
  strategy config）。
- **不引入按 venue 分支的 id 策略** —— 除非 Task 1 实证出各 venue 上限确实不同。

## 实现任务 (Tasks)

### Task 1 — 先写一条会红的测试，并把上限钉成有来源的常量

1. 用运行时**实际的**构造路径生成一个 client order id（不要在测试里手抄格式字符串 —— 手抄
   等于测试自己的假设，见本仓 C10 的教训），断言其长度满足 Binance 上限。
2. 该测试**必须在修复前是红的**，并附上红的输出；否则无法证明它守的是这件事。
3. 把 36 定义为带来源注释的具名常量（来源：适配器 / 官方文档 / 实测三者之一，写明是哪个）。

### Task 2 — 落选定的修法

按决策选 B（或 owner 改选 C）。落点是 strategy config 的构造处，而非在 host 里硬塞 —— 让
「订单 id 形态」成为一个可见的部署配置，而不是埋在 trader id 的截断长度里。

### Task 3 — 最坏情况守护（**不可省**）

断言在下列条件同时成立时 id 仍合规：counter 取该实现可能达到的最大位数、instance id 取最长
形态、strategy tag 取最长形态。选 B 时这条依然要写 —— 它证明的是「与 counter 无关」这个性质
本身，而不是某个具体长度。

### Task 4 — sandbox 未受影响

sandbox 走同一份 strategy config。确认它仍能部署、撮合、上报 —— 改动的是 id 形态，不应触及
本地撮合，但这一点要被测到而不是被推断。

## 验证清单 (Verification)

- [x] 发布门的各组成部分全绿：`pytest tests/` 2181 passed / 25 skipped / 1 xfailed、
      `make lint`、`make check-authority`。`make fmt-check` 在主干上恒红，与本 plan 无关
      —— 三个被 receipt 按字节 pin 的文件不是 format-clean，见 `historical-lessons.md` C6。
- [x] Task 1 的测试修复前红、修复后绿，两侧输出见下方 close-out。
- [x] Task 3 的最坏情况守护绿，且用的是**实现自己的** counter 上界（`2**31 - 1`，
      靠把 `set_client_order_id_count` 推到拒绝为止量出来）而不是一个圆整数。
- [x] sandbox 路径真跑了一次本地撮合（不是推断）：`BacktestEngine` + 同一份 config →
      下单 → 读回成交，并断言成交单的 id 就是新形态。
- [x] **真机证据已取得（2026-07-30，PS 侧）。** `MODE=testnet` 三轮实跑，**我方生成的**订单被交易所
      `OrderAccepted`，三个不同 client order id 各 32 字符（无连字符）：
      `a39769df774a4fa39e8b73ae9d975966`、`4ccc9459f0a44cd099c938f28b82351f`、
      `5dbb46ff35394086b8af160d9d17b419`。三轮合计 `-4015` **0 次**、`OrderRejected` **0 次**。
      区分方式：同期日志里还有 36 字符带连字符的 id（`11adee49-…` / `041b1a31-…` / `da887090-…`），
      那些是 venue 侧对账产物、不是本 runner 生成的，**不能拿它们当本判据的证据**。
      前提条件同时满足：镜像按 custos HEAD 重建（revision `3085244`），且进容器核验过
      `BINANCE_CLIENT_ORDER_ID_LEN_LIMIT = 36` 与 `_client_order_id_too_long` 确实在镜像里 ——
      PS 实跑取的是镜像里的代码，不是仓里的 wheel，所以这一步不能靠 label 推。

## 偏离与改进日志 (Deviations & Improvements)

- 若 Task 1 发现各 venue 上限不同，记在这里并说明是否需要按 venue 分支（本 plan 默认不分支）。
  —— 未发现：只有 Binance 接线，无第二个 venue 可比。见 close-out 遗留项 3。
- 若选 C 而非 B，记下当时接受的 counter 天花板与理由 —— 选了 B，无天花板可记。

### DEV-25-LANDING-POINT-IS-THE-BUILDER-NOT-THE-CLASS
- **等级**: 低
- **原因**: 先试了在 `NautilusTradingStrategyConfig` 上重声明那两个继承字段的默认值，
  msgspec 拒绝：`Required field 'trading' cannot follow optional fields`。重声明会把字段
  挪到基类位置，把必填字段挤到可选字段之后。
- **决定**: 改落在 `build_nautilus_base_config` 的返回值里，并同步扩
  `NautilusBaseConfigSections`。这其实**更贴合**计划说的「config 构造处」，而且沿用了该处
  已有的惯例 —— `oms_type` / `external_order_claims` 本来就是这样传的 NT 层字段。
- **附带好处**: 它不进 strategy 的 YAML。venue 的约束不该让策略作者知道，更不该让他写错。

### DEV-25-TASK-4-PREMISE-CORRECTED
- **等级**: 低
- **原因**: 计划 Task 4 写「sandbox 走同一份 strategy config」。实测 sandbox 有**两条**路径：
  `--engine sandbox-sim` 的 `SandboxSimulationHost.deploy` 只记 lifecycle、**根本不建 NT
  节点、不碰 strategy**（`host.py:106-137`）；而 `--engine nautilus`（默认）+
  `trading_mode: sandbox` 才建真 NT 节点，经 `SandboxLiveExecClientFactory` 本地撮合，那条
  路径确实共用这份 config。
- **决定**: Task 4 针对后者写测试，且**真跑撮合**（`BacktestEngine` 下单 → 读回成交），
  并做了证伪：把 flag 翻回旧形态，该测试变红。前者无需覆盖 —— 它碰不到 id。

### DEV-25-TOOLKIT-SOURCE-EDITED
- **等级**: 中
- **原因**: 修法落在 `packages/…/adapter/trading_config.py`，该文件被
  `docs/authority/strategy-toolkit-*.json` 按 sha256 pin 住（lesson C6 的题目）。
- **决定**: 编辑并提交。两个 pin 门（`check-toolkit-extraction` /
  `check-toolkit-typing-closure`）哈希的是历史 git blob，不看工作区，所以不会因此变红 ——
  这一点在 Plan 24 已实证。但要诚实说清：这意味着 toolkit 从那次「零重写抽取」**开始分叉**，
  而 receipt 仍指向分叉前。**建议**下一次重签 receipt 时把本次改动纳入。
- **顺带更正**: 实施中发现 `test_toolkit_release_candidate_build.py` 会拦**未提交**的
  toolkit 漂移（比对工作区与 HEAD）。这修正了 Plan 24 close-out 里「本仓无任何东西会注意到
  toolkit 被改」的过头说法，已在该 plan 内留更正段（commit `32f7381`）。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-07-30（代码侧）
- **总 Task 数**: 4，全部落地
- **偏离数**: 3（见偏离日志）
- **实施 commit**: `3d22b82`
- **状态**: ⏳ —— 计划自定的唯一完成判据（真机接单）未取得，见验证清单最后一项

### 选型：按推荐走 B，且是实测过的 B

| 形态 | 长度 | 判定 |
|---|---|---|
| 默认（tag + counter，带连字符） | 44 | ✗ 观测到的那次拒绝 |
| 去连字符 | 39 | ✗ |
| uuid **带**连字符 | **36** | ✗ 上限是「小于 36」，36 本身就被拒 |
| uuid + 去连字符 | **32** | ✓ 落地这个 |

计划的长度表逐行复核成立，包括「uuid 单独开还不够」这个反直觉点 —— 只开
`use_uuid_client_order_ids` 得到 36，仍然会被拒。**两个 flag 都要。**

### 红 / 绿两侧（Task 1 要求）

修复前：

```
E       assert 44 < 36          # 实际生成的 id
E       assert 63 < 36          # 最坏情况
E       assert 5 == 1           # 长度随 counter 变化：{44, 45, 46, 49, 54}
3 failed, 2 passed
```

修复后 `6 passed`。另外两条修复前就绿、且**应该**一直绿：一条钉住 bug 本身（旧形态确实
产出 44 且尾部与观测值逐字符吻合），一条钉住「落点只能在 config」。

### 落点：为什么只能在 config

计划说落在 config 构造处而不是「在 host 里硬塞」。实证下来这不是偏好问题，是唯一可能：

- `use_uuid_client_order_ids` / `use_hyphens_in_client_order_ids` 在
  `trading/strategy.pxd:82,84` 是 `cdef readonly` —— **从 Python 不可赋值**；
- `OrderFactory` 在 `Strategy.register()`（`strategy.pyx:297-304`）里建，而 `register`
  由 `host.py:371` 的 `add_strategy` 触发。

所以 host 虽然是 venue 边界的拥有者、约束也来自 venue，却**碰不到**这个开关。这一点本身写成
了测试（`test_the_shape_is_decided_by_the_config_and_nothing_after_it`）：如果哪天这两个属性
变成可写，修法就可能挪到比 config 更隐蔽的地方，那条测试会先叫。

### 上限值的来源

`36` 落在 `venue_binance.py` 的 `BINANCE_CLIENT_ORDER_ID_MAX_LEN`，注释写明来源是**交易所
自己的拒绝报文**（2026-07-30 USDT-M futures testnet）。这不是偷懒：NT 的 Binance 适配器
grep 无此常量，futures 文档不给该字段长度，本仓此前对 `4015` 与 36 都是零命中。没有更权威的
来源可引。

### 测试条数（取自 `pytest --collect-only`）

| 测试文件 | 条数 |
|---|---|
| `tests/test_client_order_id_length.py` | 5 |
| `tests/test_client_order_id_sandbox_execution.py` | 1 |
| `tests/engines/nautilus/test_runner_safety_execution_boundary.py` | 12 |
| `tests/test_plan_closeout_counts.py` | 11 |

上表合计 29 条。

第三行是既有文件，本 plan 的 peer-review fix 往里加了 3 条边界强制测试（9 → 12），所以在这里
数它 —— 此前没有 plan 数过它，但「动了就重数」的纪律与谁先数过无关。第四行不是本 plan 加的
测试，是本 plan **让它变了**：那个探针按「带条数表格的 plan」参数化，本 close-out 一加表就多出
两个用例（9 → 11）。间接变动同样算，所以在这里重数而不是回去改 plan 24 的行 —— 那一行记的是
它当时交付了什么，不该被后来的 plan 改成滚动值。

第三行不是本 plan 加的测试，是本 plan **让它变了**：那个探针按「带条数表格的 plan」参数化，
本 close-out 一加表就多出两个用例（9 → 11）。规则是「动了别人数过的文件就在自己的 close-out
里重数」，间接变动同样算，所以这里重数而不是回去改 plan 24 的行 —— 那一行记的是它当时交付了
什么，不该被后来的 plan 改成滚动值。

### 红线 gate 满足度

| 红线 | code 覆盖 | runtime 接线 | 本 plan 影响 |
|---|---|---|---|
| 0.1 Key/KEK 不出进程 | 未触碰 | 未触碰 | 无。改的是 id 形态，不经凭证路径 |
| 0.2 G6 host gate | 未触碰 | 未触碰 | 无。`_build_exec_plan` 的 mode 分支一字未改 |
| 0.3 失联 ≠ 停止 | 未触碰 | 未触碰 | 无 |
| 0.4 Decimal money math | 未触碰 | 未触碰 | 无。id 是标识符，不参与金额 |

说明一句它**不**是什么：本 plan 让订单能被交易所收下，这既不放宽也不兑现任何一条红线。
真正待兑现的是 0.2 之外的另一件事 —— 这条通道从未有一笔订单被真实 venue 接受过。

### 外部审查（`--peer=codex`）

codex 报 0 CRITICAL / 1 HIGH / 3 MEDIUM / 4 LOW，逐条实证后 3 条已修、4 条判为不改并写明
理由、2 条转遗留。原文归档在
`.forge/reviews/2026-07/codex/25-binance-client-order-id-length-peer-review.md`，分诊在
`.forge/fixes/2026-07/25-binance-client-order-id-length-fixes.md`。

值得单独说的三件：

1. **它抓到我自己代码里两处事实错误。** 最坏情况的 counter：我写「`2**31-1` 是能渲染的最宽
   counter，十位」，实测 generate 先自增再渲染，溢出成 `-2147483648` —— 十一字符，比最大正值
   更宽。现在两个值都测。以及常量名把一个**排他**上界叫成「max」，来源注释还断言「文档不给
   长度」——那是我没查就写的，已收回到真验过的部分。
2. **HIGH 成立且已修**：原修法是**约定**不是不变量 —— 只有走 `build_nautilus_base_config`
   才成立，而 `runtime_loader.py:81-82` 直接采信 artifact adapter 自建的 config。已在
   `RunnerSafetyExecutionDispatch` 加边界强制（超限本地拒单 + 独立 reason），并证伪过。
   这一层正是 custos 拥有 venue 交互的地方，所以现在与谁建 config 无关。
3. **一条真实跨仓隐患**：见遗留项 0 —— 它同时是 Plan 24 Slice E 的新前置条件。

### 遗留项

0. **Speculum 的 fill fallback（跨仓，Plan 24 Slice E 前置）。** 已回写 Plan 24 遗留项。
1. **真机证据（阻塞完成）。** 需 PS 侧 `MODE=testnet` 复跑。判据：不再出现 `-4015`。
2. **`portfolio_equity_ambiguous` 让熔断 fail-closed** —— 计划已登记为 follow-up，先给
   testnet 账户入金再判，未实证故不下断言。
3. **其他 venue 的上限未实证。** 本 plan 不按 venue 分支（计划默认如此），因为只有 Binance
   接线。加第二个 venue 时这个常量要么被证明通用，要么变成按 venue 查。

## Follow-up hooks（不属于本 plan scope，登记以防遗漏）

- **`portfolio_equity_ambiguous` 让熔断 fail-closed。** 同一次 testnet 跑观测到：
  `fallback_breaker_fail_closed reason: portfolio_equity_ambiguous` →
  `positions_flattened instrument_count: 0` → `offline_exposure_guard_latched`。
  出自 `engines/nautilus/portfolio_snapshot.py:129`：未指定 currency 且 `portfolio.equity(venue)`
  不是恰好一种货币时判为不明。离线通道不传 currency。**最可能是 testnet 期货账户没有 USDT 余额**，
  但未实证，故不下断言。先入金复跑；若仍报再起 plan。
- **PS 的跨仓消费者不在本仓守护范围内。** Plan 24 之后本仓有了 toolkit 的测试，但 PS 侧调用本仓
  CLI 的脚本仍会因契约变更而静默落后 —— 2026-07-30 `vault put` 新增必填 `--scope-digest` 就让
  PS 的 `bootstrap_vault.sh` 直接跑不起来（PS 侧已修）。本仓已有
  `tests/test_examples_cli_commands_are_real.py` 守护自己仓内的示例，但它到不了 PS。是否要
  在本仓提供一个「CLI 契约变更清单」或让 PS 侧订阅，另议。
