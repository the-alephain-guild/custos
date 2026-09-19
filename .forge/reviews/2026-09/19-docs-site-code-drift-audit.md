# Custos docs-site 与代码一致性审计

## Summary

- 日期：2026-09-19。
- 基线：`c1d27024312210590bc0716fe1ddff592e5f8012`；开始审计时工作树干净。
- 结论：**docs-site 明显落后，需要同步内容并补齐操作路径；现有站点结构可以保留。**
- 范围：站点目录、导航、英文与中文对应关系全量盘点；重点核对 CLI、双通道、引擎、传输、健康检查、发布和验收。不是逐行代码审查，也不是生产验收。
- 盘点：`src/custos` 87 个 Python 文件，约 28,914 行；`packages` 250 个 Python 文件（含 vendor），约 33,725 行；`tests` 231 个 Python 文件，约 51,977 行。
- 站点：英文 45 篇、中文 45 篇，路径一一对应。英文 38 篇最后修改于 07-28，3 篇于 07-29，4 篇于 08-10；代码已推进到 09-19。日期只用于定位，同步需求由以下行为差异证明。

## Strengths

1. 十个主题分区完整，无须重建网站；45/45 篇均有中文对应文件。
2. signed lane 的 instance identity、持久化 outbox、Decimal、凭据隔离等核心说明仍有价值。
3. 本次 `npm run verify` 完整通过：disclosure 35/35、CJK 10/10、术语 12/12，各内容检查、英中文构建及 TypeScript 检查均通过。
4. 当前代码有独立 offline 模块、实际 parser、venue registry、schema 和 authority manifest，可作为文档同步的数据来源。

## Concerns

证据路径均相对于 Custos 仓库根。P1 表示会直接误导启动、运行限制或验收判断；P2 表示能力遗漏、参考资料或维护缺口。

### F01 / P1：首跑命令不会启动文档承诺的部署订阅

- 文档：`docs-site/docs/02-getting-started/first-sandbox-run.md:51` 仅运行 `start --enabled-mode sandbox --engine sandbox-sim`，`:86` 却说正在等待 signed desired-state command。
- 代码：`src/custos/cli/subcommands/start.py:100` 的 `--reconcile` 默认为 false；`src/custos/cli/_daemon.py:817` 只在该值为 true 时构建 host/coordinator 并订阅控制流。
- 本次直接调用实际 parser，确认原样命令得到 `reconcile=False`。
- 后果：身份和传输准备好后，进程可能 health 通过却不接部署；`health` 检查并不等价于某个策略实例 ready，见 `src/custos/core/readiness.py:143`。
- 建议：给 signed 教程补齐 reconcile、transport、domain key、capability、artifact trust 的前提和命令；分别验证进程健康、订阅建立、实例 applied/ready。

### F02 / P1：整条 offline 使用路径缺失，且被多处绝对化陈述否定

- 文档：`02-getting-started/enrollment.md:9` 宣称 enroll 是唯一身份入口；`04-operator-guide/deployment.md:35` 宣称不创建 stream，deployment 只有 validate；`10-release-governance/upgrade-paths.md:48` 又宣称没有离线校验命令。
- 代码：`src/custos/cli/subcommands/identity.py:42` 有 `identity standalone`；`deployment.py:36` 有 validate/publish；`nats.py:20` 有 `nats bootstrap --profile standalone`；`start.py:101` 有 `--reconcile-strategy-id`。
- `authority-manifest.json:406` 明确登记 offline lane；`src/custos/offline/spec.py:60` 为独立 `OfflineDeploymentSpec`，不是 canonical signed DeploymentSpec。
- 建议：入口先选择 signed / offline。新增完整本地 sandbox 教程和 testnet 差异说明：本地身份、NATS bootstrap、spec validate/publish、策略目录/hash、venue vault、状态订阅、停止与恢复。
- 必须写清：offline 指不依赖控制平面，仍可能连接 NATS/行情/testnet；只允许 sandbox/testnet，opt-in、不可晋升，不产生 canonical receipts。不能把此通道写成 signed/live 门的绕过方式。

### F03 / P1：模式数量和实际引擎并发限制描述失真

- 文档：`03-concepts/trading-modes.md:31` 与 `09-reference/cli.md:164` 说一进程只能一种 mode。
- 代码：`src/custos/cli/subcommands/start.py:62` 使用 `action="append"`，支持重复 `--enabled-mode`；`src/custos/cli/_daemon.py` 按 mode 创建独立订阅。实际 parser 接受 sandbox + testnet。
- 另一方面，Nautilus 2 的 `src/custos/engines/nautilus/host.py:529` 明确只允许同一 event loop 一个活动 node，第二个 deployment 被拒绝。这条操作限制站点未说明。
- 建议：区分 supervisor 的多模式 transport 能力、host 声明能力、单 node 并发限制和 live 的实际启用状态。不要从“多模式 parser”推导“同进程多个策略已经可并行”。

### F04 / P2：CLI 参考只覆盖 7/11 个顶层命令

- 文档：`docs-site/docs/09-reference/cli.md:13` 只有 7 个命令。
- 代码：`src/custos/cli/subcommands/__init__.py:53` 注册 11 个；遗漏 `deployment`、`identity`、`nats`、`release-policy`。
- `start` 的直接参数表也漏了 `--reconcile-strategy-id`、`--runner-label`、`--offline-state`、`--production-state-root`。
- 建议：从 parser 生成命令、参数、默认值、required/choices，并为每条关键操作补人工编写的前提和例子。7/11 = 63.6% 只表示 CLI 页顶层章节覆盖，不能外推成整个项目文档覆盖率。

### F05 / P1：Nautilus 版本、安装边界与场所矩阵落后

- 文档：`docs-site/docs/08-toolkit/overview.md:106` 固定 `nautilus-trader==1.230.0`；`07-engines/nautilus-trader.md:112` 和 `03-concepts/live-execution-gate.md` 只列 Binance。
- 代码：`packages/custos-strategy-toolkit-nautilus/pyproject.toml:8` 依赖 `2.0.0rc5+sodex.1`；根 `pyproject.toml` 的 uv sources 为 CPython 3.12 的 macOS arm64 / Linux arm64 / Linux x86_64 fork wheels。
- `src/custos/engines/nautilus/host.py:92` 的 sandbox/testnet 集合已有 `sodex`、`sodex_perpetual`；live 集合仍只有 Binance 两种 connector。
- 建议：更新精确版本和已配置 wheel 平台；区分 base Python 要求与 NT 实际 Python 3.12 约束；新增 connector × mode 矩阵及 SoDEX 配置/诊断说明。支持矩阵中的 live 声明不代表生产执行已启用。

### F06 / P1：交接、发布和 runtime 状态仍引用旧阶段

- 文档：`08-toolkit/overview.md:150` 称 handoff 为 false；`08-toolkit/artifact-signing.md:97` 称 daemon composition 尚未启用；`06-integration/contract-versioning.md:62` 称没有消费者收据；`10-release-governance/upgrade-paths.md:13` 称没有发布过 wheel/image。
- 当前 manifest 登记的 Nautilus 2 handoff 收据为 `CANONICAL_V1_CONSUMER_HANDOFF_COMPLETE`，`command_consumer_ready`、`contract_consumer_ready` 均 true，见 `docs/authority/receipts/custos-strategy-contract-nautilus-2-v1-handoff-receipt.json:43`。
- runtime 收据 `docs/authority/receipts/custos-strategy-artifact-runtime-v1-receipt.json:4` 已登记候选镜像发布、签名、daemon composition 和局部执行证据；rc7 authority 也已登记。
- 但这些收据的 `runtime_ready` / `production_ready` 仍为 false；`src/custos/cli/_daemon.py:880` 仍显式设置 `live_execution_enabled=False`。
- 建议：建立一处按 artifact/revision 标明的状态表，区分正式稳定版本、toolkit RC、runtime candidate、local composition、deployed acceptance、live/production。候选镜像存在不等于正式 0.3.0 stable artifact 已发行。
- 本审计读取仓库中登记的历史发布证据，未访问 registry，不声称远端镜像此刻可拉取，也不将旧镜像提升为当前 HEAD 的验收。

### F07 / P2：已完成的 typing closure 仍被描述为当前债务

- 文档：`08-toolkit/overview.md:122` 至 `:140` 把 75/289 个错误写为当前状态，把 extracted implementation 描述为只守历史 baseline。
- 代码：`Makefile:32` 已检查两包 whole-package strict typing；`docs/authority/receipts/strategy-toolkit-typing-closure-receipt-v1.json` 把 75/289 列为 historical，而 verified counts 为 0/0。
- 本次 `make toolkit-typecheck` 的两个 mypy 阶段分别报告 41 / 60 个文件无问题；closure 检查结果单列在下方验证记录。
- 建议：历史债务与当前质量门分开，保留第三方 vendor 的范围说明，避免公开错误衡量当前代码质量。

### F08 / P2：集成参考漏了独立策略信号和新增 schema

- `09-reference/json-schema.md:8` 称 11 个 schema；当前目录实际 14 个，遗漏 `offline_deployment_spec.schema.json`、`runtime_candidate_acceptance_v1.schema.json`、`runtime_candidate_promotion_receipt_v1.schema.json`。
- `src/custos/core/runner_fact.py:662` 有独立签名的 strategy signal envelope，`:705` 使用专属 subject；outbox 有独立 signal stream/sequence/PubAck。站点没有 strategy signal 的说明。
- 不应把它误算成 RunnerFactBatch 的第 14 个 kind；它是有意分开的 wire surface。
- 建议：增加信号的 subject、签名前像、scope、sequence、幂等及订阅示例；更新 schema 表，并说明哪些是公开消费契约、哪些只是本地工具或候选验收资料。

### F09 / P2：NATS 页面自身矛盾，产品命名抽象没有一致落地

- `09-reference/nats-subjects.md:28` 称 ARX 是唯一 desired-state publisher，同页 `:79` 又称 ARX 不发布、不 relay、也不接收 facts；overview 图则以 ARX 表示整个上游产品。
- `docs-site/scripts/check-disclosure.mjs:9` 明确要求对外把 ARX 呈现为一个产品，避免披露后端服务分工。这是刻意的产品抽象，不能把所有 ARX 字样简单认定为代码 ownership 错误。
- 建议：保持对外产品命名，统一“控制平面签发/接收”“身份授权服务不在消息热路径”的说明；准确保留操作必需的 CLI、header、subject 字面量及受审 exemption。

### F10 / P1：镜像验证范围被夸大，历史收据维护规则过时

- `02-getting-started/installation.md:60` 声称 `make verify-local-v030` 运行 standalone acceptance。
- `Makefile:146` 明确该 target 只运行 image contract；standalone deployment-wire acceptance 不在里面。
- `06-integration/contract-versioning.md:43` 把改动内部契约文件与重签既有历史 receipt 绑定，不符合当前根 AGENTS.md 对 evolving source/internal contract 以 Git/CI 治理的规则。
- 建议：明确每道门的覆盖范围；保留历史收据对原 revision 的证据，不因当前源码变化而刷新旧收据。需要新验收时新增新 revision 的证据。

### F11 / P2：运维资料缺少最新故障与安全恢复路径

- 现有 troubleshooting 主要覆盖注册、凭据和 signed admission，没有 offline safety 操作说明。
- `src/custos/offline/safety.py:81` 已消费 spec 中的 `max_total_notional` / `max_drawdown_pct`，按 deployment 生效；`:143` 说明 trip 锁存后 flatten + stop，并拒绝后续 generation。
- `src/custos/engines/nautilus/host.py:1315` 的 engine readiness 包含可靠估值；`portfolio_snapshot.py:149` 起有带 instrument 的 `portfolio_prices_missing:*` 等故障原因。
- 建议：区分 daemon health、engine ready、valuation reliable；补 startup 等待与超时、缺价格/权益、credential scope 冲突、trip 后核对持仓/订单和恢复步骤。解释 signed runner aggregate cap 与 offline 每 deployment 限额的区别。
- 两个 live gate 页面仍引用不存在的 `tests/test_nt_binance_venue.py`；应映射到当前 `tests/test_nt_venue_wiring.py` 及 SoDEX 测试。

### F12 / P2：现有 CI 能保证网站能构建，不能保证内容跟代码一致

- `.github/workflows/docs-deploy.yml:3` 只由 main push 的 docs-site/workflow 路径和手动触发；注释说有 PR build，但实际上没有 `pull_request` trigger。
- 文案门检查 disclosure/CJK/术语，未比对 parser、版本、schema、venue matrix 和源码路径，因此本次所有站点检查绿灯时仍存在上述漂移。
- 建议：增加独立 PR 文档校验 job，覆盖相关源码/manifest 变动；deploy 仍只 main。自动检查可枚举事实，人工审阅信任边界和操作流程。

## Suggestions

1. **先修会使操作者走错路径的内容**：首跑 `--reconcile`、signed/offline 边界、mode/node 限制、引擎版本、当前能力状态与验收范围。
2. **补三条完整操作指南**：standalone sandbox；offline testnet（含 SoDEX）；signed 部署及 artifact trust provisioning。路径应从空环境到可观测结果，并写清停止和恢复。
3. **再更新参考资料和中英文**：CLI、schema、signals、venue 矩阵与版本信息同步维护。中文文件齐全不代表内容跟上代码；例如旧 1.230.0 和 75/289 两个事实在双语中均存在。
4. **收敛可变化事实的数据源**：版本从 package metadata、CLI 从 parser、schema 从目录/manifest、venue 从 registry 派生；状态说明引用对应 revision 和证据范围。
5. 后续同步不改运行时代码、不降低 disclosure 门、不重写历史收据，不把整套内部 authority 文档搬上公开站点。

## Verification

- `npm run verify`：通过，包含英中文 production build、TypeScript 及三类站点检查。
- `make check-authority`：通过；包含 historical extraction、typing closure 与 standalone authority gate。
- `make toolkit-typecheck`：通过，base 41 个文件、Nautilus 60 个文件 strict mypy 无问题，closure 为 `READY_TYPING_CLOSURE`。
- 相关 focused pytest：96 passed，覆盖 CLI deployment、offline mode guard、standalone identity、venue wiring、NT wheel sources、strategy signal contract。
- 实际 parser 抽查：首跑命令 `reconcile=False`；重复 `--enabled-mode` 被接受。
- 全量 Python 测试、Docker 构建、真实 NATS/testnet/交易所、线上网站和远端发布可用性：未验证。
- 未计算所有 public 类型/函数的文档覆盖率；对这类操作站点，该比率容易失真。可复核的窄指标为 CLI 页 7/11、schema 表 11/14、中文文件覆盖 45/45。

## Risk Assessment

**文档操作风险：高；网站构建风险：低；生产可用性：本审计不作认证。** 主要风险是用户照文档启动了健康但不接部署的进程、看不到可用的 standalone 路径，以及把旧状态或有限验证当成当前完整验收。现有结构与基础概念可保留，宜按后续同步计划修改具体页面。
