---
title: "首次部署"
sidebar_position: 4
---

先选择与你的部署通道对应的契约。

| 通道 | 目标状态来源 | 投递方式 | 应检查的证据 |
|---|---|---|---|
| 签名 | ARX | 通过已配置订阅接收签名指令 | 对应实例的已应用 generation 和生命周期 RunnerFact |
| 离线 | 操作者 | `arx-runner deployment publish` 发布到本地 NATS | 状态 subject 中符合预期的 `observed_generation` |

## 签名部署

完成[注册](/getting-started/enrollment)，按[签名 sandbox](/getting-started/first-sandbox-run)配置可信输入，并带上 `--reconcile` 启动 daemon。在 ARX 中创建并批准部署。

runner 验证签名字节和 subject，持久化目标状态，解析并激活产物，读取绑定凭据，然后等待引擎就绪。已应用状态与生命周期事实在同一事务中提交，随后才确认指令。

核对生命周期观测中的 `deployment_instance_id`、spec 摘要和 generation。`arx-runner health` 成功只表示 daemon 健康检查通过，不能证明某个部署已应用。

无效签名、冲突 generation 和准入失败会被终止处理；可恢复的本地故障按有界策略重试。详见[协调循环](/concepts/reconcile-loop)。

## 离线部署

按[独立 sandbox](/getting-started/standalone-sandbox)校验和发布 `OfflineDeploymentSpec`。后续状态更新沿用 strategy id 和 `spec_id`，并递增 `generation`。停止部署时发布终止状态，再核对本地状态与交易所持仓。

离线状态未签名，不是 RunnerFact、审批或晋升收据。状态报告某个 generation 已应用，也不等同于 Nautilus 投资组合已就绪。
