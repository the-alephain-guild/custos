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

## 容器与验证

`make verify-local-v030` 构建并检查本地镜像契约。将 runner 状态挂载到 `/home/custos/.arx`，并在运行时提供 age identity。完整签名部署仍需要签发的身份、传输和发布输入。将结果归于当前源码前，先确认镜像 revision。

按[就绪检查](/operator-guide/readiness-health)分别核对健康、订阅和已应用实例。当前支持范围与生产限制见[发布状态](/release-governance/release-status)。
