# Code Review: Custos 修复复核及交互路径深查

> **Depth**: deep / fix recheck
> **Date**: 2026-09-21
> **Reviewer**: Codex，独立单执行面
> **固定基线**: `33914dc5e9cc9ea0587179ca47415f6bb73b827a`
> **工作树**: 开始时干净；结束前 src/packages 相对基线未变。本轮只新增报告与复现脚本。

## Summary

修复有效推进了原有缺陷，但**不能将所有问题标记为完整闭合**。固定版本全套测试为 **2,918 passed / 30 skipped / 1 xfailed / 1 warning**；独立探针仍确认 **7 项 P1**，其中部分是旧问题的遗漏输入，部分是修复之间的新交互。未确认 P0。

复核没有把旧探针抛异常、签名变化或替身缺字段当成缺陷，也没有把“旧的坏行为断言失败”直接等同于修复成功。关键场景使用当前接口重新构造，并加正向对照。

用户要求的可见性核对：本地 origin 为 `git@github.com:the-alephain-guild/custos.git`。2026-09-21 未登录 GitHub API 返回 `private=false, visibility=public`，GitHub CLI 返回 `isPrivate=false, visibility=PUBLIC`。仓库为 [the-alephain-guild/custos](https://github.com/the-alephain-guild/custos)，核对入口为 [GitHub repository API](https://api.github.com/repos/the-alephain-guild/custos)。

## Strengths / 已确认的修复进展

| 前轮问题 | 本轮复核结果 |
| --- | --- |
| LB-1 / LB-2 就绪提交失败、监督取消 | 源码已将提交纳入清理范围，取消等待者与节点终态已区分；`test_stopping_must_actually_stop.py` 的正向回归通过 |
| LB-3 / LB-4 缓存顺序、构建失败清理 | 原生缓存顺序测试、失败构建释放账户归属测试通过；旧触发路径有修复支持 |
| RS-1 / RS-2 / RS-3 合约反转数量、虚假止损覆盖、成交基准 | 当前实现和对应回归支持原场景修复；不是本轮新发现的根因 |
| RS-5 / RS-8 / RS-9 日切、软暂停保护、SuperTrend 连续性 | 已接入事件时间、允许软暂停时 Tick 退出，并替换递推状态；正向回归通过 |
| EE-1 两笔普通平仓并发 | 本轮独立原生引擎对照确认：第二笔被拒绝，最终为零持仓；改单路径仍有 FR-1 |
| EE-3 / EE-4 / EE-6 归属恢复、流饥饿、价差预留 | 已加入持久化归属、公平选择流、原生 leaves_qty；对应回归通过 |
| RS-7 / RS-4 / RS-6 / RR-4 / RR-5 / RR-8 | 原修复主路径有覆盖，但下列扩展条件仍成立，不能整体关闭 |

这张表表示本轮源码复核和现有回归的支持程度，不是交易所实盘或跨仓结算验收。

## Concerns

下文 `adapter/` 指 `packages/custos-strategy-toolkit-nautilus/src/custos_toolkit_nautilus/adapter/`。

### FR-1 [P1] 冻结后允许把普通平仓单改大，成交后开出反向仓位

**位置**：`src/custos/core/order_reservation_boundary.py:190-202`；`src/custos/engines/nautilus/runner_safety.py:440-470`。

before_modify_order 根据缓存中**修改前**的订单判断是否减仓。若旧订单是合法普通平仓，立即返回免预留的 modification，没有按请求的新数量/价格重算，也没有更新在途可平数量。

**真实引擎复现**：先持有多仓 1；冻结 breaker；实际门允许挂出 SELL 1、限价 150 的普通平仓单。之后通过同一个实际门把它改为 SELL 2、限价 100。原生撮合后为 **SHORT 1**，冻结仍为 True，reservation store 没有被调用。

**对照**：同版本的旧 EE-1“双普通平仓”场景已修好，第二笔发送返回 False，最终零持仓。问题是遗漏了改单后的风险语义，不是旧双提交缺陷仍原样存在。

**建议及验收**：比较修改后的净敞口，原子调整在途可平数量与金额预留，拒绝/撤销修改时回滚；覆盖改单放大、缩小、改单拒绝及同时存在其他退出订单。不能只对 reduce_only 订单的改单做测试。

### FR-2 [P1] 部分入场先被平掉、剩余随后成交，新仓 Tick 保护没有重新初始化

**位置**：`adapter/coordinators/trade_event_handler.py:251-257`；`adapter/orders.py:183-191`；`adapter/sltp_mode.py:104-119`。

RS-7 修复保留了仍在途入场单的归属。原持仓归零时 tick_monitor 被 reset，但该入场单的累计已保护数量继续保留；下一笔成交的 initialize_position 因此为 False，模式分派只 extend_base，没有重新设置 entry price、方向或 trailing 状态。

**复现**：HYBRID 入场单先成交 0.5，此仓随后止损平掉；剩余 0.5 再成交。新的安全止损确实提交，说明原来的归属修复有作用；但 monitor._entry_price 仍为 None。价格先到 120 再到 115，追踪退出始终返回 None。

**影响边界**：本例的交易所安全止损仍存在，失效的是新持仓的 Tick/追踪退出；不能表述成所有保护都不存在。TICK 模式也走同一初始化分支。

**建议及验收**：订单累计成交和当前持仓生命周期是两套状态。归零后同一订单继续开仓时，要重新初始化新仓保护，同时保留尚未成交的订单身份。测试“成交→平仓→余量成交”，不能仅验证同一个仓位的两次连续入场。

### FR-3 [P1] 熔断时仓位已空但仍有开仓挂单，撤单逻辑被提前返回绕过

**位置**：`src/custos/engines/nautilus/host.py:1610-1640`。

flatten_positions 先从持仓构造 instrument_ids；若没有持仓，直接返回，执行不到新增的 _cancel_risk_increasing_orders。之前确认过 containment 的实例，还会在未检查挂单的情况下报告 still_clear。

**复现**：实际 host，cache.positions_open=[]、cache.orders_open=[一笔非 reduce-only 入场单]。调用 containment，cancel_order 调用次数为 0；把实例标记为曾确认完成后重试，仍为 0，且走 still_clear 分支。

**影响**：冻结只阻止新提交，不能阻止已接受的限价/条件入场单后续成交。RR-8 的修复只覆盖“已有仓位”的情况，平仓后迟到挂单或原本空仓挂单仍可重新开风险。

**建议及验收**：是否需要撤单必须独立于是否存在持仓；每次确认都要检查当前挂单，不能只依赖历史成功标记。覆盖初始空仓、平仓后残单和先前确认后新发现的订单。

### FR-4 [P1] 同一实例重启后继承旧 readiness 缓存，在启动窗口误触发永久冻结

**位置**：`src/custos/cli/_daemon.py:530-576`，尤其 `:542-543`；调用点 `:601-608`。

_SupervisionStartups 一旦把 instance_id 加入 _evaluating，之后不再调用 deployment_ready。节点自动重启或同实例换代仍用同一个 ID，也没有按引擎句柄/启动轮次失效该缓存。

**复现**：实际 readiness gate 第一次看到 ready=True，随后同实例的替换节点应为 ready=False。第二次 may_evaluate 仍直接返回 True，readiness probe 总计只调用一次。把新节点尚未取得账户状态的 unreliable status 交给实际 EngineSafetySupervisor，会冻结 breaker 并调用 containment。

**建议及验收**：启动等待属于每一次引擎启动，不能只属于实例 ID；自动重启可能 generation 也不变。绑定当前运行句柄或启动标识，清理离开的运行状态，保留有界等待。测试首次启动、同代自动重启、换代重启及重启超时。

这是 RR-4 的重启分支残留，首次部署等待测试通过不能关闭该项。

### FR-5 [P1] 一个模式的 policy 更新被应用到全部模式的安全边界

**位置**：`src/custos/cli/_daemon.py:406-430`。

renewal notifier 使用收到的 trading_mode 解析新策略，却遍历 boundaries 的所有实例，逐个 adopt_policy，没有筛选实例所属模式。

**复现**：实际 testnet 边界原上限 10,000；收到 sandbox 策略更新，上限 100。notifier 把 testnet 边界的 policy ID 和 breaker 配置都替换了。原本合法的敞口 500 随后触发 notional_breach。

**条件及影响**：runner 启用多个模式并接收各模式的控制消息。不要求同时运行两个节点；一个模式的持久化边界和另一个模式的控制消息已经足够。状态库模式校验仍可能拒绝新订单，但不能防止内存 breaker 被错误降低阈值、继而触发 containment。

**建议及验收**：按 tenant/runner/mode 的精确归属选择更新对象，同步更新 ID 与配置。增加 sandbox/testnet 混合注册表，确认目标模式更新、其他模式完全不变，并验证过期和非当前 policy 的处理。

### FR-6 [P1] 首次熔断落盘短暂失败后不再重试，重启仍可能丢失锁定

**位置**：`src/custos/core/fallback_breaker.py:116-136`、`:150-161`、`:199-200`。

状态发布失败被捕获并记录日志，但没有保留“待持久化”状态。后续 fail_closed 只有第一次变为 frozen 才发布，evaluate 只有峰值变化或首次冻结才发布；一次暂时性写入失败可以让冻结永远只留在内存。

**复现**：权益峰值 1000、未冻结状态先成功保存。执行记账错误触发 fail_closed，冻结写入仅失败一次，存储随后恢复。连续三次 fail_closed/evaluate 均不重试，保存值仍为 frozen=False。按该保存值重建 breaker 后允许新订单，尽管无人解除原锁定。

**影响边界**：没有要求在磁盘持续不可用时保证写入；本例是一次失败后存储已恢复，程序仍不再尝试。原实例内存冻结有效，失效发生在后续重启。

**建议及验收**：跟踪未确认的状态写入并有界重试；恢复时不能把未确认锁定误当作正常解锁。覆盖一次失败后恢复、连续失败、旧 durable row 仍为未冻结，以及显式人工解除与待写锁定的顺序。

这是 RS-6 的异常路径残留，正常写入和正常重启回归通过不等于故障路径闭合。

### FR-7 [P1] 无入场信号的持仓阶段不采样权益峰值，策略回撤门仍漏掉行情高点

**位置**：`adapter/trading_strategy.py:622-630`、`:641-652`；`adapter/coordinators/risk_control.py:72-87`。

Fix 13 在 check_risk_limits 中更新峰值，但真实 bar 流程只在候选入场分支调用该函数。持有期间的 NEUTRAL bar 不采样；修复测试直接调用风险函数，因此看不到调用节拍缺口。

**复现**：使用实际 core.on_bar、_process_bar、_entry_gates_pass 和 RiskControlCoordinator。初始权益 1000，配置最大回撤 5%。NEUTRAL bar 时权益升到 1100，峰值仍为 1000；下一根权益 1030 且出现入场信号，代码把峰值更新为 1030 并允许入场，漏掉真实的 6.36% 回撤。对照显式保存峰值 1100 后，同一风险门会正确拒绝。

**影响边界**：独立宿主 10% 熔断仍存在，但无法代替策略的 5% 限额；本例没有越过 10%。

**建议及验收**：可靠权益的观察节拍应独立于入场决策，把“更新风险状态”与“准入检查”分开。必须从真实 bar 流程测试上涨持有、回落后入场和软暂停期间的峰值，而非只测试 RiskController 单函数。

## Verification

### 固定版本全套测试

将 `33914dc` 以本地临时 clone 检出，保留 Git 元数据；PYTHONPATH 指向该检出的 src 和两个 Toolkit 包，PATH 包含已安装 .venv/bin。使用现有 Python/依赖环境，子进程超时 180 秒：

```sh
python -m pytest tests/ -q
```

最终结果：**2,918 passed, 30 skipped, 1 xfailed, 1 warning in 46.20s**，exit 0。warning 为 vendored pandas-ta 的无效转义 SyntaxWarning。

最初用 archive 快照运行时缺少 Git 元数据，且 PATH 没有 arx-runner，导致 3 个构建 setup error 和 1 个 CLI 失败。补齐验证条件后先重跑两文件（8 passed），再完整重跑取得上述结果。这些环境失败没有被计作代码 bug。30 skipped 与 1 xfailed 不算验收通过，也未声称跑了场所实盘测试。

### 独立复现与对照

```sh
uv run --extra dev --extra nautilus python .forge/reviews/2026-09-21-custos-fix-recheck-repro.py
```

实际使用 `.venv/bin/python`。**七个缺陷探针成立，旧 EE-1 的原生对照通过。** 脚本通过 Ruff check/format。

- FR-1：真实 BacktestEngine、真实原生挂单/改单/成交，以及实际 Runner gate；预留存储为未被调用的测试端。
- FR-2/7：实际 Toolkit 调用链，持仓、行情和回报受控。
- FR-3：实际 host containment，缓存和策略撤单端受控。
- FR-4/5：实际 daemon gate/notifier 与真实安全类，readiness/policy resolver 受控。
- FR-6：实际 breaker，注入一次状态保存失败。

没有真实交易所调用、生产密钥、业务代码修改、推送或发布。

## Suggestions

优先修 FR-1 的修改后风险判定、FR-3 的空仓撤单和 FR-5 的跨模式更新；随后闭合 FR-2/4/6/7 的生命周期与观察节拍。七项应分别增加预期正确行为的回归，保留已经通过的原场景测试，避免修一条路径又破坏另一条。

## Risk Assessment

**复核结论：有修复进展，但未通过完整闭合验收。** 现有全套测试为绿，独立组合用例仍出现改大平仓后开反向仓位、保护状态丢失、挂单未清除和错误冻结等问题。上述证据支持继续修复，不支持生产放行。
