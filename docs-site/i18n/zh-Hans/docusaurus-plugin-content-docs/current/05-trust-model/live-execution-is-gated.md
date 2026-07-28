---
title: "live 执行始终受门控"
sidebar_position: 3
---

# live 执行始终受门控

在 Custos 向真实交易所下单之前，四道独立校验必须全部通过。任何一道失败，部署即被拒绝。
这道门从不降级到更弱的模式，也从不静默接受。

机制细节见 [live 执行门](/concepts/live-execution-gate)。本页讲的是它为什么是一条保证
而不是一个特性，以及如何验证它确实成立。

## 它防的是什么

Custos 提供不止一个执行 host。noop host 会接受部署、报告健康、但不下任何单 —— 这正是
演练 enrollment 与 reconcile 时想要的。

在 `live` 模式下，同样的行为就成了危险的那个。一笔被投给"悄悄什么都不做"的 host 的订单，
从外部看与成功下单毫无区别。状态显示 running，日志显示健康，而你在对账时才发现什么都
没发生。

所以这道门选择拒绝而非降级。被拒绝的部署是响亮且可恢复的；被静默忽略的两者都不是。

## 四道校验

| 层 | 问题 | 拒绝事件 |
|---|---|---|
| 1 | 该引擎是否声明支持 live？ | `g6_gate_live_capability_denied` |
| 2 | 它是否支持部署中指定的交易所？ | `g6_gate_venue_unsupported` |
| 3 | 策略 code hash 是否与本地源码一致？ | `g6_gate_code_hash_mismatch` |
| 4 | 凭证 scope 是否为 `trade_no_withdraw`？ | `g6_gate_credential_scope_violation` |

每层发出各自的事件，因此运维能从日志直接看出是哪一层拒绝的，不必猜。

第 3 层阻止一个签名部署去跑"不是它被批准的那份"代码。第 4 层是兜底 —— 金库本来就拒绝
存储带提币权限的凭证 —— 因为单一强制点距离被绕过只有一次失误。

## 职责分离

live 部署另外要求签名部署中记录至少两个不同的审批人。没有则以 `sod_approval_missing`
拒绝。

审批是 ARX 的决定。Custos 不授予审批，也不判断谁算审批人 —— 它只是拒绝在没有"审批确实
发生过"的证据时行动。

## 如何验证

有意思的问题不是这些校验存不存在，而是它们是**活的**还是死代码。一道永远不会触发的校验，
在一份全绿的测试套件里，和一道每次都通过的校验长得一模一样。

```bash
# 引擎 host 之外没有构造交易所客户端
grep -rn 'CEXOMS\|BinanceClient\|OKXClient' src/ --exclude=host.py --exclude=venue_binance.py
```

在干净的树上没有输出。在别处构造的交易所客户端，等于整体绕开了这道门。

读 `src/custos/engines/nautilus/host.py` 里的 `supports_live` 与 `supports_venue` ——
那是准入查询的能力面。noop host 声明 `supports_live() -> False`，这正是第 1 层读取的
东西。
<!-- disclosure-ok: auditable source location -->

覆盖情况：

| 内容 | 测试 |
|---|---|
| 各 host 的能力声明 | `tests/test_nautilus_host_capability.py` |
| 某模式允许绑定哪个 host | `tests/test_main_host_selection.py` |
| 交易所适配与凭证接线 | `tests/test_nt_binance_venue.py` |

<!-- disclosure-ok: auditable source location -->

读的时候有个细节值得留意：交易模式比较是大小写不敏感的。ARX 与 runner 对模式的序列化
方式不同，大小写敏感的比较会造出一道**永远不触发**的门 —— 正是本节讲的那种死校验。

## 这道门不做什么

它守的是部署与交易所之间的边界。它不是风控引擎，对某笔交易是否明智没有意见。敞口限额
由另一套机制持续强制 —— 见[失联时安全防护依然生效](./safety-survives-disconnect)。
