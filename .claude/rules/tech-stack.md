# 技术栈约束 (custos)

custos 是**独立开源仓库** (Apache-2.0), 单栈 Python daemon. 本文件是仓库自足的技术栈规范,
不假设 monorepo workspace 存在.

## 语言与运行时

| 组件 | 语言 | 最低版本 | 包管理器 |
|------|------|----------|----------|
| custos runner | Python | >=3.11 | uv |

> **Plan 18 过渡边界**: root runner/base contracts 保持 Python >=3.11。现有 root
> `nautilus` extra 是待迁移 legacy packaging。目标
> `custos-strategy-toolkit-nautilus` 是独立 distribution，要求 Python
> >=3.12,<3.13、exact `nautilus-trader==1.230.0` 和 exact matching base version。
> 禁止用 PEP 508 marker 在 Python 3.11 静默跳过 Nautilus 依赖。

uv workspace 的开发/lock interpreter 固定为 Python 3.12，这是三个 workspace members
requirements 的共同交集；base distribution 的 Python 3.11 compatibility 由独立 wheel/import
gate 验证，不能从 workspace interpreter 推断。

## 包管理器强制规则

- **Python**: 使用 `uv`, 禁止直接使用 `pip` / `python -m pip` / `poetry`
  - 运行脚本: `uv run python script.py`
  - 运行测试: `uv run pytest` 或 `make test`
  - 安装依赖 (base + dev): `uv sync --extra dev`
  - 装 NT runtime (需 py3.12+): `uv sync --extra dev --extra nautilus`
- 锁文件 `uv.lock` 必须 commit, 保证可复现构建 (对 non-custodial 承重墙至关重要:
  外部审计员必须能确定运行的是哪份代码)

## 生产依赖 (declarative 在 pyproject.toml)

| 库 | 用途 | 备注 |
|----|------|------|
| `nats-py>=2.9` | NATS JetStream client | telemetry uplink + DeploymentSpec pull |
| `pydantic>=2.5` | Envelope schema + DeploymentSpec 数据模型 | wire contract 强类型 |
| `structlog>=24` | 结构化日志 (脱敏 / JSON output) | telemetry_actor 依赖 |
| `uuid6>=2024.1.12` | UUIDv7 for event ID | 事件时序单调 |

root optional `nautilus` extra 现在解析到独立
`custos-strategy-toolkit-nautilus==0.1.0` distribution；T4 完成前 host 仍消费 legacy
implementation。新 artifact chain 只接受独立 Nautilus distribution；base 审计安装不拉取 NT。

## 开发依赖

| 库 | 用途 |
|----|------|
| `pytest>=8` | 测试 runner |
| `pytest-asyncio>=0.24` | async 测试 (asyncio_mode=auto) |

## 过渡期可选依赖 (`nautilus` extra, 待 Plan 18 cutover 删除)

| 库 | 用途 | 备注 |
|----|------|------|
| `custos-strategy-toolkit-nautilus==0.1.0` | NT toolkit/host dependency boundary | 传递 exact `nautilus-trader==1.230.0`；Python 3.11 resolution fail closed |
| `pyyaml>=6` | 读 strategy config.yaml | nautilus 内使用 |

`nautilus` extra 在 extraction 完成前维持现有 runner 行为。Plan 18 使用 Python
>=3.11 的 base distribution 与 Python >=3.12,<3.13 的独立 Nautilus distribution；
后者必须在 Python 3.11 dependency resolution 阶段失败。

## 未来加入

- **pyright**: root runner 全仓类型检查仍待后续 plan 加

## 禁止引入

- **同步 HTTP 客户端** (`requests` / `urllib.request`): custos 是 async daemon,
  同步网络调用会阻塞 asyncio loop
- **明文密钥库** (`keyring` / 明文文件读 API key): credential_vault 只走 sops+age
  (见 `docs-site/docs/04-operator-guide/credential-vault.md`)
- **未审计的 NT MessageBus 序列化 sink**: telemetry_actor 是唯一出口
  (脱敏 + 白名单 + envelope schema)
- **cloud SDK** (aws-cli / gcloud / azure): non-custodial 承重墙 — custos daemon 只在
  用户本地跑, 不引 cloud provider SDK

## 类型检查

- **Python**: 两个 Plan 18 toolkit distributions 使用 strict `mypy`；root runner 全仓
  `pyright` 仍待未来集成
- **格式化**: `ruff format` (line-length 88, 详见 `code-style.md`)
- **Lint**: `ruff check` (默认规则集 + `E` `W` `F` `I` `B` `UP`)
