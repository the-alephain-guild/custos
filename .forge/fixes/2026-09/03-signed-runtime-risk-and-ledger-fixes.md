# 03 - signed-runtime-risk-and-ledger-fixes

> **Status**: ✅ Completed
> **Completed**: 2026-09-20
> **Created**: 2026-09-20
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-deep-followup-review.md`（commit `037dc26`）
> **For Codex**: 先写失败回归，再做最小修复；不得连接真实账户或下单。

## 修复来源

- 审查报告：`.forge/reviews/2026-09-20-custos-deep-followup-review.md`
- 复现脚本：`.forge/reviews/2026-09-20-custos-deep-followup-repro.py`
- 范围：DR-1 至 DR-9，8 项 P1、1 项 P2
- 约束：保留签名 lane、live fail-closed、Decimal money、现有历史 receipt；不刷新历史 hash

## 根因分诊

| Finding | Priority | Root Cause | Fix |
|---|---:|---|---:|
| DR-9 策略换版漏算敞口 | P1 | 实现错误：稳定 runner 风险域被错误建模为 policy_id 域 | 1 |
| DR-2 daemon 无持续风险监督 | P1 | 实现错误：已存在 supervisor，签名组合层漏接线 | 2 |
| DR-6 原生平仓事件无法序列化 | P1 | 实现错误：测试替身发明了原生 ABI | 3 |
| DR-1 generation 替换失败 | P1 | 实现错误：更新路径直接 deploy，未先有序停止旧节点 | 4 |
| DR-3 降级宿主仍发 online | P1 | 实现错误：心跳健康源与宿主可靠性源分叉 | 5 |
| DR-4 减仓改单被拒 | P1 | 实现错误：改单路径未保留 reduce-only 语义 | 6 |
| DR-8 多批入场平仓只扣一批 | P1 | 规则缺失 + 实现错误：未定义持久化批次分配 | 7 |
| DR-5 基础币手续费被拒 | P1 | 实现错误：把 settlement currency 错当 fee currency | 8 |
| DR-7 盈亏口径不一致 | P2 | 规则缺失 + 实现错误：内部净 PnL 与外部毛 PnL/费用混比 | 9 |

## 修复任务

### Fix 1: 策略换版保持 runner 风险敞口连续 [P1]

**Files**: `runner_fact.py`、`order_reservation_boundary.py`、`test_runner_policy_runtime.py`、`test_order_reservation.py`
**Step 1**: 用真实 SQLite 与两版已验签 policy 写回归：旧成交 100、上限 150，换版后新增 100 必须拒绝。
**Step 2**: 证明旧 policy 的活动预留和已成交敞口在新 head 下仍计入同一 tenant/mode/runner 风险域。
**Step 3**: 将聚合归属改为稳定 runner scope；policy_id 只决定当前约束和审计出处，不迁移或丢失旧记录。
**Step 4**: 覆盖重开数据库、活动预留、已成交敞口、过期旧 policy 与新 head。

### Fix 2: 签名 daemon 持续运行 EngineSafetySupervisor [P1]

**Files**: `_daemon.py`、`engine_safety.py`、daemon/production-loop tests
**Step 1**: 组合层回归证明 50% 回撤会冻结订单边界使用的同一个 breaker。
**Step 2**: 为每个活跃签名实例建立受监管、限时的周期 tick；使用 host 状态和该实例的共享 breaker。
**Step 3**: 生命周期停止、generation 替换、任务异常与 daemon 退出时取消/回收监督任务。
**Step 4**: 覆盖可靠、回撤、unreliable、断网、恢复与任务异常。

### Fix 3: 使用真实 Nautilus Position 事件 ABI [P1]

**Files**: `runner_fact_producer.py`、`test_strategy_signal_bridge.py`、`test_runner_fact_production_loop.py`
**Step 1**: 用安装中的原生 `PositionClosed` 建立失败测试，禁止测试替身自带不存在的 `to_dict`。
**Step 2**: 从稳定公开属性读取 event_id、position_id、instrument_id、realized_pnl、时间和关闭原因。
**Step 3**: 原生事件必须经事件桥写入 durable outbox；缺失身份或币种时 fail closed。
**Step 4**: 覆盖 long/short、费用后 PnL、重复事件与数据库重开。

### Fix 4: generation 有序替换旧节点 [P1]

**Files**: `engine_lifecycle.py`、`test_engine_lifecycle.py`
**Step 1**: 回归锁定 generation 1 running → generation 2 running：必须 stop 一次，再 deploy 一次。
**Step 2**: 解析并验证新材料后，检测当前已应用的旧 generation；先停止并确认旧节点不再 ready，再部署新代。
**Step 3**: 新代部署失败时，状态必须如实反映旧节点已停止；禁止“新命令 quarantined、旧节点继续跑”。
**Step 4**: 覆盖幂等重投、停止失败、ready timeout、重启恢复与 retry budget。

### Fix 5: 心跳消费宿主统一可靠状态 [P1]

**Files**: `runner_fact_producer.py`、`host.py`、`test_runner_fact_production_loop.py`
**Step 1**: 故障注入 runner-fact sink 后，心跳必须是 degraded，并携带脱敏原因。
**Step 2**: `_emit_observability` 在生成 online 前读取 `get_engine_status`，不能从 portfolio 可读推导健康。
**Step 3**: portfolio 或 host 状态任一不可靠均发 degraded；写事实失败继续走现有宿主降级回调。
**Step 4**: 覆盖瞬时审计失败、持续失败、故障清理和新 deployment 不继承旧故障。

### Fix 6: reduce-only 改单保留风险降低通道 [P1]

**Files**: `order_reservation_boundary.py`、`runner_safety.py`、相关测试
**Step 1**: submit reduce-only 后 modify，在无 reservation 和 breaker frozen 时仍应到达下游。
**Step 2**: 从 canonical cache 读取真实订单并判断 reduce-only；风险降低改单不创建伪 reservation。
**Step 3**: 风险增加改单仍要求 breaker 和 reservation；回滚只处理实际替换过的 reservation。
**Step 4**: 覆盖数量、限价、触发价、拒单、取消与非本实例订单。

### Fix 7: 持久化多批入场并按批次消减 [P1]

**Files**: `runner_fact.py`、`order_reservation_boundary.py`、`runner_safety.py`、规则与测试
**Step 1**: 两笔各买 1、卖 2 后敞口必须为 0 且 breaker 不冻结；另测部分减仓、反向开仓和数据库重开。
**Step 2**: 不再依赖单一 `position.opening_order_id`；按确定性 FIFO 从该 deployment 的活动已成交批次分配 reduction quantity/notional。
**Step 3**: 在一个 SQLite 事务内写分配与幂等事件；总减仓超过可归属数量时 fail closed。
**Step 4**: 将“净持仓由所有开仓批次共同归属、FIFO 消减”补入 mandatory rule/验证门。

### Fix 8: Binance 保留实际手续费币种 [P1]

**Files**: `binance_ledger.py`、`test_binance_ledger_economic_rows.py`
**Step 1**: 加 BTCUSDT 买入、commissionAsset=BTC 的回归。
**Step 2**: fill 的成交 currency 保持 quote；fee row 的 currency 使用真实 commissionAsset。
**Step 3**: 仅拒绝 RunnerFact 不支持的手续费币种，不再要求手续费币种等于 quote。
**Step 4**: 覆盖 quote/base/第三支持币种、零手续费与未知币种。

### Fix 9: 统一 gross realized PnL 与费用分项口径 [P2]

**Files**: `runner_fact_producer.py`、`binance_ledger.py`、Crucible `runner_fact_reconciliation_projector.rs`、相关 producer/ledger tests、规则文档
**Step 1**: 原生两次部分减仓建立回归：每次产生内部 realized PnL 事实；总 gross PnL=10，commission 独立=3，不再拿 net 7 对比 gross 10。
**Step 2**: 采用完整持仓周期聚合：只有周期内开仓、周期内完全平仓且两侧期末持仓均为零时才建立 PnL 可比较范围；部分减仓或跨期持仓不生成伪比较。
**Step 3**: 内部使用 Nautilus 净 PnL；Binance 毛 realized PnL 只扣 commission（不扣 funding/insurance）后比较，fee 继续作为独立范围核对。
**Step 4**: 跑 Custos wire/producer 回归；若 wire 语义变化，核对 Crucible consumer，但不擅自修改消费端。

## 验证清单

- [x] 9 个缺陷各有回归测试或消费端口径测试
- [x] 历史审查复现脚本保留不改；等价回归已进入正式测试集
- [x] 扩展定向集 146 项通过
- [x] `make check` 通过
- [x] `make test-baseline` 通过：2608 passed / 28 skipped / 1 xfailed
- [x] `make check-authority` 通过
- [x] Crucible `cargo check -p store --lib` 与 PnL 定向单元测试通过
- [x] 无真实凭据、真实账户、下单或生产节点操作
- [x] 生产 readiness 仍单独表述，不以离线测试替代

## 当前测试文件计数

以下数字来自 `pytest --collect-only`，用于覆盖本 fix 扩展过的既有文件。

| Test file | Collected |
|---|---:|
| `tests/cli/test_runner_safety_daemon_composition.py` | 8 |
| `tests/engines/nautilus/test_binance_ledger_economic_rows.py` | 8 |
| `tests/engines/nautilus/test_runner_safety_execution_boundary.py` | 34 |
| `tests/test_engine_lifecycle.py` | 12 |
| `tests/test_order_reservation.py` | 11 |
| `tests/test_plan_closeout_counts.py` | 27 |
| `tests/test_runner_fact_production_loop.py` | 9 |
| `tests/test_strategy_signal_bridge.py` | 13 |

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---:|---|---|---|
| 1 | P1 | ✅ | 2026-09-20 | runner-scope exposure + atomic rebuild |
| 2 | P1 | ✅ | 2026-09-20 | signed daemon supervisor; breaker survives generation |
| 3 | P1 | ✅ | 2026-09-20 | native event ABI |
| 4 | P1 | ✅ | 2026-09-20 | ordered replacement |
| 5 | P1 | ✅ | 2026-09-20 | truthful heartbeat |
| 6 | P1 | ✅ | 2026-09-20 | reduce-only modify |
| 7 | P1 | ✅ | 2026-09-20 | FIFO lots + partial close + reversal + restart |
| 8 | P1 | ✅ | 2026-09-20 | fee currency |
| 9 | P2 | ✅ | 2026-09-20 | complete-cycle net PnL comparison |

## 完成报告

- Custos 修改签名 daemon、生命周期、风险预留/敞口、原生事件桥、Binance 独立账本与规则测试。
- Crucible 只修改 `crates/store/src/runner_fact_reconciliation_projector.rs`；仓库中其他用户改动未触碰。
- 第一轮自省补正 runner-scope rebuild，避免新 policy checkpoint 与旧 checkpoint 重复计数。
- 第二轮自省补正 generation breaker 状态继承、宿主 peak equity 同步、side-aware FIFO lot 与非 reduce-only 反向成交。
- 历史 review/repro 文件保持审查时证据，不重写成修复后结果。
- 本轮未创建 Git commit；工作树变更等待 owner 审阅。
