# Custos 防复发教训详细记录

本文件不在自动加载规则目录内。简短索引见 `.claude/rules/historical-lessons.md`。
新条目均为 active；回归测试通过不等于已完成两次独立 dogfood。

录入验证（2026-09-20）：6 组 hash 重算、去重与索引一致性检查通过；绑定测试文件存在；191 项定向回归通过；`make check-authority` 通过。旧教训正文未改，缺少 hash 的旧条目未批量补写。


### #C17: 审计失败必须改变宿主状态

> **Status**: 🟡 active
> **Programmable**: yes
> **Skill binding**: N/A — 绑定仓库自动加载规则与已有 pytest 验证入口，未修改全局 skill。

- **日期**: 2026-09-20
- **来源**: `.forge/reviews/2026-09-20-custos-runtime-code-review.md`；`.forge/fixes/2026-09/02-runtime-ledger-fixes.md`；1d11fc6 / CR-1。
- **场景**: 成交、持仓关闭、订单初始化和状态通知写入失败后，策略回调继续执行，宿主却仍显示正常。
- **根因**: 事件桥吞掉持久化异常，外层失败回调无法更新引擎状态；日志被误当作完整的审计故障处理。
- **教训**: 隔离策略回调异常时，审计失败仍须传到宿主并使健康状态降级。
- **预防**: `tests/test_strategy_signal_bridge.py::test_audit_failure_reaches_host_without_interrupting_strategy` 覆盖四条事件路径；`tests/test_nt_trading_node_host.py` 校验宿主降级。新增 sink 必须覆盖持久化失败，并同时断言故障可见和原策略回调隔离。 对应 `.claude/rules/mandatory-rules.md` 的 Runtime observations and replacement 及 `.claude/rules/verification.md` 的 Runtime and ledger regression gates。

<!-- hash: 78088e120ad7 -->

---


### #C18: 完整对账采集必须原子落盘

> **Status**: 🟡 active
> **Programmable**: yes
> **Skill binding**: N/A — 绑定仓库自动加载规则与已有 pytest 验证入口，未修改全局 skill。

- **日期**: 2026-09-20
- **来源**: `.forge/reviews/2026-09-20-custos-runtime-code-review.md`；`.forge/fixes/2026-09/02-runtime-ledger-fixes.md`；01ab0de / CR-4。
- **场景**: 第一次采集已入库 manifest 和 chunk，估值失败；重试产生新 watermark，部分旧事件被去重后留下不一致周期。
- **根因**: 一个周期分多次事务发布，后续采集失败后按相同 ID 重采；仅按 ID 去重让旧分片和新检查点混合。
- **教训**: 先准备完整周期，再原子写入所有分片和关闭事件；相同身份的不同内容必须拒绝重放。
- **预防**: `tests/test_runner_fact_outbox.py::test_atomic_group_rolls_back_all_batches_and_sequences` 与 `test_atomic_group_replay_is_bound_to_capture_bytes` 使用真实 SQLite；`tests/test_runner_fact_production_loop.py::test_valuation_failure_publishes_no_partial_period` 验证估值失败不残留。必须测试中途失败、回滚后序号、数据库重开和冲突重放。 对应 `.claude/rules/mandatory-rules.md` 的 Runtime observations and replacement 及 `.claude/rules/verification.md` 的 Runtime and ledger regression gates。

<!-- hash: f314c7119095 -->

---


### #C19: 现金总资产必须由完整余额估值

> **Status**: 🟡 active
> **Programmable**: yes
> **Skill binding**: N/A — 绑定仓库自动加载规则与已有 pytest 验证入口，未修改全局 skill。

- **日期**: 2026-09-20
- **来源**: `.forge/reviews/2026-09-20-custos-runtime-code-review.md`；`.forge/fixes/2026-09/02-runtime-ledger-fixes.md`；6debd44 / CR-6。
- **场景**: 1000 USDT 加 1 BTC 被读成 1000 USDT，风险检查可能误触发回撤和平仓。
- **根因**: 把 CASH 账户的报价币余额当作总资产，忽略基础币库存；余额变化被误判为净值回撤。
- **教训**: 按账户类型定义权益，现金账户逐资产换算 NAV；策略持仓不得与已计价库存重复相加。
- **预防**: `tests/test_portfolio_snapshot.py::test_native_cash_portfolio_balances_are_converted_to_nav` 使用真实引擎账户和行情；同文件 `test_cash_nav_counts_all_balances_once_without_adding_position_notional`、`test_cash_nav_refuses_unpriced_nonzero_balance` 覆盖重复计价与缺价。保证金账户按部署结算币读取权益，不套用现金库存公式。 对应 `.claude/rules/mandatory-rules.md` 的 Runtime observations and replacement 及 `.claude/rules/verification.md` 的 Runtime and ledger regression gates。

<!-- hash: 07bf8b8d66e6 -->

---


### #C20: 独立账本必须对齐范围与估值口径

> **Status**: 🟡 active
> **Programmable**: yes
> **Skill binding**: N/A — 绑定仓库自动加载规则与已有 pytest 验证入口，未修改全局 skill。

- **日期**: 2026-09-20
- **来源**: `.forge/reviews/2026-09-20-custos-runtime-code-review.md`；`.forge/fixes/2026-09/02-runtime-ledger-fixes.md`；213adee、6ae2cfb、a463041；Crucible 1eaca8d / CR-2、CR-3。
- **场景**: OKX、SoDEX 缺少独立估值检查点；现货库存输出为空仓位。自省还发现永续账本携带非部署结算币余额。
- **根因**: 将永续钱包现金与含浮盈亏的权益比较，并将现货账户库存当作空策略持仓；能力声明替代了实际证据。
- **教训**: 核对前明确账户范围、币种、数量单位和价格口径；必需证据缺失时不能关闭对账周期。
- **预防**: `tests/test_venue_valuation_regressions.py` 覆盖钱包、价格、现金库存和结算币范围；`tests/test_runner_fact_production_loop.py::test_required_checkpoint_missing_data_cannot_close_period` 拒绝缺失检查点。现金使用 `cash_inventory`，不得虚构策略归属和成本。跨语言消费者运行 `cash_checkpoint_preserves_inventory_without_cost_basis` 和 `consumes_python_cash_checkpoint_digest_and_rejects_tampering`；不可用单端 mock 代替消费者验收。 对应 `.claude/rules/mandatory-rules.md` 的 Runtime observations and replacement 及 `.claude/rules/verification.md` 的 Runtime and ledger regression gates。

<!-- hash: 38f38987f5f3 -->

---


### #C21: 新 generation 必须符合真实引擎生命周期

> **Status**: 🟡 active
> **Programmable**: yes
> **Skill binding**: N/A — 绑定仓库自动加载规则与已有 pytest 验证入口，未修改全局 skill。

- **日期**: 2026-09-20
- **来源**: `.forge/reviews/2026-09-20-custos-runtime-code-review.md`；`.forge/fixes/2026-09/02-runtime-ledger-fixes.md`；76005b5 / CR-5。
- **场景**: 离线新 generation 在已附着节点上反复调用不支持的 reconfigure，旧配置继续运行，新配置一直重试。
- **根因**: 测试替身接受任意 reconfigure，掩盖真实宿主不支持结构性热重配的约束。
- **教训**: 结构性更新先解析新材料，再按真实宿主能力停止和部署；风险锁定、高水位及重试语义必须连续。
- **预防**: `tests/test_offline_reconciler.py` 覆盖新 generation、材料失败、部署失败和重复投递；`tests/test_offline_guard_waits_for_readiness.py::test_runtime_replacement_preserves_breaker_and_restarts_readiness` 校验风控串行化及高水位保留。不得用无效果的热重配或提前标记 applied 绕过宿主限制。 对应 `.claude/rules/mandatory-rules.md` 的 Runtime observations and replacement 及 `.claude/rules/verification.md` 的 Runtime and ledger regression gates。

<!-- hash: 1337529e7be8 -->

---


### #C22: 当前验收计数与历史证据分开维护

> **Status**: 🟡 active
> **Programmable**: yes
> **Skill binding**: N/A — 绑定仓库自动加载规则与已有 pytest 验证入口，未修改全局 skill。

- **日期**: 2026-09-20
- **来源**: `.forge/reviews/2026-09-20-custos-runtime-code-review.md`；`.forge/fixes/2026-09/02-runtime-ledger-fixes.md`；96eda4f / Fix 02 验证阶段。
- **场景**: 功能回归通过后，计数门禁仍拿旧计划的测试数量检查扩展后的文件。
- **根因**: 计数门禁只扫描 plans，遗漏 fixes 中的新增测试记录；新修复被旧报告的数字约束。
- **教训**: 当前验收扫描所有计划类型并实测计数；历史 close-out 和 receipt 保留其记录时点的证据。
- **预防**: `tests/test_plan_closeout_counts.py::test_fix_plans_participate_in_current_count_claims` 约束发现范围，其他计数测试调用 pytest collection 复核。新增报告应写当前计数；本地测试、真实账户验收和生产发布分别陈述，不因源码演进刷新历史 receipt。 对应 `.claude/rules/mandatory-rules.md` 的 Runtime observations and replacement 及 `.claude/rules/verification.md` 的 Runtime and ledger regression gates。

<!-- hash: fb0397bc18f8 -->

---
