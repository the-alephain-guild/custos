"""SoDEX venue-config assembly (pure functions, no IO, no side effects).

Turns a DeploymentSpec parameter dict + a decrypted credential dict into
NautilusTrader client-config objects, the same shape ``venue_binance`` produces.
Two execution modes are wired:

- sandbox: the venue's real market-data feed + a locally simulated execution venue
  (``SandboxExecutionClientConfig``), so no order reaches the exchange.
- testnet: the adapter's own execution client against the venue's testnet gateway.

Live is deliberately absent. "Can run live" is not one flag -- ``venue_binance``
spells out what it costs: promotion evidence, credential-scope checks, a live
exec builder, and real-venue evidence. None of that exists for this venue yet, so
``build_exec_client_config_live`` refuses rather than returning a config that
would look ready. The host's per-mode venue allow-list refuses it a step earlier;
this is the second layer, so neither one carries the claim alone.

non-custodial red line 0.1: the data feed is credential-free by the adapter's own
contract, so sandbox holds no secret at all. Only the testnet exec config carries
credential material, forwarded straight into a local NT config -- never logged,
printed, or published.

Spot and perps are separate venues here, not a parameter: they differ in signing
domain, API key set, balances, and reference price, and the adapter models them as
``SODEX_SPOT`` and ``SODEX_PERPS`` for that reason
(``crates/adapters/sodex/src/config.rs`` module docs).
"""

from __future__ import annotations

from nautilus_trader.adapters.sandbox import SandboxExecutionClientConfig
from nautilus_trader.adapters.sodex import (
    SODEX_PERPS,
    SODEX_SPOT,
    Market,
    Network,
    SodexDataClientConfig,
    SodexDataClientFactory,
    SodexExecClientConfig,
    SodexExecutionClientFactory,
)
from nautilus_trader.model import (
    FIXED_PRECISION,
    AccountId,
    AccountType,
    Currency,
    CurrencyType,
    InstrumentId,
    Money,
    OmsType,
    Venue,
)

from custos.core.log import get_logger
from custos.engines.nautilus.venues import venue_for_connector

_log = get_logger("custos.venue_sodex")

# connector name (spec.connector) -> the venue's engine, the NT venue it trades on,
# and the account shape it settles in. Extend here (never elsewhere) when a new SoDEX
# engine is supported.
#
# Keyed by connector rather than by ``Market``: the adapter's enum is a pyo3 class and
# is not hashable, so it cannot be a dict key.
_SODEX_CONNECTORS: dict[str, tuple[Market, str, AccountType]] = {
    # Spot settles what it holds; perps margin a position. Same split venue_binance makes.
    "sodex": (Market.SPOT, SODEX_SPOT, AccountType.CASH),
    "sodex_perpetual": (Market.PERPS, SODEX_PERPS, AccountType.MARGIN),
}

# The modes this module can build an execution config for. The host's per-mode
# allow-list must agree with this; a drift-guard test asserts the two sides match.
SUPPORTED_MODES = frozenset({"sandbox", "testnet"})

CONNECTORS_BY_MODE: dict[str, frozenset[str]] = {
    "sandbox": frozenset(_SODEX_CONNECTORS),
    "testnet": frozenset(_SODEX_CONNECTORS),
    "live": frozenset(),
}

_OMS_TYPE_NETTING = OmsType.NETTING

# Sandbox fills locally and needs real prices, so it reads the production gateway --
# the same reason venue_binance points sandbox at the LIVE feed. Testnet reads the
# testnet gateway so its instruments match the venue its orders go to. There is no
# live row: this module builds no live execution, and a mode with no exec config has
# no business naming a data gateway.
_NETWORK_BY_MODE: dict[str, Network] = {
    "sandbox": Network.MAINNET,
    "testnet": Network.TESTNET,
}

# The account this runner's clients speak for. A node holds one credential scope, so
# one ordinal at this issuer is exact -- it selects nothing.
_ACCOUNT_ORDINAL = "001"

# The exec config resolves any field left None from the process environment
# (SODEX_API_KEY_NAME and friends, config.rs:44-59). That is a convenience for the
# adapter's own examples and a hazard here: a deployment whose vault credential is
# incomplete would pick up whatever the host environment happens to hold and trade
# with it. Every field is therefore required explicitly, and missing one raises
# before any NT object exists.
_REQUIRED_CREDENTIAL_FIELDS = ("api_key", "api_secret")
_REQUIRED_ACCOUNT_FIELDS = ("wallet_address", "sodex_account_id")


def _wiring(connector: str) -> tuple[Market, str, AccountType]:
    """Resolve connector to (engine, venue, account type), rejecting anything else."""
    wiring = _SODEX_CONNECTORS.get(connector)
    if wiring is None:
        raise NotImplementedError(
            f"connector {connector!r} is not a SoDEX engine — this module wires "
            f"({', '.join(sorted(_SODEX_CONNECTORS))})"
        )
    return wiring


def _market(connector: str) -> Market:
    """The venue engine this connector trades on."""
    return _wiring(connector)[0]


def venue_name(spec: dict) -> str:
    """The NT venue this spec's connector trades on: ``SODEX_SPOT`` or ``SODEX_PERPS``.

    Read from the NT-free table so a caller without NautilusTrader installed gets the
    same answer; the wiring below is still consulted first so an unwired connector is
    refused by this module's own message.
    """
    _wiring(spec["connector"])
    return venue_for_connector(str(spec["connector"]))


def client_name(spec: dict) -> str:
    """The id the data and execution clients register under.

    The venue name rather than the adapter's registry key: one node can hold a spot
    and a perps client built from the same factory, so the id has to tell them apart.
    The adapter's own example registers both clients under the venue name for that
    reason (``examples/live/sodex/exec_tester.py``).
    """
    return venue_name(spec)


def data_client_factory() -> SodexDataClientFactory:
    return SodexDataClientFactory()


def exec_client_factory() -> SodexExecutionClientFactory:
    return SodexExecutionClientFactory()


def network_for_mode(mode: str) -> Network:
    """Gateway for a trading mode. A mode with no exec config here is refused."""
    network = _NETWORK_BY_MODE.get(mode.lower())
    if network is None:
        raise NotImplementedError(
            f"SoDEX is not wired for trading mode {mode!r} "
            f"(wired: {', '.join(sorted(_NETWORK_BY_MODE))})"
        )
    return network


def _trading_pairs(spec: dict) -> list[str]:
    pairs = spec.get("pairs")
    if isinstance(pairs, list) and pairs:
        return list(pairs)
    pair = spec.get("pair")
    if not pair:
        raise RuntimeError("spec declares no trading pair")
    return [str(pair)]


def build_instrument_id_strings(spec: dict) -> tuple[str, ...]:
    """Instrument ids for the configured pairs, in the order the spec lists them.

    The spec's pairs are the venue's own symbols, used verbatim. Unlike Binance there
    is no canonical ``BASE-QUOTE`` to translate from: the two engines quote different
    assets and name them differently -- spot lists ``vBTC_vUSDC`` (the venue's own
    v-prefixed tokens, underscore-separated), perps lists ``BTC-USD``. A translation
    rule would have to invent the quote asset, and a wrong symbol does not fail loudly
    here; it loads an empty instrument set.
    """
    venue = venue_name(spec)
    return tuple(f"{pair}.{venue}" for pair in _trading_pairs(spec))


def build_instrument_ids(spec: dict) -> list[InstrumentId]:
    """The same ids as NT objects — the SoDEX data config takes ids, not strings."""
    return [InstrumentId.from_str(value) for value in build_instrument_id_strings(spec)]


def sodex_account_id(spec: dict) -> AccountId:
    """The account this runner's SoDEX clients speak for."""
    return AccountId.from_str(f"{venue_name(spec)}-{_ACCOUNT_ORDINAL}")


def build_data_client_config(spec: dict, mode: str) -> SodexDataClientConfig:
    """Market-data config for the requested trading mode.

    No credential parameter exists on this config: the venue serves market data
    unsigned (``config.rs`` "Deliberately credential-free"), so a data client holds
    no secret in any mode.
    """
    return SodexDataClientConfig(
        network=network_for_mode(mode),
        market=_market(spec["connector"]),
        instrument_ids=build_instrument_ids(spec),
    )


def build_data_client_config_for_mode(
    spec: dict,
    credential: dict,
    mode: str,
) -> SodexDataClientConfig:
    """Uniform entry point the host calls for every venue.

    ``credential`` is accepted and ignored for signature parity with the Binance
    module, whose authenticated modes do need it.
    """
    del credential
    return build_data_client_config(spec, mode)


def _require(mapping: dict, field: str, what: str) -> str:
    value = mapping.get(field)
    if value is None or str(value).strip() == "":
        raise RuntimeError(f"sodex execution requires {what} {field!r}")
    return str(value)


def build_exec_client_config_testnet(spec: dict, credential: dict) -> SodexExecClientConfig:
    """Real SoDEX execution against the testnet gateway.

    Four values identify the account, and they come from two different places on
    purpose. The API key name and its private key are the secret half and come from
    the vault credential. The wallet address and the numeric account id are public
    identifiers -- the adapter says so of the wallet in as many words -- and come from
    the owner-signed spec, which is where account identity belongs.

    The wallet is required even though it signs nothing: the account reads are keyed
    by it, and pointing them at the API key's address answers 200 with an empty
    account, which reconciliation would read as flat.
    """
    api_key_name = _require(credential, "api_key", "credential field")
    api_private_key = _require(credential, "api_secret", "credential field")
    wallet_address = _require(spec, "wallet_address", "spec field")
    raw_account_id = _require(spec, "sodex_account_id", "spec field")
    try:
        account_id = int(raw_account_id)
    except ValueError as exc:
        raise RuntimeError(
            f"spec field 'sodex_account_id' must be the venue's numeric account id, "
            f"got {raw_account_id!r}"
        ) from exc
    return SodexExecClientConfig(
        network=network_for_mode("testnet"),
        market=_market(spec["connector"]),
        account_id=account_id,
        api_key_name=api_key_name,
        api_private_key=api_private_key,
        wallet_address=wallet_address,
    )


def build_exec_client_config_live(spec: dict, credential: dict) -> SodexExecClientConfig:
    """Refuse: this venue has no live delivery.

    Reached only if the host's per-mode allow-list is widened without the rest of a
    live delivery arriving with it. The message names what is missing rather than
    failing later as a credential or gateway error.
    """
    del spec, credential
    raise NotImplementedError(
        "SoDEX live execution is not delivered: no promotion-evidence gate, no live "
        "credential handling, and no real-venue evidence exist for this venue"
    )


def _ensure_currency_registered(code: str) -> None:
    """Make a venue coin nameable before an instrument has been loaded.

    The sandbox's starting balances are built at config time, while the adapter
    registers the venue's coins when it parses an instrument -- later. So a balance
    denominated in one of the venue's own tokens (``vUSDC``) cannot be constructed
    yet, and NT answers "Unknown currency".

    Registered at the engine's full width, which is what the adapter registers at and
    for a reason it states: the listing's ``coinPrecision`` governs what orders the
    venue accepts, while a currency's precision bounds ``Money``, which has to hold
    whatever the ledger says. ``overwrite=False`` so a currency the engine already
    defines keeps its own definition.
    """
    try:
        Money.from_str(f"0 {code}")
    except ValueError:
        Currency.register(
            Currency(code, FIXED_PRECISION, 0, code, CurrencyType.CRYPTO),
            False,
        )
        _log.info("sodex_sandbox_currency_registered", currency=code, precision=FIXED_PRECISION)


def _sandbox_money(balance: str) -> Money:
    parts = balance.split()
    if len(parts) != 2:
        raise RuntimeError(f"starting balance {balance!r} is not '<amount> <currency>'")
    _ensure_currency_registered(parts[1])
    return Money.from_str(balance)


def build_exec_client_config_sandbox(
    spec: dict,
    credential: dict,
    starting_balances: list[str],
) -> SandboxExecutionClientConfig:
    """Locally simulated execution venue for sandbox mode.

    Fills are matched in-process against the venue's real-time prices, so no
    credential is used -- ``credential`` is accepted for signature parity with the
    testnet builder.
    """
    del credential
    _, venue, account_type = _wiring(spec["connector"])
    return SandboxExecutionClientConfig(
        venue=Venue(venue),
        starting_balances=[_sandbox_money(balance) for balance in starting_balances],
        account_id=sodex_account_id(spec),
        account_type=account_type,
        oms_type=_OMS_TYPE_NETTING,
        default_leverage=None,
    )


def venue_ledger_source(spec: dict, credential: dict):
    """Refuse: this venue has no independent ledger source yet.

    Reached only for testnet and live, where a RunnerFact claims reconciliation
    coverage. Returning ``None`` instead would publish facts that merely record the
    coverage as unavailable, which reads like a runtime condition rather than like a
    venue this runner cannot yet reconcile at all.
    """
    del spec, credential
    raise NotImplementedError(
        "SoDEX has no independent venue ledger source: reconciliation evidence for "
        "testnet and live cannot be produced for this venue yet"
    )


def client_order_id_len_limit() -> int | None:
    """No cap has been measured against this venue, so none is claimed.

    Binance answers an over-long id with -4015 and the limit is written down with the
    session that measured it. Nothing equivalent exists here: the adapter carries no
    such constant and no order has been placed on this venue from this runner. Copying
    Binance's 36 would be a claim about a different exchange, and a guard set from a
    guess gives assurance it cannot support -- if this venue's real cap is shorter, the
    guess passes ids the venue will refuse.

    What this costs: an over-long id reaches the venue and is refused there, one round
    trip later, with the venue's message rather than this runner's. The runner's own
    ids are a fixed 32 characters, so it cannot be the source of one. Measure the cap
    during the first real session on this venue and replace this.
    """
    return None
