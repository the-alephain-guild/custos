# 常见错误 (custos)

---
paths:
  - "src/**/*.py"
  - "tests/**/*.py"
---

custos 单栈 Python daemon, 但涉及 NT lifecycle + async NATS + sops+age + Pydantic wire
contract 多个技术边界; 以下是历次开发中命中的典型陷阱, 修一次记一次.

## Python 通用

### uv 与 pip 混用

- **症状**: 本地 `pip install foo` 装了包, `uv sync` 后又不见了; CI 全绿本地跑不通
- **原因**: uv 管理独立 venv, pip 在系统 Python 装; 二者不共享
- **修**: 严格 `uv sync --extra dev` 或 `uv add foo`; 禁 `pip install`

### `pytest-asyncio` 未开 auto mode

- **症状**: async test 报 `no async fixture` 或 `coroutine was never awaited`
- **原因**: 默认 pytest-asyncio 需要 `@pytest.mark.asyncio`
- **修**: `pyproject.toml` 已配 `asyncio_mode = "auto"`, 若报错检查 pyproject.toml 未被覆盖

### `Decimal(0.1)` 精度丢失

- **症状**: `Decimal(0.1) == Decimal("0.1")` 是 False
- **原因**: `Decimal(float)` 保留 float 二进制精度 (0.1000000000000000055...)
- **修**: 一律 `Decimal(str(value))`, 参考 `code-style.md` §Money math

### stdlib `extra={...}` 在本仓配置下静默丢字段

- **症状**: 日志事件名出现在 stdout, 但所有结构化字段消失; 而断言字段的单测全绿
- **原因**: `core/log.py` 的 `configure()` 用
  `logging.basicConfig(format="%(message)s")`, 只渲染 message。stdlib 的
  `extra={...}` 挂在 LogRecord 属性上, `%(message)s` 不会输出它们。
  `caplog.records` 读的正是这些属性, 所以测试看得见、运维看不见。
- **实测**: `CredentialDecrypted` 审计事件曾只输出 `credential_decrypted` 一行,
  不含 credential_id / tenant_id / initiator —— 而红线要求"每次 decrypt 必发审计事件"
- **修**: 用 `from custos.core.log import get_logger` + kwargs
  (`_log.info("evt", credential_id=cid)`); 测试改用
  `structlog.testing.capture_logs()` 断言渲染后的事件字典
- **注意**: 有人曾为了让 caplog 测试继续通过而**刻意**保留 stdlib logging (源码注释留有
  记录)。测试通过不是保留错误实现的理由 —— 应该改测试去测真实输出路径

## NautilusTrader (NT) lifecycle

### `TradingNode` stop 后无法重启

- **症状**: `node.stop()` → `node.start()` 报 `StateError: node is not in state INITIALIZED`
- **原因**: NT 状态机: `STARTED` → `stop()` → `STOPPED`, 需 `dispose()` + `TradingNodeConfig`
  重新构造才能重启
- **修**: `nautilus_host.py` 用 `_recreate_node()` 而非直接重跑 `start()`
- **参考**: `docs-site/docs/07-engines/nautilus-trader.md`

### G6 gate 检查通不过 (`NoopHost` 上 live)

- **症状**: Plan 00c 之后, `LIVE_MODE=true` 时 `start()` 报 `G6DenyLive: NoopHost cannot execute live`
- **原因**: 红线 0.2, 设计上如此; `NoopHost` 只允许 paper/sim
- **修**: 生产用户先跑 Plan 00a `NtTradingNodeHost` 真实现, 否则保持 `paper_only=True`

### NT MessageBus 事件订阅漏事件

- **症状**: telemetry_actor 收到部分事件, 部分被 NT 内部 drop
- **原因**: NT MessageBus 用 handler 匹配 topic, 未匹配即 drop
- **修**: `telemetry_actor.py` 用宽 topic 匹配 (`msgbus.subscribe("orders.*", ...)`)
  + 白名单在 telemetry_actor 里过滤, 不依赖 NT 侧过滤

## async / NATS

### `nats-py` 客户端断线后不自动重连

- **症状**: 云端 arx 短暂重启后 custos 收不到 DeploymentSpec 变更
- **原因**: 若 `nats.connect()` 未配 `reconnect_time_wait` 或 `max_reconnect_attempts=-1`,
  只重试一次或不重试
- **修**: `nats_client.py` 显式配 `reconnect_time_wait=2, max_reconnect_attempts=-1`
- **参考**: `docs/authority/nats-transport-contract.md` §连接管理

### async 任务 exception silent drop

- **症状**: 后台 asyncio task 抛异常但主流程不知道
- **原因**: `asyncio.create_task()` 返回的 Task 若 exception 未被 await, 只在 GC 时输出
  warning, 主流程不 raise
- **修**: 用 `add_done_callback` 显式检查 exception, 或用 `asyncio.gather(return_exceptions=True)`
  + 显式处理; 违反 lesson #21 "对账不静默"

### `asyncio.gather` 一个失败全部取消

- **症状**: 5 个 telemetry 事件并发发送, 一个 timeout 其他 4 个都取消
- **原因**: gather 默认 `return_exceptions=False`, 首个 exception 触发 cancel 其他
- **修**: 若要独立处理, 用 `return_exceptions=True` + 遍历 result 分类

## sops + age

### `SOPS_AGE_KEY_FILE` 未设置

- **症状**: `sops --decrypt` 报 `no age key found`
- **原因**: sops 通过 env var 找 age key
- **修**: `credential_vault.py` 显式 `env={"SOPS_AGE_KEY_FILE": ~/.custos/vault/age.key}`;
  或 fallback 到 stdin

### age key 与 sops YAML 不匹配

- **症状**: `sops --decrypt` 报 `no key could decrypt`
- **原因**: sops encrypt 时使用的 recipient (age public key) 与解密用的 age private key 不匹配
- **修**: 用户 `age-keygen` 时输出的 public key 需 encrypt 时用 `--age <pub-key>`; 参考
  `docs-site/docs/04-operator-guide/credential-vault.md`

## Pydantic v2

### `.dict()` 变 `.model_dump()`

- **症状**: `envelope.dict()` 报 `AttributeError`
- **原因**: v2 API 迁移
- **修**: 一律 `.model_dump(mode="json")` 用于 wire 序列化 (自动处理 Decimal → str
  若配 `field_serializer`)

### `Config` 变 `ConfigDict`

- **症状**: `class Config: extra = "forbid"` 报 warning
- **修**: `model_config = ConfigDict(extra="forbid")`

### `parse_obj` 变 `model_validate`

- **修**: `DeploymentSpec.model_validate(dict_from_nats)`

## Wire contract

### schema 版本升级破坏 old consumer

- **症状**: v2 加了 `schema_version` 字段, arx 老版本 v1 消费不到 telemetry
- **原因**: envelope 结构变化未 dual-read
- **修**: v1→v2 加字段用 `Optional[...]` 默认 None; 消费者双 schema 都能 parse

### `str(Decimal)` 精度尾巴

- **症状**: `Decimal("100.00")` 序列化 wire 变 `"100.00"`, 消费方期望 `"100"`
- **原因**: str(Decimal) 保留 scale
- **修**: 契约明确: wire 是 str, scale 由消费方 quantize; 或统一 quantize 到 8 位小数

## Foundation Scan 陷阱

### grep 空命中 ≠ 不存在

- **症状**: `grep pattern src/` 空返回, 就认为不存在; 实际是 pattern 拼写错
- **修**: grep 前先跑一次已知命中的 sanity check (`grep 'import structlog' src/` 应命中)
- **参考**: 生态 lesson #9/#11 不信推理信实证
