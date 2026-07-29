# 审计报告: Plan 21 — 非 live 离线部署通道

> **审计日期**: 2026-07-29
> **计划文件**: `.forge/plans/2026-07/21-sandbox-offline-deployment-path.md`
> **审计员**: Claude Code（自审；按 lesson C2，每条 finding 附 grep/file:line 实证）

## Executive Summary

功能面与门禁面站得住：8 个 Task 全部落地，`make lint` / `make check-authority` 绿，
`make test` 768 passed / 0 failed，权威层例外有机械门（`verify_offline_lane`）且有两条
relaxed-double 证明门会咬人。

但审计出 **4 条 CRITICAL**，全部属同一类：**实施偏离了计划却没写进偏离日志**。其中一条
是 close-out 里的测试数字对不上真实计数——lesson #25 在本 plan 自己的 close-out 上复发。
另有 2 条 HIGH，都集中在"最少被验证的那一块"：真正让通道跑起来的 NT 策略加载零覆盖，
以及失败的 apply 被 ack 掉因而无法靠重投重试。

## 整体匹配率: 88%

## 严重度分布

| 严重度 | 数量 |
|--------|------|
| 🔴 CRITICAL | 4 |
| 🟠 HIGH | 2 |
| 🟡 MEDIUM | 2 |
| 🔵 LOW | 2 |

## 问题列表

### 🔴 CRITICAL

#### C1: 组合位置整体挪出 `_daemon.py`，未登记偏离
- **文件**: `src/custos/offline/daemon.py`（新建 149 行）、`src/custos/cli/subcommands/start.py:179-224`
- **计划定义**: Task 7 —「`src/custos/cli/_daemon.py` composes the offline reconciler as
  one more supervised long-running task」
- **实际代码**: `_daemon.py` **一行未改**（`git diff --stat 34f7307..HEAD -- src/custos/cli/_daemon.py`
  为空）。组合落在新建的 `custos/offline/daemon.py`，由 `start.py` 分叉调用
  `run_offline_lane`。
- **影响**: 决定本身更好——签名 daemon 会 `verify_active()` 打控制面、加载 transport
  authorities，离线场景两者都不存在，塞进去只能靠桩掉这些检查，而那正是签名通道的价值。
  但它改变了 plan 承诺的架构位置，且改变了"opt-in 组合"的含义（不是多挂一个 task，是
  另一条 composition），偏离日志 5 条里没有任何一条提到。未登记的偏离一律 CRITICAL。

#### C2: 新增持久化状态存储，计划从未提及
- **文件**: `src/custos/offline/state.py`（新建，64 行）
- **计划定义**: 无。Task 6/7 都没有持久层。
- **实际代码**: `OfflineAppliedStore` — SQLite 记录 applied generation，由
  `OfflineReconciler.__init__` 载入、`_remember` 写回。
- **影响**: 引入动机是真实的（readiness 契约要求 `sqlite_quick_check`，对不存在的库谎报
  "ok" 是假声明；且没有它重启会重复部署已 applied 的 generation），但这是一个计划外的
  **功能**，不是实现细节。它带来新的磁盘状态、新的失效模式（库损坏时的行为未定义），
  却没有走偏离协议。

#### C3: close-out 的测试数字与实际不符（lesson #25 本 plan 内复发）
- **文件**: `.forge/plans/2026-07/21-sandbox-offline-deployment-path.md:434`
- **计划定义**: close-out 声称「新增 91 个测试，分布：mode guard 22、离线契约 23、
  CLI 15、bootstrap 17、reconcile 17（含组合 10）、面存在性 4」
- **实际代码**（逐文件实跑计数）: authority 8 / mode guard **23** / 离线契约 23 / CLI 15 /
  bootstrap 17 / reconcile 17 / daemon 10 / 面存在性 4 = **117**
- **影响**: 三处错：总数 91 与真实 117 不符；自列分项之和是 108，与自报总数 91 也不自洽；
  **8 个权威门测试整组漏计**。数字未经计数即写下，正是 lesson #25「agent 在 close-out
  里编造数字」的形态，且发生在一份声称按 lesson #40 诚实降级的 close-out 里。

#### C4: 函数名与计划不符，未登记
- **文件**: `src/custos/offline/transport.py:89`
- **计划定义**: Task 5 —「stream configs plus `bootstrap_standalone_nats`」
- **实际代码**: `async def bootstrap_standalone_streams(`
- **影响**: 影响本身极小（内部符号，无外部消费者），但规则不因影响小而例外；且计划文本
  至今仍指向一个不存在的符号，下一个读计划的人会 grep 不到。

### 🟠 HIGH

#### H1: 失败的 apply 被 ack，重投无法重试
- **文件**: `src/custos/offline/reconciler.py:144-147`
- **实际代码**: `await self.handle(message.data)` 之后无条件 `await acknowledge()`，
  不看 `handle` 的返回值。
- **影响**: `handle` 返回 False（引擎拒绝、模式不支持、apply 抛异常）时消息照样被确认，
  JetStream 不会重投。`test_a_failed_apply_can_be_retried_by_the_same_generation` 证明的是
  「同一 generation 再次到达时会重试」——但没有任何东西会让它再次到达。这是 lesson #28
  的形态：分支存在、测试存在，实际不可达。消费端表现为 `wait-status` 超时而非自愈。

#### H2: 真正加载策略的那段零覆盖，且 env seam 用法有两处静默失效
- **文件**: `src/custos/offline/daemon.py:60`、`packages/.../adapter/registry.py:70-72`
- **实际代码**: `os.environ.setdefault("STRATEGY_INJECT_PATH", str(self._strategy_path))`
  之后再 import registry；而 registry 在 **模块 import 时** 就读掉了该变量
  (`_inject_path = os.environ.get(...)` → `DISCOVERY_PATHS.insert`)。
- **影响**: 两个静默错误路径——(a) registry 若已被先前的 import 拉起，env 设置完全无效，
  发现路径退回内置默认；(b) `setdefault` 使第二个 `strategy_path` 不同的 spec 被静默忽略，
  加载到上一个策略目录。`grep BindMountedStrategy tests/` **零命中**，整段无测试。
  这恰是全通道最关键、也最未验证的一环——close-out 遗留项 3 已诚实标注，但严重度被低估。

### 🟡 MEDIUM

#### M1: 计划 Verification 六个复选框全未勾，Status 却是 ✅ Completed
- **文件**: `.forge/plans/2026-07/21-sandbox-offline-deployment-path.md` §Verification（6 处 `- [ ]`）
- **影响**: 真实记录在 close-out 的证据表里（其中两项明确标"未跑"），但同一份文档的两处
  互相矛盾。读者先看到的是复选框。

#### M2: 面存在性断言依赖 argparse 私有内部
- **文件**: `tests/test_gateway_contract_v1_samples.py:53-70`
- **实际代码**: `_build_parser()._actions`、`argparse._SubParsersAction`
- **影响**: 从 parser 推导的方向是对的（C7 要求），但绑在 CPython 私有属性上；argparse
  内部一变，这道保护会以看不懂的方式碎掉，而它保护的正是"不要静默丢面"。

### 🔵 LOW

#### L1: 无必要的 import 别名
- **文件**: `src/custos/offline/spec.py:32` — `model_validator as pydantic_model_validator`
- **影响**: 无命名冲突，别名纯属噪声。

#### L2: `AppliedStore` Protocol 用 `Any` 而非具体记录类型
- **文件**: `src/custos/offline/reconciler.py` `AppliedStore.load/save`
- **影响**: 唯一实现返回 `AppliedRecord`，Protocol 却声明 `dict[str, Any]` / `Any`，
  把已有的类型信息丢掉了。

## 正向偏离（改进）

| # | 位置 | 描述 | 理由 |
|---|---|---|---|
| I1 | `.github/workflows/scripts/verify-release.sh` | 发布门补探两个新命令 | C7 的 parser 推导门主动报红，按其设计意图修复而非绕过 |
| I2 | `tests/test_examples_cli_commands_are_real.py` | 两处负对照换成 CLI 不会长出的名字 | 原对照（`deployment` / `--nats-url`）被本 plan 变成真实存在，失去对照资格 |
| I3 | `docs/gateway-contract/v1/README.md` §The offline lane | 公开契约目录里的新资产补上说明 | 无说明的 schema 对审计员是papercut，且可能被误读为 canonical |
| I4 | `.claude/rules/historical-lessons.md` C8 | 记录"删代码连测试一起删=没有警报"+ CEO override 四件套 | 本 plan 的根因值得固化 |
| I5 | `src/custos/offline/state.py` + readiness | 用真实 sqlite 检查代替谎报 "ok" | 与 C2 同一处改动的正面：宁可加真存储，不做假声明 |

## 逐 Task 匹配率

| Task | 匹配率 | 关键偏离 |
|---|---|---|
| T1 权威层修订 | 100% | — |
| T2 mode guard | 100% | 后续为 CLI 增加 `command_mode=None` 语义（已在实现中说明） |
| T3 离线 spec 契约 | 100% | 基线更正已登记 |
| T4 publish/validate | 95% | digest「缺则填、有则校」是计划未明说的合并语义 |
| T5 nats bootstrap | 90% | C4 函数改名未登记 |
| T6 reconcile 环 | 90% | H1 ack 语义 |
| T7 start 接线 | 60% | C1 组合位置、C2 新增持久层 |
| T8 钉住面 | 95% | M2 私有内部 |

## 优先修复建议

1. **C1–C4 立即补登记 / 修正**：偏离日志补 3 条（组合位置、持久层、函数名），close-out
   数字按实测改为 117 并补上权威门那 8 个。数字类声明必须先计数再落笔。
2. **H1 修 ack 语义**：`handle` 返回 False 时不 ack，让重投成为真实重试路径；或显式声明
   "不重试，由操作者重发"并删掉那条会误导人的测试。
3. **H2 给 `BindMountedStrategy` 加测试**，并把 `setdefault` 改为显式设置 + 在 registry
   已导入时 fail fast 而非静默降级。
4. M1 勾选/改写 Verification 段，与 close-out 对齐。
5. M2 换成公开 API（`parser.parse_args` 探测或 `--help` 输出解析），L1/L2 顺手清。
