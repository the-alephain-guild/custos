# 代码风格 (custos)

---
paths:
  - "src/**/*.py"
  - "tests/**/*.py"
  - "scripts/**/*.py"
---

Python 单栈项目, ruff 承担格式化 + lint 双职. custos 特化风格约束以 non-custodial
承重墙为出发点 (脱敏日志 / 英文字段 / async 优先 / Decimal money math).

## 格式化 (ruff format)

- `line-length = 100` (与生态其他 Python 子系统 synedrion 一致; custos 现有代码
  也在此范围, 未来独立 style plan 决定是否收紧到 88)
- `indent-width = 4`
- 字符串默认双引号 (ruff 默认)
- 保留 trailing comma (function call / list literal / dict literal 多行)

## Lint (ruff check)

启用规则集:

- `E` — pycodestyle 错误
- `W` — pycodestyle 警告
- `F` — pyflakes (未使用 import / 变量)
- `I` — isort (import 排序)
- `B` — flake8-bugbear (常见 bug)
- `UP` — pyupgrade (Python 3.11 语法)

## 命名约定

- 模块: `snake_case` (对应 6 module: `enrollment.py` / `reconcile.py` / etc)
- 类: `PascalCase` (`DeploymentSpec` / `EnrollmentToken` / `CredentialVault`)
- 函数 / 变量: `snake_case`
- 常量: `UPPER_SNAKE`
- Pydantic 模型: `PascalCase`, 字段 `snake_case` (wire 协议保持 snake_case)

## 日志规范 (custos 特化)

- **统一用 `custos.core.log.get_logger`**, 禁用裸 `print()` 和 `logging.getLogger()`
  ```python
  from custos.core.log import get_logger
  _log = get_logger("custos.enrollment")
  _log.info("enrollment_completed", runner_id=runner_id, tenant_id=tenant_id)
  ```
- **字段用 kwargs, 禁 `extra={...}`**. 这不是风格偏好: `configure()` 里
  `logging.basicConfig(format="%(message)s")` **不渲染** stdlib record 的 `extra`
  属性, 所以 `logging.getLogger(...).info("evt", extra={...})` 在生产中只输出一个
  事件名, 字段全部丢失。structlog 的 kwargs 才会进 JSON。
- **字段名**: 全英文 snake_case (非中文, 便于生态其他系统消费)
- **事件名**: 动词过去时 / 状态名词, 如 `credential_decrypted` / `sops_decrypt_failed` /
  `runner_command_processed` / `credential_scope_violation`
- **禁字段**: `api_key`, `secret`, `password`, `token` (原文), `age_key`, `kek`,
  `venue_credentials` (原文) — 红线 0.1
- 若必须记录 key 相关事件, 用哈希 / prefix: `log.info("key_loaded", key_hash=sha256_first8(key))`
- **测试断言日志必须读渲染后的事件**, 用 `structlog.testing.capture_logs()`;
  **禁**用 `caplog.records` 断言 record 属性 —— 属性存在不等于输出里有, 这正是上面那条
  丢字段缺陷能长期全绿的原因。
- **例外 (不可修)**: `cli/_daemon.py` 与 `core/runner_command_runtime.py` 仍用 stdlib
  logging 且共 11 处 `extra={...}` 丢字段。二者被 `docs/authority/**` 资产索引按字节
  pin 住, 改动会破坏证据链 —— 见 `historical-lessons.md` C6 与 `.forge/README.md`
  §后续 plan 规划。**不要顺手修它们**, 必须与 receipt 重新签发同批进行。

## async 规范

- 主流程走 asyncio (nats-py / structlog 均 async 友好)
- 阻塞调用 (文件 I/O / subprocess) 用 `asyncio.to_thread`
- **禁**: `requests.get()` / `time.sleep()` (asyncio loop 里阻塞)
- 允许: `asyncio.sleep()` / `aiofiles` (若加依赖) / async subprocess

## Money math 规范 (红线 0.4)

- 所有价格 / 数量 / notional 用 `decimal.Decimal`
- 从字符串 / API 响应构造: `Decimal(str(raw_value))` (禁 `Decimal(float_value)`)
- 序列化 wire: `str(decimal_value)` (禁 `float(decimal_value)`)
- Pydantic 字段: `Decimal` type + `json_encoders={Decimal: str}` 或用 v2 的
  `field_serializer`
- 参考: `test_telemetry_money_contract.py`

## Pydantic 惯例

- Pydantic v2 (`>=2.5`)
- Envelope / DeploymentSpec / DeploymentStatus 用 `pydantic.BaseModel`
- 字段类型注解显式 (禁裸 `dict` / `list`)
- 用 `ConfigDict(extra="forbid")` 拒绝未声明字段 (wire 契约防御)

## 测试风格

- pytest fixture: `snake_case`, 前缀语义 (`vault_with_test_key` / `nats_mock_connected`)
- 参数化: `@pytest.mark.parametrize` 优于循环 assert
- async 测试: 依赖 `asyncio_mode=auto` (pyproject.toml 已配), 无需 `@pytest.mark.asyncio`
- 失败模式测试单独文件: `test_<module>_failure_modes.py`
- Money contract test: `test_*_money_contract.py`
- Wire contract test: `test_wire_*.py` (跨语言 fixture)

## 禁止的模式

- **裸 `except:`** (至少 `except Exception:` + 显式 log 或 re-raise)
- **`except: pass`** silent drop (违反生态 lesson #21 "对账不静默")
- **循环里同步 sleep** (阻塞 asyncio loop)
- **magic number in trading path** (用命名常量 + 注释来源)
- **module 级 side effect** (禁模块 import 时读 vault / 连 NATS)

## 注释规范

- 默认不写注释, 命名清楚即可
- 需写注释的场景:
  - 红线约束 (`# non-custodial 红线 0.1: 禁 log raw key`)
  - 非直觉 workaround (`# NT lifecycle 需 stop 后 wait_for=STOPPED 才能重启`)
  - Wire contract 版本约束 (`# schema v2 起加此字段; v1 消费者需 dual-read`)
- **禁**: 编号追踪引用 (Plan 00a / task N / lesson #M 出现在源码注释, 生态 lesson #15)

## 语言约束 (代码工件必须英文)

对齐 `CLAUDE.md` §Language Policy (Code Artifacts) — RED LINE 与生态根仓库 `.claude/rules/code-style.md` §6, 本规则强制:

- **必须英文** (source 与 runtime artifacts):
  - 注释 (行内 `#` + docstring `"""..."""`)
  - 日志描述文本 (`log.info("<event_name>", ...)` 的事件名 + message 均英文 snake_case)
  - 异常/错误消息 (`raise ValueError("...")` 面向开发者/服务器日志的文本)
  - 标识符 (变量/函数/类型/文件名/表列名)
  - Commit message (Conventional Commits, scope + subject 英文)
  - Wire contract 字段名 (延续既有"英文字段"约束, 供 arx 等下游生态消费)
- **允许中文** (仅这些):
  - 面向中文终端用户的 UI/CLI 文案 (custos v1 非 UI 项目, 事实上不适用)
  - 计划文档 (若未来引入 `.planning/` 目录)
  - AI ↔ user 会话 (全局规则不变)
- **强制机制**: 由 `scripts/check-code-english.py` 在 pre-commit hook 中扫描 staged diff **新增行**并阻断含 CJK 字符的 commit (只挡新增, 不扫存量; "触碰即改"策略避免大 diff)。首次 clone 后须运行 `bash scripts/install-hooks.sh` 挂载 hook (幂等)。豁免: 极少数确需 CJK 的行末加 `# noqa: language` 并注明理由。
- **既有中文注释/日志**: 触碰到即改, 不做大规模一次性重写 (与其他 finishing pass 项一致, 避免 diff 噪声)。
- **动机**: 部署服务器不支持中文显示 (中文日志会乱码), non-custodial 独立开源仓库外部审计员默认英文可读, arx / 生态其他子系统消费 custos 日志字段需一致 snake_case (custos 侧已明文要求"英文字段", 本规则把范围扩到注释与人读文本层)。
