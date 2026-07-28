# 03 — 实现细节

> 技术栈 · 依赖 · 项目结构 · 运行方式.

## 技术栈

- **语言**: Python >=3.11 (async 优先)
- **包管理**: `uv` (禁 pip / poetry)
- **格式化 / lint**: `ruff` (format + check, line-length=100)
- **测试**: `pytest` + `pytest-asyncio` (`asyncio_mode=auto`)
- **未来**: `pyright` 类型检查 (待 Plan 02+ 集成)

详见 [`../../.claude/rules/tech-stack.md`](../../.claude/rules/tech-stack.md).

## 生产依赖 (`pyproject.toml`)

| 库 | 版本 | 用途 |
|----|------|------|
| `nats-py` | >=2.9 | NATS JetStream client (telemetry uplink + spec pull) |
| `pydantic` | >=2.5 | Envelope schema + DeploymentSpec 数据模型 |
| `structlog` | >=24 | 结构化日志 (脱敏 + JSON output) |
| `uuid6` | >=2024.1.12 | UUIDv7 for event ID (时序单调) |

## 开发依赖 (`optional-dependencies.dev`)

| 库 | 版本 | 用途 |
|----|------|------|
| `pytest` | >=8 | 测试 runner |
| `pytest-asyncio` | >=0.24 | async 测试 |
| `ruff` | >=0.6 | format + lint |

## 未来依赖 (待独立 plan)

- `nautilus-trader` — NT runtime, 作为 `nautilus` optional-dep 加入 (Plan 00a)
- `pyright` — 类型检查, dev 加 (未来独立 style plan)

## 项目结构

```
custos/
├── src/custos/               ← Python 模块 (导入名 custos, pip 名 custos-runner)
│   ├── __init__.py
│   ├── __main__.py            ← 薄 shim (`from custos.cli.main import main`; main 是 0.2.0 legacy stub)
│   ├── core/                  ← 引擎无关承重墙
│   │   ├── config.py          ← local configuration models (DeploymentSpec authority belongs to Crucible)
│   │   ├── enrollment.py      ← 一次性 EnrollmentToken NATS 低层 client (供 CLI 层调用)
│   │   ├── credential_vault.py ← _BaseVault + AuditEvent enum + CredentialVault mock (SopsAgeVault 已在 0.2.0 删除)
│   │   ├── per_key_vault.py   ← PerKeyVault 生产 runtime (~/.arx/vault/<key-id>.enc, 0.2.0 起唯一 runtime 路径)
│   │   ├── runner_toml.py     ← ~/.arx/runner.toml 原子写 + 0600 mode 契约
│   │   ├── nats_client.py     ← JetStream client + build_subject() 函数
│   │   ├── reconcile.py       ← ReconcileLoop (level-triggered)
│   │   ├── runner_command_runtime.py ← signed command/runtime 唯一 V1 coordinator
│   │   ├── runner_fact.py     ← single SQLite state/sequence/signed-outbox deep module
│   │   ├── runner_fact_producer.py ← typed engine observations → RunnerFact builders
│   │   └── log.py             ← structlog 配置
│   ├── engines/nautilus/      ← NT Python 引擎
│   │   ├── host.py            ← SandboxSimulationHost + NtTradingNodeHost + execution admission
│   │   ├── risk.py            ← 本地 fallback breaker (drawdown + max_notional)
│   │   ├── runtime_loader.py ← 已验证 activation 的唯一 V1 entry-point loader
│   │   └── venue_binance.py   ← Binance venue 适配
│   └── cli/
│       ├── main.py            ← 0.2.0 legacy stub (`python -m custos` → sys.exit(2) + pointer)
│       ├── _daemon.py         ← run_daemon coroutine + local vault/host/safety composition
│       ├── validators.py      ← boundary 校验 (`validate_id` + `validate_backend_url` scheme allowlist)
│       └── subcommands/       ← `arx-runner` 五子命令
│           ├── __init__.py    ← main(argv) dispatcher (argparse add_subparsers)
│           ├── enroll.py      ← HTTP POST /api/v1/enrollments + runner.toml 持久
│           ├── vault.py       ← put / verify / list (sops encrypt per-key)
│           └── start.py       ← 读 runner.toml → _daemon.run_daemon(ns)
│
├── tests/                     ← pytest 测试 (115 pass baseline, 9 wire_shapes fail known)
├── scripts/
│   └── generate_wire_fixtures.py  ← 跨语言 wire fixture 生成 (arx 侧参照)
├── docs/
│   ├── domain.md              ← 顶层纸面 spec (6 BC + 分层信任边界)
│   ├── design/                ← 本目录 (架构 + 模块设计)
│   ├── ops/                   ← 部署 + runbook
│   └── guides/                ← 测试策略 + 开发上手
├── .claude/                   ← Claude Code 独立规则集
├── .forge/                    ← forge 工作流 plan + teams 配置
├── pyproject.toml
├── uv.lock                    ← 依赖锁 (必 commit, 保证可复现构建)
├── Makefile                   ← check / test / verify 等 target
├── CLAUDE.md                  ← AI 助手导航图
├── README.md                  ← 门面 (对外)
├── LICENSE                    ← Apache-2.0
└── NOTICE                     ← 归属声明
```

## Python 模块命名

- **pip 分发名**: `custos-runner` (`pyproject.toml` `[project].name`)
- **Python 导入名**: `custos` (`src/custos/`)
- rename `arx_runner` → `custos` 已由 Plan 05 完成

## 运行方式

### 装依赖

```bash
uv sync --extra dev
```

### 跑 daemon (paper mode 默认)

自 0.2.0 起, 通过 `arx-runner` console script 三步操作 (Plan 11 clean-break;
`python -m custos` 已 `sys.exit(2)`):

```bash
# 一次性: age identity + nonce-bound PoP → encrypted machine principal
export SOPS_AGE_KEY_FILE=~/.arx/age.key
export SOPS_AGE_RECIPIENT=age1...
arx-runner enroll --token <one-time-token> --backend https://arx.internal:8000 \
                  --tenant-id acme \
                  --runner-id 018f8b5f-6f7d-7e23-8c31-bd34ab9d0d41
arx-runner credential verify

# 一次性 (每 credential): sops+age encrypt → ~/.arx/vault/<key-id>.enc
arx-runner vault put --key-id binance-paper \
  --scope-digest '<DeploymentSpec credential_scope.scope_digest>' \
  --api-key '<venue-api-key>' --api-secret-stdin

# 日常启动 (读 ~/.arx/runner.toml + ~/.arx/vault/*.enc)
export SOPS_AGE_KEY_FILE=~/.arx/age.key
arx-runner start --enabled-mode sandbox \
  --nats-sim-url tls://crucible-nats.internal:4222 \
  --nats-sim-server-name crucible-nats.internal \
  --nats-sim-issuer-public-key "$CRUCIBLE_NATS_SIM_ISSUER_PUBLIC_KEY"
```

关键 `start` 参数:
- `--nats-sim-url` / `--nats-live-url` — Crucible-owned TLS NATS endpoints;
  workstation sandbox demos may instead explicitly use loopback-only
  `--development-local-nats-url`.
- `--wal-path` — telemetry WAL 路径 (默认 `~/.arx/state/telemetry-wal.db`)
- `--engine` — 引擎选择 (默认 `nautilus`, 未来 `hummingbot` / `athanor` 等接入槽已预留)
- `runner.toml` 只含 public binding metadata；opaque credential + Ed25519 private key
  只从 sops+age `runner-machine.enc` 解密到内存

### 跑测试

```bash
make verify         # check (fmt-check + lint) + test-baseline (115 pass)
make test           # 完整 pytest (含 wire_shapes 9 known fail)
make test-baseline  # 可绿基线 (排除 wire_shapes)
```

### Non-Custodial 4 红线专项 grep

```bash
# 见 .claude/rules/verification.md §Non-Custodial 4 红线专项检查
grep -rnE 'log\.(info|debug|warning).*api[_-]?key' src/ tests/
grep -rn 'CEXOMS\|BinanceClient\|OKXClient' src/ --exclude=host.py --exclude=venue_binance.py
grep -rn 'stop_all_strategies\|force_shutdown' src/custos/
grep -rnE 'float\(.*price|float\(.*amount' src/
```

## 关键设计约束

- **async 优先**: 主流程 asyncio, 阻塞调用 `asyncio.to_thread`
- **Pydantic v2 wire 契约**: `ConfigDict(extra="forbid")` 拒未声明字段
- **Decimal money math**: 所有价格/金额路径 (红线 0.4)
- **structlog 事件名**: 动词过去时 / 状态名词 (英文 snake_case)
- **UUIDv7 event ID**: 时序单调 (`uuid6.uuid7()`)

## 依赖参考

- 顶层技术栈: [`../../.claude/rules/tech-stack.md`](../../.claude/rules/tech-stack.md)
- 代码风格: [`../../.claude/rules/code-style.md`](../../.claude/rules/code-style.md)
- 常见错误: [`../../.claude/rules/common-errors.md`](../../.claude/rules/common-errors.md)
