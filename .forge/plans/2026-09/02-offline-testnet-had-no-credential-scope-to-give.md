# 离线通道的 testnet 走不通的三处：给不出凭据范围，就绪检查读错属性名，就绪判据不含估值

- **Status**: ✅ Completed
- **日期**: 2026-09-19
- **触发**: 操作者在 philosophers-stone 跑 `make start STRATEGY=supertrend MODE=testnet`，
  健康检查停在 `expected health healthy, got unhealthy`

## 现象与两层根因

第一层是镜像。本机 `custos-runner:v0.3.0` 构建自 `9b3af5f`（2026-08-03），落后 HEAD 138 个
提交，里面装的是 NautilusTrader `1.230.0`。策略代码按 2.0 写（`StrategyConfig` 在 2.0 是
Rust pyclass，可以定义 `__init__`），1.x 的 `StrategyConfig` 仍是 `msgspec.Struct`，于是
导入直接失败：

```
Failed to import strategy from /opt/ps/trend/supertrend/refinement/nautilus/strategy.py:
Struct types cannot define __init__
```

随后每 5 秒一条 `Unknown strategy: 'supertrend'. Available: []` 是它的次生现象 —— 注册表
因导入失败而为空。按仓库既有入口 `make docker-build-local-v030` 重建后，镜像是
`2.0.0rc5+sodex.1`，与 `uv.lock` 一致，策略导入成功。

第二层是本 plan 修的那个。策略装载成功后，错误变成：

```
real-venue deployment requires instance and credential-scope identity
```

`NtTradingNodeHost._claim_execution_account_partition` 在非 sandbox 模式下要求
`credential_scope.scope_id`，用它把真实场所账户分区，好让两个部署不会把同一个交易所账户
交易进彼此的仓位。而 `OfflineDeploymentSpec` 用 `extra="forbid"` 且没有 scope 字段，
PS 那边填不进来 —— **离线通道的 testnet 因此完全不可用，与 PS 怎么渲染无关**。

时间线解释了它为什么没被发现：

| 日期 | 事件 |
|---|---|
| 2026-07-29 | 离线规格模型定型：禁止未知键，只拒 live，放行 testnet |
| 2026-08-14 | 宿主加入分区校验，非 sandbox 要求一个该模型不接受的字段 |

`tests/test_nt_trading_node_host.py` 自己构造带 `credential_scope` 的规格，所以它一直绿；
离线那条路径从未被它覆盖。这是新增必填字段没有收敛所有生产者，与教训 #4 同型。

## 改动

`runtime_spec()` 的职责本就是「把离线规格转成宿主读的形状」，三个身份字段已经在那里由
`spec_id` 派生。凭据范围同理由 `provenance_ref.credential_id` 派生：

```python
"credential_scope": {
    "scope_id": str(uuid5(_IDENTITY_NAMESPACE, f"credential:{spec.provenance_ref.credential_id}")),
},
```

选这个位置而不是给 `OfflineDeploymentSpec` 加字段，理由是分区要回答的正是「这是哪套
凭据」，而离线规格携带的就是凭据本身 —— 让操作者再声明一次范围只是把同一件事写两遍。
宿主那条拒绝保持原样，不放宽；PS 侧无需改动。

`scope_digest` 没有补：宿主的分区只读 `scope_id`，摘要是签名命令那条路径上
`VaultRunnerCredentialResolverV1` 的校验材料，离线路径不走它。

## 测试条数（`pytest --collect-only` 实跑）

| 测试文件 | 条数 |
|---|---|
| `tests/test_offline_reconciler.py` | 39 |
| `tests/engines/nautilus/test_readiness_checks_what_it_claims.py` | 18 |
| `tests/test_plan_closeout_counts.py` | 22 |

各行都是该文件今天的总数，不是本次增量。本次在第一个文件加了 2 条（testnet 规格带上
范围、两套凭据分区互不相同而同一套凭据跨代次稳定），在第二个加了 4 条（替身的属性名
必须是真实 Portfolio 上的那个，以及估值判据的三条）。

第三行是计数门自己：它有两条按「带计数表的记录」参数化的用例，本文件加进来就让它
各多一个，20 变 22。谁加记录谁重新计数，这条规则对这个文件同样成立。

复跑命令：

```
uv run --package custos-runner pytest tests/test_offline_reconciler.py -q
uv run --package custos-runner --extra nautilus pytest tests/engines/nautilus/test_readiness_checks_what_it_claims.py -q
uv run --package custos-runner pytest tests/test_plan_closeout_counts.py -q
```

## 变异验证

四处变异逐个跑，全部转红，还原后复绿：

| 变异 | 结果 |
|---|---|
| 完全不补 `credential_scope` | 红 3 项 |
| 范围改由 `spec_id` 派生（而非凭据） | 红 2 项 |
| `scope_id` 置空串 | 红 3 项 |
| 派生时掺入 `generation`（使其不稳定） | 红 2 项 |

第四处是补写的：首版的稳定性断言用同一个规格调两次，掺入代次也不会让两次结果不同，
所以那次变异是绿的。改成跨代次比较后才真正钉住 —— 代次每次发布都递增，范围若随它走，
分区在每次重新发布后都会指向一个新账户，这条拒绝就形同虚设。

## 第三处：就绪检查读的属性名 Nautilus 2 已经改了

范围补上之后节点真的起来了，然后崩在就绪探针：

```
AttributeError: 'nautilus_trader.portfolio.Portfolio' object has no attribute
'initialized'. Did you mean: 'is_initialized'?
```

`_readiness_checks` 读 `portfolio.initialized`，而 Nautilus 2 上只有 `is_initialized`
（`dir(Portfolio)` 实测，全仓仅此一处用旧名）。Nautilus 2 升级漏了它，原因在测试自己：

```python
portfolio=SimpleNamespace(initialized=portfolio_initialized),
```

**替身是照着被测代码的写法造的**，所以代码把名字写错时替身跟着错，两边一致，14 条用例
全绿。这个错误只能在真实 Portfolio 出现的地方显形 —— 也就是第一次真实 testnet 部署，
在节点已经起来并连上场所之后，是最不该发现打字错误的位置。

修法是两件事：代码改用 `is_initialized`，并加一条把替身钉在真实类上的测试。三处变异
逐个跑：

| 变异 | 结果 |
|---|---|
| 代码与替身一起退回旧名（原本那个共谋） | 红 2 项 |
| 只有替身退回旧名 | 红 26 项 |
| 就绪检查不再看 portfolio（恒真） | 红 3 项 |

第一处是关键：它复现的正是让这个 bug 活下来的形态，现在会红。

## 第四处：就绪判据里没有守卫紧接着要用的那个答案

属性名改对之后节点起来了、跑起来了、连上了场所，然后熔断：

```
fallback_breaker_fail_closed  reason=portfolio_prices_missing
positions_flattened
```

时序是决定性的 —— 订阅 mark price 发出后 **1.97 秒**守卫就评估了，而整份日志里
`MarkPriceUpdate` 一次都没出现：第一笔行情根本没回来。

这不是「有仓位就不让起」。守卫要算敞口就得知道每个持仓标的的现价：账户为空时它不需要
任何价格，直接通过；有仓位时它需要那个标的的价格，而价格还在路上。八月三日那次从空账户
起步并成功，留下的正是现在这个仓位 —— 空账户能过、有仓位过不了，两边都有实证。

后果比现象严重：带着仓位重启在运维里是常态（进程崩、机器重启、版本升级），而按这个行为
**任何持仓中的策略重启后都起不来**，报的还是「组合价格缺失」，看起来像行情故障。

根因在判据而不在机制。`_may_evaluate` 的注释记着 2026-08-01 那次：熔断在账户余额到达前
116ms 触发，于是加了「等 `deployment_ready` 再评估」。机制是对的，但那七项判据里没有一项
是「守卫问得出敞口」—— 守卫问的是 `get_open_notional`，它在快照不可靠时直接抛。于是同一个
失败换了个原因回来了：那次是 equity missing，这次是 marks missing。

所以把它加进判据，让那个等待真的等到它在等的东西：

```python
portfolio_valuation_ready=valuation.reliable,
```

超时兜底不变（90 秒后照常评估并 fail-closed），所以这不会变成一扇永不开的门。

`all_ready()` 顺带从七个位置参数改成具名 —— 加一项字段时位置参数不会报错，只会把每个值
向左挪一格，八条用例因此红了；具名之后新增字段会在构造处直接失败。

三处变异，全红：ready 不再看这一项、估值恒为就绪、估值恒不就绪。

## 遗留

- 离线通道的 testnet 端到端仍未在真实场所验证完。本 plan 交付到「守卫不再在数据到齐前
  评估」为止；它之后的下单、收线与持续运行行为不在范围内。
- 账户里那个 2026-08-03 留下的空头（BTCUSDT -0.0055）仍在。它现在不再阻止启动，但平仓
  单被交易所以 `-2022 ReduceOnly Order is rejected` 拒绝，而 positionRisk 与 account
  两个端点都确认它存在、账户可交易、无挂单。原因未查实。
- 三类 spec 模板（sandbox / testnet / live）在 PS 侧只差一个 `trading_mode` 字段，而
  live 在 custos 侧是被 `refuse_live` 直接拒的。那份 live 模板目前没有任何通路能用它。
