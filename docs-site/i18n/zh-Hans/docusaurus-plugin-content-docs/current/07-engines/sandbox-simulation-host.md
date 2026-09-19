---
title: "Sandbox 模拟宿主"
sidebar_position: 2
---

`--engine sandbox-sim` 选择 `SandboxSimulationHost`。它仅接受 sandbox，不持有交易所仓位，也不连接交易所。

## 验证范围

签名通道的外围运行时验证并激活产物、解析本地凭据、应用生命周期变化，并通过适配器发布签名事实。离线通道用模拟宿主验证本地目标状态投递、金库读取、实例附着和未签名状态报告。

模拟宿主不导入或运行交易策略。测试策略在行情和本地成交环境中的行为时，应使用 `--engine nautilus` 和兼容策略。

## 观测

未平仓名义价值为零，flatten 仅记录日志。模拟实例部署完成后即可就绪，无需等待行情。签名事实适配器提供签名通道的观测接口；离线输出仍为未签名本地状态。

可执行练习见[独立 sandbox](/getting-started/standalone-sandbox)，模式限制见[执行准入](/concepts/live-execution-gate)。
