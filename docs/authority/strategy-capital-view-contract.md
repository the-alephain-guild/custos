# Schema-only strategy capital view

`custos_toolkit.contracts.strategy_capital` defines an immutable, strict value
contract. It does not read a capital book, authenticate a command, create a
RunnerFact, install a strategy hook, authorize an order, or transfer money.
`StrategyCapitalContractSupportV1` requires explicit `runtime_supported=false`
and `capital_change_supported=false`; integer zero is not an accepted boolean.
The current asset index registers both models as `contract_only=true`.
Existing StrategyManifest and strategy `supported_commands` are unchanged.

## Values and availability

The public view has exactly four fields: `allocated_capital`, `realized_pnl`,
`unrealized_pnl`, and `capital_changes`. Every field has an independent
`known`/`unavailable` discriminator. An unavailable money field contains an
explicit null amount, not zero; unavailable change history contains null
entries, not an empty complete history. Nullable keys remain required.
Unavailable PnL does not make a separately proved allocation unavailable.

Known allocation is nonnegative. PnL is signed. Changes have a nonzero signed
amount and exactly one of contribution, redemption, internal transfer or profit
distribution as their kind. Only the existing contribution/redemption allocation
reversals can be represented. A reversal UUID does not authorize a payment
reversal, recovery, liability or other unimplemented financial writer. Neither
parsing nor a sample provenance object proves that any financial effect occurred.

Known fields share one execution scope and UTC calendar month. Known control
state fields share their exact command, capital revision, state digest, phase,
operation and time. Both known PnL fields share one observation and bind the same
capital state. The observation cannot precede that state, even by one nanosecond.
The command time can be earlier than the observation. Change timestamps belong
to the same period and cannot follow their state's observation time.

Public PnL provenance is intended to be assembled **after** a future outbox
commits its actual batch and sequences. No current RunnerFact body was changed,
and no self-referential batch/payload digest is introduced. Schema samples are
synthetic type samples, not signed commands or observations. Actual same-sample
PnL, period boundary lots, restart recovery and host injection remain future work.

Allocation principal is distinct from an account's opening/handover equity.
Terminal equity can include predecessor profits; these models do not make those
profits a new capital allocation. In a future preparing state the control-plane
approved target constrains trading before the economic effect is committed;
provenance records that phase instead of claiming a transfer happened.

## Exact scalar boundaries

Money on the wire is canonical decimal text. The range is the existing
RunnerFact/rust_decimal 96-bit mantissa and at most 28 decimal places. Validation
uses integer arithmetic and does not round through the ambient Decimal context.
The eight currencies are USD, USDT, USDC, BTC, ETH, VUSDC, VBTC and VETH. Binary
floats, JSON numeric amounts, exponent notation, leading plus, negative zero,
whitespace, extra leading zeros and decimal trailing zeros are rejected.

UUID text is lowercase, canonical and non-nil. Digests are 64 lowercase hex
characters. Revisions and sequences are strict positive i64 values; source
frontiers can be zero. Booleans do not pass as integer revisions. Tenant names
use the existing RunnerFact-compatible ASCII subject-token limit of 64 bytes.
All identifier fields here are ASCII, so their character and UTF-8 lengths agree.
This contract introduces no free-text provider identifier whose byte limit could
be confused with JSON Schema's character-based `maxLength`.

Times remain strings in UTC RFC3339 AutoSi form: whole seconds or 3, 6 or 9
fractional digits followed by Z. Semantic checks retain nanoseconds, validate
calendar dates and UTC month boundaries, and reject noncanonical spellings.
JSON Schema expresses structural limits, closed variants and required nullable
fields. Runtime validation additionally enforces the exact decimal range,
canonical AutoSi form and cross-field bindings. Generated negative vectors state
which failures JSON Schema can itself detect; passing schema validation is not
financial verification or complete Rust consumer qualification.

## Current generation and source evidence

The normal `scripts/generate_strategy_contract_assets.py` generator emits the
view schema, golden, negative vectors and sidecars, plus the support schema,
negative-capability golden and sidecar. The current strategy asset index records
them and records the exact source bytes of both strategy_execution.py and
strategy_capital.py in `producer.source_models`. The existing source_path and
source_sha256 continue to identify strategy_execution.py truthfully.

The current authority checker rejects missing or changed source records, asset
mismatches, an altered contract-only registration, or a declaration of runtime
support even when its asset hash has been updated. Contract revisions are
registered through the existing owner revision generator. Historical handoff,
RC and external receipts, as well as previously produced U2 artifacts and
installed environments, are not rewritten.

Run the normal generator and check, contract revision check, authority gate,
strict toolkit type check and repository verification before committing. This
unit establishes only the contract and its generation/source evidence. Runtime
capability may be declared only after the later host installation and real
capital-change/PnL acceptance work has passed.
