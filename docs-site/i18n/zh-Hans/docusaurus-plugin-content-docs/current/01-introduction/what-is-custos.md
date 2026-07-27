---
title: "什么是 custos"
sidebar_position: 1
---


# 什么是 custos

## 限界上下文

custos 只承担本地执行机制:

- runner enrollment 材料与本地机器凭据;
- the control plane 签名指令的验证;
- 期望部署态到本地引擎的对账 (reconcile);
- 进程监督、看门狗与本地安全熔断器;
- 观察到的 runner facts 的签名与发布.

custos **不**承担 actor 授权、审批流、策略与风控配置、发布决策、组合真相、结算真相
或规范的部署生命周期.
