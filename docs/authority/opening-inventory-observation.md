# Conservative opening inventory observations

`NtTradingNodeHost.read_opening_account_inventory(authority)` is a local read-only
observation API for the current signed instance. It returns an immutable
`OpeningAccountInventory`, a detached `NativeOpeningAccountInventory` observation,
or a typed `InventoryUnavailable`. It never opens the
trading gate, submits an order, writes a RunnerFact, or allocates a fact sequence.
This is not a new wire payload or a successful opening-release acknowledgement.

The scope binds tenant, runner, mode, instance, immutable spec id/digest and
generation, plus connector, network, main wallet and numeric account id. Public
account configuration is reconstructed from the durable signed command and
compared with the provider created for the actual host lifecycle. Missing or
different source authority refuses the read; it never selects an unsigned lane.
Unsupported real-venue connectors return unavailable rather than reading NT cache
as a venue-wide inventory. Sandbox sources use a separate native owner handle,
not the public venue-account routes.

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

## Native sandbox owner

For a sandbox execution client, the host obtains
`node.account_capture_handle(ClientId(client_name))` after building the node and
before `run_async` consumes it. The source must match the actual registered
client/account/venue and execution account type. Missing APIs or malformed source
identity produce explicit unavailability. The handle returns bounded closed JSON;
consumer conversion refuses duplicate fields, numeric money, unknown fields,
foreign or replayed boundaries and unsupported environments. It preserves exact
amount strings, every asset, position and order, nullable prices and native
currency metadata. It never reads a starting balance map as current account data.
Source configuration retains its original digest and independently verifies the
configured currency definitions before any cash completeness claim.

Native capture uses the existing single unfinished collection slot and durable
before/after desired-state checks. Network/owner waits hold no SQLite writer
transaction. A stop attempt, replacement, scope change or retired handle prevents
signing the old observation, including when the native result arrives later.
`runner_fact_opening_bundle` keeps portfolio failure distinct from inventory
failure and admits a native cash sample only against actual registered capability
and runtime artifact evidence. The checked-in capability has no native complete
model, and the currently installed wheel has no capture API. Local future-handle
fixtures do not establish actual native runtime acceptance or release permission.

## Completeness and acceptance

`account_identity_matches` checks the reported main-wallet/account echo and a
positive observed height. It does not prove signer-key registration or complete
account identity. Wrong echoes are false; missing or zero-height evidence stays
unknown. The per-dimension completeness view is immutable. Identity, balances,
liabilities, positions, order categories, pagination and common-boundary coverage
remain unproven for these SoDEX routes; their `inventory_complete` is always false
and cannot be enabled by caller configuration. Native complete-cash conversion
has separate model, mapping, artifact and common-owner-boundary requirements.

Local fixtures validate collection, isolation, invalidation and exact parsing;
they do not establish venue capability. Actual account identity, key ownership,
pagination, liabilities, hidden order categories and common snapshot semantics
still require authorized read-only venue evidence. Producer schema/goldens and
the strict downstream consumer must be coordinated before any new inventory
field enters signed facts. Persistent process fencing, stop/pause precedence and
the atomic signed release acknowledgement remain prerequisites for any future
gate-opening implementation.

## Installed native payload measurement

`native_runtime_artifact.measure_native_runtime_archive` measures a local wheel
against the runtime actually imported by the current process. It verifies the
archive digest, distribution and package versions, compatible wheel/Python tags,
and the origins of the package, native extension, live and sandbox modules. Every
package payload file, METADATA and WHEEL must match the archive bytes. Duplicate
or escaping archive paths, symlinks, foreign imports, extra installed payloads
and files changed during the bounded read are refused. Blocking reads execute
outside the asyncio loop. The installation is not modified or repaired.
Generated bytecode is accepted only at this interpreter's standard cache path
for an archived Python source member. Its header must match that source, and its
code bytes must equal compilation of the trusted archive source at the declared
optimization level. The cache directory is not an exemption for extra files.

The result is an immutable measurement, without a native capability identity or
gate permission. Its native base-package version is recorded separately from the
distribution version; neither is substituted for the capture source's Cargo
runtime version. Composition must still bind the measured artifact to the
accepted capability and its supported model/API/mapping, then match the actual
owner source. This primitive does not add that composition, register a model,
change the checked-in engine dependency, or establish a full host acceptance.
Synthetic archive tests exercise measurement failures only; actual candidate
installation and native lifecycle evidence remain separate requirements.
