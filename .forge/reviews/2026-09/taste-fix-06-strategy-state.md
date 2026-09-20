# 品味审查报告 — fix 06 策略状态与订单保护

- 扫描范围: fix 06 触及的 11 个 adapter 源文件（`--files` 清单模式）
- 宪法版本: 生态级 `the-alephain-guild/.claude/rules/coding-taste.md`（核心条款 + 8 收尾条）+ custos `code-style.md` 特化（英文红线、Decimal money math）
- 检测: grep 机械层 + Claude 语义层（未调 codex 交叉，未传 `--depth=deep`）

## 🔴 高危害 (0)

无。

## 🟡 中 (2)

| 宪法 | 位置 | 现象 | 建议 |
|------|------|------|------|
| 四 诚实（死代码）| `capital_allocator.py:82` | **本轮引入**。修复前 `get_tier_limit` 读 `self._tiers` 的值；改走 `_explicit_tiers`/`_implicit_pairs` 后，`_tiers` 只剩 `:219`/`:222` 读 **keys**，而 `:82` 仍在写入 `share` 值。该值此后无人读取。注释（`:72-75`）诚实说明了「只有 keys 被读」，但没有消除写入本身 | 二选一：让 `_tiers` 的值与 `get_tier_limit` 的 Decimal 结果一致，或把它降为 `_known_pairs` 集合。不要靠注释养一个死值 |
| 二 类型（getattr 防御）| `order_reconciler.py:483,484`、`trade_event_handler.py:163`、`orders.py:434,438,753` | 6 处 `getattr(obj, "field", default)` 访问强类型对象字段。**全部既有，非本轮引入**（逐条 `git blame` 范围外） | 归入既有技术债，不建议随本轮修复夹带。下轮若动这些文件，顺手改成协议/类型断言 |

## 🟢 低 (0)

编号注释 0 · 装饰分隔条 0 · print 0 · 中文日志 0 · 裸 except 0。

本轮新增行经 `grep -P '\p{Han}'` 实测 **0 处 CJK**——custos `code-style.md` 的英文红线（部署服务器不支持中文显示）守住。

## ⬆️ 上界·核心条款 (3) — 数据结构上界待审，非机械修复

| 宪法 | 位置 | 结构 / 职责问题 | handoff（数据结构先行，非拆分中心）|
|------|------|------|------|
| 三 尺度 | `tick_monitor.py` 540 → **687 行**（本轮 +147）| **本轮把该文件推过 600 软上限**。新增的层级生命周期（三态 + 归属映射 + 已成交/在途记账 + 基数计算）共 7 个方法，与既有的 `TrailingStopManager`、`TickMonitorManager` 两个类同居一文件 | 待审的是：层级的「状态 + 数量账本」是否应成为独立的值对象（如 `ScaledExitLedger`），而非挂在 monitor 上的 4 个平行 dict。判据：抽出后 monitor 是否只剩「读价格 → 问账本 → 产出动作」；特殊情况（无基数回退、末档吸收余量）是否随之消失。**不是为了行数把类搬走** |
| 三 尺度 | `coordinators/order_reconciler.py` 583 → **637 行**（本轮 +54）| **本轮推过软上限**。该文件同时承载恢复、保护补足、孤儿清扫、拒单分级、撤单归属五类职责 | 待审：拒单处置（`handle_order_rejected` 的四类分支）是否属于独立的「venue 回报分诊」概念。判据同上——分开后是否有特殊情况消失，而非只是行数搬家 |
| 三 / 核心·函数 | `signal_execution.py:85` `execute_entry_for_pair` **165 行 / 30 语句**（本轮 +21）| 既有 god 函数，**本轮又为它增加了一个职责**（资金预留门）。现承载：撤旧单 → 持仓上限 → 反手 sizing → 资金预留 → 建单 → 记账 → 提交 → 追踪，共 8 段 | 待审：入场是否应拆成「决定下多少」与「把这一单落地」两个概念。判据：拆完后反手 sizing 的特殊情况是否不再与资金预留纠缠。**禁用「按行数切」** |

> `orders.py` 974 行（本轮 +32）**不列为本轮 finding**：它在修复前已是 942 行，超限是既有状态，本轮增量占 3%。列出供上界审视时一并考虑。

## 🔬 近似探针命中复核 (22) — 命中→复核→结论，禁 silent skip

> 对账：god 函数 Step3 命中 **11** = finding 2 + 排除 9 ✓；垃圾变量名命中 **5** = finding 0 + 排除 5 ✓；getattr 命中 **6** = finding 6（既有）+ 排除 0 ✓；成组裸参数命中 0；巨型文件命中 3（见 ⬆️）。

| 近似类型 | 位置 | 复核判定 | 理由 |
|------|------|------|------|
| god 函数 | `signal_execution.py:85`（真实 165 行 / 30 语句）| **finding → ⬆️** | AST 实测非行距误算。8 段职责串联，本轮又加一段 |
| god 函数 | `order_reconciler.py:463`（真实 127 行 / 13 语句）| **finding → ⬆️** | 语句仅 13 但分四类互斥处置（tracked stop / tracked TP / reduce-only 分级 / entry），是分支树而非顺序流 |
| god 函数 | `signal_execution.py:251`（真实 75 行 / 12 语句）| 排除 | 单一职责：平仓决策 + 一次提交。行数多因大段解释性注释（plain close 的两条限制） |
| god 函数 | `trade_event_handler.py:43`（真实 87 行 / 19 语句）| 排除 | 单一事件回调，顺序处理一次成交；本轮 +6 行（层级确认） |
| god 函数 | `trade_event_handler.py:131`（真实 86 行 / 19 语句）| 排除 | 既有，本轮未碰。单一事件回调 |
| god 函数 | `order_reconciler.py:117`（真实 78 行 / 14 语句）| 排除 | 单一职责：补足缺失的保护量。本轮 +20 行仍未改变职责单一性 |
| god 函数 | `order_reconciler.py:398`（真实 64 行 / 14 语句）| 排除 | 既有，本轮未碰 |
| god 函数 | `sltp.py:229`（真实 62 行 / 9 语句）| 排除 | 既有，本轮未碰 |
| god 函数 | `orders.py:578`（真实 47 行 / 6 语句）| 排除 | 行距近似误算——真实长度低于阈值 |
| god 函数 | `orders.py:689`（真实 88 行 / 19 语句）| 排除 | 既有，本轮未碰 |
| god 函数 | `orders.py:814`（真实 65 行 / 11 语句）| 排除 | 既有，本轮未碰 |
| 垃圾变量名 | `signal_execution.py:86,194,251,327`（`bar` / `bar=bar`）| 排除 ×4 | `bar` 是 K 线，领域概念，非 foo/bar 占位符。正则无法区分 |
| 垃圾变量名 | `orders.py:297` | 排除 | 命中的是注释文本 "every later bar"，非变量名 |
| getattr | `order_reconciler.py:483,484`、`trade_event_handler.py:163`、`orders.py:434,438,753` | finding ×6（既有）| 见 🟡 表。全部先于本轮存在 |

## 语义层结论（机械层测不到的）

- **炫技 / 过度设计（八）**：`TakeProfitLevelState` 三态枚举**非 finding**。判据「不引入这个抽象会更难懂吗」答是——bool 表达不了「已派发待确认」，而那正是 ST-2 的根因。`entry_reservation.py`（53 行单函数模块）**非 finding**：它被两个不同协调器共用，放进任一方都会造成另一方跨 import，独立模块是单一地址的正当形态。
- **docstring 重复（一.2）**：未发现。本轮新增的 7 个 monitor 方法 docstring 各自描述独有语义。
- **依赖方向（核心·模块图）**：正方向。`adapter/` 是平台适配层，import `nautilus_trader` 属 `IO→框架`，非 `领域→IO`。无 finding。
- **观察项（不计 finding）**：`release_level(level)` 与 `release_level_order(order_id)` 语义相邻（前者用于未派发即放弃，后者用于订单终结），是两个入口。当前区分清晰，但若 ⬆️ 中的 `ScaledExitLedger` 抽出，二者应合为账本上的一个操作。

## 统计与优先级

- **探针存活证明**: 10/10 条经 positive 语料按**本轮 files 清单形态**实证会命中（编号注释 7 · 装饰条 4 · getattr 2 · print 4 · 中文日志 4 · 裸 except 2 · 垃圾变量名 6 · 成组裸参数 1 · god 函数 1 · 巨型文件 1），与 skill 记录的参考量级逐项吻合。本轮对目标 scope 的调用退出码**全部合法**。故本报告中的 0 命中项记「干净」的两个前提均成立。
- **目录尺度探针**: 垃圾桶包名 / 包扁平度①② 记 **N/A（清单模式无目录尺度）**，不记 0、不记干净。
- 各级: 🔴0 🟡2 🟢0 ⬆️3
- 按条目分布: 二 类型 6（既有）· 三 尺度 2 · 四 诚实 1 · 核心·函数 1
- 维度范围: **all**（未传 `--dim`，全维度）
- 对抗审查: 未传 `--adversarial`，跳过
- **建议先处理**:
  1. 🟡 `capital_allocator._tiers` 死值——本轮引入，改动小，随下一次触碰该文件即可清掉
  2. ⬆️ `tick_monitor.py` 的层级账本——本轮把该文件推过上限，是最值得做的一次结构澄清
  3. ⬆️ `execute_entry_for_pair`——本轮为它增加了职责，债务在累积
