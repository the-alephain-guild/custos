---
title: "DeploymentSpec 与 DeploymentInstance"
sidebar_position: 1
---

# DeploymentSpec 与 DeploymentInstance

这两者的区别是运行时正确性的基础：一个说的是**配置了什么**，另一个说的是**哪个正在运行的
东西**做了某件事。

## DeploymentSpec

一份由上游拥有的**不可变**配置。它包含策略产物来源、模式、目标 runner、凭据范围、参数，
以及 —— 对 live 模式 —— 放行证据。

`deployment_spec_id` 与 `deployment_spec_digest` 是**来源凭证**。它们记录配置内容，
不指向任何正在运行的东西。

## DeploymentInstance

在某台 runner 上运行某份 DeploymentSpec 的**一次尝试**。`deployment_instance_id` 是运行时
主键。

同一份 spec 的重试、重新部署与并行实例，各自拥有**不同的**实例标识。这正是重试不会作用到
错误进程上的原因：这个标识命名的是「这次尝试」，不是配置。

一个具体后果：看到同一个 spec id 出现两次，不代表出了问题 —— 可能是两个合法的并行实例。
而同一个 instance id 出现两次，才是需要解释的事。

## 期望 generation 与本地水位

generation 是附在签名期望状态指令上的**单调整数**。

Custos 把「已应用 generation」与「已上报 generation」分开跟踪。因此一次事实入队失败只会
重试**上报**，而不会重复一次已经成功的引擎动作 —— 工作本身与工作的记录可以各自独立恢复。

## 引擎句柄

对应**一个**部署实例的本地引擎资源。所有引擎协议操作都接收 `deployment_instance_id`；
spec 标识只作为来源凭证保留在事实与诊断中。

## RunnerFact

由 Custos 发出的签名观测，陈述这台 runner 观测到或执行了什么。

它**本身不是**规范的业务生命周期。ARX 先验证并持久化它，之后才改变规范状态。runner 负责
报告，不负责决定它的报告意味着什么。
