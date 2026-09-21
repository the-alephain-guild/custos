# 27 - a-policy-renewal-must-reach-the-live-boundary

> **Status**: ✅ Completed
> **Created**: 2026-09-21
> **Project**: custos
> **Source**: `.forge/reviews/2026-09-20-custos-fix03-recheck-review.md` RR-5

## 根因

`RunnerReservationBoundary` 在 deploy 时绑定一个 `policy_id`，此后**不再变**
（`order_reservation_boundary.py:120` 的 `self._policy_id = policy_id`，`:405` 用它去 reserve）。

策略换版走的是另一条路：`runner_control_consumer.py:97-121` 的 `_process_policy` 验签成功后调
`record_verified_runner_safety_policy` 推进数据库 head，然后 ack —— **完全不碰活着的 boundary**。

而 reserve 要求用的就是 head（`runner_fact.py:5283-5292`）：

```python
if require_current:
    head = self._current_policy_for_scope(connection, row)
    if head["policy_id"] != row["policy_id"] or ...:
        raise RunnerStateAuthorityError("reservation requires the current effective runner policy")
```

所以一次正常换版之后，活着的策略**每一笔新订单都会被拒**，即使新策略放宽或保持限额。
报告的实证：两份都验过签、额度都是 150；旧 boundary 提交一笔额度仅 25 的订单被拒，
同一数据库用新 policy id 预留 25 则成功。

## 已经对的那一半

熔断器侧不需要改：`FallbackBreaker.apply_config` 的注释写着「Peak equity + frozen state are
preserved so a refresh does not silently reset the drawdown high-water mark or clear an existing
trip」。验收要的「保留 breaker 锁定和高水位」在那里已经成立，本 plan 要做的是**让它真的被调到**，
外加 boundary 的 policy_id 一起换。

风险域也已经是稳定的：`runner_risk_latch` 与敞口检查点按 tenant + mode + runner 键，不按 policy_id
（fix 20 的 close-out 里核过），所以换版不会丢敞口归属。这一条要有测试钉住，不能只是「看起来是」。

## 修复任务

### Fix 1: 换版原子地到达活着的 boundary [P1]

**Files**: `src/custos/core/order_reservation_boundary.py`、`src/custos/core/runner_control_consumer.py`、
`src/custos/cli/_daemon.py`、`tests/test_a_policy_renewal_reaches_the_live_boundary.py`

1. 先写失败测试：boundary 用第一版建好，存入第二版，随后一笔在额度内的订单必须**通过**。
2. `RunnerReservationBoundary` 加 `adopt_policy(policy_id, breaker_config)`：换 policy_id、
   对 breaker 调 `apply_config`。**一步之内两件事都做完**——只换一个会让 boundary 和它的上限
   各说各话。
3. 消费者不认识 boundary，也不该认识。给它一个可选的「提交成功之后」回调，daemon 供给一个
   闭包，闭包持有 `boundaries` 与 resolver。消费者只负责在**落盘成功之后**叫它。
4. 回调抛错不能让消费者把已经验签落盘的策略 nak 掉——落盘已经成功，重投只会重复落盘。
   记录并继续，但要**大声**。

### Fix 2: 换版不得丢掉守护状态 [P1]

**Files**: 测试

三条分别断言，不合成一条：

| 要保住的 | 断言 |
|---|---|
| breaker 冻结 | 换版前冻结 → 换版后仍冻结 |
| 高水位 | 换版前 peak=1000 → 换版后仍 1000 |
| 稳定风险域 | 换版前记的敞口/闩在换版后仍被看见 |

**验收**（报告原文）：为已验签策略更新实现原子的约束切换，并保留稳定风险域、breaker 锁定和
高水位。覆盖不断开节点的 policy renewal。

「不断开节点」是关键词：整条测试不得经过 stop / deploy。

## 验证清单

- [x] 失败测试先红后绿
- [x] 全程不 stop/deploy（「不断开节点的 policy renewal」）
- [x] 冻结 / 高水位 / 风险域三条各自有断言
- [x] 回调失败不把落盘成功的策略 nak 掉
- [x] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 换版到达 boundary | P1 | ✅ | 2026-09-21 | RR-5 |
| 2 守护状态不丢 | P1 | ✅ | 2026-09-21 | RR-5 验收 |

## 偏离与改进日志

无。审查报告的验收条款被逐句拆开对照（C27），没有一句需要改写设计。

## 完成报告 (Close-out Report)

- **完成日期**: 2026-09-21
- **总 Fix 数**: 2
- **偏离数**: 0
- **验证结果**: 全部通过
- **实施 commit 范围**: `bd23a0e`（plan）..`03c08d2`（实施）
- **契约影响**: 无。`RunnerControlConsumerV1.__init__` 新增的 `on_policy_committed` 是
  keyword-only 且默认 `None`，既有调用点不改也能构造；wire 契约、schema、authority 资产均未动。
- **红线守护**: 四条全数守住。与本 fix 最相关的是红线 0.3「失联≠停止」——换版通知失败时
  boundary 保留旧 policy 继续守，不降级、不放行；消费者也不因此 nak 掉已落盘的策略。

### 测试条数（`pytest --collect-only` 实跑）

| 测试文件 | 条数 |
|---|---|
| `tests/test_a_policy_renewal_reaches_the_live_boundary.py` | 9 |
| `tests/test_plan_closeout_counts.py` | 75 |

第二行不是本 fix 新写的测试：那个探针按 plan / fix 文件参数化，本份 close-out 的存在让它从 73
长到 75。按 `progress-management.md` 的规则，动了别人数过的文件就得在自己的 close-out 里重数
一遍，而不是去改 fix 26 那份历史记录。

### 验收条款逐句对照（C27）

报告 RR-5 的验收原文：「为已验签策略更新实现原子的约束切换，并保留稳定风险域、breaker 锁定和
高水位。覆盖不断开节点的 policy renewal。」拆成五个分句，逐句点名覆盖它的测试：

| 验收分句 | 覆盖它的测试 |
|---|---|
| 原子的约束切换 | `test_adopting_the_renewal_lets_the_strategy_keep_trading` —— 一次 `adopt_policy` 之内 policy id 与 breaker 上限同时换掉；扰动 P1 / P2 各拆掉其中一件，都会红 |
| 保留稳定风险域 | `test_a_renewal_keeps_the_exposure_recorded_under_the_old_revision` |
| 保留 breaker 锁定 | `test_adopting_a_renewal_keeps_an_existing_freeze` |
| 保留高水位 | `test_adopting_a_renewal_keeps_the_high_water_mark` |
| 不断开节点 | 整个文件不出现 stop / deploy —— 缺陷本身就是「必须重新 deploy 才恢复」，用 deploy 演示等于没演示 |

另有两条不在验收原文里、但缺了就等于没接线：消费者提交后真的叫了通知
（`test_the_consumer_calls_the_notifier_after_it_commits`），以及 daemon 真的把通知器交给了
消费者（`test_the_daemon_hands_the_notifier_to_the_control_consumer`）。后者是 lesson #40 的
形态——能力齐备而 composition root 没接，测试照样全绿。

### 扰动验证

五处，各用独立的 `PYTHONPYCACHEPREFIX`（C15），还原一律从 scratchpad 的备份拷回、不从
`git show HEAD:` 取（C15 续编：本次修复尚未提交时 HEAD 上是缺陷版本）。

| # | 把修复改回什么 | 转红的测试 |
|---|---|---|
| P1 | `adopt_policy` 不再换 policy id | 3 条，含换版后仍被拒 |
| P2 | `adopt_policy` 不再 `apply_config` | 2 条，含新上限不生效 |
| P3 | 消费者提交后不叫通知 | 2 条 |
| P4 | 通知抛错不再被拦住 | `..._does_not_undo_a_committed_policy` |
| P5 | daemon 构造消费者时不传通知器 | `..._hands_the_notifier_to_the_control_consumer` |

还原后 9 条全绿。

### 本次顺带修掉的一处静默

`_build_policy_renewal_notifier` 里「策略换版但没有 owner policy 可采纳」那条警告，初版写成
stdlib `log.warning(..., extra={...})`。本仓 `configure()` 是 `format="%(message)s"`，**不渲染
extra**——运维只会看到一个孤零零的事件名，`trading_mode` 整个丢掉。改走
`custos.core.log.get_logger` 的 kwargs（`_slog`）。`_daemon.py` 既有的 11 处 `extra={}` 按 C6
不动，它们被 `docs/authority/**` 按字节 pin 住。

### 功能验证（主路径）

1. 让一个 runner 在 live boundary 上跑着，从控制面下发一份新的已签名 runner safety policy
   （同 tenant / mode / runner，policy id 递增）。
2. 不做任何 stop 或 redeploy，观察日志里应出现 `runner_policy_renewal_adopted`，
   每个活着的 deployment instance 一条，带新的 `policy_id`。
3. 让策略继续下一单额度之内的委托：应当正常通过，而不是被
   `reservation requires the current effective runner policy` 拒掉。若新策略收紧了上限，
   超过新上限的那一单应当被拒——这两件事一起才说明换的是同一份策略。
