# Conservative opening inventory observations

`NtTradingNodeHost.read_opening_account_inventory(authority)` is a local read-only
observation API for the current signed instance. It returns an immutable
`OpeningAccountInventory` or a typed `InventoryUnavailable`. It never opens the
trading gate, submits an order, writes a RunnerFact, or allocates a fact sequence.
This is not a new wire payload or a successful opening-release acknowledgement.

The scope binds tenant, runner, mode, instance, immutable spec id/digest and
generation, plus connector, network, main wallet and numeric account id. Public
account configuration is reconstructed from the durable signed command and
compared with the provider created for the actual host lifecycle. Missing or
different source authority refuses the read; it never selects an unsigned lane.
Unsupported connectors return unavailable rather than reading NT cache as a
venue-wide inventory.

The host rechecks durable running desired state, command fingerprint, gate
binding, node, provider and lifecycle identity before and after the network read.
SQLite reads finish before venue I/O; no writer transaction spans network
latency. Stop attempts invalidate an in-flight observation even when shutdown
fails and leaves the node alive. This local observation revision is not a
persistent process fence and grants no execution permission. Successful stop,
failed startup and native task retirement remove the provider with the node.

## SoDEX source and exact parsing

`SodexOpeningInventoryCollector.collect` reads the public main-wallet/account
routes for state, balances, ordinary open orders and perps positions. Spot does
not read the unsupported positions route; that section is explicitly unavailable.
Collection runs in `asyncio.to_thread` using the existing bounded GET transport.
The collector has no credential argument and performs no signed venue action.
Account/client routing labels do not substitute for the main wallet identity.

The strict JSON entry preserves numeric decimal digits and refuses duplicate
object keys, nonfinite constants and oversized responses. A typed amount that
arrives as a Python float is refused, not converted. Local observation amounts
are bounded to 96 coefficient digits and exponent magnitude 96 to bound parsing
and arithmetic resources; exceeding this bound makes the source unknown. Exact
spot subtraction uses sufficient local Decimal precision. These are local
observation bounds, not a statement that every value fits the signed money wire
contract.

Each source retains its own request/receive times, block height, source time and
raw response digest. A failed source has `rows=None` and a structured reason; a
successfully observed empty list has `rows=()`. Bad rows, duplicate asset or
record identities and malformed boundaries invalidate their source rather than
being skipped into an empty list. Other successfully read sources survive.
Different heights stay separate; even equal heights do not establish a verified
common snapshot contract here.

Balances retain every returned coin, and positions/orders retain all returned
symbols without a configured-pair filter. Inactive and zero positions are not
silently removed. Spot `locked` and perps `collateral` stay separate; liabilities
remain unknown and are never inferred as zero or equated with posted collateral.
The ordinary order route does not establish coverage of TWAP, conditional or
other hosted order categories. Pagination hints can report more data but cannot
prove endpoint exhaustion.

## Completeness and acceptance

`account_identity_matches` checks the reported main-wallet/account echo and a
positive observed height. It does not prove signer-key registration or complete
account identity. Wrong echoes are false; missing or zero-height evidence stays
unknown. The per-dimension completeness view is immutable. Identity, balances,
liabilities, positions, order categories, pagination and common-boundary coverage
remain unproven; `inventory_complete` is always false in this implementation and
cannot be enabled by caller configuration.

Local fixtures validate collection, isolation, invalidation and exact parsing;
they do not establish venue capability. Actual account identity, key ownership,
pagination, liabilities, hidden order categories and common snapshot semantics
still require authorized read-only venue evidence. Producer schema/goldens and
the strict downstream consumer must be coordinated before any new inventory
field enters signed facts. Persistent process fencing, stop/pause precedence and
the atomic signed release acknowledgement remain prerequisites for any future
gate-opening implementation.
