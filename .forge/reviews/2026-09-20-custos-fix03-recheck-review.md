# Code Review: Custos Fix 03 复核与深度审查

> **Depth**: deep
> **Date**: 2026-09-20
> **Reviewer**: Codex，单执行面；无独立外部 reviewer。
> **Custos 基线**: `037dc26dc1febe8bbeefdbe34091caae9fb1d2b4` 加当前未提交 Fix 03 修改。
> **Custos src diff 快照指纹**: `1a29b6bc349fbc779507d0fde76a25ea0ee3a262a603142d2132efb708cb368e`。
> **消费端基线**: Crucible `2936a22dcd708b5051a8081d2418cd9bbc46c0a3` 加未提交的对账投影修改。
> **消费端投影 diff 指纹**: `e1c349d63ea4c6fbbebc9b36d178e465b16e6d5bfad0c8eb29f849f066758e09`。

指纹仅定位本次未提交审查快照，不构成对演进源码的永久约束。用户修复文件及未跟踪 Fix 03 计划均保留原样，本轮只新增报告与探针。

## Summary

修复已覆盖上一轮多项根因，106 项 Custos 定向测试和 12 项消费端对账单元测试通过。进一步检查实际原生 API、启动状态、策略热更新和旧库持仓后，确认 9 项遗留或新发现：6 项 P1、3 项 P2。

最关键的是风险边界对原生订单接口覆盖不完整：普通非报价数量订单会发生 Price 类型错误，合法改单使用的 ClientOrderId 无法通过包装器，批量改单则完全绕过包装器。真实原生回测中，breaker 已冻结，挂单数量仍从 1 改成 200，而预留仍为 1。

| 严重度 | 数量 | 结论 |
| --- | --- | --- |
| P1 / Critical | 6 | 阻塞生产准入 |
| P2 / Warning | 3 | 账务或恢复边界必须补正 |

## Strengths

- 原生 PositionClosed 公开属性读取与 degraded 心跳已有直接回归。
- runner 风险域跨 policy revision 的敞口聚合、FIFO 新批次与反向成交已有真实 SQLite 测试。
- 新 generation 已改为先 stop 后 deploy；签名 daemon 已接入使用共享 breaker 的周期任务。
- 两仓当前定向测试通过。本轮新增证据补充了原测试未覆盖的真实接口与跨状态路径。

## 上轮 9 项复核

“原场景通过”只覆盖列明的路径，不等同于整条生产链路验收。

| 原 Finding | 本轮确认 | 仍有的边界 |
| --- | --- | --- |
| DR-1 generation 替换 | stop → deploy → wait_ready 顺序回归通过 | 存在开仓挂单时，原生撤单参数错误导致 stop 失败，见 RR-2 |
| DR-2 无周期风控 | 共享 breaker 在 50% 回撤用例中冻结 | 启动未就绪就锁死；熔断不撤销既有开仓挂单，见 RR-4、RR-8 |
| DR-3 降级仍 online | 宿主 unreliable 时产生 degraded 心跳用例通过 | 本轮未发现该原触发场景继续失败 |
| DR-4 减仓改单拒绝 | 订单对象测试替身的 reduce-only 路径已放行 | 原生接口接收 ID，真实调用仍不通，见 RR-2 |
| DR-5 基础币手续费被拒 | BTC 手续费不再抛错，独立费用行币种正确 | 对应成交行未携带 fee_currency，见 RR-9 |
| DR-6 PositionClosed ABI | 原生 PositionClosed 产生净 PnL 平仓事实用例通过 | 其他原生下单接口仍有 ABI 差异，见 RR-1、RR-2 |
| DR-7 PnL 口径 | 改为仅比较单周期完整开平仓，净值计算 helper 测试通过 | 非 Binance 净值被再次扣佣金，见 RR-6；跨周期与部分持仓比较仍被跳过 |
| DR-8 多批次减仓 | 新库 FIFO、多笔入场、反向成交与重开用例通过 | 旧库带持仓时缺少 lot 映射，见 RR-7 |
| DR-9 换版漏算敞口 | 新 policy 汇总旧敞口并拒绝超限预留用例通过 | 已运行订单边界仍绑定旧 policy ID，见 RR-5 |

## Concerns

### RR-3 [P1] 原生批量改单绕过冻结状态与预留更新

- **位置**：`src/custos/engines/nautilus/runner_safety.py:490-505`。
- **根因**：安装器只包 submit_order、submit_order_list、modify_order 和 market_exit。当前原生 Strategy 还公开 modify_orders；它在原生层直接构造批量修改命令，不经过 Python 的 modify_order 包装器。
- **实证**：真实 BacktestEngine、CurrencyPair、LimitOrder 和 Python Strategy 子类。先放入数量 1、价格 1 的未成交订单，冻结同一个 RunnerReservationBoundary，再安装实际 gate。调用 modify_orders 后原生缓存订单为 ACCEPTED、数量 200，名义额超过测试策略总限额 150；before_modify_order 调用次数 0；真实 SQLite 预留仍为 1。
- **影响**：已有订单可在 breaker 冻结后增加数量，runner 风险预留不随之更新。实验使用本地模拟撮合，没有真实交易。
- **建议**：覆盖批量改单并保证多腿修改的预留原子性，或在支持前显式拒绝该入口；按当前原生公开 API 枚举覆盖面。

### RR-1 [P1] 常规订单金额计算向原生接口传入错误价格类型

- **位置**：`src/custos/engines/nautilus/runner_safety.py:192-205`、`:208-219`。
- **根因**：_order_price 将价格规范化为 Decimal，_instrument_notional 又把它传给要求原生 Price 的 instrument.notional_value。
- **实证**：原生 LimitOrder 数量 1、价格 100。直接使用原生 quantity/price 计算得到 100 USDT；调用 NautilusCachedOrderSemantics.order_notional 抛出 `TypeError: 'Decimal' object is not an instance of 'Price'`。
- **影响**：非 quote_quantity 的普通限价单，以及经相同价格路径计算的订单，会在风险边界拒绝；假 Instrument 接受 Decimal 的测试无法发现这一问题。
- **建议**：内部金额仍用 Decimal，但调用原生接口时保留或构造合法 Price。增加原生现货和永续订单的 submit → reservation 验证。

### RR-2 [P1] 单笔改单与停机撤单仍按旧的订单对象接口调用

- **位置**：`src/custos/engines/nautilus/runner_safety.py:265-267`、`:352`；`src/custos/engines/nautilus/host.py:1190-1191`。
- **根因**：当前原生 modify_order 与 cancel_order 接收 ClientOrderId。包装器却从传入对象读取 `.client_order_id`；preserve 停机则把整个 Order 传给 cancel_order。
- **实证**：向 gate 传合法原生 ClientOrderId，调用下游前即抛 AttributeError。真实 `_preserve_and_confirm_shutdown` 使用原生 Strategy 和 Order 时，cancel_order 抛要求 ClientOrderId 的 TypeError。
- **影响**：上轮减仓改单修复只在订单对象替身上成立；默认 preserve 停机遇到开仓挂单时也无法完成，新 generation 的有序替换因此仍可能失败。
- **建议**：按原生 ID 接口转发，从 canonical cache 读取订单风险语义。同步审计改单、撤单及其批量入口，增加真实原生调用测试。

### RR-4 [P1] 签名风控在启动就绪前永久锁死 breaker

- **位置**：`src/custos/cli/_daemon.py:437-454`；`src/custos/engines/nautilus/host.py:697-738`。
- **根因**：节点注册到活跃集合后，周期任务立即评估组合，未等待账户和持仓 reconciliation 就绪。启动阶段暂缺余额被解释为运行中失去可信快照。
- **实证**：实际宿主快照路径处于非 Running、余额尚空；周期监督使 breaker 因 portfolio_equity_missing:USDT 冻结。随后注入正常余额，宿主可靠性恢复，breaker 仍保持冻结。
- **影响**：正常连接/对账延迟足以让新部署无法开仓；新 generation 继承同一 breaker，重部署也可能继承这次错误锁定。
- **建议**：为初次启动和替换建立明确、限时的就绪等待；运行中失联仍须 fail closed。复用经过验证的 readiness 语义，不能用无限宽限或清空 breaker 替代。

### RR-5 [P1] 策略热更新后活跃边界继续使用旧 policy ID

- **位置**：`src/custos/cli/_daemon.py:424-431`；`src/custos/core/order_reservation_boundary.py:347-352`；`src/custos/core/runner_control_consumer.py:121-130`。
- **根因**：边界只在 deploy 时绑定 policy_id。策略消息仅推进数据库 head，未更新运行中的边界；新订单仍用旧 ID 调用要求 current policy 的 reserve。
- **实证**：两份实际验签的策略，额度均为 150。工厂在第一版创建边界，随后存入第二版。旧边界提交额度仅 25 的订单，报 reservation requires the current effective runner policy；同一数据库用新 ID 预留 25 则成功。
- **影响**：即使新策略放宽或保持限额，正常换版也会阻断活跃策略的新订单；修好的跨版本敞口汇总没有解决边界切换。
- **建议**：为已验签策略更新实现原子的约束切换，并保留稳定风险域、breaker 锁定和高水位。覆盖不断开节点的 policy renewal。

### RR-8 [P1] 熔断只请求平仓，未撤销既有开仓挂单

- **位置**：`src/custos/core/engine_safety.py:62-66`；`src/custos/engines/nautilus/host.py:1566-1608`。
- **根因**：签名监督触发 flatten_positions 后没有撤销活动风险增加订单，也没有调用受控停止流程。freeze 只拦后续提交，交易所已接受的订单不再经过该入口。
- **实证**：实际 supervisor 与 host 路径，缓存存在头寸和一笔开仓挂单。回撤触发后发出平仓调用，cancel_order/cancel_all_orders 调用均为 0，挂单和策略仍保留。本探针使用缓存及策略动作替身，没有声称实际 venue 成交。
- **影响**：剩余挂单可在平仓后再次成交、重新打开风险，直到下一次监督 tick 才可能再次处理。
- **建议**：制定有确认、有重试的熔断控制流程，撤销所属实例的风险增加挂单并保留必要减仓保护；不能把提交平仓请求当作零风险确认。

### RR-6 [P2] OKX 净 PnL 被消费端再次扣除佣金

- **位置**：Custos `src/custos/engines/nautilus/okx_ledger.py:158`；Crucible `crates/store/src/runner_fact_reconciliation_projector.rs:1593-1595`。
- **根因**：新消费端对所有 venue 执行 gross_realized_pnl - commission。但 OKX 独立采集已经把 pnl + fee 输出为净值。
- **实证**：真实 OKX 转换函数输入 pnl=12、fee=-2，输出 10；消费端当前统一公式再扣佣金 2，变为 8，无法与净值 10 对齐。这里是采集转换执行与消费源码公式追踪，未运行真实账户或 PostgreSQL 投影。
- **影响**：当新比较条件满足同周期完整开平仓时，OKX 正常交易也会产生费用大小的假差额。
- **建议**：生产端与消费端统一明确的净/毛口径，避免给不同 venue 套用未经约定的扣费规则；加入跨 venue fixture 到投影的验证。

### RR-7 [P2] 旧库已有持仓被接受，但缺少新 FIFO 归属

- **位置**：`src/custos/core/runner_fact.py:1673-1686`、`:4387-4404`。
- **根因**：初始化给旧库创建空的 lot 表，却不检查已有 filled_quantity 是否具备 position 归属。新平仓处理在 position_id 存在时强制按 lot 核销。
- **实证**：临时数据库保留旧形状的成交数量 1、成本 50，删除尚未存在于旧版的空 lot 表模拟升级前形状。新初始化正常打开并建表；随后按运行时 position_id 平仓，报 exceeds durable position lot quantity，旧敞口仍为 50。
- **影响**：带持仓升级或恢复旧预留后，首次平仓才暴露无法归属，并触发记账失败冻结。全新库创建并记录新格式 fill 的测试不覆盖此情况。
- **建议**：从可信执行证据恢复归属，或在启动阶段拒绝带未映射持仓的旧库并给出明确恢复流程；不能默默接受后再在真实成交后失败。

### RR-9 [P2] Binance 成交行仍缺少实际手续费币种

- **位置**：`src/custos/engines/nautilus/binance_ledger.py:395-405`。
- **根因**：修改了独立 fees 行的 currency，却未将 commission_currency 写入对应 fills 行的可选 fee_currency 字段。
- **实证**：commissionAsset=BTC 时，fees 行正确写 BTC；同笔 fills 行仍为 fee=0.001、currency=USDT，且 fee_currency 不存在。
- **影响**：采集不再报错，但签名成交记录没有完整表达该费用金额的币种，与对应费用行不一致。当前按独立 fees 行汇总的路径不受这个缺字段影响。
- **建议**：使用现有 fee_currency 契约保留实际币种，并验证 venue snapshot 序列化后的成交行，而不只测试独立费用列表。

## Verification

```bash
uv run --extra dev --extra nautilus python .forge/reviews/2026-09-20-custos-fix03-recheck-repro.py
uv run --extra dev --extra nautilus pytest tests/cli/test_runner_safety_daemon_composition.py tests/test_engine_lifecycle.py tests/test_runner_policy_runtime.py tests/test_order_reservation.py tests/engines/nautilus/test_runner_safety_execution_boundary.py tests/test_strategy_signal_bridge.py tests/test_runner_fact_production_loop.py tests/engines/nautilus/test_binance_ledger_economic_rows.py -q
```

- Custos 原有及新增修复用例：106 passed。
- Crucible：`CARGO_TARGET_DIR=/tmp/custos-fix02-cargo CARGO_INCREMENTAL=0 CARGO_BUILD_JOBS=2 cargo test -p store --lib runner_fact_reconciliation_projector::tests`，12 passed。
- RR-1 至 RR-9 探针断言成立；RR-6 为明确标识的跨仓公式追踪，其余执行实际转换、宿主/边界方法或临时 SQLite。RR-3 使用真实原生回测和订单缓存。
- 复现脚本 Ruff 检查通过；未重写上一轮历史复现脚本。
- 复核过程中 Custos 18 个原有修改文件未发生字节变化；不把本次审查报告提交混入用户修复。
- 无真实账户、密钥读取、外部下单或生产节点操作；未作全库逐行安全证明。

## File-by-File Summary

| File | 本轮重点 |
| --- | --- |
| `src/custos/cli/_daemon.py` | 修复复核；RR-4、RR-5、RR-8 |
| `src/custos/core/engine_lifecycle.py` | DR-1 顺序改动；联查 RR-2 |
| `src/custos/core/engine_safety.py` | 风控高水位与冻结；RR-4、RR-8 |
| `src/custos/core/order_reservation_boundary.py` | 减仓改单、FIFO 与旧策略绑定；RR-2、RR-5 |
| `src/custos/core/runner_fact.py` | 稳定风险域、FIFO 新表与恢复；RR-7 |
| `src/custos/core/runner_fact_producer.py` | DR-3、DR-6 原场景复核 |
| `src/custos/engines/nautilus/binance_ledger.py` | DR-5 原场景；RR-9 |
| `src/custos/engines/nautilus/runner_safety.py` | 原生价格、单笔和批量改单；RR-1、RR-2、RR-3 |
| `src/custos/engines/nautilus/host.py` | 部署就绪、撤单和平仓；RR-2、RR-4、RR-8 |
| `src/custos/engines/nautilus/okx_ledger.py` | RR-6 净值口径 |
| Crucible `crates/store/src/runner_fact_reconciliation_projector.rs` | DR-7 修复；RR-6 跨 venue 回归 |

另交叉检查原生 Strategy 的公开签名、批量命令实现、离线 readiness 语义和各修复测试。未列新 finding 的文件仅表示本轮未确认新缺陷。

## Suggestions

1. 优先修复 RR-1/2/3，建立以原生 API 为基准的下单、改单、撤单、批量入口契约测试。
2. 修复 RR-4/5/8，使启动、运行、策略更新和熔断形成连续且有状态确认的控制流程。
3. 补齐 RR-6/7/9 的跨 venue 账务语义、持仓库恢复与完整 wire 字段。
4. 在签名 daemon 组合层做包含启动慢、已有挂单、带仓换版、重启和断网的验收；继续保留默认 live 准入限制。

## Risk Assessment

当前修复不能按“9 项全部关闭”验收。主要阻塞是实际原生订单路径与测试替身不一致，以及新增监督缺少生命周期边界。上述本地证据足以要求继续修复；生产可用性仍需真实环境与运行验收。
