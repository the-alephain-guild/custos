# Code Review: Custos 执行与账务链路

- **Depth**: deep，关键调用链抽查，非全库无遗漏证明
- **Date**: 2026-09-20
- **Reviewer**: Codex
- **Repository / revision**: custos / main / `32ca3bfa47c222ec00c2e456421220a090776946`
- **初始工作区**: clean
- **变更范围**: 仅本报告和配套复现脚本；业务源码、部署配置、历史收据均未修改。

## Summary

确认 6 组可复现 bug：5 个 P1、1 个 P2。最高风险是现货买入被误识别为权益回撤并触发平仓，以及事实写入失败无法使宿主降级。新 OKX/SoDEX 账单采集器还没有闭合现货持仓与永续权益的对账口径。对账重试存在真实 SQLite 证据混合问题。

审查重点为签名准入、Nautilus 宿主、事件转发、事实发件箱、账单采集、投资组合估值和离线更新，交叉阅读了 Crucible 的实际消费规则。未进行账户连接、下单、生产部署或真实市场测试，也未将旧验收收据视为当前生产证明。

| 严重度 | 数量 | 含义 |
|---|---:|---|
| Critical / P1 | 5 | 上线前应修复，影响风控、审计或账务一致性 |
| Warning / P2 | 1 | 正常更新路径不可用，应修复 |
| Info | 0 | 本报告不列风格意见 |

## Strengths

- 当前代码保留签名准入、模式隔离与 Decimal 金额边界；本轮没有修改或绕过这些保护。
- 现有 97 项相关测试通过，说明已有测试路径仍成立。但下面的资金形态、异常恢复及真实宿主更新情形未被那些断言覆盖。

## Concerns

### CR-6 — [P1] 现货权益只取报价币现金，正常买入会触发错误回撤

**位置**：`src/custos/engines/nautilus/portfolio_snapshot.py:249-252`，调用点 `:147-158`；后续 `src/custos/core/engine_safety.py:48-61`。

Nautilus 的 CASH 账户 `portfolio.equity()` 返回按币种分开的余额，只有 MARGIN 账户另行加入未实现盈亏。Custos 只选 `requested_currency` 对应金额，就把它作为整个部署的 `current_equity`，其他现货资产没有折算进去。

**复现**：真实安装的 Nautilus `BacktestEngine` 创建 CASH 账户，余额为 `1000 USDT + 1 BTC`；Custos 返回 `1000 USDT`。假定 BTC 的可信价格为 `9000 USDT`，组合实际价值仍为 `10000 USDT`。以 `10000` 为既有峰值、回撤阈值 `10%` 调用真实 `FallbackBreaker`，得到 `drawdown_breach`，回撤 `90%`。

**影响**：正常把现金换成现货，被当成亏损；`EngineSafetySupervisor` 在 breaker 触发后调用 `flatten_positions`。现金恢复后再买入还可能重复触发。这个问题适用于 CASH 账户路径，不限于一个交易所。

**建议**：区分可用现金与组合权益。按可信价格将部署范围内的现货资产折算到结算币，并校验价格完整性；增加买入前后价格不变、NAV 不变的端到端测试。

### CR-1 — [P1] 成交事实失败被吞掉，宿主的审计失败检测失效

**位置**：`src/custos/core/runner_fact_producer.py:408-410`，同类位置 `:631-633`；传播机制 `src/custos/engines/nautilus/strategy_event_forwarding.py:100-105`；宿主使用点 `src/custos/engines/nautilus/host.py:1656-1673`。

事件桥接器捕获事实构建/写入异常后只写普通日志并正常返回。`StrategyEventForwarder` 只有在 sink 抛出异常时才调用 `on_sink_failure`，因此 `_event_forwarding_failures` 不会记录这次事实丢失。live readiness 和 `get_engine_status` 也无法从这次失败得知审计链已断。

**复现**：已归属本部署的合法 `OrderFilled` 通过真实 forwarder 进入 bridge，令事实 emitter 抛出 `OSError`。观察到 emitter 被调用一次、宿主失败回调为零、策略原处理器仍被调用。

**影响**：仅事实路径失败时，例如不支持的手续费币种或发件箱写入异常，成交/费用事实可以丢失而这条失败路径不触发宿主降级。其他 sink 的故障可能另外触发降级，不能作为这里的保障。

**建议**：让异常到达已经负责隔离策略线程的 forwarder，或显式上报宿主；对无法持久化的执行事实保留可恢复证据，并验证错误出现后不会继续报告审计能力正常。

### CR-4 — [P1] 对账重试拼接不同采集批次，成功标志与落盘证据不一致

**位置**：`src/custos/core/runner_fact_producer.py:793-810`、`:811-821`、`:868-896`；去重行为 `src/custos/core/runner_fact.py:1742-1752`。

manifest/chunk 在估值检查前分别提交。后续估值或发布失败后，重试重新采集账单，但 `snapshot_id` 和事件 ID 仅由同一 period 派生。SQLite 发件箱按 `event_id` 丢弃重试的新 manifest/chunk，不核对其内容；尚未提交的 checkpoint 则使用新采集的 watermark。最后仍能写入 period close 并返回 `True`。

**复现**：使用真实 `RunnerFactOutbox` 和临时 SQLite。首次采集 `capture-1` 已写入 manifest/chunk，估值暂不可用使关闭返回 `False`；第二次采集 `capture-2` 后关闭返回 `True`。检查持久化批次：manifest 的 watermark 是 `capture-1`，同一 snapshot ID 的 checkpoint 却指向 `capture-2`。

**影响**：Crucible 的 `runner_fact_reconciliation_projector.rs:1283-1294` 要求 snapshot ID、venue、watermark 同时匹配，会拒绝此闭合周期。Custos 已把周期推进，正常重试无法恢复。Binance watermark 包含采集时间，因此即使余额未变也可能遇到该问题。

**建议**：在写入前完成所需数据采集与验证，并原子提交完整闭合证据；或者持久化一次采集、重试复用同一批次，或为新采集分配新的完整快照身份。重复事件不同内容必须明确拒绝，不能静默混合。

### CR-2 — [P1] OKX/SoDEX 永续用钱包余额与含浮盈权益核对

**位置**：`src/custos/engines/nautilus/okx_ledger.py:310-329`、`src/custos/engines/nautilus/sodex_ledger.py:297-316`；降级路径 `src/custos/core/runner_fact_producer.py:811-815`。

两个新增采集器都不填 `valuation_collection_started_at`、`venue_wallet_balances`、`valuation_positions`，因而无法生成共同价格下的估值 checkpoint。即便能力声明包含 checkpoint，生产循环也只是跳过它，仍发布周期关闭。另一方面，OKX `cashBal` 和 SoDEX `wb/total` 是钱包余额，内部 MARGIN `portfolio.equity()` 包含未实现盈亏。

**复现**：对两家采集器分别输入合法形状的永续余额与持仓响应，返回估值字段为 `None`。启用 checkpoint 能力调用真实周期关闭逻辑，结果 `True` 且只包含 manifest/chunk/period close，没有 valuation checkpoint。按现有消费逻辑，钱包 `100`、浮盈 `10` 时会比较内部权益 `110` 与外部余额 `100`。

**影响**：正常持仓盈亏会变成虚假余额差异。消费端现有无 checkpoint 路径确实直接比较 `equity_snapshot` 和 `balance.total`（Crucible `runner_fact_reconciliation_projector.rs:1411-1424`）。

**建议**：补全具有可信共同 mark、钱包余额、持仓数量/成本的估值证据；需要该能力时不得静默跳过。测试正负浮盈以及采集过程中价格变化。

### CR-3 — [P1] OKX/SoDEX 现货库存没有对应持仓证据，却声明 positions complete

**位置**：`src/custos/engines/nautilus/okx_ledger.py:226-230`、`:316-324`；`src/custos/engines/nautilus/sodex_ledger.py:193-194`、`:303-311`。

两个采集器只有 perpetual 分支构造 `positions`。现货持有基础资产时，只返回 balances，`positions=[]`，但 `positions_complete=True`。当前内部宿主会通过 `cache.positions_open()` 发布现货策略持仓；消费端将双方按 instrument/side/currency 比较，没有把外部基础资产余额转换成可比较的持仓。

**复现**：OKX 输入 `1 BTC`、SoDEX 输入 `1 vBTC` 的现货余额，两者都返回空 positions 且标记 complete。内部已成交形成数量 `1` 的现货持仓时，消费端的对应外部数量为 `0`。

**影响**：买入后正常库存被误报为持仓不一致。余额行不能在当前消费契约下自动替代持仓行。这与 CR-6 的风控估值问题是不同调用链，也需要独立修复。

**建议**：建立现货专用库存/成本基础与持仓核对模型，或明确调整生产者及消费端的现货比较规则。对无法提供的维度，不得声明已经完整覆盖。

### CR-5 — [P2] 离线 runner 的运行中 generation 更新永久重试

**位置**：`src/custos/offline/reconciler.py:363-373`；`src/custos/engines/nautilus/host.py:1505-1517`。

同一实例已 attached 时，离线 reconciler 必然调用 `reconfigure(runtime_spec(...))`。正常离线 schema/runtime_spec 不产生 `reconfigure.runtime_tunable_only` 字段，而真实 Nautilus 宿主对这种调用抛出 `NotImplementedError`。reconciler 又把它当成可重试错误，保持旧实例运行，无法应用新的参数。

**复现**：构造合法 generation 2 的 testnet spec，保留 generation 1 的已运行实例，调用真实 `_engage` 和真实 `NtTradingNodeHost.reconfigure`，得到 structural reconfigure requires stop + re-deploy。现有相关测试使用接受任意 reconfigure 的假引擎，没有覆盖真实宿主行为。

**影响**：离线 sandbox/testnet 常规参数更新不能生效；会持续重试。终止状态走独立 stop 分支，不属于本条失败场景。

**建议**：明确哪些配置可热更新并真正应用；其他更新在 reconciler 实现有序 stop/redeploy，或终态拒绝并说明需要重启，不要无限重试不可实现操作。

## Verification

复现脚本：[2026-09-20-custos-runtime-bug-repro.py](2026-09-20-custos-runtime-bug-repro.py)。

```bash
uv run --extra dev --extra nautilus python .forge/reviews/2026-09-20-custos-runtime-bug-repro.py
```

脚本断言的是本次审查确认的错误行为，输出成功表示 bug 被复现，不代表产品测试通过。使用模拟的 REST 响应、临时真实 SQLite 和真实 Nautilus CASH 回测账户；没有外部网络访问、用户密钥、真实账户或订单。

结果：6 组均复现。97 项既有相关测试通过：

```bash
uv run --extra dev --extra nautilus pytest \
  tests/test_independent_venue_ledgers.py tests/test_portfolio_snapshot.py \
  tests/test_runner_fact_production_loop.py tests/test_offline_reconciler.py \
  tests/test_runtime_admission.py -q
```

复现脚本经过 ruff 检查。没有用本轮结果覆盖之前的构建、镜像或传输验收记录；它们证明的路径仍与本轮发现的异常/资金状态场景不同。

## Suggestions

1. 优先处理 CR-6、CR-1、CR-4：错误平仓、审计失败传播、重试证据一致性。
2. 联合处理 CR-2、CR-3，并补足每个交易所的现货/永续经济对账测试，不以配置构建测试代替。
3. 修复 CR-5 的真实宿主更新路径，补 generation 变化的集成测试。

## Risk Assessment

当前版本不宜据此前的通过数量直接开放 live。这里存在正常买入触发风控、合法持仓导致虚假对账差异，以及临时故障破坏闭合证据的实际路径。修复后需要再次验证对应状态转换，再进行已规划的真实账户与 Ubuntu 实机验收。


## 修复记录（2026-09-20）

上述结论保留为审查时点的证据。六项问题的修复、回归、自省及部署边界见
[Fix 02](../fixes/2026-09/02-runtime-ledger-fixes.md)。Custos 实施提交截至 `a463041`，配套 Crucible 消费端提交为 `1eaca8d`。未把本地源码验证等同于生产上线验收。
