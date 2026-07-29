---
title: "架构一览"
sidebar_position: 3
---

# 架构一览

Custos 是一个跑在你自己机器上的守护进程。它接收签名指令，用**从不离开这台机器**的凭据对接交易所执行，并以签名声明的形式回报实际发生了什么。

本页用一次阅读给出整体形状。每一节都链到更深入的章节。

## 切分

```text
  ARX  ──── 签名的部署指令 ────▶  Custos
   ▲                              │
   │                              ├─▶ 凭据金库（本地）
   └──── 签名的 runner 事实 ───────┤
                                  └─▶ 交易所（Binance…）
```

**ARX** 认证你是谁、授权你所请求的事、持有部署记录、决定什么应该在跑。它从不持有交易所凭据，也从不下单。

**Custos** 验证指令、在本地解析凭据、运行策略、执行本地安全约束，并对引擎实际做了什么签名作证。它从不决定什么**应该**跑，也无法批准自己的部署。

两个方向，两次签名校验，两者之间没有共享秘密。这就是全部信任模型；下面的内容都是它如何被撑住的。

## 边界为什么划在这里

Runner 之所以开源，是因为这是「凭据不出机」这项主张唯一能被核实的方式。一个闭源的
runner 要求你把交易所密钥交给它，等于在索取它无法证明的信任。

所以读者应该能回答的问题不是「我信不信这份文档」，而是「代码是否真的照它说的做」。本站每一章保证都点名了文件与测试，让你可以**核实**而不是**采信** ——
引导版本见[审计清单](/zh-Hans/trust-model/audit-checklist)。

## 四条保证

这四条是结构性的。它们不是可开关的功能，每一条都有独立章节说明它如何被撑住。

| 保证 | 在哪里撑住 |
|---|---|
| 凭据不出本机 | [密钥不出本机](/zh-Hans/trust-model/keys-never-leave-the-host) |
| 实盘执行始终受门控 | [实盘执行始终受门控](/zh-Hans/trust-model/live-execution-is-gated) |
| 失联不等于停止 | [失联不等于停止](/zh-Hans/trust-model/safety-survives-disconnect) |
| 金额运算精确 | [金额运算精确](/zh-Hans/trust-model/exact-money-arithmetic) |

### 凭据不出本机

交易所凭据以 `sops`+`age` 加密文件形式存放在 `~/.arx/vault/` 下，一把密钥一个文件。解密在构造交易所客户端的那一刻于进程内完成；明文从不写入状态、日志、指令或事实。

机器身份同理。Ed25519 私钥在注册时于本地生成，在不被传输的前提下证明持有，并存放在同一个加密金库中。

### 实盘执行始终受门控

准入在任何引擎被构造之前运行，校验七项条件 —— artifact 就绪、模式一致、引擎支持该模式、引擎支持该连接器、凭据范围、该构建是否根本启用了实盘执行，以及指令是否携带签名的放行证据。

实盘能力属于**构建**，不属于配置文件。它无法在运行期被打开。

### 失联不等于停止

若 ARX 变得不可达，运行中的部署会依据持久化的已应用状态继续运行，本地守卫继续生效：总名义本金上限、回撤熔断器与僵尸看门狗全部在本地判断。

失去「接收新指令」的能力，与失去「保护账户」的能力不是一回事。把两者混为一谈，意味着上游一次故障要么停掉一个正常工作的策略，要么撤掉它的监管。

### 金额运算精确

价格、数量与名义本金全程使用 `Decimal`，在 wire 上序列化为字符串。Python 二进制浮点在任何持久化之前被递归拒绝，因此签名绝不依赖某种语言碰巧如何渲染浮点数。

## 六个模块

六个模块承载这些保证。每个都有自己的章节；本表是地图。

| 模块 | 职责 | 章节 |
|---|---|---|
| 注册 | nonce 绑定的持有证明、加密的机器凭据、轮换与吊销 | [注册](/zh-Hans/getting-started/enrollment) |
| 指令接收与 reconcile | 验证签名的期望状态、收敛本地运行时、持久记录结果 | [reconcile 循环](/zh-Hans/concepts/reconcile-loop) |
| 引擎宿主 | 监督交易引擎、配置交易所客户端、执行准入 | [NautilusTrader 引擎](/zh-Hans/engines/nautilus-trader) |
| 凭据金库 | 在进程内解密交易所凭据，并绑定签名范围 | [凭据金库](/zh-Hans/operator-guide/credential-vault) |
| RunnerFact | 经由持久本地队列发出的带类型签名声明 | [RunnerFact](/zh-Hans/concepts/runner-fact) |
| 传输 | 订阅签名的期望状态；发布签名事实 | [NATS subject](/zh-Hans/reference/nats-subjects) |

引擎宿主被刻意设计为唯一知道底层是哪个交易引擎的模块。其余一切都通过协议与它对话 ——
这正是安全守卫在任何引擎下表现一致的原因。

## 运行时身份

运行期一切都由一个标识键入：`deployment_instance_id`。reconciler、引擎、看门狗、熔断器、凭据解析与事实流全部按它索引。

spec 标识是配置来源的凭据 —— 它记录的是**配置了什么**，而不是**哪个正在运行的东西**做了某件事。同一份不可变 spec 的两个实例是两个独立的东西，一次作用到错误实例上的重试是一次真实事故。

见 [spec 与 instance](/zh-Hans/concepts/deployment-spec-vs-instance)。

## 模式

三个，且只有三个：`sandbox`、`testnet`、`live`。不存在隐式回落模式，也不存在第四个表示「生产」的值。模式是签名指令的一部分、subject 的一部分，也是每条事实的一部分。

见[交易模式](/zh-Hans/concepts/trading-modes)。

## 接下来读什么

- 第一次跑起来：[安装](/zh-Hans/getting-started/installation)
- 深入四条保证：[信任模型](/zh-Hans/introduction/trust-model)
- 消费它发出的事实：[消费 RunnerFact](/zh-Hans/integration/consuming-runner-fact)
- 自己核实这些主张：[审计清单](/zh-Hans/trust-model/audit-checklist)
