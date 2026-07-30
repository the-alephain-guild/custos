# 25 — Binance 拒收本机生成的 client order id（超长，订单一律发不出）

> **Status**: 🔲 Not started
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

- [ ] `make verify` 全绿
- [ ] Task 1 的测试在修复前红、修复后绿（两侧输出都记进 close-out）
- [ ] Task 3 的最坏情况守护绿
- [ ] sandbox 部署路径仍绿（Task 4）
- [ ] **真机证据**：PS 侧以 `MODE=testnet` 跑到一笔订单被交易所**接受**，或被拒但原因是市场性的
      （余额 / 精度 / 最小名义额），不再是 `-4015`。**这是本 plan 唯一的完成判据** ——
      单测证不了交易所会不会收（本仓 C10：绿的测试套说明不了任何订单被接受过）。

## 偏离与改进日志 (Deviations & Improvements)

- 若 Task 1 发现各 venue 上限不同，记在这里并说明是否需要按 venue 分支（本 plan 默认不分支）。
- 若选 C 而非 B，记下当时接受的 counter 天花板与理由，且 Task 3 的守护必须用那个天花板作断言 ——
  接受一个上限可以，接受一个**没写下来的**上限不行。

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
