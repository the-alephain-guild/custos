# 历史教训 (custos)

本文件继承 workspace `the-alephain-guild/.claude/rules/historical-lessons.md` 中与 custos
开发直接相关的**精华教训**. 独立仓库 clone 场景外部开发者仍能读到 lesson 核心防护;
完整叙事保留在生态 archive, 本仓库只留 rule 卡片 + custos 特化 binding.

> **custos 内部 lesson 用 `C1` `C2` … 前缀区分生态数字编号** (见文末"记录新 lesson")。

## 本轮已落地防护的教训索引

完整叙事在 `.forge/lessons-archive/active-detail.md`；强制约束与验证入口分别在同目录的 `mandatory-rules.md` 和 `verification.md`。


### #C17: 审计失败必须改变宿主状态
> Full: `.forge/lessons-archive/active-detail.md` #C17

<!-- hash: 78088e120ad7 -->


### #C18: 完整对账采集必须原子落盘
> Full: `.forge/lessons-archive/active-detail.md` #C18

<!-- hash: f314c7119095 -->


### #C19: 现金总资产必须由完整余额估值
> Full: `.forge/lessons-archive/active-detail.md` #C19

<!-- hash: 07bf8b8d66e6 -->


### #C20: 独立账本必须对齐范围与估值口径
> Full: `.forge/lessons-archive/active-detail.md` #C20

<!-- hash: 38f38987f5f3 -->


### #C21: 新 generation 必须符合真实引擎生命周期
> Full: `.forge/lessons-archive/active-detail.md` #C21

<!-- hash: 1337529e7be8 -->


### #C22: 当前验收计数与历史证据分开维护
> Full: `.forge/lessons-archive/active-detail.md` #C22

<!-- hash: fb0397bc18f8 -->


### #C23: 已执行事实不能被准入规则回滚
> Full: `.forge/lessons-archive/active-detail.md` #C23

<!-- hash: 2b05a5c68d18 -->


### #C24: 辅助任务不能覆盖主操作终态
> Full: `.forge/lessons-archive/active-detail.md` #C24

<!-- hash: 2a738f842253 -->


### #C25: watcher 动作前必须重验 durable authority
> Full: `.forge/lessons-archive/active-detail.md` #C25

<!-- hash: 0b2c169dd9a3 -->

---

## C32 规则装在被发现的那个入口上，而不是装在它约束的那个决定上 (2026-09)

- **事件**: 一份复核报告一次给出七项，其中五项是同一形状：规则本身是对的，只是装错了地方。
  「这张委托是不是只减仓」装在提交路径上，改单路径读的是**改之前**那张，于是一张合法的
  平多 1 可以被改成卖 2，不过冻结检查、不占预留，撮合完账上是个空头。「熔断要撤掉增险挂单」
  装在「有持仓」这道与它无关的闸门后面，空仓书上的入场单一次都不会被撤。「策略换版」
  遍历所有边界而不是这个模式的边界。「回撤基准采样」装在准入检查里，而准入检查只有在
  出现入场候选时才到达，持仓期间的行情新高一次都没进过基准。「就绪等待」按
  deployment id 记一次，而 id 标识的是**部署**不是**这一次运行**，节点重启后替换节点
  继承了前一个的判定。
- **根因**: 七个 finding 里有五个是同一形状：规则本身写对了，但被装在**报告当初指出的那个入口**
  上，而不是装在**它所约束的那个决定**上。判定「这张委托是不是只减仓」装在提交路径，改单路径
  读的是改之前那张；撤增险挂单装在「有持仓」这道与它无关的闸门后面；策略换版装在「所有边界」
  而不是「这个模式的边界」；回撤基准采样装在准入检查里，而准入检查只在有入场候选时才到达；
  就绪等待按 deployment id 记一次，而 id 标识的是部署不是这一次运行。另外两个不同源：FR-2 是
  两套状态（订单累计成交 / 持仓生命周期）被当成一套，FR-6 是失败路径压根没有规则。
- **教训**: 装规则时先说清它约束的是**哪一个决定**，再把到达那个决定的路径数出来。只在一条路径上
  装，另一条路径就会在某次审查里被单独报上来——而它从第一天起就是敞着的。
- **预防**:
  - 写完一条规则，问一句「**还有谁会做同一个判断**」。判断有名字时就 grep 那个名字
    （`order_is_risk_reducing` 在提交和改单两条路上都该出现）；没有名字时，给它起一个——
    一个被两处调用的判定函数，比两处各写一遍更难写歪。
  - **规则的作用域要和它约束的东西同尺度**：就绪属于一次运行，所以不能按部署记；策略属于
    一个模式，所以不能按「所有边界」发；撤单属于挂单，所以不能挂在持仓判空之后。
    写下作用域的那个 key，和这条规则谈论的那个名词对不上，就是装错地方了。
  - 无法把规则收进一处时，**两处都装，并各自可测**（lesson #22/#28）：外层选择、内层拒绝，
    扰动掉任一层，另一层的测试必须仍然转红。本轮的策略换版就是这么做的。
- **与 C27 / #35 的区分**: C27 是「验收条款有并列多支，只做了一支」（**同一份验收**的分句漏了）；
  #35 是「改名要扫全部消费者」（**同一个常量**的消费方漏了）；本条是「**同一个判断**的调用路径
  漏了」。三条都是覆盖面不足，漏掉的东西不同：分句 / 消费者 / 路径。
- **Binding**: 无自动化 binding——「还有谁做同一个判断」不能机械枚举。实例与扰动表见
  `.forge/fixes/2026-09/30-answers-outliving-their-question.md`。

<!-- hash: 0d4f4888bece -->

---

## C30 defer 的理由是当时的判断，不是问题的属性——接着做之前先核它 (2026-09)

- **事件**: fix 26 关掉 RR-8 的「撤销增险挂单」之后，把验收里「有确认、有重试」那一半显式
  defer 了，理由写的是「确认需要一条可信的 venue 状态读取回路，属于独立的工作面」。fix 29 接手
  时按 C29 的纪律先核这条理由：那条回路**早就在仓里**，而且在生产路径上跑着——
  `host.py:1159 _preserve_and_confirm_shutdown` 与 `:1200 _flatten_and_confirm_shutdown`，
  形状齐全（deadline + 轮询 + 重发 + ack 窗口），读的还是同一个 `runtime.cache`。
  真正缺的只是「熔断这条路没用上关停那条路已有的确认」，比 defer 说的小一个量级。
- **根因**: defer 记录的是**当时那一刻的判断**，可它写出来的样子是一句关于问题本身的论断——「这需要一条
可信的 venue 状态读取回路，属于独立的工作面」。下一个人读到它，读到的是「这件事很大」，于是把
它当前提引用，而不是当结论复核。仓库在这期间一直在变：fix 22 为了关停确认写的
`_preserve_and_confirm_shutdown` 与 `_flatten_and_confirm_shutdown` 正是那条回路，
两者读的还是同一个 `runtime.cache`。defer 那句话在写下的当天可能就已经不准了。
- **教训**: 接着做一件被 defer 的事之前，**先把那条 defer 理由拿到今天的代码上核一遍**，核完在新 plan 里
明写它成不成立。不成立就当场撤回，再动手；成立就把新证据补上。照抄上一轮的理由去论证「所以还是
做不了」，是把一条推理的遗迹当成了事实。
- **预防**:
  - defer 在 close-out 里就要**写清依据的是当时的哪一段代码**（`文件:行号`），而不是只写一句
    定性判断。有锚点的理由，下一个人几秒钟就能复核；没有锚点的理由只能被相信。
  - 接手的 plan 开头单列一段「先撤回/确认那条理由」，把核实结果写进去。fix 29 的
    `## 先撤回那条理由` 是这条的样板。
  - defer 的**代价**也要写：fix 26 写了「撤单请求若在场所侧失败，本轮监督不会发现」，
    这句话让接手的人知道该先修什么。
- **与 C29 的关系**: C29 说「复现探针还绿不等于缺陷还在」，本条说「defer 的理由还写在那里不等于
  它还成立」。两条都是**把一段旧的推理痕迹读成了当前事实**，只是载体不同：一个是探针，一个是
  上一轮自己写下的话。
- **Binding**: 无自动化 binding。实例见 `.forge/fixes/2026-09/29-containment-must-look-before-it-claims.md`
  的「先撤回那条理由」段与偏离日志第二条。

<!-- hash: ab7cd5248fbb -->

---

## C31 扰动没被施加，和扰动不咬，在输出上是同一片绿 (2026-09)

- **事件**: 一个会话里两次。①  写了个 `run()` helper 跑扰动，引号让 pytest 一条都没收集到，
  `no tests ran in 0.00s` 夹在几条真结果中间，差点被当成「这处扰动不咬」记进 close-out；
  ② 扰动替换器的锚点在文件里匹配 0 次，`assert` 按 C10 的规矩拦住了替换、没有去猜——
  可紧接着那条 pytest 照常跑完，打出 `9 passed`，而那是**未扰动**的结果。
- **根因**: 扰动验证是两步：先改代码，再跑测试。**只有第二步会打印出像结论的东西。** 第一步失败时，第二步
照常对着未改动的代码跑，打出一片绿——而那片绿在日志里，与「这处扰动不咬」长得一模一样。
本轮两次都是这么来的：一次是 shell 引号让 pytest 一条都没收集到（`no tests ran in 0.00s`
夹在几条真结果中间），一次是替换锚点匹配 0 次、断言拦住了替换，但紧随其后的 `9 passed` 是
**未扰动**的结果。
- **教训**: 一处扰动只有在**证明它真的被施加了**之后才算证据。改完立刻 `git diff --stat`，空的就中止，
不要让这一步的失败悄悄流进下一步。跑出来的绿，先当作「扰动可能没发生」，而不是「缺陷可能没被守住」。
- **预防**:
  - 扰动之后、跑测试之前，`git diff --stat` 必须非空；空就中止，不要进下一步。
  - 不要把「改」和「跑」写进同一条不中断的命令串——第一步失败时，`&&` 之外的写法会让第二步
    照常执行。
  - 扰动脚本用**独立的一条命令**，它自己打印「perturbed」之类的确认；日志里没有那句，
    后面那片绿就不作数。
  - 锚点断言（C10）拦住的是「改错地方」，拦不住「没改」被误读成「改了也没事」——两者要各管各的。
- **与 C15 / C26 / 生态 #50 的关系**: C15 是仪器给出错误结论（字节码陈旧），C26 是仪器量程不够
  （验不出被删的旧行为），本条是**仪器根本没启动，而读数看起来正常**。三条都在说同一件事的不同
  侧面：扰动验证本身是一种观测手段，它也需要被证伪。
- **Binding**: 无自动化 binding。两处实例见
  `.forge/fixes/2026-09/29-containment-must-look-before-it-claims.md` 的扰动表脚注（R6）。

<!-- hash: 1f9ac622104d -->

---

## C29 复现探针仍然成立，不等于缺陷仍在——先说清它挑的输入不是生产的输入 (2026-09)

- **事件**: 同一轮 fix 里连着遇到两次。EE-6（价格改善留下预留）修完之后，审查方的
  `improved_fill_price_leaks_runner_reservations` 照常打印成功——它直接调 `record_order_fill_sync`
  且不传未成交数量，走的正是修复保留下来的降级路径。EE-3（桥接器重建丢订单归属）修完之后，
  `new_bridge_drops_existing_order_fill` 也照常打印成功——它给两个桥接器各建了一个内存替身
  `_Emitter()`，两块互不相通的记忆，而生产里两者背后是同一个数据库文件。两次都不是没修好：
  同一段生产路径的测试（`test_the_boundary_reports_the_unfilled_quantity_to_the_store`、
  `test_a_rebuilt_bridge_still_owns_an_order_it_did_not_initialize`）都从红转绿，且把生产接线
  那一行删掉就立刻转红。
- **根因**: 复现探针是审查方用来**证明缺陷存在**的，它挑一条能走通的输入把缺陷演出来。那条输入
  不必是生产路径——只要能触达同一段代码就够了。于是修复之后会出现一种读起来很像「没修好」的状态：
  生产路径已经不会再走进缺陷，而探针挑的那条输入仍然会，因为修复恰恰是靠**让生产路径带上更多
  信息**（未成交数量、共享的持久存储）来消除缺陷的，而探针的替身不提供这些信息。把「探针仍然成立」
  直接读成「缺陷仍在」会让人去追一个不存在的问题；把它读成「已经修好了，不用管」又会放过真实的
  降级路径。两种读法都错在同一件事：没有说清探针的输入形态与生产形态差在哪里。
- **教训**: 复现探针翻转与否都不是结论，它只是一条证据。修复后探针仍然成立时，必须当场写清三件事：
  探针挑的输入形态是什么、生产的输入形态是什么、哪条测试钉住了生产形态。第三件缺失就等于没修——
  「生产不会这么调」是个断言，需要一条会因接线被拆掉而转红的测试来证明。
- **预防**:
  - **close-out 里给探针单列一段**，写明它中止在哪一行（修好了）或为什么还绿（输入形态不同）。
    只写「探针不再成立」而不给行号，和只写「探针仍然成立」而不解释，都不足以让下一个人判断。
  - **降级路径要么消灭，要么出声**。EE-6 的降级路径无法消灭（「已全部成交」这个终态在持久层没有
    独立事实可查），所以它现在发 `order_reservation_unfilled_quantity_unknown` 警告并带上被扣住的
    金额——沉默的保守行为会长成看不见的账。
  - **测试要按生产形态写，不按方便形态写**。EE-3 第一版测试复用同一个 emitter 对象就能过，但那
    证明不了任何事：它自己的内存回答了问题。改成对同一个数据库路径新建 outbox 与 emitter
    （`_restarted()`），才是重启该有的样子，而且这个改动之后扰动验证才真的会咬。
  - **别改测试替身去迎合别人的探针**。让 `_Emitter` 共享一块模块级记忆能让 EE-3 的探针转红，但那
    是在伪造证据，而且会污染所有用它的测试。
- **与「探针红 ≠ 缺陷在」的关系**: 那是同一枚硬币的另一面——RS-5 的探针不传时间戳（用的是修复前的
  签名）、RS-8 的探针 harness 缺 `_shutdown_position_policy`（AttributeError 被吞），都是探针自己
  跑不动，与被测代码无关。两个方向的判据是同一条：**探针的输入形态是证据链的一部分，必须和结论
  一起写出来。**
- **Binding**: 无自动化 binding。两份实例见 `.forge/fixes/2026-09/18-a-price-improvement-must-not-keep-the-reservation.md`
  与 `.forge/fixes/2026-09/19-order-attribution-must-outlive-the-bridge.md` 的「探针为什么还绿」段。

<!-- hash: 0b7827473bca -->

---

## C27 修完根因不等于满足验收——验收条款常是并列多支 (2026-09)

- **事件**: fix 06 修完 Codex 报告的 8 项 findings，每项根因都改掉、复现脚本的断言全部翻转、新增 37 项回归、`make verify` 与 `make verify-nt` 双绿 2666 passed，随后 close-out 标 ✅。用户问「这份报告处理完了吗」，逐条对照验收条款才发现 **ST-1 没完**：验收要求「逐档成交、跳空触发、部分成交」三支下总退出量一致，只有逐档一支有测试。实测跳空：两档各 50%，一个 tick 跨过两档只退出 0.5，剩 0.5 永远不退——而价格冲高再回落正是分批止盈要抓的行情。
- **根因**: 审查报告的结构是「位置 → 根因 → 复现 → 建议 → **验收**」。修复时注意力自然落在根因和复现上——那是缺陷的证明，改完它测试就从红转绿，收尾的手感很完整。但复现只是缺陷的一个实例，**验收才是正确性的定义**，而且常常是并列多支的。ST-1 的验收写着「逐档成交、跳空触发、部分成交下的总退出量一致」三支，复现只演示了第一支；我修完根因（比例基数）、第一支转绿，就按「这项做完了」收尾了。跳空那一支从未被执行过，实测也不成立。
- **教训**: close-out 之前，把每项 finding 的验收条款逐条拆成分句，对每个分句点出覆盖它的测试名；没有测试对应的分句就是没做完。「根因修掉了 + 复现不再成立 + 全量绿」三者同时为真，仍然不等于验收满足。
- **预防**:
  - **close-out 前做一张验收对照表**：每项 finding 的验收原文逐句拆开，每句后面写覆盖它的测试名。空着的格子就是未完成项，不是「已隐含覆盖」。
  - **验收里的顿号是并列，不是同义反复**：「逐档成交、跳空触发、部分成交」是三个独立场景，不是一个场景的三种说法。读到顿号就拆句。
  - **复现脚本翻转 ≠ 验收满足**：复现是缺陷的存在性证明，用一个实例即可；验收是正确性的全称要求。前者转红只说明那一个实例被修好了。
  - **写「已完成」之前问一句：审查方会怎么验收这项？** 本次正是这个问题暴露了缺口，而它在 close-out 时没有被问。
- **与 lesson #28 / #40 区分**: #28 是 plan 的复合分句被「借位」到隔层实现（本层无 guard）；#40 是 close-out 承袭红线名当兑现声明（test 绿 ≠ runtime 接线）；**本条是审查报告的验收分句被根因修复整体盖过**——三者都是「声明的覆盖面大于实证的覆盖面」，载体不同：plan 分句 / 红线名 / 验收条款。
- **Binding**: 无自动化 binding。对照表见 fix 08 close-out 的「ST-1 验收三支的最终状态」段，缺口的实证与修复见 `.forge/fixes/2026-09/08-scaled-exit-gap-through.md`。

<!-- hash: f8197111918b -->

---

## C28 对有副作用的查询写循环：终止要有与状态无关的上界，提前退出要归还 (2026-09)

- **事件**: 修 C27 那个跳空缺口时，让 tick 回调循环调用 `check()` 取完所有已达标层级。**同一轮里踩了两次**。第一次：循环不终止，测试跑满 120s 被中止——因为派发失败时 `abandon_level()` 会**有意**把层级退回 ARMED（那正是本地拒绝保持可重试的机制），于是同一档被无限提供。第二次：改用 `offered` 集合限制每档一轮后，判断放在 `check()` 之后，而 `check()` 返回前会把层级标记为 PENDING——「这轮不执行、直接 return」于是留下一个 PENDING 层级，后续 tick 再也不提供它，ST-2 的重试测试当场转红。
- **根因**: `check()` 是一个有副作用的查询——它在返回层级之前把该层级标记为 PENDING。围绕它写循环时踩了两次：其一，循环的终止寄托在「状态会推进」，而另一条路径（派发失败后 `abandon_level`）**有意**把层级退回 ARMED 以保持可重试，于是同一档被无限次提供，测试挂死；其二，改用计数集合限制每档一次后，把判断放在 `check()` 之后，于是「这轮不执行、直接 return」留下一个 PENDING 层级，后续 tick 再也不会提供它。
- **教训**: 对有副作用的查询写循环，两件事都要显式保证：终止条件不能依赖状态推进（要有与状态无关的上界，如「每个元素一轮」），任何提前退出都必须归还这次查询已经拿走的东西。
- **预防**:
  - **终止条件要能证明**。「状态会推进所以会停」在有回退路径时不成立，而回退往往是另一个功能**有意**设计的。改用与状态无关的上界：每个元素一轮、计数集合、固定轮数。
  - **问一句「这次查询拿走了什么」**。`check()` 拿走的是层级的 ARMED 状态。凡在拿到结果后决定不用它，都要还回去。
  - **两道保证都要扰动验证**。本次去掉循环 → 4 条跳空测试红；去掉归还 → ST-2 重试测试红。第二道若不单独扰动，很容易被当成冗余删掉。
  - 讽刺留作提醒：ST-2 的根因就是「`check()` 在成交前消费层级」。修它的同一轮，我在新写的循环里以另一种形式重犯了同一件事。
- **与 C12 区分**: C12 是「membership 不是 liveness」（记录的清理是异步的，所以记录答不了"现在还活着吗"）；本条是「查询本身改变了被查的东西，所以问一次是有代价的」。
- **Binding**: `ExecutionCoordinator._drain_exit_actions` 的两道保证；探针见 `tests/toolkit/test_strategy_state_and_order_protection.py::TestScaledExitsAgreeAcrossAllThreePaths` 与 `::TestAScaledLevelIsSpentOnlyByAFill::test_a_locally_refused_partial_retries_at_the_same_price`。

<!-- hash: e3bd8cdcfc33 -->

---

## C26 扰动验证验的是「我加的会不会失效」，验不出「我删的不该删」 (2026-09)

- **事件**: fix 06 修 ST-2 时，把恢复路径从 `check()` 换成新写的 `observe()`——后者只保留了 `update_peak`，丢掉了 `_check_trailing_tp` 里 `_trailing_manager.check()` 设置的 activation 标记。37 项新测试、2666 项全量测试、**11 个扰动点**全部通过。实测后果不小：重启时行情在激活线之上、随后跌回激活线以下，trailing stop 该触发却不触发，从峰值的回撤完全没有保护。缺陷由随后的代码审计抓出（H1），不是由这一整套验证抓出的。
- **根因**: 扰动验证的形态是「把修复改回缺陷形态，确认测试转红」——它保护的是**新增的能力**。而一次替换（把 `check()` 换成新写的 `observe()`）同时包含两件事：新写的要对，以及旧调用的哪些副作用必须保留。后者不在扰动集里，因为没有人会想到去扰动一个「本来就该继续在那里」的行为。11 个扰动点全部打在本轮新增的语义上，一个都没打在被顺手去掉的旧语义上。
- **教训**: 用新调用替换旧调用时，先读旧调用的完整实现、列出它的全部副作用，逐条判定「保留」或「有意去掉 + 理由」；再用同一输入分别走新旧两条路径，比对可观测状态。扰动验证回答「我加的会不会失效」，回答不了「我删的该不该删」。
- **预防**:
  - **替换类改动先列副作用清单**：`X()` → `Y()` 之前读完 X 的实现，把它做的每件事列出来，逐条标「保留」或「有意去掉 + 理由」。本次 `check()` 做两件事（更新峰值、设置激活），清单会让第二件当场暴露。
  - **写一个新旧对照探针**：同一输入分别走两条路径，比对可观测状态。本次正是这么确诊的——`check()` 后 `_activated=True`，`observe()` 后 `False`，峰值两者相同。这个探针几行就能写，比读代码可靠。
  - **判据要区分「消费性」与「非消费性」状态**：拿掉一个副作用之前问它代表什么。scaled 层级代表一次退出配额（拿掉是对的，那正是 ST-2 的 bug），trailing 的激活标记不代表任何配额（拿掉纯是损失）。
  - **扰动集要覆盖「删除」方向**：除了「把新逻辑改回旧缺陷」，再加一类「把被删掉的旧行为加回来」，看测试是否仍绿——若仍绿，说明没有任何测试在保护那个被删的行为。
- **与 C9 / C15 区分**: C9 是防线守着一个比现实窄的定义（fail-closed 只围着 `except` 写，挂起不抛异常所以没人设防）；C15 是扰动验证因字节码缓存给出错误结论（仪器坏了）；**本条是扰动验证工作正常、结论也正确，但它的覆盖面天生不含「被删掉的旧行为」**（仪器没坏，量程不够）。
- **Binding**: 无自动化 binding——这是实施纪律。本轮的对照探针见 `.forge/reviews/2026-09/06-strategy-state-and-order-protection-review.md` H1，修复与回归见 fix 07 Fix 2（`d319859`）。

<!-- hash: 8eebf0dfef80 -->

---

## C16 并行 agent 的 `git add` 会带走你正在写的文件，且表现为「文件变回旧内容」(2026-09)

- **事件**: NT 2.0 升级 Slice C 期间，另一个 agent 并行在做 Task 10（测试面迁 2.0 import layout）。
  两者都要改 `tests/engines/nautilus/test_runner_safety_host_wiring.py`——我因为 Task 9 删掉了它
  引用的类，对方因为 import 路径。我写入新版本、验证落地（`read_text() == content` 通过、
  `wc -l` 对得上、测试 6 passed），随后它两次变回旧内容。我按 C15 的思路先怀疑字节码缓存，再怀疑
  工具写入未落地（教训 #13），写了 probe 文件排除 `ruff format`，最后 `git log -- <file>` 才看到
  一个不是我做的 commit `3dd7ff6` 改了它。对方 `git add` 时把我当时磁盘上的版本一并提交了，
  所以内容没丢——但 commit message 只说「toolkit 与 host 测试迁 2.0 import layout」，实际含
  Task 9 的重写。
- **根因**: 两个 agent 共享一个工作区。教训 #27 讲的是「`git add <specific-file>` 会带走 index 里
  别人 pre-staged 的改动」；这是它的反向形态——**对方按自己的文件清单 `git add`，而清单里的文件
  正被另一个 agent 写着**，于是对方的 commit 里混进了我的工作，而我的下一次读取看到的是对方
  写入的版本。「文件变回旧内容」这个表象与「写入没落地」「缓存陈旧」完全一样，而三者的排查方向
  互不相同。
- **教训**: 共享工作区里出现「我写的内容不见了」，**第一件事是 `git log --oneline -3` 与
  `git log -- <file>`**，看有没有不是自己的 commit。工具返回成功、内容验证过、随后又变了，
  这个组合指向并发写入，不指向自己的工具链。
- **预防**:
  - 排查顺序固定为：`git log -- <file>`（有没有别人的 commit）→ `git status`（是否已被 stage）
    → 写 probe 验证工具链 → 最后才怀疑缓存。本次顺序反了，多花了两轮。
  - 自己的改动**尽早 commit**，不要攒着（教训 #24 的另一个理由：并行场景下未提交的文件既可能丢，
    也可能被别人提交并冠上别人的 message）。
  - 发现自己的工作被别人提交后，不要重写历史去「取回」——内容已在 HEAD，重写会破坏对方的提交。
    在偏离日志里记明哪个 commit 实际包含了什么即可。
  - 与 #27 合并适用：#27 防「我的 commit 带走别人的」，本条防「别人的 commit 带走我的」，
    两者是同一个共享 index 问题的两个方向。
- **Binding**: 无代码 binding。plan 01 偏离日志「多 agent 并行」条记录了本次实例与受影响的 commit。

---

## C15 同秒还原 + 等长改动 = pytest 跑的是旧字节码，扰动验证会骗人 (2026-09)

- **事件**: NT 2.0 升级 Slice C 中，给新写的事件转发器做扰动验证：备份源文件 → 施加扰动 → 跑测试 →
  `cp` 还原 → 再跑。其中一个扰动是把两段代码**互换顺序**（字节数完全不变），还原发生在同一秒内。
  于是 `.pyc` 的失效判据（源文件的 mtime **秒**数 + size）两项都没变，pytest 复用了扰动版字节码。
  表现为「还原之后测试仍然红」，而 `git show HEAD:<path>` 与磁盘内容都是正确的。一度以为自己提交了
  坏代码，实际提交的内容没问题、跑的字节码有问题。`PYTHONPYCACHEPREFIX` 指向一个新目录后立刻全绿。
- **根因**: CPython 默认按 `(源文件 mtime 秒, size)` 判断 `.pyc` 是否新鲜。`cp` 会更新 mtime，但
  精度只到秒；一次「改→跑→还原」在一秒内完成、且改动不改变字节数时，两项判据都骗过了它。这类
  改动在扰动验证里**特别常见**——交换两段代码的顺序、把 `if X:` 换成等长的 `if Y:`，都是等长的。
- **教训**: 扰动验证的结论只有在字节码确实被重新编译时才成立。而"是否重新编译"这件事默认不可见，
  也不会报错。
- **预防**:
  - 每次扰动跑测试都带 `PYTHONPYCACHEPREFIX=<每次唯一的目录>`，把缓存写到别处，从根上绕开判据。
    比 `touch` 可靠：`touch` 仍受同秒问题影响。
  - 还原后那次「应该全绿」的确认跑，同样要带（否则它自己就是下一个受害者）。
  - 一旦出现「还原后仍红」，**先怀疑字节码而不是怀疑自己的改动**：比对 `inspect.getsource()`
    与实际行为，两者不一致即可确诊（本次正是这么确诊的）。
  - 与生态 #50 同族：那条讲"自建观测工具要先被证伪"，本条是它在**验证流程**自身上的形态——
    扰动验证是一种观测手段，它也会在看起来正常工作时给出错误结论。
- **Binding**: 无代码 binding（这是流程纪律）。plan 01 偏离日志「扰动验证流程」条记录了本次实例。

**续编：还原扰动时 `git show HEAD:` 拿到的是 HEAD，不是你的工作（2026-09-20）**

fix 07 做扰动验证时用 `git show HEAD:<file> > <file>` 还原，而被扰动的那个修复**尚未提交**——
HEAD 上是修复前的版本，还原动作把修复本身抹掉了。表象与本条主体完全一样：「还原之后测试仍然红」。

三种成因的表象至此重合，排查方向却互不相同：字节码缓存（本条主体）、并行 agent 的提交（C16）、
**还原源头取错（本续编）**。固定做法：**扰动前 `cp` 一份到 scratchpad，还原用那份备份**；只有当被
扰动的内容已经提交时，`git show HEAD:` 才与备份等价。区分的办法是还原后先 `grep` 一下修复的标志性
文本还在不在，再跑测试——文件内容是事实，测试结果是推论。

---

## C14 跨仓契约字段被当成自有常量 — 写出一条自己无权满足的验收项 (2026-09)

- **事件**: Plan 01（NautilusTrader 1.230.0 → fork 2.0.0rc5）Task 2 只列 `toolkit_rc.py:113-118` 与三个 authority json，验证清单写「1.230.0 残留 grep 归零」。审计实读：`engine_version` 还以 `Literal["1.230.0"]` 写在 `custos_toolkit/contracts/strategy_execution.py:140,197`、以 `"const": "1.230.0"` 写在 `docs/gateway-contract/v1/` 三份 schema、以内嵌 canonical JSON 写在 `docs/authority/vendor/crucible-runner-strategy-release-resolution-v1.golden.json` 8 处。最后那份由 Crucible 生成，authority-docs.md 明写「Never invent, vendor or pre-register downstream receipts」。
- **根因**: 版本号出现在自有代码里就被当成自有常量。engine_version 同时以 Literal 写在 canonical V1 契约实现、以 const 写在三份 gateway-contract schema、以内嵌 JSON 写在 Crucible 所有的 vendored golden 里，是 PS / custos / Crucible 三方 exact-byte 握手的字段；plan 只列了 toolkit_rc.py 与三个 authority json，并写下「1.230.0 残留 grep 归零」——不改 Crucible 的文件就不可能满足，而那份文件 custos 无权改。
- **教训**: 起 plan 改任何版本号、枚举值或字段常量前，先 grep docs/authority/vendor/** 与 docs/gateway-contract/**；命中即按 mandatory-rules §3 列为跨仓协调项并指名对端 owner，验收项按「自有文件归零」与「对端交付后复核」分列，不写一条自己无权满足的门。
- **预防**:
  - plan 起草的 Foundation Scan 加一条固定探针：对每个将改的常量值 `grep -rn '<值>' docs/authority/vendor docs/gateway-contract`，命中的文件按 owner 分栏列进 File Inventory（`Modify` / `Blocked`）。
  - 验收项措辞禁用「全仓归零」；改为「custos 自有文件归零」+「对端 owner 交付后由 `make check-authority` 复核」。
  - 与生态 #46 同族（断言的覆盖面大于实证的覆盖面）：这次窄的不是 grep 范围，是「谁有权改」的范围。
- **Binding**: Plan 01 修订版 Task 2a / 2b 与「跨仓影响分析」段（commit `a47bdbd`）；审查原文 `.forge/reviews/2026-09/01-nautilus-trader-2-0-upgrade-review.md` C2。forge `planning/SKILL.md` Step 1.5 的探针待加（forge 仓）。

**反向的那一半：跨仓的措辞会让人放过自己那半（2026-09-21，fix 28）。** 本条防的是「把别人的东西
当自己的、写出无权满足的验收」。同一条边界还有反向的失手：RR-6 的标题是「OKX 净 PnL 被**消费端**
再次扣除佣金」，位置行同时给了 custos 与 Crucible 两个锚点，读起来像一件要两边协调才能办的事。
实读之下，**根因整个在生产端**——`okx_ledger.py` 把 `pnl + fee` 写成 realized PnL，而那笔 fee 已经
作为独立 commission 行从 `fills-history` 发过一次；同一个采集窗口里这笔钱被数了两遍，与消费端怎么
算无关。改成报毛值之后，消费端那条 `gross - commission` 对 OKX 自然成立，Crucible 一行都不用动。

判据：读到跨仓 finding，先问「**不看对端，我这半自洽吗**」。自洽才是协调项；不自洽就是自己的缺陷，
只是症状在边界上才被看见。两个方向合起来才完整——C14 正文管「别把别人的份额写进自己的验收」，
本段管「别因为标题提到了别人，就把自己的那半也推出去」。

<!-- hash: 2a752d4d4aba -->

---

## C13 构建产物不是仓库事实 — 未跟踪的本机 .so 被当成上游「物理约束」 (2026-09)

- **事件**: Plan 01 的 D2 写「3.12 → 3.13，物理约束，非偏好：fork 唯一编译产物是 `_libnautilus.cpython-313-darwin.so`」，据此改 `.python-version`、改 `tech-stack.md`「固定 3.12」、放宽 toolkit 的 `requires-python`，并写了一条失败模式测试「3.12 解释器安装须 fail closed 且错误指向 cp313」。审计实读：`git -C <fork> ls-files 'python/nautilus_trader/_libnautilus*'` 返回 0 条；fork `Makefile:327` `maturin develop --release` 在本机 venv 下生成它，`:404` clean 会删；fork `python/pyproject.toml:25` `requires-python = ">=3.12,<3.15"`。三处偏离建立在空前提上，那条测试断言的失败在 path 源下根本不会发生。
- **根因**: 把本机观察到的构建产物当成上游的约束。那份 .so 是 maturin develop 在本机 venv 下生成的、未被 git 跟踪、clean 目标会删，而 fork 自己声明 requires-python >=3.12。起草者看到目录里只有一个 cp313 文件就写下「物理约束」，没有问「它被版本控制跟踪吗、谁生成的、生成命令的输入是什么」。
- **教训**: 凡把编译产物、lock、缓存、dist 目录里的东西当作「约束」引用，先 git ls-files 确认它是否被跟踪，再找到生成它的命令；未跟踪的产物只能证明「本机上次这么构建过」，不能证明上游要求如此。
- **预防**:
  - plan「契约证据」表里凡锚点指向 `*.so` / `*.dylib` / `dist/` / lock 文件，锚点内容列必须写明 `tracked` 或 `untracked（由 <命令> 生成）`，untracked 的不得作为决策依据。
  - 以「物理约束」「唯一产物」措辞给出的决策，审查时固定问一句：约束方自己声明的范围是什么（`requires-python` / `Cargo.toml` / CI matrix），与观察到的产物是否一致。
  - 与 #9/#11「不信推理信实证」同族，但这次实证了错的对象：读的是产物，该读的是产物的来源。
- **Binding**: Plan 01 修订版 D2 撤回 + 契约证据表「fork 编译产物」行改述为「本机构建产物，非约束」（commit `a47bdbd`）；审查原文 `.forge/reviews/2026-09/01-nautilus-trader-2-0-upgrade-review.md` H1。forge `planning/SKILL.md` Step 1.5 的产物类锚点体例待加（forge 仓）。

<!-- hash: 81592b3554c5 -->

---

## C12 清理是被调度的 callback 时, "记录还在"与"东西还在"是两件事 — registry membership 不是 liveness (2026-07)

- **事件**: Plan 26 修的正是「跨一个边界后仍相信旧记录」: `container_id` 描述引擎的附着, 而附着
  随进程消失, 于是重启后新 generation 被当成结构性重配拒掉、同一 generation 不调引擎就报 healthy。
  修法是给 `OfflineEngine` 加 `attached()`, 让分派问引擎而不是读记录。计划把语义写得很清楚 ——
  `_active_nodes` 持的是活的 `(node, task)`, 即「**真的有个节点在跑**」。我照它选对了 dict, 写成
  `deployment_instance_id in self._active_nodes`。**codex peer review 指出这答不了「在跑」**:
  registry 由 `add_done_callback` 清理, 而 callback 是**被调度**、不是立即执行。实测确认窗口真实
  存在 —— 让 run 循环自行结束后只 `await asyncio.sleep(0)`, entry 仍在 registry 里, 而
  `attached()` 已经答 True。
- **根因**: 与本 plan 修的是**同一个错误, 只是尺度不同** —— plan 修的是「跨进程边界后仍信旧记录」,
  这条是「跨一次 loop 调度后仍信旧 registry」。两次都是把**代表过去的东西**当成**现在的答案**。
  membership 的含义是「曾经注册过、且还没被清理」; liveness 是「现在还在跑」。只有当清理是**同步**
  发生时两者才相等, 而本仓的清理不是同步的。我把计划那句话读了一半就动手: `entry is not None`
  表达得了「有记录」, 表达不了「活着」。
- **教训**: 凡 registry 配**异步清理**(done-callback / weakref / 定时清扫 / 另一个 task 负责摘除),
  **membership 不是 liveness**。要回答「它还活着吗」, 就去问那个活着的东西本身 —— task 的
  `done()`、进程的 `poll()`、连接的状态 —— 不要问记录它的那个字典。
- **预防**:
  - 写「X 还在吗」这类查询时, 先问一句: **谁负责把 X 从这里摘掉, 什么时候摘?** 答案里出现
    「callback」「稍后」「另一个 task」, membership 就不够。
  - 这类窗口**可以被确定性地测出来**, 不必靠时序碰运气: 让被观察对象在**恰好一次**
    `await asyncio.sleep(0)` 内完成, 此时 callback 尚未运行。测试里**先断言记录还在**(证明窗口真
    存在、不是 callback 恰好晚跑), 再断言查询答 False —— 否则这条测试可能是绿在别的原因上(C9/#28
    同型)。
  - 收紧了这类查询之后, **回头看还有谁在用同一个 dict 做同类判断** —— 本次 `deploy()` 的幂等守卫
    (`host.py:296`)仍按原始 membership 拒绝, 与收紧后的 `attached()` 口径不一。不一致本身可以是
    有意的(那处放宽等于允许静默顶掉一个从未 dispose 的节点), 但**必须被写下来**, 否则下一个读者
    会以为是漏改。
- **同批的第二条 (计划里"这两个现在等价"是可检验的断言, 不是旁注)**: 同一份 plan 的 Task 2 写着
  `_active_nodes` 与 `_lifecycle_authorities`「当前同生同死, 所以查错了今天也看不出来」。**实测不
  成立**: `stop()` 两个都摘, 但 `_on_node_task_done`(`host.py:831-839`)只摘前者 —— 节点自己结束时
  两者立刻分岔。把实现改指 `_lifecycle_authorities` 会让一条测试变红(已实跑)。**判据**: plan 里
  凡出现「A 与 B 当前等价 / 选哪个都一样 / 今天看不出差别」, 那是一个**可以扰动验证的断言**, 应当
  在实施时跑一次, 把结论写成「我扰动过, 会红/不会红」, 而不是留一句凭印象的旁注。写「看不出来」
  的代价是: 真有差别时, 没有人会去看。
- **与 C9 的区分**: C9 是「fail-closed 只围着 `except` 写, 挂起不抛异常所以没人设防」; 本条是
  「查询只看记录, 而记录的清理是异步的」。两者都是**防线守着一个比现实窄的定义**, C9 窄在「坏 =
  抛异常」, 本条窄在「在 = 有记录」。
- **Binding**: `NtTradingNodeHost.attached`(`src/custos/engines/nautilus/host.py`)查 entry 且查
  `not task.done()`; 探针 `test_a_finished_node_is_not_held_before_its_callback_runs`
  (改回 membership-only 会红, 已证伪)。第二条的证据与登记见
  `.forge/plans/2026-07/26-attachment-state-outlives-the-engine.md` 偏离日志
  `DEV-26-TWO-REGISTRIES-DO-NOT-DIE-TOGETHER`, 审查原文与分诊见
  `.forge/reviews/2026-07/codex/26-attachment-state-outlives-the-engine-peer-review.md` 与
  `.forge/fixes/2026-07/26-attachment-state-outlives-the-engine-fixes.md`。

---

## C11 外部系统的约束要在**跟它说话的那道边界**上强制, 不在值被构造的地方 (2026-07)

- **事件**: 离线通道首次真连 Binance testnet, 每一单都被 `-4015` 拒 —— client order id
  44 字符, venue 上限「小于 36」。修法把 id 改成 32 字符的无连字符 UUID, 落在
  `build_nautilus_base_config`(策略 config 的装配处), 六条测试全绿。**codex peer review
  一句戳破**: 这是**约定**, 不是不变量 —— 只有走那个 builder 才成立, 而
  `runtime_loader.py:81-82` 直接采信签名 artifact 的 adapter 自己 `build_config` 的产物,
  不校验; 策略显式传 `client_order_id` 时更是完全绕过生成器。两条路都能让每一单重新被拒,
  而关于 builder 的测试**一条都不会红**。
- **根因**: 约束来自**外部系统**(venue), 但被落在**值的生产者**那一侧。生产者是可插拔的
  —— 少一个、换一个、绕一个, 约束就不在了。而 id 形态**只能**在 config 决定
  (`strategy.pxd:82,84` 两个 flag 是 `cdef readonly`, `OrderFactory` 在
  `Strategy.register()` 里建), 于是"落在唯一能决定它的地方"看起来还很正当 —— **能决定它的
  唯一地点, 不等于该强制它的地点**。
- **教训**: 凡约束由外部系统定义(venue 上限 / 交易所字符集 / 对端 schema / 第三方 API 配额),
  强制点应在**本仓与该系统对话的那道边界**, 而不是在值被算出来的地方。边界处的强制与"谁构造
  了这个值"无关, 这正是它成为不变量的原因。生产者侧仍然要改对(否则每单都在边界被本地拒),
  但那是**默认值**, 不是**保证**。
- **预防**:
  - 修这类 bug 时先问: **除了我改的这条路, 还有几条路能产出这个值?** 本次答案是两条, 而且
    其中一条是签名 artifact —— 它的产物本该更可信, 反而更不该被免检。
  - 边界处强制要用**本地拒绝**而不是让对端拒: 少一次往返, 且理由可以是自己的
    (`custos_runner_client_order_id_too_long_for_venue`), 不必去解读对端的错误码。
  - 组合单(order list)一腿不合规就整单拒 —— 对端只会拒那一腿, 剩下的成了没人要的半个结构。
  - 与生态 #22/#28 同族(承诺要多层 fail-fast, 不靠任一层自觉), 本条是它在**外部契约**上的
    形态, 并补一条判据: **层的选择不是"越多越好", 而是"选与约束来源对话的那一层"**。
- **同批的第二条 (量了 A, 却断言了关于 A 的 B)**: 同一次审查还抓出我一处事实错误, 而它不是
  没实证 —— 我**确实**量过 `set_client_order_id_count` 的上界(`2**31 - 1`, 靠推到拒绝为止),
  然后写下"所以这是能渲染的最宽 counter, 十位"。错在 generate **先自增再渲染**: set 到上界后
  溢出成 `-2147483648`, 渲染成**十一**字符, 比最大正值更宽。**量了上界, 断言的却是上界的
  推论**, 而推论没量。这是 #9/#11「不信推理信实证」的一个更细的形态 —— 实证与断言之间还差
  一步时, 那一步同样要跑一遍。判据: 断言里出现"所以 / 即 / 因此"就停下来, 把结论本身也量一次。
- **同批的第三条 (这个 bug 为什么能活到今天)**: 本仓 grep `4015` 与 36 字符处理**零命中**,
  sandbox 在本地撮合、**从不向交易所提交**, 数据客户端在 sandbox 还不认证。所以"测试全绿 +
  sandbox 全绿"与"零个订单被交易所接受过"完全可以同时成立。C7/C10 说的是同一件事, 本次是它
  在**交易所对接**上最贵的一次实例。**判据**: 一条通道的完成判据必须是**对端接受过**, 不是
  本仓绿 —— Plan 25 因此不标 ✅, 停在 ⏳ 等真机证据。
- **Binding**: `RunnerSafetyExecutionDispatch._client_order_id_too_long` +
  `venue_binance.BINANCE_CLIENT_ORDER_ID_LEN_LIMIT`; 三条边界测试见
  `tests/engines/nautilus/test_runner_safety_execution_boundary.py`(把守卫改 `return False`
  会红两条, 已证伪)。审查原文与分诊:
  `.forge/reviews/2026-07/codex/25-binance-client-order-id-length-peer-review.md` 与
  `.forge/fixes/2026-07/25-binance-client-order-id-length-fixes.md`。

---

## C10 批量改写源码时, 正则的作用域必须是语法结构而不是行 — 一半损坏能通过语法检查 (2026-07)

- **事件**: Plan 24 把 84 个测试文件从另一个仓库搬进来, 收尾要剥掉 65 处内部追踪号
  (`plan 36 T8` / `lesson #21` 之类, 本仓读者无从查起)。我对**裸行**跑了一组正则, 其中
  两条是灾难: `\(\s*\)` 用来清"删引用后空掉的括号", 实际把 `f.is_ready()` 改成了属性
  访问; `\s+\)` 用来收尾空白, 实际压掉了缩进的收尾括号。9 个文件语法错**能看见**,
  更多文件是**能解析但语义已变**——而那些正是断言辅助调用, 测试照样"通过"。
- **第二次尝试也错了**: 我随后改用 tokenize + AST 限定"只碰 COMMENT token 与 docstring
  所在的行"。**仍然出错**——带行尾注释的**代码行**整行合格, 于是
  `assert_called_once()  # lesson #15` 又被改坏一次。散文修复同样不可机械化: 机械版
  留下了 `deleted in .` / `block 2c :` / `(now live via )` 这类断句。
- **根因**: "行"不是语法单位。一行可以同时是代码和注释, 而清理性的正则(补空括号、
  收空白)天生不区分它落在哪半。把作用域从"行"收窄到"注释 token 所在的行"只是把
  错误变罕见, 没有消除它——**唯一正确的作用域是 token 或 AST 节点本身**。
- **恢复**: 已提交的文件用 `git show HEAD:<path>` 写回(不用 `git checkout --`, 护栏拦
  得对); 未提交的从源仓重取 + 按序重放, 重放脚本每步**先断言再改**。恢复后测试数与
  损坏前逐条一致。事后又做了一道独立证据: 剥掉 docstring 与全部字符串常量后比 AST,
  差异集正好等于声明的改动集(47/60 文件逻辑逐字节等同源仓)——重放若漏了或多了什么,
  这个等式不会成立。
- **预防**:
  - 批量改源码用**逐条手写替换**, 且替换器要求**整行唯一匹配**(匹配数 != 1 就拒绝,
    不猜) + 写前 `ast.parse`。只用行号做键不够: 过时的行号会静默改错行。
  - 需要机械化时, 作用域只能是 token / AST 节点, 不能是行; 且**改完必须看 diff**,
    不能只看 pytest ——静默那半就是靠"测试还是绿的"活下来的。
  - 与 C7 同族: 绿色由两个同源错误互相印证得出。这次是"被改坏的断言辅助 + 仍然通过的
    测试", C7 是"陈旧矩阵 + 同龄陈旧镜像"。
- **同批的第二条 (「全绿」不含「有没有在跑」)**: 收尾时 `pytest` 报 `1308 passed`, 而
  `pytest --collect-only` 显示两个刚翻译完的文件**一条都不跑**——一个卡在
  `importorskip("pandas_ta")`(上游包, 本仓正是把它 vendor 进来才不依赖), 一个要
  `redis` + 活服务(本仓无 redis 依赖, 走 NATS)。46 条断言差一步就永久沉默。**收尾必看
  collect 计数, 不只看 passed。** 与 C8 同向: 那次是删掉面时连测试一起删所以没人变红,
  这次是搬进来的测试从未被执行。
- **同批的第三条 (rename 让断言里的字面量静默失效)**: 七处 layering 守卫查
  `"shared.filters"` / `"shared.nautilus.snapshot"`——那个包在本仓
  `find_spec('shared')` 为 `None`, 完全不存在, 守卫因此**永不失败**。上游改了 import,
  没改断言里的字符串。修法是重指到真包名, 并**注入一条违规导入证明它会咬**。这是
  lesson #35(boundary constant rename fanout)的一个未列形态: 改名清单要含**断言里的
  包名字面量**, 它不在 import 语句里, grep import 找不到。
- **Binding**: 恢复与核实的探针留在 plan 24 的 close-out 与
  `.forge/reviews/2026-07/24-adopt-the-toolkit-test-suite-review.md`(含结构对比表);
  计数侧的机械防护是 `tests/test_plan_closeout_counts.py`(本轮扩为 profile-aware,
  被 collect 期跳过的文件**点名报告为未核验**而非静默豁免, 豁免本身由
  `test_the_probe_tells_a_skipped_file_apart_from_an_uncollectable_one` 证伪)。
- **dogfood (2026-09-20, fix 19)**: 本条明写「替换器要求**整行唯一匹配**(匹配数 != 1 就拒绝,
  不猜)」，我仍然中了——这次不是正则，是**按索引**的批量缩进脚本：用 `next(i for i, l in ...)`
  取**第一个**匹配的锚点行（`            if not instrument:`），而那行在文件里有两处，第一处在
  另一个方法里。脚本于是从错误的位置起手，把 228 行整体去掉 4 空格（预期只有 25 行），
  `_on_order_initialized` 的 try 体被拆散，`ast.parse` 当场报 `expected 'except' or 'finally'`。
  **`next(...)` 是「第一个」不是「唯一一个」**——它和正则的 `.*?` 一样，会安静地跨越几百行匹配到
  别处。修法与 C10 原文同：先断言匹配数为 1 再动手，改完 `ast.parse` 验证。本次靠 `git diff --stat`
  与逐 hunk 核对确认修复范围没有外溢。

---

## C9 fail-closed 守的是"错了", 不是"不吭声" — 没有异常可抓的那条路上没人设防 (2026-07)

- **事件**: `EngineSafetySupervisor` 是既有组件、已被审过、对 `get_engine_status` **抛异常**
  的情形处理得很干净(`except Exception` → `fail_closed` → flatten)。Plan 22 把它接进离线通道
  后, 审查 M4 发现它对"引擎收下问题却永不返回"完全没有防线 —— 因为没有异常可抓, 而整段防御
  是围着 `except` 写的。后果不止 tick 停摆: `_run_together` 的 `finally` 里那次
  `asyncio.wait(tasks)` 也永远等不回来, 连关停都出不去。
- **根因**: fail-closed 的设计直觉是"出错就按最坏情况处理", 而"出错"在代码里的形状是异常。
  **挂起不产生异常, 于是它落在防御的视野之外**。审查时我自己第一遍也判它"出范围"(理由是
  `ZombieWatchdog` 才是指定负责人), 复看才翻过来 —— 指定负责人没接线时, "归它管"等于没人管
  (grep 实证: `ZombieWatchdog` 全仓零接线)。
- **教训**: 任何 fail-closed 的守卫都要被问一句 —— **不返回算不算一种坏法**。对外部进程 /
  引擎 / IO 的每一次 await, 只要它挂住会让守卫本身停摆, 就必须有期限; 超时按"读不出"处理并
  fail closed, 但记录必须说清 **containment 没有被确认**, 不能写得像已经兜住了。
- **预防**:
  - 期限加在**整次评估**上, 不是加在单个调用上 —— 挂在 `flatten` 上和挂在 `get_status` 上
    都得能退出
  - 超时后**不做第二次尝试**: 挂住的引擎不会在第二次调用时回答, 堆重试只会新增挂起路径
  - 超时用独立事件名, 不与"已 flatten"共用一个 —— 否则日志读起来像containment 成功了
  - "指定负责人在别的 plan 里"不是不设防的理由; 先 grep 那个负责人是否真的接了线
- **Binding**: `src/custos/offline/safety.py` 的 `_evaluate_within_deadline` +
  `EVALUATION_DEADLINE_SECS`; 测试 `test_an_engine_that_never_answers_fails_closed` /
  `test_a_wedged_engine_is_recorded_as_containment_not_confirmed` /
  `test_the_tick_ends_against_an_engine_that_never_answers`。
  **期限在离线通道的 wrapper 里, 不在 `core/engine_safety.py`** —— `EngineSafetySupervisor`
  本身仍无超时(grep 实证 0 处), 它现在唯一的生产消费者就是这个 wrapper; 签名通道将来接它时
  会原样继承这个缺口。

---

## C8 删掉一个面时连同它的测试一起删, 就没有任何东西会变红 — 消费者在别的仓库时更看不见 (2026-07)

- **事件**: `deployment publish` / `nats bootstrap` / 未签名 reconcile 环在 `324da6e`
  (2026-07-14) 随"consume crucible deployment authority"重构一并删除, 残余的
  `deployment validate` 与 spec schema 在 `8c4454f` (2026-07-21) 扫尾。两次删除都把
  **覆盖它们的测试放在同一个 commit 里删掉**, 所以两次都没有任何测试变红。它有一个活着
  的消费者 —— 另一个仓库的离线 harness —— 而那个仓库在一周后 (2026-07-28) 才刚把这条
  lane 写成规范, 完全没注意到它已经断了。起 Plan 21 时的一手判断("`8c4454f` 删了两个
  命令")在实测下崩了两次: 真正的断点是 `324da6e`, 断裂面是**五处不是两处**。
- **根因**: 删除一段代码时, 覆盖它的测试是"同一次改动的一部分", 顺手一起删是最自然的
  动作 —— 而这恰好消灭了唯一会报警的东西。跨仓库消费者让这件事更隐蔽: 本仓 CI 全绿,
  坏掉的是别人的 harness, 而那个仓库不跑本仓的测试。
- **教训**: 判断"某个面还有没有人用"不能只看本仓。删除**任何**有外部调用者的 CLI 命令 /
  subject / schema 前, 先 grep 消费仓库的调用点; 删除时若同时删测试, 必须在 commit
  message 里说明"这个面为什么不再需要被覆盖", 否则等于静默摘掉警报。
- **预防**:
  - 对外可调用的面 (CLI 子命令 / subject / 公开 schema) 的存在性断言, **从 parser /
    router 推导**并在**独立于实现的测试文件**里维护, 这样删实现不会顺手删掉断言 (与 C7
    同一条: 清单不硬编码、不与被测对象同期陈旧)。
  - 断言的 docstring 里**写清消费者是谁、删了会坏什么** —— 下一个做收敛重构的人在测试
    变红时就能读到, 而不是在别的仓库的 CI 里读到。
  - 跨仓库消费的面在 `.forge/README.md` 索引里标注 Blocks 指向消费方, 让"谁在用"这件事
    留在会被读到的地方。
- **同批的第二条 (CEO override 记录路径)**: 恢复这条 lane 需要修改
  `mandatory-rules.md` §Trust —— 它此前不允许**任何 mode** 接收未签名 desired state。
  CEO wukai 2026-07-29 决定修权威层而非绕过它, 边界画在 **live**(红线自己画的地方:
  §Trust 不可让步的一句是 live 无签名晋升证据即 fail closed, 红线 0.2 也只把 live 锁死
  在 G6 之后)。四件套齐: 本条 lesson + Plan 21 偏离日志 `DEV-21-AUTHORITY-AMENDMENT` +
  `.forge/README.md` Offline lane note + `authority-manifest.json` `offline_lane` 机械门。
  与 C1 同型 (custos 独立仓形态的 lesson #38 记录路径), 本条是第二个先例。
- **同批的第三条 (close-out 数字必须来自实跑)**: 本 plan 的 close-out 写"新增 91 个测试",
  实际 117; 自列分项之和 108 与自报总数也不自洽; 整组漏掉了 8 个权威门测试。**一句话里错
  三处, 没有一处需要说谎, 只需要不数就写**。审计以 C3 记之。这是生态 lesson #25 在 custos
  内首次复发, 形态与它相同 (agent 在 close-out 里写未经计数的数字), 因此不另开条目, 在此
  合并记录。**防护**: close-out 改为逐文件表格, 由 `tests/test_plan_closeout_counts.py`
  向 pytest 实际 collect 计数核对, 并拒绝任何该表支撑不了的合计数; 探针本身经扰动验证会红。
- **Binding**: `tests/test_gateway_contract_v1_samples.py` 的 offline lane 面存在性断言
  (走公开入口 `main([... , "--help"])` 退出码, 含必然不存在的负对照, docstring 点名消费者)
  + `scripts/check-authority-docs.py` `verify_offline_lane`
  + `.claude/rules/mandatory-rules.md` §Trust 离线通道段
  + `tests/test_plan_closeout_counts.py` 与 `.claude/rules/progress-management.md`
  §"数字类声明必须来自实跑"。

---

## C7 硬编码矩阵 + 同期陈旧产物 = 自洽的假绿; 从未跑过的流水线, 形状测试再多也不是验证 (2026-07)

- **事件**: 核对一页升级文档的声明时, 连带挖出**三处发布链缺陷**, 都活了两周以上:
  1. `verify-release.sh` 对已发布镜像跑 `deployment validate --help`。该子命令在
     "deployment authority 上移" 那次重构里被删了, 现在退出码 2, 而脚本在 `set -e` 下
     —— 稳定 tag **已经公开之后**才中止。同时 `credential` / `nats-transport` /
     `publish-capability` 三个真实命令从未被探测。
  2. `test_docker_runtime_contract.py` 的硬编码矩阵要求镜像暴露 `nats bootstrap` 与
     `deployment publish`, **而且它是绿的** —— 本地 `custos-runner:test` 构建于
     `536983931699` (2026-07-13), 早于删掉这两个命令的重构。陈旧的清单和陈旧的镜像
     互相印证。
  3. `SOURCE_DATE_EPOCH` 绑到 `github.event.head_commit.timestamp`, 那是 ISO 8601;
     hatchling 对它调 `int()`。**实测**: `ValueError: invalid literal for int() with
     base 10: '2026-07-12T10:33:00+00:00'` —— 首个 job 必崩, 整条发布从来跑不起来。
     `workflow_dispatch` 下更隐蔽: 没有 push payload, 表达式静默求值为空字符串,
     构建照常成功但不再可复现。
- **根因**: `git tag` = **0** —— 这条流水线从未执行过。所有测试断言的是它的**形状**
  (文本片段、步骤先后), 不是它的**行为**; 而形状测试与被测对象同期编写、同期陈旧,
  于是彼此一致、全绿。硬编码矩阵是第二个放大器: 清单与产物同龄时, 漂移不产生任何症状。
  绿色来自两个同源错误互相印证, 而不是来自与真相比对。
- **预防**:
  - **命令矩阵 / 子命令清单 / 端点清单类断言一律从权威源推导**(parser / router /
    schema), 不硬编码。硬编码清单唯一能证明的是"有人曾经这么写过"。
  - **针对产物的契约测试必须先校验产物身份**: 比对 `org.opencontainers.image.revision`
    与 HEAD, 不匹配就跳过并**指名 revision**。对陈旧产物跑出来的绿是无意义的绿, 且比
    红更危险 —— 它看起来像证据。
  - **env 注入值若有格式契约就要有断言**: `SOURCE_DATE_EPOCH` 是整数秒, OCI
    `image.created` 是 RFC 3339 —— 同一个时间戳在两处格式相反, 照搬必错。优先在 run
    块里推导, 且**用与文档教给审计者的同一条命令**推导, 顺带消灭文档与 CI 的分歧。
  - **GitHub 表达式在事件不匹配时静默求值为空字符串**, 不报错。凡 `${{ github.event.* }}`
    注入的关键值, 都要问一句"换一种触发方式时它是什么"。
  - **从未跑过的流水线 = 未验证**。形状测试必要但不充分; 首次发布前应在 fork 或预发
    tag 上真跑一次完整 workflow。
  - **文档核对是发现流水线漂移的有效入口** —— 本次三处缺陷全部由"核对升级文档的一句
    声明"连带发现。文档和流水线引用的是同一批命令名, 文档有人读, 流水线没人跑。
- **与 C3 / C4 区分**: C3 是发布链上**把步骤顺序当身份**(shape gate ≠ artifact
  identity); C4 是 **mock 结果 + 绕过 public surface** 的双重假绿; 本条是**硬编码清单
  与同龄陈旧产物互证** + **形状测试断言一个从未运行过的流水线**。三者同一个家族:
  绿色由两个同源错误互相印证得出, 没有任何一处与权威源比对过。
- **Binding**: `tests/test_release_workflow_shape.py::test_post_publish_command_matrix_matches_the_real_cli`
  与 `::test_source_date_epoch_is_an_integer_derivation`;
  `tests/test_docker_runtime_contract.py` 的 `_command_matrix()` (从 parser 推导) 与
  `_require_image()` revision guard; 文档侧对应探针
  `tests/test_examples_cli_commands_are_real.py::test_documentation_names_only_real_subcommands`;
  `.claude/rules/verification.md` §Release gate assertions。2026-07-28 首次 dogfood。

---

## C6 签名资产把源文件冻结成契约 — 措辞级改动也会毁证据链 (2026-07)

- **事件**: 为把内部系统名从 public 仓库里收敛掉, 对 `src/` 做纯措辞改名
  (docstring / 错误消息, 不含任何 wire 标识)。22 个文件里 16 个被
  `docs/authority/**` 的资产索引按 **path + size_bytes + commit** pin 住。改一句
  docstring 即触发 `test_machine_request_consumer_assets_are_exactly_pinned`
  失败 (`assert 26599 == 26594`) —— 一个**字节大小**断言。回退 15 个 pinned 文件后
  恢复基线。
- **根因**: "源文件" 与 "签名证据" 在这些路径上是同一份东西。资产索引记录的是字节,
  不是语义, 所以 formatter 换行、typo 修正、注释改写与逻辑改动**在 pin 面前等价**。
  改动者按 "这只是措辞" 判断风险, 而 pin 按字节判断。两者永远不会一致。
- **同源副作用**: pin 把文件冻结在 formatter 之外。`src/custos/core/runner_fact.py`
  与 2 个 pinned integration test 当前不是 format-clean, 使 `make fmt-check`
  (进而 `make verify`) 在主干上恒红。这不是疏忽, 是两个约束互斥的必然结果 ——
  跑 `make fmt` 会修好 gate 但破坏 pin。
- **预防**:
  - 动 `src/` 前先查是否被 pin:
    `grep -rho '"src/custos/[^"]*\.py"' docs/authority/ | sort -u`
  - pinned 文件的任何改动 (含纯措辞 / 纯格式) 都必须与 **receipt 重新签发同批**提交,
    不可作为独立的清理 / 格式化 commit
  - 批量改名类操作先做 pinned / free 切分, 只对 free 集合执行; 已误改的用
    `git stash push -- <pinned files>` 隔离而非 `git checkout --` 丢弃 (可恢复,
    且 guardrail 会拦后者)
  - 恒红的 gate 要显式记录成 known-red 并说明互斥原因, 否则下一个人会以为是自己弄坏的,
    或者跑 `make fmt` "顺手修好" 从而破坏 pin
- **与 C3 区分**: C3 是 "pre-publish shape gate 不等于 artifact identity gate"
  (**发布链**上把顺序当身份), 本条是 "源文件同时是签名资产" (**开发期**把措辞当无风险)。
  两者共同点: artifact identity 由字节定义, 不由意图定义。
- **dogfood (2026-07-30, Plan 25)**: 本条明写"跑 `make fmt` 会修好 gate 但破坏 pin", 我仍然
  中了 —— 不是跑了 `make fmt`, 是跑了 `uv run ruff format tests/ src/ packages/` 这种**大范围**
  的等价物, 一次把三个 known-red 的 pinned 文件"修好"了。**教训收窄为: 格式化的范围只给本次
  真正改过的文件**, 不给目录。`git status` 立刻看出来并用 `git show HEAD:<path>` 写回, 但如果
  当时顺手 commit, 破坏的就是证据链而不是格式。
- **Binding**: `.forge/README.md` §后续 plan 规划 "内部系统名对外收敛 — 两项 deferred"
  记录了受影响文件集与解冻条件; `test_machine_request_consumer_assets_are_exactly_pinned`
  是该约束的自动化探针 (2026-07-28 首次 dogfood 命中)。

---

## C5 verbatim migration 把内部文档搬上公网 — 素材源必须按受众选, 且要有机械 gate (2026-07)

- **事件**: 文档站 T5 迁移把 `docs/**.md` 逐字搬进 `docs-site/`, 模式明写 "content
  copied verbatim"。审计发现 406 处内部标识跨 45 章 — 其他生态系统名、私有仓库路径、
  跨服务机制词、内部 plan/DEV/lesson 编号。gh-pages 已于 2026-07-21 发布, 96 页中
  9 页命中, 属**已发生的对外泄漏**而非险些泄漏。
- **根因**: 素材源与受众错配。`docs/**.md` 是内部设计文档, 写作目的是在内部系统之间
  划分职责 — 每句都属实, 而属实正是它不该被发布的内容。"verbatim 迁移"把审阅责任
  隐式转移给了"反正内容是对的"这个前提, 但对外产出物的判据不是**是否属实**, 是
  **是否该对这个受众说**。站点自建站起就没有 disclosure gate, 全靠人记。
- **预防**:
  - 对外产出物的素材源取自**用户可见的产品面**(CLI / 配置文件 / 可观测事件 / 公开
    API)。内部文档只能用于**核实事实**, 不能用作**叙事骨架**。
  - 站点必须有 disclosure gate 且置于 CI 构建**之前** — 泄漏不可逆, 构建失败可逆。
    gate 必须扫全文含代码块与 HTML 注释: 内部标识贴进示例 payload 与写在正文等价。
  - gate 需自带回归测试(证伪基线), 否则无法证明它真的会拦。逃生舱
    `disclosure-ok: <理由>` 要过审, 不是静音开关。
  - 纪律写进产出物**自身的 README**, 让下一个写作者在动笔处看到。
  - 章节标题与内容必须匹配 — 本次 "Configuration Reference" 实为贡献者指南,
    读者点进来拿不到期待的东西, 是与泄漏并存的独立缺陷。
- **Binding**: `docs-site/scripts/check-disclosure.mjs` + `test-check-disclosure.mjs`
  (19 用例) + `.github/workflows/docs-deploy.yml` disclosure 前置 +
  `docs-site/README.md` §"This site is customer-facing"。生态原文见
  workspace lesson #42 与 `mandatory-rules.md` §9; custos 是该 lesson 的第二次复发,
  证明"只有 arx/docs-site 有 gate"这个已知缺口会被真实触发。

---

## C4 mock subprocess + 绕过 public surface 会形成双重假绿 (2026-07)

- **事件**: Plan 17 前，`arx-runner vault verify` 的 unit test mock 了合法 JSON stdout，
  但没有断言真实 subprocess argv；standalone integration 又直接调用带正确 JSON flags 的
  底层 `sops`。两层测试同时全绿，却遗漏 public CLI 实际执行
  `sops --decrypt <key-id>.enc`，SOPS 因 `.enc` 后缀误判 binary store，downstream 真实
  Docker smoke 才暴露失败。
- **根因**: mock 只替代结果，没有锁定发给外部进程的 command contract；integration 验证
  了底层能力，却绕过用户实际调用的 public acceptance surface。两个缺口互相遮蔽，形成
  双重假绿。
- **预防**:
  - subprocess mock 必须断言关键 argv、env 与 stdin；对格式、身份文件和 secret transport
    等边界不得只伪造 stdout。
  - integration 必须经过用户公开入口并断言公开结果；CLI 产品面不能用内部 helper 或底层
    binary smoke 替代。
  - 底层工具 smoke 只能作为补充诊断，不能替代 public surface acceptance。
- **Binding**: `tests/test_cli_vault_put_verify.py::test_vault_verify_uses_explicit_json_sops_types_for_enc_suffix`、
  `tests/test_per_key_vault.py::test_cli_verify_and_runtime_share_json_decrypt_command` 与
  `tests/test_cli_vault_put_verify.py` 的 public put → verify roundtrip。
  (原 binding 指向的 `tests/integration/test_standalone_runtime.py` 已随重构删除;
  2026-07-28 核实后改指向现存覆盖 — lesson 的 binding 必须指向仍然存在的防护,
  否则教训看起来已固化, 实际防护已消失。)

---

## C3 pre-publish shape gate 不等于 artifact identity gate (2026-07)

- **事件**: Plan 14 release workflow 在下载 signed wheel 之前运行 `verify-runtime`，随后以
  signed wheel 重新 build/push。文本测试只证明 gate 位于 `push: true` 前，没有证明 gate
  消费的镜像与公开稳定 tag 是同一 digest；post-publish verify 失败时稳定 tag 已暴露。
- **根因**: 测试锁定了步骤相对顺序，却没有锁定 artifact identity。`before publish` 被错误
  等同为 `same artifact`，忽略 build 是产生新身份的边界。
- **预防**: release workflow 必须先构建 SHA-scoped candidate，按 registry digest 运行完整
  runtime gate，再把同一 digest promotion 到稳定 tags。shape test 同时断言 signed input、
  candidate build、digest-targeted gate、stable promotion source，且 gate 后没有 rebuild。
- **Binding**: `.claude/rules/verification.md` §Release artifact identity +
  `tests/test_release_workflow_shape.py`。所有 future release review 必须提供 artifact identity
  gate 证据，不能只提供步骤名或字符串顺序。

---

## C2 输出污染可贯穿 review 与 self-review — self-review 不豁免 (lesson #13 复现在 review 阶段) (2026-07)

- **事件**: Plan 03 execute-team close-out 阶段, safety-validator (opus-4-6[1m]) 8-checklist
  safety review 中 append 到 marker 的 verdict + 2 个 non-blocking follow-up 均为幻觉/冗余:
  - **FU-1**: 声称 `docs/domain.md` phase vocab = `{pending, starting, running, stopping,
    stopped, failed}`, `phase='degraded'` ∈ health vocab 但 ∉ phase vocab → drift
  - **FU-2**: 声称 `test_credential_lifecycle.py` 缺 credential-path canary 正控

  两个 finding 已随 Lead fallback salvage 进入 main report + triage + Plan 05 candidate
  backlog。CEO 事后要求 safety-validator 深度复核 (`git show branch/main` + 实跑 pytest),
  safety-validator **自我发现**两者均是错误:
  - domain.md L104 实际 vocab = `pending/running/degraded/stopped` (**含 degraded**);
    starting/stopping/failed 在 domain.md 命中 0/0/0 次 → **FU-1 是 review 阶段幻觉**
  - `test_credential_lifecycle.py:121-122` 已有 `assert data_cfg.api_key == _SENTINEL_KEY` +
    `assert data_cfg.api_secret == _SENTINEL_SECRET` → **FU-2 冗余**

  safety-validator 主动 escalate 撤销请求, Lead 独立 grep 实证后清理 salvage report +
  triage + marker + Plan 05 candidate backlog。若未清理, Plan 05 起草会基于错误前提
  (为不存在的 drift 起 task / 为已有 canary 加冗余 canary)。

- **根因**:
  1. **lesson #13 (文档内容可注入伪造工具结果) 在 review 阶段的复现变体**: Read 通道
     可返回污染的文件内容; review 阶段推理建立在污染内容之上, 得出错误 finding
  2. **self-review 不豁免**: safety-validator 是自身应用 lesson #13 三重交叉印证
     (git-blob-SHA + AST + grep -c 纯数字) 防御的 role, 但防御应用在**代码路径实证**
     (payload 5 字段 / payload_schema_version=1 / close 不 cancel 等) 而非 **vocab 定义 /
     测试断言存在性** 这类基础事实上, vocab 与 canary 声明未被同等严格核实
  3. **批判框架下的归罪偏置**: safety-validator 在"发现问题"心智下, 对 ambiguous 现象
     倾向归因为 drift / gap, 而非诚实标注"未核实, 需 git show 复查"; safety-validator
     自省"连续制造 FU-1 错觉 + 误判 marker 谎报" — 后者是紧接着的第二个归罪 (误认为
     marker 谎报 phase vocab, 实际是 marker 正确、review 幻觉)

- **教训**: review 的**红线核心依据 + follow-up 起源事实**必须 git show / 实跑击穿,
  self-review 不豁免; vocab / 断言存在性等"文本类事实"与"代码路径行为"同等严格核实;
  批判框架下要主动打断"归因为错"的默认倾向, 用"未核实即标 UNVERIFIED"打底

- **预防**:
  - safety-validator (以及所有 review role) spawn prompt 加"每条 finding 起源必附
    git show / grep 实证锚点; 无实证锚点的 finding 标 UNVERIFIED, 不计入 verdict"
  - Lead fallback salvage 时, 独立 grep 核实 finding 起源事实再决定登记 (fallback 阶段
    不是照单全收 marker 内容, 应对每条 finding 加实证 gate)
  - CEO 深度复核请求可作为默认最后一道 gate: review approved 后再跑一次 git show 核心声明
  - 与 lesson #13 (文档内容伪造) + lesson #37 (spawner 元层实证不豁免) 合并适用: 三者
    共同构成 "实证不豁免" 完整方法论 — #13 是外部输入污染, #37 是 spawn prompt 元层实证,
    #C2 是 review/self-review 阶段实证

- **Binding**: 未来 review role 的 spawn prompt 加"实证锚点强制要求"; Lead fallback
  protocol 加 finding 起源 grep 核实步骤; safety-validator 自我 escalation 撤销 (2026-07-09,
  Plan 03 close-out 后) 已是本 lesson 首次 dogfood 应用 (main HEAD retraction 落地已完成)

---

## C1 CEO override 单 plan 依赖跳过路径 (custos 独立仓形态) — 生态 lesson #38 具体化 (2026-07)

- **事件**: Plan 00c (G6 gate capability + Binance testnet/live) 头部声明 `Depends on: Plan 00a + 00b`, 但 00b (telemetry 桥) 未 close-out。CEO wukai 2026-07-07 经 `/forge:execute-team` AskUserQuestion 显式选择先做 00c (核心 G6 gate/testnet/live 与 00b 遥测桥独立)。属高风险偏离 (跳过声明的 plan 依赖), 走生态 lesson #38 CEO override 记录路径。
- **根因**: plan 依赖声明是保守全序 (00a→00b→00c), 但实际 00c 主干与 00b 正交; CEO 战略判断"先放行 live 通道能力, 遥测观测度后补"。custos 独立仓无 ADR 框架, 需把 override 记录落到本仓内自足载体。
- **预防 / 4 件套 (custos 独立仓形态)**:
  - ① CEO 决定: handoff packet §0 (`.forge/handoff/2026-07/00c-execute-team-packet.md`, gitignore 会话物件, 但决策上下文已复制进本 lesson + plan DEV 条)
  - ② 偏离登记: Plan 00c 偏离日志 `DEV-00c-DEP-SKIP-CEO-OVERRIDE` (高风险条)
  - ③ 权威文档: custos 无 ADR → 落 `.forge/README.md` 索引 00c 行 `Depends on` 脚注 ¹ (生态 lesson #38 用 ADR revision, custos 用 plan 索引脚注等效)
  - ④ 本 C1 lesson (先例记录)
  - 四件套齐 = 与 Council/ADR 等效的决策留痕, 非静默 override。后果诚实声明: e2e 观测面部分启用 (00b 未落地, testnet 真跑 fill/OrderDenied 只本地 structlog)。
- **未来同型 (custos 内 plan 依赖跳过)**: 先看四件套 (CEO 决定 + DEV 条 + `.forge/README.md` 脚注 + 本文件 Cx lesson) 是否齐, 齐则批准, 缺则回补。

**Binding**: 生态 `deviation-protocol.md` CEO override 例外路径 (lesson #38) 在 custos 独立仓的等效落点 = plan DEV 条 + `.forge/README.md` 索引脚注 + 本 C1。

## #9/#11/#18/#37 「不信推理信实证」— 全场景适用

- **触发**: fix / review / 起 plan / spawn prompt / SendMessage / 编辑权威 spec 时
  引用代码符号 (enum 变体 / struct 字段 / fn 签名 / 表名 / API 字段) 未 grep 实证
- **防护**: 编辑前必 grep 实证一次, 尤其对称语义 (`create ↔ delete` / `on ↔ off` / `tripped
  ↔ restored`) 不豁免, 双向 grep
- **custos 特化**: NT lifecycle 方法名 (`start` / `stop` / `dispose` / `wait_for_state`)
  / NATS subject naming / Pydantic model 字段名 编辑前必 grep 源定义

## #14/#30/#33/#33b Foundation Scan Gate — 四维方法论

- **触发**: 起 plan / 起 fix / spawn agent
- **防护**: 起草前系统扫骨架 (空间维 #14) + grep migrations DDL (命名空间维 #30) +
  上游 plan close-out 后 as-of 时间锚 (时间维 #33) + 影响面多轮迭代 (层次维 #33b)
- **custos 特化**: 6 模块骨架小 (`ls src/arx_runner/`) + wire fixture 现状扫
  (`ls tests/test_wire_*.py`) + 上游 arx Plan 60 subtree split 影响的现状 as-of 时间锚

## #17 happy-path 测试全绿 ≠ 失败模式覆盖

- **触发**: 起 plan / TDD 实现
- **防护**: 起 plan 声明失败模式覆盖契约 (NATS down / vault_locked / g6 gate deny /
  wire schema drift / async task 异常 silent drop / Decimal 精度丢失)
- **custos 特化**: 已有 `test_telemetry_actor_failure_modes.py` / `test_nats_wal_resilience.py`
  实践该原则; 新增模块须并行加 `test_*_failure_modes.py`

## #21 零静默红线 — silent 路径必接 structlog

- **触发**: 写 try/except / fire-and-forget / drop policy / WAL 暂存 / queue overflow
- **防护**: silent 控制流必须 `structlog.get_logger().warning("<event_name>", **context)`,
  否则加 `# noqa: SILENT-OK <reason>` 注明 fail-safe 理由
- **custos 特化**: telemetry_actor / nats_client 全数覆盖 (对账不静默 = non-custodial 承重墙
  可观测性)

## #22/#28 多层 fail-fast + 独立可测

- **触发**: 设计红线 / 承重墙 / 安全承诺
- **防护**: 多层防御 (config / connection / repository / DDL / SQL where) + 每层独立
  可测 (relaxed-double test 证明 inner layer 不是 dead branch)
- **custos 特化**:
  - Non-Custodial 红线 0.1 (Key 不出进程) 多层守: telemetry_actor 白名单 + structlog
    processor 脱敏 + envelope schema 只允许公开字段
  - G6 gate (红线 0.2) 多层守: `nautilus_host.start()` gate + `LIVE_MODE` env + `paper_only`
    reconciler 默认

## #25 反 fabricated close-out — 契约表测试名必 grep 实存

- **触发**: close-out 报告 / 契约表 / 验证清单
- **防护**: 契约表点名的 `test_*` 函数必须 `grep -rn 'def test_X' tests/` 实证真存在;
  数字统计对齐
- **custos 特化**: close-out 前跑 `pytest --collect-only tests/` 对比契约表

## #26 `pub String` boundary / boundary constant 校验

- **触发**: 边界字段 (fs path / NATS subject / SQL string interp / cookie / env var / storage key)
- **防护**: smart constructor 收口 invariant; 边界裸用前 `validate_*_for_<sink>` 拦截
- **custos 特化**:
  - `TenantId` / `RunnerId` / `StrategyId` 不裸 str 拼 NATS subject
  - `nats.subject` 构造用 `build_subject(tenant, kind, *parts)` 函数收口 (参考
    `test_subject_builder_contract.py`), 拼接前对每个 part 校验字符集/长度

## #27 commit scope discipline — 前必 `git status --short`

- **触发**: commit 前 (含 fix / execute / bootstrap 等各种 stage)
- **防护**: `git add <specific-file>` (禁 `.` / `-A`); commit 前 `git status --short` 核对
  staged 范围, pre-staged 污染即 `git restore --staged` 退出
- **custos 特化**: 独立仓库虽然无跨仓库 add 风险, 但 workspace 场景内改 custos + arx 双仓
  时同样适用; hooks 自动 stage 也可能污染, commit 前核对是双保险

## #29 校验类操作不覆盖 host

- **触发**: 建 config 文件 / 跑 dry-run 校验 / 生成参考 fixture
- **防护**: 用 `/tmp/` 临时路径 + `[ -f <path> ] || cp` 防御性 cp + 不覆盖不 rm 用户真实文件
- **custos 特化**: `credential_vault` test 用 `mktemp -d` fixture, 绝不碰用户真实
  `~/.custos/vault/`

## #34 teammate 收 pre-merge 指令需先 git log 核实

- **触发**: 多 session 编排 / worktree merge 后收到旧 context 指令
- **防护**: 收到关键指令前 `git log -1` + `git worktree list` 核实当前仓库状态, 状态变化
  即上报
- **custos 特化**: 独立仓库单人开发场景少, 但 workspace 场景内多 agent 并行改 custos +
  其他子系统时适用

## #35 boundary constant rename fanout

- **触发**: storage key / cookie name / env var / NATS subject prefix / pip 分发名 / Python
  module 名改名
- **防护**: 起草 rename plan 时 grep 全仓消费者, 显式列改名清单; zustand-类持久化改名
  需外加显式 `removeItem(oldKey)`
- **custos 特化**: Python module 名 `arx_runner` → `custos_runner` (README 已声明 follow-up)
  必须走此协议, 涉及 40+ import site fanout
- **改名清单必须含断言里的包名字面量** (2026-07 实测复发, 见 C10 同批第三条): 七处
  layering 守卫查 `"shared.filters"` 字面量, 而那个包改名后在本仓已不存在, 守卫**永不
  失败**。上游改了 import 语句、没改断言里的字符串, 而 grep import 找不到这类消费者。
  重指后必须**注入一条违规**证明它会咬, 否则只是把一个空转换成另一个。

## #40 含 defer 决策的红线 gate close-out 声明必须显式降级 partial scope — code test 覆盖 ≠ runtime wire 兑现

- **触发**: plan close-out 涉及红线 gate (mandatory-rules §0) 且 plan 内含 defer 决策 (DEV-* 记录)
- **防护**: close-out 声明必须**显式区分三层** —
  (a) **code-level test coverage** (unit / integration 覆盖了什么逻辑) /
  (b) **runtime wire 接线兑现** (composition root 是否真接线) /
  (c) **defer scope** (哪些接线延后到 follow-up plan)。
  不能承袭红线名 (如"Key 不出进程" / "G6 不绕过") 当兑现声明 — 红线名是设计意图 (vision),
  兑现声明是能力实现 (reality), 两者严禁混淆
- **custos 特化**: plan 模板 "完成报告" 章节固定含 "红线 gate 满足度" 表 —
  每条红线一行: `red_line | code_coverage | runtime_wire | defer_status | follow_up_plan_ref`。
  Plan 03 是本 lesson 落地**模板样本** (`FailureEvent.reason_code` 撤除标注 "契约认知修正" 非 defer)
- **custos dogfood #2 (Plan 22, 2026-07-29)**: 红线表**已经在用**, 仍然中了 —— 0.3 行写
  "离线通道已兑现"且不带条件。审查 H1 实跑证实: 持久化状态里 generation 已是 1 的新进程,
  收到重投的 generation 1 会走 `==` 短路 —— 报 healthy、不部署、也不 watch; 敞口设成 `$9999`、
  上限 `$200`, 守卫一次都没被问过。修法是把声明降级到"本进程内部署过的 generation", **不改
  代码**(重启后盲目 watch 一个新进程里不存在的 instance, 会让 `get_engine_status` 抛错 →
  fail closed → 对着空气 flatten 并锁死, 比不 watch 更糟)。
  **与 Plan 03 的差别在谁抓到**: 03 是自省抓的, 22 是审查抓的。自评过不了这一关 —— 写声明的
  人和验声明的人是同一个, 而这条 lesson 防的正是"承袭红线名当兑现声明"这种自己看不见的措辞。
  有表不等于表里的话被验过。
- **与 #17/#22/#28 合并适用**: #17 缺失失败模式测试 / #28 分句借位无 guard / #22 dead-branch
  遮蔽 / **#40 unit-test ≠ runtime wire (close-out 声明侧, 接线 defer 时必须显式降级)**

## 生态 lesson 完整清单

以下 workspace lesson 与 custos 关联度较低, 但保留编号占位便于跨引用:

- #1-#8, #10, #12, #13, #15, #16, #19, #20, #23, #24, #31, #32, #36 (workspace 特化,
  完整叙事见 workspace `historical-lessons.md`, 独立 clone 时可视为背景阅读, 不阻塞 custos 开发)

## 记录新 lesson (custos 内)

内部编号继续使用 `C1`、`C2` 等前缀，与生态编号区分。新增条目使用 `### #C<序号>: <标题>`。
根因与教训各自折叠连续空白、去除首尾空白，用单个换行连接后计算 SHA-256，保留前 12 位作为 hash。

防护已落地且可从规则或测试入口触发的条目，在本文件保留标题、Full 路径和 hash；完整记录写入
`.forge/lessons-archive/active-detail.md`。尚依赖人工提醒的条目保留完整卡片。
新条目默认 active。至少两次独立成功 dogfood 且防护落地后，才可升级为 dogfooded。

旧条目保留原编号和格式；本轮未对缺少完整字段的继承卡片推测根因或批量补 hash。
