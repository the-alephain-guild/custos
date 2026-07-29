---
title: "排障"
sidebar_position: 6
---

# 排障

面向"起不来、收不了部署、连不上交易所"的症状式诊断。连接中断与恢复见[应急手册](./emergency-playbook)。

## 读日志

日志是结构化 JSON。运行时事件以 `deployment_instance_id` 为键；`spec_id` 只作为来源出现，不是运行时句柄。

Custos 从不记录 API secret、不透明机器凭据、私钥、enrollment token 或解密后的金库值。若你在日志里看到其中任何一项，请按安全问题处理，见
[SECURITY.md](https://github.com/the-alephain-guild/custos/blob/main/SECURITY.md)。

## 启动身份校验失败

**症状**：`Runner startup authority check failed`，且不产生 ready 文件。

依次检查：

1. `runner.toml` 存在、模式为 `0600`、且只含公开元数据。
2. 其 `machine_vault_path` 指向 enroll 出来的机器金库。
3. `SOPS_AGE_KEY_FILE` 存在、模式为 `0600`，且确实能解开那个金库。
4. 凭据 ID、版本、有效期、tenant、runner 与机器密钥与元数据**逐项**一致。

不要手工编辑身份文件。请在上游吊销或轮换，或用新的一次性 token 重新 enroll 一个机器主体。被手工改过的 runner 无法自证任何东西 —— 而自证正是这道校验的全部意义。

## 交易所凭据失败

**症状**：引擎部署前出现凭据解密失败或 permission-scope 失败。

```bash
arx-runner vault verify --key-id binance-testnet --tenant-id acme
```

每个交易所凭据必须是独立的 sops+age 文档，scope 为 `trade_no_withdraw`。用
`arx-runner vault put` 替换有问题的条目。**绝不要**把密钥放进 argv、`runner.toml`
或部署本身。

## 部署指令被拒

常见原因：

- 签名或 key ID 无效；
- subject 中的 tenant / runner / 实例与签名载荷不一致；
- canonical digest 不匹配；
- generation 过期，或既有实例的策略身份发生变化；
- 策略发布的快照 / 产物 / manifest 绑定不匹配；
- 类型化的 `execution_config` 非法，或所选引擎不支持；
- live 指令缺少 promotion 证据；
- live 指令被投给不支持 live 的引擎。

修复路径永远在上游：改正 canonical 状态，让上游发出新的签名 generation。**绝不要**往传输层直接注入指令 —— Custos 本来就会拒绝，而一条被接受的注入指令是不可验证的。

## 引擎或交易所失败

认证类失败：检查交易所密钥状态、IP 白名单、时钟同步，以及该凭据确实是
`trade_no_withdraw`。

code-hash 类失败：部署与签名部署相匹配的、经过评审的策略字节。不要试图绕过
[live 执行门](/concepts/live-execution-gate) —— 它拒绝，正是因为它校验的东西对不上。

熔断器、本地名义敞口上限与 zombie watchdog 都以 `deployment_instance_id` 为键。一个实例触发不得平掉或停掉另一个实例；若观察到这种现象，值得作为 bug 上报。

## 事件对照

| 事件 | 含义 |
|---|---|
| `runner_command_runtime_intake_failed` | 指令订阅不可用 |
| `deployment_spec_decode_failed` | 签名事件或 subject 校验/解析失败 |
| `deployment_reconcile_failed` | 某实例的本地引擎 apply 失败 |
| `deployment_lifecycle_fact_enqueue_failed` | 已应用的 generation 未能持久上报 |
| `engine_admission_live_capability_denied` | 该 host 无法安全执行 live |
| `nt_stop_noop_unknown_instance` | 对不存在实例的幂等 stop |
