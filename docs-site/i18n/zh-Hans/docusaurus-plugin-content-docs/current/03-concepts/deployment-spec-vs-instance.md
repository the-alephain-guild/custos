---
title: "部署 spec 与实例"
sidebar_position: 1
---

签名 `DeploymentSpec` 记录不可变配置，`DeploymentInstance` 标识该配置的一次运行实例。

| 标识 | 用途 |
|---|---|
| `deployment_spec_id` / `deployment_spec_digest` | 配置来源记录 |
| `deployment_instance_id` | 寻址引擎、生命周期、watchdog、熔断器和事实流 |
| `generation` | 排序同一实例的目标状态变化 |

多个实例可引用同一 spec。传输重试和同一已接受指令的重放保持实例身份；新签发的部署实例拥有自己的 id。该身份模型不代表宿主并发不受限，Nautilus 当前在同一 event loop 上只接受一个活动 node。

已应用与已报告进度分别跟踪，使报告失败后可重试而不重复已提交的引擎操作。RunnerFact 同时携带运行身份和配置来源，ARX 验证后再更新正式业务状态。

离线 spec 使用独立 `spec_id` 和 generation，桥接层从 spec id 派生确定性的本地运行 UUID，以便重启后识别。该 UUID 和未签名本地状态不能替代 ARX 签名实例授权。
