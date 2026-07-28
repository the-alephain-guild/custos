---
title: "第一次 sandbox 运行"
sidebar_position: 3
---

# 第一次 sandbox 运行

目标是：runner 启动、证明身份、报告就绪，且**不接触任何交易场所**。本章里没有任何一步
能下单。

你应当已完成[注册](/zh-Hans/getting-started/enrollment) —— `~/.arx/runner.toml` 与加密
的机器金库必须已存在。

## 1. 写入一份凭据

即便是 sandbox 运行也会解析凭据，因为走的是与真实部署完全相同的那条路径。在这里先演练
一遍，意味着「第一次真正要紧」不会同时是「第一次运行」。

```bash
export SOPS_AGE_KEY_FILE="$HOME/.arx/age.key"
export SOPS_AGE_RECIPIENT="$(age-keygen -y "$SOPS_AGE_KEY_FILE")"

printf '%s\n' '<sandbox-api-secret>' | arx-runner vault put \
  --key-id binance-sandbox \
  --tenant-id acme \
  --api-key '<sandbox-api-key>' \
  --api-secret-stdin \
  --scope-digest '<64-hex-scope-digest>' \
  --age-recipient "$SOPS_AGE_RECIPIENT" \
  --permission-scope trade_no_withdraw
```

请用 `--api-secret-stdin` 这种形式。其他几个是给非交互场景准备的，但通过
`--api-secret` 传入的密文会出现在 `ps` 输出与你的 shell 历史里。

确认 runner 真的读得回来：

```bash
arx-runner vault verify binance-sandbox
```

它跑的是真实解密路径，而不是它的模拟。手工调 `sops` 证明的是另一回事 —— 见
[凭据金库](/zh-Hans/operator-guide/credential-vault)。

## 2. 启动守护进程

```bash
arx-runner start \
  --enabled-mode sandbox \
  --engine sandbox-sim
```

`--enabled-mode` 是**必填**的，取值恰为 `sandbox` / `testnet` / `live` 之一。它没有默认值，
因为默认值等于一个没有任何人选择过的模式。

`--engine sandbox-sim` 选择模拟宿主：artifact 激活、凭据解析、持久化、就绪判定与事实发布
全部真跑，且从不连接场所。它只声明 `sandbox`，因此即便误操作也无法被指向真实资金模式 ——
见[实盘执行门](/zh-Hans/concepts/live-execution-gate)。

若你想要「真实行情 + 本地模拟成交」的 sandbox 会话，改用 `--engine nautilus`。两者都安全，
区别只在于是否涉及真实行情。

## 3. 检查就绪

```bash
arx-runner health
arx-runner health --json
```

就绪**不等于**「进程起来了」。它意味着：机器金库与 age 身份已找到、凭据未过期、tenant /
runner / credential id / version / expiry / key id 全部一致、权威方确认该凭据仍然有效，
且一份绑定到同一公钥的能力回执已通过验证。

非零退出码意味着其中一项没过。这是刻意的行为：**无法证明自身授权的 runner 不会启动**。

## 接下来发生的事不由你做

runner 现在正在等待一条签名的期望状态指令。它**无法自己造一条**。部署在 ARX 侧撰写并
批准，经由订阅到达 —— 见[你的第一次部署](/zh-Hans/getting-started/first-deployment-spec)。

如果什么都没来，runner 会一直等。这是正确的：一个没有指令的空闲 runner 是健康的，不是
卡住了。

## 起不来的时候

| 症状 | 原因 |
|---|---|
| 报机器金库相关错误退出 | 注册未完成，或 `SOPS_AGE_KEY_FILE` 未设置 |
| 报凭据绑定错误退出 | `runner.toml` 与金库不一致 —— 两者都不要手工编辑 |
| `vault verify` 失败但 `sops` 能跑通 | 你测的是另一条路径；CLI 才是验收面 |
| 能启动但始终不就绪 | 能力回执缺失，或未绑定到这把密钥 |

更多见[排障](/zh-Hans/operator-guide/troubleshooting)。
