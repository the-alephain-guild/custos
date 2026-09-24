---
title: "独立 sandbox"
sidebar_position: 5
---

本练习使用本地 NATS、未认证的独立身份和 `sandbox-sim`，验证加密凭据读取、目标状态投递、本地应用和状态报告。它不导入交易策略、不连接交易所，也不产生签名 RunnerFact。

在 Custos 仓库执行 `make install`，并安装 `sops`、`age` 与 Docker。练习使用临时目录，与正常的 `~/.arx` 状态隔离。

## 1. 启动独立 broker

在另一个终端运行：

```bash
docker run --rm --name custos-docs-nats \
  -p 127.0.0.1:14222:4222 nats:2 -js
```

该 broker 无认证，只绑定 loopback，适用于本次临时练习。容器移除后数据丢失；持续运行时应使用有认证、持久化的自有基础设施。

## 2. 创建本地身份与演示金库条目

```bash
umask 077
export DEMO_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/custos-docs.XXXXXX")"
uv run python docs-site/examples/standalone/prepare.py --root "$DEMO_ROOT"
age-keygen -o "$DEMO_ROOT/age.key"
export SOPS_AGE_KEY_FILE="$DEMO_ROOT/age.key"
export SOPS_AGE_RECIPIENT="$(age-keygen -y "$SOPS_AGE_KEY_FILE")"
uv run arx-runner identity standalone \
  --tenant-id docs-demo \
  --runner-toml "$DEMO_ROOT/runner.toml" \
  --machine-vault "$DEMO_ROOT/vault/runner-machine.enc"
printf '%s\n' 'sandbox-demo-secret' | uv run arx-runner vault put \
  --key-id docs-demo --tenant-id docs-demo --api-key sandbox-demo-key \
  --api-secret-stdin --scope-digest "$(printf '0%.0s' {1..64})" \
  --vault-dir "$DEMO_ROOT/vault"
uv run arx-runner vault verify \
  --key-id docs-demo --tenant-id docs-demo --vault-dir "$DEMO_ROOT/vault"
```

保留输出的 `DEMO_ROOT` 路径。全零 scope digest 和演示密钥仅用于本次离线模拟，不是签名部署的凭据或范围证明。

`identity standalone` 写入加密机器身份，`backend_url=http://standalone.invalid`。它不能注册签名传输、发布签名能力或启动签名通道。

## 3. 初始化、校验与发布

```bash
uv run arx-runner nats bootstrap --profile standalone \
  --tenant-id docs-demo --nats-url nats://127.0.0.1:14222
uv run arx-runner deployment validate \
  --spec-file "$DEMO_ROOT/spec.json" --strategy-dir "$DEMO_ROOT/strategy" \
  --mode sandbox
uv run arx-runner deployment publish \
  --spec-file "$DEMO_ROOT/spec.json" --strategy-dir "$DEMO_ROOT/strategy" \
  --mode sandbox --tenant-id docs-demo --strategy-id docs-sandbox \
  --nats-url nats://127.0.0.1:14222
```

校验成功时退出码为零并输出目录摘要；发布会等待 JetStream 确认。`--strategy-dir` 为内存中的 spec 绑定或校验 code hash，不会改写 JSON 文件。目标状态 stream 为每个 subject 保留最新消息。

## 4. 运行模拟宿主

```bash
uv run arx-runner start \
  --runner-toml "$DEMO_ROOT/runner.toml" \
  --vault-dir "$DEMO_ROOT/vault" \
  --ready-file "$DEMO_ROOT/ready.json" \
  --offline-state "$DEMO_ROOT/offline.db" \
  --reconcile-strategy-id docs-sandbox --runner-label docs-runner \
  --engine sandbox-sim --nats-url nats://127.0.0.1:14222
```

保持进程运行。在另一个终端进入同一仓库，将 `DEMO_ROOT` 导出为步骤 2 的路径，然后执行：

```bash
uv run arx-runner health --ready-file "$DEMO_ROOT/ready.json" --json
uv run python docs-site/examples/standalone/status.py \
  --subject arx.docs-demo.deployment_status.docs-runner.docs-sandbox \
  --generation 1 --phase running
```

预期 daemon 为 `ready: true`，本地状态为 `observed_generation: 1`、`phase: running`、`health: healthy`。这表示模拟宿主已应用请求，不能证明真实引擎或投资组合已就绪。

## 5. 停止部署

```bash
uv run arx-runner deployment publish \
  --spec-file "$DEMO_ROOT/stop.json" --strategy-dir "$DEMO_ROOT/strategy" \
  --mode sandbox --tenant-id docs-demo --strategy-id docs-sandbox \
  --nats-url nats://127.0.0.1:14222
uv run python docs-site/examples/standalone/status.py \
  --subject arx.docs-demo.deployment_status.docs-runner.docs-sandbox \
  --generation 2 --phase stopped
```

观察到 generation 2 后，在 runner 终端按 Ctrl-C，再在 broker 终端按 Ctrl-C。排错期间保留临时目录。不要把此演示策略目录用于 `--engine nautilus`。

## 运行真实策略代码

使用 Nautilus 时执行 `make install-nt`，提供兼容的策略目录（其 `config.yaml` 须设置 `trading.connector`、`trading.pairs` 和 `trading.leverage`）、注册名称和适当的风险限额。runner 从该文件读取交易所、交易对和杠杆，而不是从 spec 读取。离线加载器在每个进程中只选择一次策略目录。另一个目录或 node 需要独立进程及状态。

使用 Binance testnet 时配置测试网专用凭据，将 spec 改为 `testnet`、移除 sandbox 余额，并选择 `--engine nautilus`，详见[离线 testnet](/operator-guide/offline-testnet)。SoDEX 的额外输入限制见[SoDEX](/engines/sodex)。
