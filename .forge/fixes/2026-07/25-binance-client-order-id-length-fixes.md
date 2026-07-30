# 25 — Binance client order id length: peer-review fixes

> **Status**: ✅ Completed
> **Created**: 2026-07-30
> **Project**: custos
> **Plan**: `.forge/plans/2026-07/25-binance-client-order-id-length.md`
> **Source**: codex peer review (`--peer=codex`, `model_reasoning_effort=high`)

## 分诊

codex 报 0 CRITICAL / 1 HIGH / 3 MEDIUM / 4 LOW。每条都先实证再采纳（lesson #9/#11 ——
审查者的事实断言同样要核，尤其是**指控我自己代码**的那两条）。

| # | codex 判 | 实证结果 | 处置 |
|---|---|---|---|
| 1 | CRITICAL 无 | — | — |
| 2 | HIGH：约束是约定不是不变量 | **成立** | 已修（Fix 1） |
| 3 | MEDIUM：测试没走它声称的 runner 路径 | 部分成立 | Fix 1 覆盖了要害；余下记为遗留 |
| 4 | MEDIUM：Binance 政策放进多 venue builder | **成立** | 不改，记录理由 |
| 5 | MEDIUM：Speculum 的 fill fallback 与 UUID 不兼容 | **成立**（当前不触发） | 跨仓遗留，已登记 |
| 6 | LOW：只测长度未测字符集/唯一性 | 成立 | 不改，理由见下 |
| 7 | LOW：最坏情况 counter 的算术是错的 | **成立 —— 我错了** | 已修（Fix 2） |
| 8 | LOW：常量名与来源措辞不准 | **成立 —— 我错了** | 已修（Fix 3） |
| 9 | LOW：base 门会跳过全部新断言 | 成立 | 不改，close-out 已写明 |

## Fix 1 — 把约束落到 venue 边界（HIGH #2）

**根因**：实现错误，但类型是「防护放错层」。id 形态只能在 config 决定（两个 flag 在
`strategy.pxd:82,84` 是 `cdef readonly`，`OrderFactory` 在 `Strategy.register()` 里建），
于是「订单 id 够短」变成了一条**约定**：只要走 `build_nautilus_base_config` 就成立。

codex 指出两条绕过它的真实路径，均已实证：

- `runtime_loader.py:81-82` 直接用 artifact adapter 自己的 `build_config` 产物，不校验；
- 策略显式传 `client_order_id` 时根本不经生成器（本仓 toolkit 目前 grep 零命中，但没有东西
  阻止它）。

两条都会让每一单重新被 `-4015` 拒，而关于 builder 的六条测试全绿。

**修法**：在 `RunnerSafetyExecutionDispatch`（custos 已有的下单拦截层，`submit_order` /
`submit_order_list`）加一道长度门，超限即走既有的**本地拒单**路径，并给独立 reason
`custos_runner_client_order_id_too_long_for_venue`。这一层正是 custos 拥有 venue 交互的地方，
所以约束落在这里就与「谁建的 config」无关了。

order list 全拒而非只拒那一条：venue 会拒那一腿，剩下的就成了没人要的半个组合单。

**证伪**：把判断改成 `return False` 后，两条新测试变红；恢复后 12 passed。

## Fix 2 — 最坏情况的 counter 我算错了（LOW #7）

**根因**：事实性错误，我的。原注释说 `2**31 - 1` 是「能渲染的最宽 counter，十位」。实测：
generate **先自增**再渲染，所以 set 到上界后溢出成 `-2147483648` —— 渲染成**十一**字符
（含符号），比最大正值更宽。

```
set(2147483647) -> 'O-19700101-000000-t-000--2147483648'   counter 段 11 字符
set(2147483646) -> 'O-19700101-000000-t-000-2147483647'    counter 段 10 字符
```

**修法**：两个值都测（`LARGEST_POSITIVE_ORDER_COUNT` 与 `ORDER_COUNT_THAT_WRAPS`），注释
写明为什么溢出那个才是真最坏，并明说旧版本的描述是错的。

## Fix 3 — 常量名与来源措辞（LOW #8）

**根因**：两处不准。`MAX_LEN = 36` 把一个**排他**上界起名成「可接受的最大值」；注释还断言
「API 文档不给该字段长度」——那是我没查就写的。

**修法**：改名 `BINANCE_CLIENT_ORDER_ID_LEN_LIMIT`（配「必须严格短于此」），来源措辞收回到
我真的验过的部分（适配器 grep 无常量 + 交易所实测报文），并注明 codex 报告官方 schema 为
`{1,36}`，与实测一致。

## 不改的，以及为什么

- **#4 多 venue builder**：`VENUE_MAP` 确实含 OKX / Bybit / KuCoin / Gate（实证）。但
  32 位无连字符 UUID 是**所有** venue 上最保守的形态（更短、纯字母数字），不存在「对更严的
  venue 更危险」——原来那个 44 字符的结构化 id 才危险。custos 自己只接 Binance，
  `_binance_exchange_type` 对其他 connector 直接 `NotImplementedError`。
- **#6 未测字符集与唯一性**：codex 自己核过 UUID4 的小写 hex 满足 Binance 文档的
  `^[\.A-Z\:/a-z0-9_-]{1,36}$`，唯一性在两种解读下都安全。为一个恒真的属性加断言只会变成
  又一条空转守卫。
- **#9 base 门跳过新断言**：属实，且是本仓既有安排 —— 引擎依赖的测试在 base profile
  `importorskip`，由 `make verify-nt` 承重，两者都在发布 CI 里（`release.yml:54-58`）。
  与 Plan 24 记的 profile 分裂是同一件事，close-out 已写明。

## 遗留

1. **#3 的残余**：测试仍未穿过 `NtTradingNodeHost` 与 registry。Fix 1 让这件事不再要害
   （边界强制与谁建 config 无关），但「artifact adapter 自建 config」这条路径本身在签名通道
   接线后应有一条端到端覆盖。
2. **#5 跨仓**：Speculum 的 fill 解析在缺时间戳列时会从 `O-YYYYMMDD-HHMMSS` 形状的 index
   反解，UUID 会让它抛 `ValueError` → **静默跳过该笔成交**（`adapter.py:669-687`，注释自
   承 "skip the fill"）。今天不触发，因为 Speculum 用的是 PS 的
   `shared.nautilus.trading_config`（`config_builder.py:271`）。但 **Plan 24 正在推 PS 删
   `shared/`**，届时 Speculum 切到本仓 toolkit 就会真发生。这是 Slice E 的一个**新前置条件**，
   已回写 Plan 24 遗留项。
3. **真机证据仍未取得** —— Plan 25 自定的唯一完成判据，属 PS 侧。
