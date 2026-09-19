# 离线通道的 testnet 从来给不出宿主要的凭据范围

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
| `tests/test_plan_closeout_counts.py` | 22 |

第一行是该文件今天的总数，不是本次增量 —— 本次加了 2 条（testnet 规格带上范围、
两套凭据分区互不相同而同一套凭据跨代次稳定）。

第二行是计数门自己：它有两条按「带计数表的记录」参数化的用例，本文件加进来就让它
各多一个，20 变 22。谁加记录谁重新计数，这条规则对这个文件同样成立。

复跑命令：

```
uv run --package custos-runner pytest tests/test_offline_reconciler.py -q
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

## 遗留

- 离线通道的 testnet 端到端仍未在真实场所验证完。本 plan 交付到「宿主接受这份规格」
  为止，它之后的连接、下单与收线行为不在范围内。
- 三类 spec 模板（sandbox / testnet / live）在 PS 侧只差一个 `trading_mode` 字段，而
  live 在 custos 侧是被 `refuse_live` 直接拒的。那份 live 模板目前没有任何通路能用它。
