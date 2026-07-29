---
title: "审计清单"
sidebar_position: 7
---

# 审计清单

Custos 开源，是为了让把交易所凭据托付给它的人能自己检验这份信任。本页是这个承诺的具体版本：跑什么、读什么，以及通过意味着什么。

这里没有任何一步需要我们配合。clone 下来自己走一遍。

```bash
git clone https://github.com/the-alephain-guild/custos.git
cd custos
uv sync --extra dev
```

## 第 1 步 —— 复现基线

```bash
make verify
```

它会跑格式化、lint 与独立测试基线。在一份干净 clone 上、没有凭据、也不需要访问我们的基础设施，它必须通过。若通不过就先停 —— 后面每一步都以绿基线为前提。

只跑测试不跑风格门用 `make test-baseline`。

## 第 2 步 —— 密钥不出本机

**声明**：凭据在进程内解密，从不写入日志、发布到上游，或传到任何可被观察到的地方。

```bash
# 日志调用里没有凭据材料
grep -rnE 'log\.(info|debug|warning).*api[_-]?key' src/ tests/

# 出站调用里没有凭据材料
grep -rnE 'publish.*password|send.*secret' src/
```

两条都应该没有输出。

然后读金库本身 —— `src/custos/core/per_key_vault.py` 与 `machine_credential_vault.py`。要看的是：secret 通过 stdin 而非 argv 递给 `sops`；解密结果用于构造客户端而不被留存；每次解密发出的审计事件只带标识符。

相关测试是 `tests/test_per_key_vault.py` 与 `tests/test_credential_lifecycle.py`。后者更有意思：它遍历构造出来的引擎对象图，断言从中触达不到任何凭据。

## 第 3 步 —— live 执行始终受门控

**声明**：抵达 live 交易所需要四道独立校验，且门是 fail closed 的。

```bash
# 引擎 host 之外没有构造交易所客户端
grep -rn 'CEXOMS\|BinanceClient\|OKXClient' src/ --exclude=host.py --exclude=venue_binance.py
```

应该没有输出 —— 在别处构造的交易所客户端就是一条绕过门的路径。

去读 `src/custos/core/engine_lifecycle.py` 里的 `_require_authorized_runtime` ——
这道门的全部就在这一个函数里，没有任何调用方能绕过它抵达引擎构造。它所查询的能力面是
`src/custos/engines/nautilus/host.py` 中的 `supports_trading_mode` 与 `supports_venue`。

然后确认每项条件都有测试证明它是活的而非死代码。这个区分很重要：一道永远不会触发的校验，看起来和一道每次都通过的校验一模一样。七项条件分别是什么见[实盘执行门](/zh-Hans/concepts/live-execution-gate)。

`tests/test_nautilus_host_capability.py` 覆盖能力声明，
`tests/test_main_host_selection.py` 覆盖某个选择绑定哪个宿主，
`tests/test_engine_lifecycle.py` 断言能力受阻与实盘拒绝都发生在任何引擎动作之前。

## 第 4 步 —— 失联时安全防护依然生效

**声明**：平台不可达时本地强制继续工作，runner 既不停止也不失去防护。

```bash
# 运行时任何地方都没有一刀切停机
grep -rn 'stop_all_strategies\|force_shutdown' src/custos/
```

应该没有输出。

三道防护各有自己的模块与测试：

| 防护 | 模块 | 测试 |
|---|---|---|
| 聚合上限 | `src/custos/core/local_cap.py` | `tests/core/test_local_cap.py` |
| fallback 熔断器 | `src/custos/core/fallback_breaker.py` | `tests/core/test_fallback_breaker.py` |
| zombie watchdog | `src/custos/core/zombie_watchdog.py` | `tests/core/test_zombie_watchdog.py` |

确认每一道都在本地 tick 上评估，而不是响应上游消息 —— 一道需要平台来喊它才运行的防护，防不住平台消失。

## 第 5 步 —— 金额运算精确

**声明**：全程 decimal，wire 上是字符串，money 路径里没有浮点。

```bash
grep -rnE 'float\(.*price|float\(.*amount|float\(.*notional' src/
```

应该没有输出。

`tests/test_nt_risk_engine.py` 与 `tests/test_runner_fact_store.py` 覆盖 decimal 路径及其 wire 表示。读的时候要盯的是：数值是用 `Decimal(str(x))` 而不是 `Decimal(x)` 构造的
—— 后者会静默继承二进制浮点误差，而两者扫一眼看不出区别。

## 第 6 步 —— 验证你真正要跑的那份产物

干净的源码树不能证明你部署的那个二进制。

```bash
make verify-local-v030
```

它会构建镜像、记录 revision label，并跑完整的 Docker 运行时契约以及针对真实 broker 的独立验收。

发布产物见[签名发布链](./signed-release-chain) —— wheel 有签名、镜像 digest 被记录，且运行时门是针对那个精确 digest 跑的，之后稳定 tag 才会指向它。

## 第 7 步 —— 核对边界声明

读[什么是 custos](/introduction/what-is-custos) 与[信任模型](/introduction/trust-model)，然后对照代码确认 runner：

- 除了自己发起的订阅之外，不暴露任何入站网络面；
- 没有本地创建或审批部署的路径；
- 缺少 promotion 证据的 live 部署会被拒绝，而不是继续执行；
- 无法被指使去解密一份凭据并把结果返回。

最后一条值得直接查。`decrypt` 只被本地 reconciler 调用。如果你找到任何一条从网络消息到
"解密结果离开进程"的路径，那是 critical 级发现 ——
[请上报](https://github.com/the-alephain-guild/custos/blob/main/SECURITY.md)。

## 通过意味着什么

每一步都通过，意味着你 clone 到的这棵树里的代码守住了四条保证，且由它构建出的产物行为一致。

它**不**意味着你的部署是安全的。Custos 无法替你挡住：带提币权限的凭据、没有 IP 限制的交易所账户、别人能读的主机，或者一个正确地在亏钱的策略。这些仍然是你的责任。
