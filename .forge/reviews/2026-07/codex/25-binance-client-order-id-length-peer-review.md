# codex peer review — Plan 25 (Binance client order id length)

> **日期**: 2026-07-30
> **调用**: `codex exec -c model_reasoning_effort="high" -o <file> --sandbox read-only … < /dev/null`
>   (lesson #10/#12 模板: 重定向 stdin + 降 effort + `-o` 取最终消息 + 输出直写文件)
> **codex-cli**: 0.145.0 · exit 0 · verdict 5215 bytes
> **分诊与处置**: `.forge/fixes/2026-07/25-binance-client-order-id-length-fixes.md`
>
> 逐条实证结果见分诊表。**codex 抓到两处我自己代码里的事实错误**(最坏情况 counter 的算术、
> 常量名与来源措辞), 一处架构层的 HIGH(约束是约定不是不变量), 以及一条真实的跨仓隐患
> (Speculum 的 fill fallback)。原文照录如下, 未编辑。

---

1. **CRITICAL — none found.**

2. **HIGH — the venue constraint is a convention, not an enforced invariant.** A valid signed artifact may return a strategy built without the shared builder; the loader accepts arbitrary `build_config`/`build_strategy` output and the host trusts it ([runtime_loader.py](/Users/wukai/data/repos/github/the-alephain-guild/tesseract-trading/custos/src/custos/engines/nautilus/runtime_loader.py:72), [host.py](/Users/wukai/data/repos/github/the-alephain-guild/tesseract-trading/custos/src/custos/engines/nautilus/host.py:297)). Explicit `client_order_id` arguments also bypass the generator. Either path can recreate universal `-4015` rejection while all six tests pass. Validate the actual submitted ID at the Binance execution boundary; readonly flags prevent mutation, not validation.

3. **MEDIUM — the tests do not exercise the runner path they claim.** Both construct the builder output directly; the stronger test reaches real `Strategy.register`, but through `BacktestEngine.add_strategy`, not the registry, artifact loader, `NtTradingNodeHost`, or `SandboxLiveExecClientFactory` ([sandbox test](/Users/wukai/data/repos/github/the-alephain-guild/tesseract-trading/custos/tests/test_client_order_id_sandbox_execution.py:76)). Registry or artifact wiring could bypass the builder without turning these tests red.

4. **MEDIUM — a Binance-specific policy is imposed by a multi-venue toolkit builder.** The same builder supports OKX, Bybit, KuCoin and Gate ([utils.py](/Users/wukai/data/repos/github/the-alephain-guild/tesseract-trading/custos/packages/custos-strategy-toolkit-nautilus/src/custos_toolkit_nautilus/adapter/utils.py:19)). Those venues and all backtests now receive random UUID IDs even though the rationale says the rule belongs to Binance. That can break stricter future venue limits and makes raw order/event snapshots nondeterministic.

5. **MEDIUM — the concrete cross-repo backtest fallback is incompatible with UUID IDs, although its current builder has not received this change.** Current Speculum imports Philosophers-Stone’s separate `shared.nautilus` builder, not this one ([config_builder.py](/Users/wukai/data/repos/github/the-alephain-guild/alchymia-labs/speculum/backend/app/engine/adapters/nautilus/config_builder.py:257)). If UUIDs propagate there, fills lacking a recognized timestamp column lose the existing `O-YYYYMMDD-HHMMSS` fallback; UUID indexes fail parsing and the fill is skipped ([adapter.py](/Users/wukai/data/repos/github/the-alephain-guild/alchymia-labs/speculum/backend/app/engine/adapters/nautilus/adapter.py:662)). Normal reports with `ts_event` are unaffected.

6. **LOW — the tests bind length but not Binance’s complete contract.** They do not assert the allowed-character regex or uniqueness across generated IDs. Today bare UUID4 is lowercase hex, which satisfies Binance’s documented `^[\.A-Z\:/a-z0-9_-]{1,36}$`; Binance states uniqueness “among open orders,” without a documented client-order-ID reuse window. UUID4 safely satisfies either account-wide or per-symbol interpretation. [Binance New Order documentation](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order)

7. **LOW — the “worst case” counter explanation is wrong.** Setting `2**31-1` and then generating a structured ID overflows the C `int` to `-2147483648`, producing an eleven-character counter component, not the claimed ten-digit maximum ([generators.pyx](/Users/wukai/data/repos/github/the-alephain-guild/tesseract-trading/custos/.venv/lib/python3.12/site-packages/nautilus_trader/common/generators.pyx:129)). Use `2**31-2` to render the largest positive value. The UUID property still holds; the genuinely missed variable is an explicitly supplied client ID.

8. **LOW — the constant’s provenance and name are already stale.** Current official documentation does state `{1,36}`, and the error-code page says “not more than 36,” contrary to the source comment. Given your verified testnet observation, `< 36` is the correct conservative assertion, but `MAX_LEN = 36` names an exclusive bound as an accepted maximum. [Binance error code -4015](https://developers.binance.com/docs/derivatives/usds-margined-futures/error-code)

9. **LOW — the base release gate may skip every new assertion.** Both files use `importorskip`; `make verify` explicitly permits that, while only `make verify-nt` requires NautilusTrader ([Makefile](/Users/wukai/data/repos/github/the-alephain-guild/tesseract-trading/custos/Makefile:59)). Release CI runs the latter, so this is gate fragility rather than an untested release.

The UUID choice itself is sound for USDT-M: reconciliation treats client IDs as opaque keys and rebuilds prior orders from venue reports, so timestamp/counter determinism is unnecessary. [NautilusTrader’s reconciliation contract](https://nautilustrader.io/docs/latest/developer_guide/spec_exec_testing/) UUID generation bypasses the cache collision loop, but UUID4 collision risk is negligible.

The diagnosis and 44/39/36/32 arithmetic are correct. Only the claimed counter worst-case arithmetic is wrong. Focused verification: **6 passed**; worktree remained clean.

