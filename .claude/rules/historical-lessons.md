# 历史教训 (custos)

本文件继承 workspace `the-alephain-guild/.claude/rules/historical-lessons.md` 中与 custos
开发直接相关的**精华教训**. 独立仓库 clone 场景外部开发者仍能读到 lesson 核心防护;
完整叙事保留在生态 archive, 本仓库只留 rule 卡片 + custos 特化 binding.

> **custos 内部 lesson 用 `C1` `C2` … 前缀区分生态数字编号** (见文末"记录新 lesson")。

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

custos 自身开发中出现的 lesson 直接在本文件顶部按 workspace 模板追加:

```markdown
### #<N> <标题> (<YYYY-MM>)

**事件**: {发生了什么}

**根因**: {为什么会发生}

**预防**:
- {措施}

**Binding**: {落到 rule / hook / skill 哪里}
```

编号避免与 workspace 冲突: custos 内部编号用 `C1` `C2` ... 前缀区分.
