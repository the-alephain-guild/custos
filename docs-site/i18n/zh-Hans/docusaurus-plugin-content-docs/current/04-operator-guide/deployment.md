---
title: "部署"
sidebar_position: 1
---

选择一条操作路径，并配套管理身份、broker 和状态文件。

| 路径 | 指南 | 所需上游 |
|---|---|---|
| 本地生命周期演练 | [独立 sandbox](/getting-started/standalone-sandbox) | 仅自有 NATS |
| 策略测试网执行 | [离线 testnet](/operator-guide/offline-testnet) | 自有 NATS 与支持的交易所测试网 |
| 签名部署 | [签名 sandbox](/getting-started/first-sandbox-run) | ARX 身份、传输、目标状态和发布材料 |

签名通道消费已签发指令，不创建自己的控制拓扑。离线通道提供 `identity standalone`、`nats bootstrap` 和 `deployment validate/publish`，用于操作者自有环境。离线结果不能晋升为生产。

## 签名发布信任

执行不可变发布材料前，取得被接受的 runner 本地策略 authority，以及预期发布方的 Sigstore root 和 workflow identity。策略与产物分开验证，产物不能选择自己的信任根。

下列命令使用已有 authority key 签发策略。所有变量需设为批准的输入，输出路径应尚不存在。

```bash
uv run arx-runner release-policy issue \
  --authority-private-key "$POLICY_PRIVATE_KEY_FILE" \
  --authority-public-key "$POLICY_PUBLIC_KEY_FILE" \
  --sigstore-trusted-root "$SIGSTORE_ROOT_FILE" \
  --policy-id "$POLICY_ID" --version 1 \
  --not-before "$POLICY_NOT_BEFORE" --expires-at "$POLICY_EXPIRES_AT" \
  --issuer "$SIGSTORE_ISSUER" --workflow-identity "$WORKFLOW_IDENTITY" \
  --source-repository "$SOURCE_REPOSITORY" \
  --envelope-output "$POLICY_ENVELOPE_FILE" \
  --receipt-output "$POLICY_RECEIPT_FILE" \
  --environment-output "$POLICY_ENV_FILE"
```

要接受多个发布方的 release，按发布方逐一重复 `--issuer`、`--workflow-identity` 与 `--source-repository`，三者成组、顺序一致。

`release-policy generate-development-authority` 可为隔离的本地练习创建密钥。该 authority 明确仅供开发，不构成生产批准。

使用生成的 envelope、公钥、派生 key id 和可信根配置 runner：

```bash
export CUSTOS_ARTIFACT_RELEASE_POLICY_ENVELOPE="$POLICY_ENVELOPE_FILE"
export CUSTOS_ARTIFACT_RELEASE_POLICY_PUBLIC_KEY="$POLICY_PUBLIC_KEY_FILE"
export CUSTOS_ARTIFACT_RELEASE_POLICY_KEY_ID="$POLICY_KEY_ID"
export CUSTOS_ARTIFACT_SIGSTORE_TRUSTED_ROOT="$SIGSTORE_ROOT_FILE"
export CUSTOS_ARTIFACT_REGISTRY=ghcr.io
```

key id 应采用生成的策略输出中的值。私有 registry 需同时设置 `CUSTOS_ARTIFACT_REGISTRY_USERNAME` 与 `CUSTOS_ARTIFACT_REGISTRY_TOKEN`，不要把 token 放入命令参数。信任输入缺失或认证发布材料不可用时，应先修复条件，不会自动使用开发材料兜底。

## 持久化状态

持久化身份元数据、机器/交易所金库、age identity、能力、传输授权和事实数据库。为产物缓存、隔离与激活目录配置合适的本地存储。`--production-state-root` 可将可变的签名通道路径绑定在同一持久化根下，会拒绝离线选择和不安全目录。

不要让不同 runner 进程共享状态根。离线通道通过 `--offline-state` 使用独立 SQLite 数据库，不把它作为签名业务权威。

## 每个 runner 进程一个实例

一个 runner 进程同一时间只运行一个策略实例：Nautilus 引擎每个进程只支持一个交易节点，runner 把它交给自己启动的那个实例。要同时运行多个实例，就运行多个 runner 进程，每个进程使用自己的身份和状态根。

在同一个进程里，实例可以先后运行。停止正在运行的实例之后，runner 就可以启动另一个实例，包括同一策略的新版本，或者同名的另一个策略。每个经过验证的产物都在自己的策略注册表中加载，先前产物注册过的名字不会挡住后来的产物。

runner 无法执行某个启动命令时的处理方式：

| 情形 | runner 的处理 | 可观察到的结果 |
|---|---|---|
| 本进程中有另一个实例正在运行 | 在加载新产物之前拒绝这次启动 | 一条签名的生命周期结果 `retry_exhausted`，状态为 `stopped`；正在运行的实例不受影响 |
| 正在运行的实例正在停止 | 等待并重试这次启动 | 停止完成后启动照常进行 |
| runner 的能力回执没有覆盖这个新实例 | 确认并保留该命令，不加载也不启动任何东西 | 日志事件 `runner_command_awaiting_capability_binding`；暂时没有生命周期结果 |

以上任何情形下，后续命令（包括对正在运行实例的停止命令）都照常处理。

能力回执只在 runner 启动时读取一次。在那之后创建的部署，要等签发了覆盖它的能力回执并重启 runner 之后才被覆盖。重启时，runner 会启动或拒绝之前保留的命令，并回报签名结果；仍未被覆盖的命令继续保留，runner 记录日志 `durable_command_recovery_skipped`。对能力回执未覆盖实例的停止、暂停或归档命令同样被保留：这样的实例从未由本进程启动，没有需要停止的东西。重启后如果能力回执覆盖了该实例，runner 会执行保留的命令，并回报一次签名的生命周期结果。如果同一实例之后又收到了更新的命令，被取代的保留命令不再回报，runner 改为处理更新的命令。

重启后如果有多个实例需要恢复，runner 会逐个恢复，重启前正在运行的实例优先。其余实例按占用被拒绝并回报。

## 容器与验证

`make verify-local-v030` 构建并检查本地镜像契约。将 runner 状态挂载到 `/home/custos/.arx`，并在运行时提供 age identity。完整签名部署仍需要签发的身份、传输和发布输入。将结果归于当前源码前，先确认镜像 revision。

按[就绪检查](/operator-guide/readiness-health)分别核对健康、订阅和已应用实例。当前支持范围与生产限制见[发布状态](/release-governance/release-status)。
