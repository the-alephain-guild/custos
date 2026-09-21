# 27 - a-policy-renewal-must-reach-the-live-boundary

> **Status**: 🔲 Not started
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

- [ ] 失败测试先红后绿
- [ ] 全程不 stop/deploy（「不断开节点的 policy renewal」）
- [ ] 冻结 / 高水位 / 风险域三条各自有断言
- [ ] 回调失败不把落盘成功的策略 nak 掉
- [ ] `make verify` 与 `make verify-nt` 均 exit 0

## 进度追踪

| Fix | Priority | Status | Completed | Notes |
|---|---|---|---|---|
| 1 换版到达 boundary | P1 | 🔲 | | RR-5 |
| 2 守护状态不丢 | P1 | 🔲 | | RR-5 验收 |

## 偏离与改进日志
