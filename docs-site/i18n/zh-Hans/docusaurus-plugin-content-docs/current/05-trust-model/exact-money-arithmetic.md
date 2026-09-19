---
title: "十进制金额运算"
sidebar_position: 5
---

Custos 在类型化金额边界使用 `Decimal`，金额 wire 值使用规范十进制字符串。非金额计时值可使用 float，但不能进入金额运算或签名事实载荷。

## 构造与序列化

从精确输入字符串构造 Decimal。`Decimal("0.1")` 精确表示 0.1，`Decimal(0.1)` 则保留二进制浮点近似。已经舍入的 float 转成字符串，也不能恢复原始精度。

事实 wire 中的金额使用 `"100.00"` 这样的字符串，序号与计数仍使用整数。消费者应解析为精确十进制类型，并在需要时执行契约规定的精度与舍入规则。

## 执行边界

引擎快照中的金额字段拒绝二进制浮点数。事实校验在持久化前递归拒绝 float 和非有限值。敞口、订单预留和熔断代码使用十进制运算。

相关入口为 `src/custos/core/engine_protocol.py`、`src/custos/core/order_reservation_boundary.py`、`src/custos/core/fallback_breaker.py` 和 `src/custos/core/runner_fact.py`。测试包括 `tests/test_nt_risk_engine.py` 与 `tests/test_runner_fact_store.py`。

审查时同时检查上游转换和计算。结果类型为 Decimal 不能证明输入精确；正确表示也不能证明价格新鲜、估值可靠或策略盈利。
