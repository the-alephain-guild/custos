# 审计报告: Plan 24 — Take ownership of the toolkit's test suite

> **审计日期**: 2026-07-30
> **计划文件**: `.forge/plans/2026-07/24-adopt-the-toolkit-test-suite.md`
> **审计范围**: `04293b0`(plan 落盘)..`5f84178`
> **审计员**: Claude Code(内部自审 —— 不构成独立第二意见)

## Executive Summary

计划的目标达成且可证伪:toolkit 的覆盖现在住在拥有它的仓库里,并且**改坏 toolkit
会让本仓变红**——四次刻意变异四次变红,每次以 `git diff -- packages/` 为空确认字节级
还原。计划的 Non-goal「不改 toolkit 行为」也守住了:`git diff 1221103~1..HEAD --
packages/ src/` 为空。

审计发现的问题集中在**同一处**:执行过程中出现一次真实的代码损坏事件(用正则批量改写
源码),已恢复且有对照证据,但它暴露的方法论缺口值得单独固化。除此之外没有 CRITICAL。

一句必须说清的话:**本报告是自审**。写实现、写断言、写 close-out、做审计是同一个人。
本仓 lesson C2 记的正是「输出污染可贯穿 review 与 self-review」,而 lesson #40 的 custos
dogfood #2 明说红线表「有表不等于表里的话被验过」——那次是审查抓的,自评过不了那关。
所以下面每条结论都附实证锚点,而不是结论本身。

## 整体匹配率: 96%

扣分只在 scope 数字(计划写 83/18/42,实测 92/8/41/23)与 Task 4 措辞(计划说「默认
验证」,实际是两道门),两者都已在 plan 内更正并留痕。

## 严重度分布

| 严重度 | 数量 |
|--------|------|
| 🔴 CRITICAL | 0 |
| 🟠 HIGH | 1 |
| 🟡 MEDIUM | 3 |
| 🔵 LOW | 2 |

## 问题列表

### 🟠 HIGH

#### H1: 执行中用正则批量改写源码,造成静默的语义损坏

- **位置**: 过程性;已恢复。证据见 `DEV-24-REGEX-CORRUPTED-THE-PORT`。
- **发生了什么**: 剥内部追踪号时对**裸行**跑正则,其中 `\(\s*\)` 把 `f.is_ready()`
  改成属性访问,`\s+\)` 压掉缩进的收尾括号。9 个文件语法错(可见),更多文件是**能解析
  但语义已变**(不可见)。
- **为什么算 HIGH 而不是过程噪声**: 如果我没在 pytest 前先看 diff,静默那半会被
  「1308 passed」盖住——被改的正是断言辅助调用,而测试仍然「通过」。这与本仓 C7
  「硬编码矩阵 + 同期陈旧产物 = 自洽的假绿」同族:绿色来自两个同源错误互相印证。
- **恢复证据**: 已提交的 24 文件用 `git show HEAD:<path>` 写回;未提交的 61 文件从 PS
  重取并按序重放,重放脚本每步先断言再改。恢复后 `1308 passed / 2 skipped`,与损坏前
  逐条一致。
- **已落地的防护**: 追踪号改为逐条手写,`apply_exact.py` 要求**整行唯一匹配** +
  写前 `ast.parse`。中途我还试过用 tokenize 限定「只碰注释与 docstring 行」,**仍然
  出错**——带行尾注释的**代码行**整行合格,于是 `assert_called_once()` 又被改坏一次。
- **建议固化**: 这条教训的形状不是「正则要小心」,而是「**对源码做批量文本改写时,
  作用域必须是语法结构而不是行**;而且散文修复(删引用后把句子接回去)不可机械化——
  机械版留下了 `deleted in .` 这类断句」。建议进 `historical-lessons.md`(见下)。

### 🟡 MEDIUM

#### M1: 七处 layering 断言曾是空转,靠人工发现而非门禁

- **位置**: 五个 `test_nautilus_filter_*.py` + `test_snapshot_subsystem_removed.py:20,24`
- **实证**: 断言查字面量 `shared.filters` / `shared.nautilus.snapshot`,而
  `find_spec('shared')` 为 `None` ——那个包在本仓完全不存在,守卫因此永不失败。
- **已修**: 重指到 `custos_toolkit.filters` 与 `custos_toolkit_nautilus.adapter.*`,并
  证明会咬(注入违规导入 → 守卫变红)。
- **残留风险**: 没有任何机械探针会发现「下一个改名让某条断言变成空转」。这类
  「断言里的包名字面量」在 rename 后必然漂移,而 rename 本身是 lesson #35 的题目。

#### M2: 两个文件在本仓一条都不跑,是靠 collect 计数发现的

- **位置**: `test_nautilus_filter_adx.py`(卡 `importorskip("pandas_ta")`)、
  `test_msgbus_stream_e2e.py`(需 `redis` + 6380 活服务)
- **实证**: `pytest --collect-only` 对这两个文件返回 0 条,而 `pytest` 报告 `1308 passed`。
- **已处理**: adx 指向 vendored `pandas_ta`(`_pandas_ta.py:7` 证实运行时用的就是它);
  msgbus 退回 PS(custos 无 redis 依赖,走 NATS)。
- **教训**: 「全绿」不含「有没有在跑」。46 条断言差一步就永久沉默在本仓。

#### M3: 计数探针原先无法同时活在两个 profile 下

- **位置**: `tests/test_plan_closeout_counts.py`
- **实证**: base profile 下 49 个适配层文件 collect 为 0,与 close-out 声明的条数冲突,
  会让 `make verify` 变红。
- **已改**: 被 collect 期跳过的文件不再判错,而是**点名报告为本 profile 未核验**;豁免
  只给真 skip,由新探针证伪(在 `tests/` 下写一个真会 skip 的文件跑一遍,同时断言一个
  真 collect 了的文件不被豁免)。
- **注意**: 这是把探针的前提从「一个文件的条数是固定值」放宽到「取决于 profile」。放宽
  必然削弱一点,所以选择了**点名**而不是静默豁免——green 的运行仍然看得见哪些没核。

### 🔵 LOW

#### L1: 「不得抛异常」型测试若实现退化为 no-op 仍会通过

- **位置**: 5 处(`test_decimal_precision.py:270`、`test_event_publisher.py:526`、
  `test_nautilus_filter_manager.py:234`、`test_sltp_mode.py:175`、
  `test_startup_validator.py:112`)
- **判断**: 都是合法的「异常即断言」形态,不是空洞断言(我第一遍因读截断误判过一条,
  读全后撤回)。但它们对「方法被改成 no-op」不敏感。
- **不修的理由**: 计划 Non-goal 明说不重写;这是 PS 侧既有设计,改它属另一件事。

#### L2: scope 数字与 Task 4 措辞在计划落盘时不准

- 已在 plan 内更正并留 `DEV-24-CLASSIFICATION-DRIFT`,close-out 用两道门表述替代
  「默认验证」。属计划质量反馈,不是实现缺陷。

## 正向偏离(改进)

| # | 位置 | 描述 |
|---|---|---|
| I1 | close-out | 发现 `docs/authority/strategy-toolkit-*.json` 的 `target_sha256` 哈希的是**历史 git blob**(`check-toolkit-extraction.py:158-161`),不是工作区。实测变异 toolkit 源码后该门仍通过。这比计划的论点深一层:那些哈希证明「抽取当时忠实」,不证明「此后未被改」。**2026-07-30 更正**: 由此推出的「本仓无任何东西会注意到 toolkit 被改」过头了 —— `test_toolkit_release_candidate_build.py` 比对工作区与 HEAD,会拦**未提交**的漂移(守的是可复现构建,不是抽取忠实度);**已提交**的漂移仍然一路全绿,实质结论对后者成立。详见 plan 24 close-out 内的更正段。 |
| I2 | `tests/toolkit/test_strategy_core.py` | 自省抓出函数级切分留下的孤儿 helper。且它的扫描根已错——文件从 `tests/` 移到 `tests/toolkit/` 后 `parents[1]` 从仓库根变成 `tests/`,调用返回 `[]`。删除后**移植集中已无任何一处依赖自身文件路径**,整类风险关闭,而不只是关掉被点名的五个实例。 |
| I3 | 8 个文件退回 PS | 它们的哨兵在没有策略的仓库里正确地拒绝通过。其中一条参数化在空 glob 上——在这里它不是失败而是整条消失,那正是它旁边那条哨兵存在的理由。 |

## 逐 Task 匹配率

| Task | 匹配率 | 关键偏离 |
|---|---|---|
| 1 landing zone | 100% | conftest 需求实测为零(92 个文件无一使用 PS fixture) |
| 2 platform-neutral | 100% | 22 而非 25 文件:3 个主体是策略 |
| 3 engine adapter | 95% | 61 而非 63 文件;11 条到岸即红全部退回;含 H1 恢复 |
| 4 load-bearing | 90% | 「默认验证」修正为两道门;计划的变异配方成立但理由与计划设想不同(哈希不护工作区) |

## 六步审计取证

| 审计 | 结果 | 锚点 |
|---|---|---|
| ① 文件清单 | 计划外改动 3 个文件,全部有记录 | `.forge/README.md`(索引)、plan md、`test_plan_closeout_counts.py`(M3) |
| ② 签名 | 无 API 变更 | `git diff -- packages/ src/` 为空 |
| ③ 行为 / 死代码 | 死代码 0 | AST 扫全 84 文件的模块级未引用定义 |
| ④ 测试覆盖 | 零断言测试 0(5 处为异常即断言) | 见 L1 |
| ⑤ 偏离文档 | 6 条 DEV 齐;plan 首 commit `04293b0` 严格早于实施首 commit `1221103` | `git log` |
| ⑥ 深度 | 未启用 `--deep` | — |

## 优先修复建议

1. **无 CRITICAL,无需阻断。**
2. H1 的教训进 `historical-lessons.md`(下一步 lessons 阶段处理)。
3. M1 的残留风险(断言里的包名字面量在 rename 后静默失效)与 lesson #35 同源,建议
   在那条卡片下补一句子探针。
4. 遗留项以 close-out 为准,其中 **PS Slice E 仍被阻塞**这一条最要紧——它是跨仓库的,
   而且五个文件里两个 import 扫描看不见。

## 恢复后的文件与 PS 原文差一在哪 —— 机械核实

审计初稿把这一条列为「留给第二双眼睛」。随后我把它做成了探针,因为这件事比眼睛更适合
机器做:**剥掉 docstring 与全部字符串常量后比 AST**。docstring 与断言消息本身就是 AST 的
一部分,而本次移植确实重写了它们——直接比原始 AST 会对几乎每个文件报差异,什么也证明
不了。剥掉之后剩下的是逻辑:哪些调用、什么顺序、哪些非字符串参数、有哪些断言。

| 集合 | 对比文件数 | 可执行结构有差异 |
|---|---|---|
| platform-neutral | 22 | **0** |
| engine adapter | 60 | 13 |

neutral 集**逐个文件逻辑不变**,坐实了「纯搬运」。引擎集那 13 个**逐一对应声明的四类**,
没有第五类:

| 文件 | 差异原因 |
|---|---|
| `test_base_strategy_multi_pair.py` | 退回 1 个仓库扫描方法 |
| `test_config_self_validation.py` | 退回策略 glob 段 |
| `test_equity_provider.py` / `test_event_publisher.py` | 加引擎 importorskip 守卫 |
| `test_nautilus_filter_adx.py` | 指向 vendored `pandas_ta` + 重指 layering 守卫 |
| `test_nautilus_filter_{momentum,regime,volatility,volume}.py` | 重指 layering 守卫 |
| `test_nautilus_filter_regime_t9.py` | 退回 1 个 schema 扫描 |
| `test_sltp_mode.py` | 退回 1 个 schema 扫描 + 加守卫 |
| `test_strategy_core.py` | 退回 2 个 AST 扫描 + 删随之孤立的 helper |
| `test_toolkit_nautilus_indicators.py` | 指向 vendored `pandas_ta` |

**47 个引擎文件逻辑逐字节等同 PS 原文。** 这是 H1 恢复完整性的独立证据:如果重放漏了
或多了什么,差异集不会正好等于声明集。

两道门的说法也核实过了:`.github/workflows/release.yml:54-58` 一个 step 里依次跑
`make verify-base-clean` → `make install-nt` → `make verify-nt`,与 close-out 的表述一致。

## 仍然留给外部审查的

上面两条已闭环,但**自审的结构性局限没有因此消失**:写实现、写断言、写 close-out、做
审计、写这些探针都是同一个人。探针能证明「差异集等于声明集」,不能证明「声明集本身是
对的选择」——比如把 8 个文件判给 PS、把 adx 指向 vendored 副本,这些是判断,不是事实。
本仓 lesson C2 记的正是自审也会中招,建议这两类判断由 `--peer` 外部 AI 或人复核。
