---
title: "部署"
sidebar_position: 1
---

# 部署

Custos 跑在你自己的基础设施上。ARX 负责授权意图、持有部署业务状态、签发指令并接收执行事实；Custos 负责校验这些指令、协调本地运行时，并对上报的事实签名。

本章覆盖从零到跑起一个部署的完整过程。

## 运行时产物

当前下游开发使用的产物是经过验证的本地镜像：

```text
custos-runner:v0.3.0
```

用 `make verify-local-v030` 构建并过门。远程发布仍在推迟中。请直接消费该镜像，**不要**自行维护派生 Dockerfile —— 派生镜像不是门验证过的那一份产物。

## 前置条件

- 一个 ARX enrollment 端点和一次性 enrollment token。
- ARX 的 Ed25519 domain-event 公钥及其 key ID（必须精确匹配）。
- 能连到签名指令流的网络。
- 每个交易所凭据一个 sops+age 加密文件，scope 均为 `trade_no_withdraw`。

Custos 从不创建流，也从不发布部署指令。它的 `deployment` CLI 只有一个离线动作
`validate`。

## Enrollment 与交易所凭据

```bash
mkdir -p "$HOME/.arx/vault" "$HOME/.arx/state"
chmod 700 "$HOME/.arx" "$HOME/.arx/vault" "$HOME/.arx/state"
age-keygen -o "$HOME/.arx/age.key"
chmod 600 "$HOME/.arx/age.key"
export SOPS_AGE_KEY_FILE="$HOME/.arx/age.key"
export SOPS_AGE_RECIPIENT="$(age-keygen -y "$SOPS_AGE_KEY_FILE")"
install -m 600 /dev/null "$HOME/.arx/enrollment-token"
printf '%s' '<一次性 enrollment token>' > "$HOME/.arx/enrollment-token"

arx-runner enroll \
  --token-file "$HOME/.arx/enrollment-token" \
  --backend https://arx.internal \
  --tenant-id acme \
  --runner-id 22222222-2222-4222-8222-222222222222
rm -f "$HOME/.arx/enrollment-token"

printf '%s\n' '<交易所 api secret>' | arx-runner vault put \
  --key-id binance-testnet \
  --tenant-id acme \
  --api-key '<交易所 api key>' \
  --scope-digest '<部署中的 credential_scope.scope_digest>' \
  --api-secret-stdin \
  --age-recipient "$SOPS_AGE_RECIPIENT" \
  --permission-scope trade_no_withdraw
```

`runner.toml` 只保存公开的绑定元数据。不透明的机器凭据与 Ed25519 私钥一起加密存放在
`runner-machine.enc` 中。任何模式下都不支持手工编造 runner 记录 —— 无法自证 enrollment
的 runner 不会启动。

## 启动 runner

```bash
arx-runner start \
  --enabled-mode sandbox \
  --nats-sim-url tls://arx-nats.internal:4222 \
  --nats-sim-ca "$HOME/.arx/certs/arx-nats-ca.pem" \
  --nats-sim-server-name arx-nats.internal \
  --nats-sim-issuer-public-key "$ARX_NATS_SIM_ISSUER_PUBLIC_KEY" \
  --crucible-domain-public-key "$HOME/.arx/crucible-domain-event.pub" \
  --crucible-domain-key-id arx-domain-v1 \
  --engine nautilus
```

就绪判定是 fail-closed 的。只有在机器身份校验通过、且精确的 runner 订阅建立之后，
`arx-runner health` 才会成功。

`deployment_instance_id` 是运行时主键 —— reconciler 状态、引擎句柄、watchdog、熔断器和事实都以它为键。`spec_id` 标识不可变的配置来源，不是运行时句柄。

### 工作站演示

本地做不可提升的演示时，有唯一一个明文例外，即显式的 loopback sandbox 会话：

```bash
arx-runner start --enabled-mode sandbox --reconcile \
  --development-local-nats-url nats://127.0.0.1:24222 \
  --crucible-domain-public-key /tmp/demo/crucible-domain-event.pub \
  --crucible-domain-key-id arx-domain-v1 \
  --engine sandbox-sim
```

该开发标志会拒绝非 loopback 主机、`testnet`、`live`、URL 内嵌凭据，以及任何同时存在的生产端点。它**绝不是** TLS 或密钥权威校验失败后的降级通路 —— 校验失败就是失败。

## 部署生命周期

部署及其每一次期望状态变更都源自上游。Custos 没有本地创建路径。它会校验签名事件、
canonical digest、tenant、runner、部署实例与 generation，然后通过带认证的所有权边界解析策略发布物料。

生产策略执行还需要下面这组信任配置，要么完整、要么不启动：

```bash
export CUSTOS_ARTIFACT_CACHE_DIR=/var/lib/custos/artifacts
export CUSTOS_ARTIFACT_RELEASE_POLICY_ENVELOPE=/etc/custos/artifact-release-policy.json
export CUSTOS_ARTIFACT_RELEASE_POLICY_PUBLIC_KEY=/etc/custos/artifact-release-policy.pub
export CUSTOS_ARTIFACT_SIGSTORE_TRUSTED_ROOT=/etc/custos/sigstore-trusted-root.json
export CUSTOS_ARTIFACT_RELEASE_POLICY_KEY_ID=custos-artifact-release-policy-v1
export CUSTOS_ARTIFACT_REGISTRY=ghcr.io
```

如果用私有 registry，还需同时设置 `CUSTOS_ARTIFACT_REGISTRY_USERNAME` 与
`CUSTOS_ARTIFACT_REGISTRY_TOKEN`。token 刻意不提供 CLI flag，因此不会进入进程参数。

Custos 只接受配置的 HTTPS registry 上带签名的 detached 物料坐标，校验完整的快照与证据链，并把不可变 blob 存到 `$CUSTOS_ARTIFACT_CACHE_DIR/sha256/<digest>`。信任配置缺失或不完整会导致启动失败；解析器不可用时也绝不回落到开发物料。

live 执行需要已签发的 `promotion_id` 与 `promotion_evidence_digest`。Custos 校验它们存在且绑定正确 —— 它不清点审批人，也不自行实现职责分离策略。

已应用的生命周期 generation 通过签名事实 outbox 以
`RunnerDeploymentLifecycleFact.v1` 上报，序号由 outbox 分配。若事实无法持久入队，指令就不会被 ack。重投时会沿用同一实例与激活身份，不会重复执行已提交的引擎动作。

## 容器示例

可直接运行的 `examples/supertrend-testnet` Compose 文件只启动 runner；签名指令流是外部依赖。

```bash
make verify-local-v030
cd examples/supertrend-testnet
test -f .env || cp .env.example .env
docker compose up
```

务必持久化 `/home/custos/.arx`。临时挂载会丢失机器身份与交易所凭据，缺了它们 runner
不会启动。

出问题时见[排障](./troubleshooting)；中断与恢复见[应急手册](./emergency-playbook)。
