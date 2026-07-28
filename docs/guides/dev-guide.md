# Dev Guide — 开发上手

> custos 独立仓库开发上手指南. 首次 clone 到跑通 `make verify` 再到发起首个 PR.

## 先决条件

- **Python >=3.11**
- **`uv`** (Rust-based Python 包管理器, 快, 无 pip / poetry)
- **git**
- (可选) **sops** + **age** — 需要 vault 相关开发时
- (可选) **NATS server** — 需要跑集成时

## 首次上手

### 1. Clone + 装依赖

```bash
git clone https://github.com/the-alephain-guild/custos.git
cd custos
uv sync --extra dev
```

### 2. 跑测试确认环境

```bash
make verify
```

预期输出: `check` (fmt-check + lint) 通过 + `test-baseline` 115 pass + `✅ make verify passed`.

如果 `make verify` fail, 参考 `common-errors.md` (uv/pip 混用 / pytest-asyncio auto mode
未开等).

### 3. 熟悉项目布局

```
src/custos/               ← Python 模块源码 (core/ + engines/ + cli/)
tests/                    ← pytest 测试
docs/domain.md            ← 顶层纸面 spec (必读)
docs/design/              ← 6 module + 4 骨架
docs/ops/                 ← 部署 + runbook
CLAUDE.md                 ← 项目导航图 (AI 助手入口)
.claude/rules/            ← 独立规则集
.forge/plans/             ← 实施 plan
Makefile                  ← 标准化 target
```

从 [`../design/00-overview.md`](../design/00-overview.md) 开始, 然后 domain.md, 然后
按需读 6 模块设计文档.

## 日常开发循环

### 装新依赖

```bash
uv add <package>              # 生产依赖
uv add --extra dev <package>  # 开发依赖
```

`uv.lock` 会自动更新, 必须 commit.

### 改代码 → 测试

```bash
# 改代码 (例: src/custos/core/runner_command_runtime.py)
$EDITOR src/custos/core/runner_command_runtime.py

# 跑相关测试
uv run pytest tests/test_runner_command_runtime.py -v

# 全量 verify
make verify
```

### 格式化 (自动)

- Claude Code hooks: `.claude/settings.json` 已配 PostToolUse Write|Edit → `ruff format`
- 手动: `make fmt`

### Lint 修

```bash
make lint                                    # 显示问题
uv run ruff check --fix src/ tests/ scripts/  # 自动修 (34 fix pattern)
```

## 首次 PR 检查清单

- [ ] `make verify` 全绿 (check + test-baseline 115 pass)
- [ ] 改动符合 `.claude/rules/code-style.md` (ruff auto-fmt 自动兜底)
- [ ] Non-Custodial 4 红线未违反 (`.claude/rules/verification.md` §红线专项 grep)
  - Key / KEK 未 log / publish / send raw
  - 未新增 cloud SDK 依赖
  - money math 未混入 float
  - engine execution admission 未绕过
- [ ] 若改 wire contract (envelope / subject), 同步更新:
  - `test_nats_envelope.py` / `test_subject_builder_contract.py`
  - `docs/design/nats_client.md` §schema versioning
  - `scripts/generate_wire_fixtures.py` 重跑
- [ ] 若改 6 模块, 同步更新对应 `docs/design/<module>.md`
- [ ] 若改 domain 概念 (Deployment / Runner / Enrollment), 同步 `docs/domain.md`
- [ ] Commit message 遵循 Conventional Commits, scope 为 `custos`
- [ ] `git status --short` 核对 staged 范围, 无 pre-staged 污染 (lesson #27)

## Forge 工作流 (推荐)

对非平凡改动 (>50 行 / 涉及新模块 / 跨 module 契约变更), 走 forge plan-driven 开发:

```bash
/forge:plan  # 起草计划文件 → .forge/plans/YYYY-MM/NN-<slug>.md
# review + commit 计划
/forge:execute .forge/plans/YYYY-MM/NN-<slug>.md
```

详见 [`.forge/README.md`](../../.forge/README.md).

## Non-Custodial 4 红线速查

修任何触及以下场景的代码前先读 `.claude/rules/mandatory-rules.md` §0:

| 场景 | 红线 |
|------|------|
| 加 log / publish / send 字段 | 0.1 Key / KEK 不出进程 |
| 修改 `nautilus_host.py` 或 CEX adapter | 0.2 verified artifact + live admission 不绕过 |
| 修改 `reconcile.py` 中云端断线路径 | 0.3 失联 ≠ 停止 |
| 修改价格 / 金额 / notional 计算 | 0.4 Decimal + wire str |

红线的紧急预案是**降级到 paper**, 不是绕过红线.

## 常见开发问题

见 [`../../.claude/rules/common-errors.md`](../../.claude/rules/common-errors.md):

- uv / pip 混用导致本地绿 CI 红
- `pytest-asyncio` auto mode 未开
- `Decimal(0.1)` 精度丢失
- NT `TradingNode` stop 后无法重启 (`_recreate_node()`)
- `nats-py` 客户端断线不自动重连
- async task 异常 silent drop (lesson #21 零静默)
- sops `SOPS_AGE_KEY_FILE` 未设

## 独立仓库自足纪律

custos 是**独立开源仓库** — 你的贡献会被外部审计员单仓 clone 查:

- 规则集 / 权威文档 / 验证入口全在本仓库内, 不引 workspace root
- 敏感字段脱敏 (log / envelope / status report)
- `uv.lock` 必 commit (可复现构建 = non-custodial 承重墙一部分)
- 测试 fixture 用 `mktemp` 不碰 `~/.custos/`

## 参考

- 顶层 CLAUDE.md 导航图: [`../../CLAUDE.md`](../../CLAUDE.md)
- 权威文档路径: [`../../.claude/rules/authority-docs.md`](../../.claude/rules/authority-docs.md)
- 测试策略: [`04-testing.md`](04-testing.md)
- 部署方式: [`../ops/05-deployment.md`](../ops/05-deployment.md)
