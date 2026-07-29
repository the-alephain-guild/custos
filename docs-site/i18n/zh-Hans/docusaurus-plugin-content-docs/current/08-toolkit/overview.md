---
title: "策略工具包概览"
sidebar_position: 1
---

# 策略工具包概览

工具包是策略产物与执行它的 runner 之间的边界。它定义执行 ABI、Custos 在发布前签名的产物引用形状，以及决定某个产物是否**允许被导入**的验证器。

## Custos 在这里拥有什么

Custos 拥有四样东西，其余刻意不拥有：

- 策略**执行 ABI**；
- **工具包实现**；
- 签名前的 **`StrategyArtifactRefV1`**；
- 本地 **fail-closed 验证器**。

其上游的一切属于 ARX：策略源码、发布物料清单、签名的发布声明、分离的证明引用、产物证据、验收回执、产物选择、DeploymentSpec、生效配置与业务风险策略。

这个切分重要，是因为它决定了一台被攻破的 runner 能做什么。它能拒绝执行，但它**不能**换一个产物、批准一个产物，或宣布某个产物已发布。

## 执行 ABI

入口点组是固定的：

```text
alephain.strategy_runtime.v1
```

`deployment_instance_id` 是唯一的运行时地址。spec id、spec digest 与 generation 是来源与排序输入 —— 它们说明「配置了什么、以什么顺序」，而不是「该跟哪个运行中的东西对话」。目录别名从不授权、也从不寻址执行。

适配器的最终生效配置来自已验证的签名指令。它不能合并默认值、不能读配置文件，也不能修改拿到的东西。Custos 把 JSON 数字解析为 `Decimal`，拒绝重复键与非有限值，递归冻结容器，并重算 `effective_config_digest`。

冻结正是 digest 有意义的原因。一份适配器能在 digest 算完之后修改的配置，其 digest 描述的将是一个不再运行的东西。

### 规范 JSON

`sha256-canonical-json-v1`：UTF-8，对象键递归排序，数组顺序保留，数字为有限 `Decimal`，无无意义空白。

请按这些规则实现，而不是按某语言的默认编码器 —— 大多数至少在其中一条上不同。

## 产物边界

`StrategyArtifactRefV1`（`schema_version: 1`）**只**描述签名之前就存在的东西：确切的可执行文件与清单字节、运行时产物、SBOM、契约 schema。

它刻意不携带 bundle 坐标或 digest、不携带证书或透明性证明、不携带信任策略身份，也不携带发布 / 部署 / 批准 / 选择状态。这些是之后才产生的，一个声称拥有它们的引用等于在断言尚未发生的事实。

`StrategyManifestV1` 是产物本地的兼容性元数据，仅此而已。

Custos 不定义规范发布 BOM。它消费严格的 BOM 对象，并要求对每个成员做无损的内存投影：
base、contracts、Nautilus 与策略 wheel，加上清单、SBOM、契约 schema、规范化源码树，以及每一个运行时产物。证明 bundle 是分离的 —— 它永远不是 BOM 或 ArtifactRef 的成员。

签名指令绑定运行时身份、spec 来源、generation、release id、完整 BOM 对象与 digest、签名前 ArtifactRef、已验收证据，以及生效配置 digest。任何单独序列化的成员表都不允许成为「发布包含什么」的第二权威。

## 验证 fail closed

验证覆盖证书链、Fulcio 身份与有效期、SCT、DSSE PAE 与签名、Rekor 条目 / body / SET、包含性证明与 checkpoint。验证与安全解包都在导入之前完成。

以下**都不是**生产验证路径：跳过开关、Python 或 `cosign` 子进程、sidecar、HTTP 验证器，以及一个仅仅「结构上看起来合理」的 bundle。

信任根与预期的 issuer / workflow / policy 来自签名且不可变的本地发布配置。产物元数据可以**引用**信任根，但永远不能**选择**信任根。一个能挑选验证它自己的权威的产物，等于在自证。

## 两个 distribution

| Distribution | Python | 说明 |
|---|---|---|
| `custos-strategy-toolkit` | `>=3.11` | base 与 contracts |
| `custos-strategy-toolkit-nautilus` | `>=3.12,<3.13` | 需精确匹配的 base 版本，`nautilus-trader==1.230.0` |

在 Python 3.11 上，解析 Nautilus distribution 必须**失败**，而不是悄悄装成一个没有
NautilusTrader 的环境。静默省略会产出一个 import 干净、却根本不能交易的环境。

## 清单与类型债

已发布的清单对每一个确定性输入分类：**241** 个文件 —— 36 个平台无关、55 个 Nautilus 专用、
150 个私有 vendor。抽取过程把它们一一映射进 `custos_toolkit`、
`custos_toolkit_nautilus.adapter` 与私有的 `custos_toolkit_nautilus._vendor.pandas_ta`
命名空间。

抽取不得发布顶层 `shared` 或 `pandas_ta`、不得改 `sys.path`、不得伪造 distribution，也不得留下两份可写的规范副本。

类型检查如实分层报告，而不是给一个笼统的通过 / 失败：

| 范围 | 标准 |
|---|---|
| Custos 自有契约与包壳 | 严格 mypy，必须通过 |
| 清单抽取出的实现 | 对照精确记录的基线检查 |
| 私有第三方 vendor 代码 | 不进 mypy；由精确 digest 与固定输入 parity 守护 |

基线当前记录 **75** 个平台无关错误与 **289** 个 Nautilus 适配器错误。这是**已承认的债**，不是严格通过，并且是公开的而非被抹平 —— 一份你看不到的基线，是一份你无法据以追责的基线。

## 复现这些资产

```bash
make strategy-contract-assets   # 重新生成 schema、golden、回执资产、digest 索引
make check-toolkit-extraction   # 从固定源码重建每一个抽取目标
make toolkit-typecheck          # 该严格处严格，其余对照基线
```

`strategy-contract-assets` 拒绝保留前代轨道。另有一份独立的 parity golden，把抽取之前的固定输入信号 / 订单意图行为与私有 vendor 指标行为冻结下来 —— 从而能够证明这次搬迁没有改变结果。

## 当前状态

生产者回执为 `CANONICAL_V1_PENDING_CONSUMER_RECEIPTS`。交接、运行时与生产就绪**全为 false**。

Custos 发布执行 ABI；消费方必须钉住同一份确切的 V1 字节，协同交接才算闭合。Custos 不代替他们撰写回执 —— 一份由 Custos 替对方写的回执，无法证明对方真的读得懂这些字节。

这是首个生产契约：一个在用的 V1 parser、dataclass、schema、golden 集、资产索引与权威条目。被取代的形状是被删除，而不是留作别名或兜底。审计证据保存在 Git 历史与不可变 digest 里，运行时代码不携带它。
