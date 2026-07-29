---
title: "你的第一次部署"
sidebar_position: 4
---

# 你的第一次部署

一台运行中的 runner 在部署到达之前什么都不做。本章讲的是：到达的是什么、runner 拿它做什么、以及你怎么确认它成功了。

:::note 这不由你创建
Custos **无法**撰写或批准 DeploymentSpec，也不存在发布它的 CLI 命令。部署在 ARX 侧创建并批准，以签名事件的形式到达。如果这看起来不方便 —— 正是同一个性质，阻止了一台被攻破的
runner 给自己部署任何东西。
:::

## 到达的是什么

一条签名的期望状态指令，携带不可变的部署 spec、确切的部署实例、期望的生命周期状态，以及 generation。对 live 模式还额外携带放行证据。

runner 在解析**任何字段之前**，先对**精确字节**与**精确 subject** 验证签名 ——
未经验证的消息里没有任何内容可信，包括那些本该告诉你「是否该信任它」的字段。

## runner 做什么

1. 验证 envelope 与 subject 绑定。
2. 校验 tenant、mode、runner、instance、spec digest、release 绑定与 generation。
3. 持久化确切指令，并与该实例已接受的 generation 比对。
4. 通过已认证的解析器解析策略发布物。
5. 在不可变激活根下验证并激活 artifact。
6. 在本地解析凭据范围，并经引擎应用。
7. 等待带类型的就绪回执 —— **任务被创建不等于就绪**。
8. 在同一个事务里提交已应用状态与生命周期事实。
9. **在该事务之后**才确认。

完整细节见 [reconcile 循环](/zh-Hans/concepts/reconcile-loop)。

## 观察它发生

守护进程把结构化 JSON 写到 stdout。最值得盯的三个事件是：指令被接受、引擎报告就绪、生命周期事实入队。

```bash
arx-runner health --json
```

就绪只在第 8 步之后翻转。如果日志显示引擎有动作而 health 仍报未就绪，说明部署正在应用过程中，而不是失败了。

## 确认跑的是对的东西

去问 runner 它**应用了什么**，而不是你以为你发了什么：

- `deployment_instance_id` 是运行时身份。同一份 spec 的两个实例是两个独立的东西。
- spec id 与 digest 作为来源记录随每一条事实传出，因此事实流本身就能回答「跑的是哪份配置」，无需信任本地状态。

见 [spec 与 instance](/zh-Hans/concepts/deployment-spec-vs-instance)。

## 如果它被拒绝

部署可能在任何引擎被构造之前就被拒绝。那是**准入**，且是刻意的 —— 门选择拒绝，而不是启动一个更弱的版本。七项条件列在[实盘执行门](/zh-Hans/concepts/live-execution-gate)。

拒绝是终态且留审计的，**不重试**，因为那些条件不会因为等待而变为真。而瞬时失败 ——
引擎因可恢复原因启动失败 —— 则在持久化的重启预算下重试。两者被刻意归为不同类别的事件。

## 然后呢

一个运行中的部署不依赖「保持连接」。若上游权威变得不可达，部署会依据持久化的本地状态继续运行，本地守卫继续生效 —— 见[失联不等于停止](/zh-Hans/trust-model/safety-survives-disconnect)。
