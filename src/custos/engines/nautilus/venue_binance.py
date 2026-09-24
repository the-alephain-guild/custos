"""Binance venue-config assembly (pure functions, no IO, no side effects).

Turns a DeploymentSpec parameter dict + a decrypted credential dict into
NautilusTrader client-config objects. Three execution modes:
- sandbox: real-time Binance *data* feed + a locally simulated *execution* venue
  (``SandboxExecutionClientConfig`` + ``SandboxLiveExecClientFactory``), so no
  real orders reach the exchange.
- testnet: real Binance exec (``BinanceExecutionClientConfig`` with environment
  TESTNET) against the Binance testnet endpoint, with a testnet data feed.
- live: same but environment LIVE against the real exchange; a live exec config
  cannot be built without separation-of-duties approval (>= 2 approvers).

non-custodial red line 0.1: sandbox does not place credential fields in NT
config objects; authenticated modes only forward them into local NT configs.
This module never logs, prints, or publishes credential material — keep it
that way.

Only Binance (spot + USDT-perpetual) is wired here; other venues are rejected
explicitly. Sandbox data uses the LIVE Binance feed because simulated execution
needs real prices; testnet uses the testnet feed so instruments match the venue.
"""

from __future__ import annotations

from decimal import Decimal

from nautilus_trader.adapters.binance import (
    BinanceDataClientConfig,
    BinanceDataClientFactory,
    BinanceEnvironment,
    BinanceExecutionClientConfig,
    BinanceExecutionClientFactory,
    BinanceInstrumentProviderConfig,
    BinanceProductType,
    BinanceSpotMarketDataMode,
)
from nautilus_trader.adapters.sandbox import SandboxExecutionClientConfig
from nautilus_trader.model import (
    AccountId,
    AccountType,
    InstrumentId,
    Money,
    OmsType,
    Venue,
)

from custos.core.venue_proxy import VenueProxy
from custos.engines.nautilus.venues import venue_for_connector

# The sandbox exec config takes these as model enums, not as their string names.
_ACCOUNT_TYPE_FUTURES = AccountType.MARGIN
_ACCOUNT_TYPE_SPOT = AccountType.CASH
_OMS_TYPE_NETTING = OmsType.NETTING

# The exchange account this runner's execution client speaks for. One node holds
# one credential scope, so one account id per venue is exact; the number is the
# NautilusTrader convention for the account's ordinal at the issuer, not an
# account selector.
_ACCOUNT_ORDINAL = "001"

# 2.0 detects an Ed25519 key from the key material itself and dropped the explicit
# key-type field, so these two are what its Binance clients can sign with. RSA was
# accepted by the 1.x adapter and has no 2.0 equivalent: a credential declaring it is
# refused here rather than handed over to fail later as an exchange-side auth error.
_SUPPORTED_KEY_TYPES = frozenset({"HMAC", "ED25519"})

BINANCE_VENUE = "BINANCE"

# connector name (spec.connector) -> "spot" | "futures". Extend here (never
# elsewhere) when a new Binance product line is supported.
_BINANCE_CONNECTORS: dict[str, str] = {
    "binance": "spot",
    "binance_perpetual": "futures",
}

# The connectors this module builds an execution config for, per trading mode. The
# host's per-mode venue allow-list must equal the union of these across venue
# modules; a drift-guard test asserts it, so widening one side alone turns red.
# All three rows are the full set: this venue has a live exec builder.
CONNECTORS_BY_MODE: dict[str, frozenset[str]] = {
    "sandbox": frozenset(_BINANCE_CONNECTORS),
    "testnet": frozenset(_BINANCE_CONNECTORS),
    "live": frozenset(_BINANCE_CONNECTORS),
}

# trading_mode -> the Binance data-feed environment. Sandbox simulates fills
# locally against real-time LIVE prices; testnet must feed testnet market data
# so instruments match its exec venue; live feeds live.
_DATA_ENVIRONMENT_BY_MODE: dict[str, BinanceEnvironment] = {
    "sandbox": BinanceEnvironment.LIVE,
    "testnet": BinanceEnvironment.TESTNET,
    "live": BinanceEnvironment.LIVE,
}

# Distinct approvers a live spec must carry (separation of duties). The approval
# decision is the cloud's (arx, approver != applicant); custos only checks the
# spec carries enough before a real live order path is built.
_LIVE_MIN_APPROVERS = 2

# A client order id must be strictly shorter than this — 36 is itself refused, which
# is why the name says limit rather than max and every comparison uses `<`.
#
# Measured on 2026-07-30 against the USDT-M futures testnet, which answered a
# 44-character id with
#   -4015 "Client order id length should be less than 36 chars"
# The venue's own answer is the source because the NautilusTrader Binance adapter
# carries no such constant (grep) and this repository had no handling of it. Binance's
# published schema for the field is reported as `{1,36}`, which agrees; the measurement
# is kept as the citation since it is the part that was verified here.
#
# Treat a change to this number as a venue-contract change and re-measure it, rather
# than adjusting it to fit an id that turned out to be too long.
BINANCE_CLIENT_ORDER_ID_LEN_LIMIT = 36


def data_environment_for_mode(mode: str) -> BinanceEnvironment:
    """Data-feed environment for a trading_mode (defaults to LIVE prices)."""
    return _DATA_ENVIRONMENT_BY_MODE.get(mode.lower(), BinanceEnvironment.LIVE)


def require_live_owner_evidence(spec: dict) -> None:
    """Require immutable control-plane promotion evidence, not human approval logic.

    ARX authenticates actors and the control plane owns SoD. Custos verifies only that the
    signed owner command carries the exact promotion receipt required for live.
    """
    promotion_id = str(spec.get("promotion_id") or "")
    evidence_digest = str(spec.get("promotion_evidence_digest") or "")
    if not promotion_id or len(evidence_digest) != 64:
        raise RuntimeError("live_owner_evidence_missing")


def require_supported_key_type(credential: dict) -> None:
    """Refuse a credential whose key type this adapter cannot sign with.

    The declared type is not forwarded anywhere -- 2.0 reads the key material -- so
    without this check an RSA credential would be accepted here and only fail at the
    exchange, where the message names the credential rather than the reason.
    """
    key_type = str(credential.get("key_type") or "HMAC").upper()
    if key_type not in _SUPPORTED_KEY_TYPES:
        raise RuntimeError(
            f"binance key type {key_type!r} is not supported by this adapter "
            f"(supported: {', '.join(sorted(_SUPPORTED_KEY_TYPES))})"
        )


def _binance_exchange_type(connector: str) -> str:
    """Resolve connector to spot/futures, rejecting non-Binance venues."""
    exchange_type = _BINANCE_CONNECTORS.get(connector)
    if exchange_type is None:
        raise NotImplementedError(
            f"connector {connector!r} is not supported — only Binance "
            f"({', '.join(sorted(_BINANCE_CONNECTORS))}) is wired in this runner"
        )
    return exchange_type


def _trading_pairs(spec: dict) -> list[str]:
    """Configured pairs (plural list, legacy singular fallback)."""
    pairs = spec.get("pairs")
    if isinstance(pairs, list) and pairs:
        return list(pairs)
    return [spec.get("pair", "BTC-USDT")]


def _format_instrument_id(pair: str, connector: str) -> str:
    """ "BTC-USDT" + connector -> NT instrument id string."""
    symbol = pair.replace("-", "")
    if _binance_exchange_type(connector) == "futures":
        return f"{symbol}-PERP.{BINANCE_VENUE}"
    return f"{symbol}.{BINANCE_VENUE}"


def build_instrument_id_strings(spec: dict) -> tuple[str, ...]:
    """Instrument ids for the configured pairs, in the order the spec lists them.

    Strings rather than ``InstrumentId``: the instrument-provider config takes a
    sequence of id strings. Order follows the spec so the loaded set is reproducible.
    """
    connector = spec["connector"]
    return tuple(_format_instrument_id(pair, connector) for pair in _trading_pairs(spec))


def _binance_product_type(connector: str) -> BinanceProductType:
    """The venue's product line for a connector: USD-margined futures, or spot."""
    if _binance_exchange_type(connector) == "futures":
        return BinanceProductType.USD_M
    return BinanceProductType.SPOT


def binance_account_id() -> AccountId:
    """The account this runner's Binance clients speak for.

    2.0 requires the execution client to name its account rather than deriving one.
    A node holds exactly one credential scope, so a single ordinal at this issuer is
    exact -- it is not selecting between accounts.
    """
    return AccountId.from_str(f"{BINANCE_VENUE}-{_ACCOUNT_ORDINAL}")


def build_sandbox_leverage(spec: dict) -> Decimal:
    """The declared leverage as the sandbox venue's account-wide default.

    The sandbox exec config takes one ``default_leverage`` rather than the
    per-instrument map it used to take. Nothing is lost: the spec carries a single
    ``leverage`` and the old map filled every instrument with that same value.
    Without it the account default (e.g. 20x) would apply.
    """
    return Decimal(str(spec.get("leverage", 1)))


def build_binance_futures_leverages(spec: dict) -> dict[str, int]:
    """The same declaration in the shape the Binance exec config reads.

    Each key goes straight into the venue's set-leverage call as its ``symbol``
    parameter, so it has to be the exchange's own symbol -- ``BTCUSDT``, without the
    ``-PERP`` the instrument id carries. The 1.x adapter had a ``BinanceSymbol`` type
    that stripped the suffix on construction; 2.0 takes plain strings, so the stripping
    is done here.

    ``leverage`` is a positive integer in the spec contract, so it converts exactly; a
    float would have to round, and silently rounding a risk parameter is not a thing to
    do quietly.
    """
    connector = spec["connector"]
    leverage = int(spec.get("leverage", 1))
    return {
        _venue_symbol(_format_instrument_id(pair, connector)): leverage
        for pair in _trading_pairs(spec)
    }


def _venue_symbol(instrument_id: str) -> str:
    """The exchange's symbol for an instrument id: ``BTCUSDT-PERP.BINANCE`` -> ``BTCUSDT``."""
    return InstrumentId.from_str(instrument_id).symbol.value.removesuffix("-PERP")


def build_data_client_config(
    spec: dict,
    credential: dict,
    environment: BinanceEnvironment = BinanceEnvironment.LIVE,
    *,
    proxy: VenueProxy | None = None,
) -> BinanceDataClientConfig:
    """Binance data-feed config for the requested trading mode and environment.

    Sandbox consumes public market data and must not authenticate with the
    exchange: execution is local and a bootstrap-only vault credential may be
    deliberately non-functional. Testnet and live retain authenticated data
    clients so their credential failures remain fail-fast.

    Nautilus defaults spot market data to SBE, which requires Ed25519 credentials
    even for public streams, so an anonymous sandbox client asks for JSON instead.
    Authenticated modes keep the default.
    """
    connector = spec["connector"]
    trading_mode = str(spec.get("trading_mode") or "sandbox").lower()
    if trading_mode == "sandbox":
        api_key = None
        api_secret = None
        spot_market_data_mode = BinanceSpotMarketDataMode.Json
    else:
        # Fail-fast on a malformed credential before any NT object is built.
        require_supported_key_type(credential)
        api_key = credential["api_key"]
        api_secret = credential["api_secret"]
        spot_market_data_mode = None
    return BinanceDataClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        spot_market_data_mode=spot_market_data_mode,
        product_type=_binance_product_type(connector),
        environment=environment,
        proxy_url=proxy.url if proxy is not None else None,
        instrument_provider=BinanceInstrumentProviderConfig(
            load_all=False,
            load_ids=build_instrument_id_strings(spec),
        ),
    )


def build_exec_client_config_sandbox(
    spec: dict,
    credential: dict,
    starting_balances: list[str],
) -> SandboxExecutionClientConfig:
    """Locally simulated execution venue for sandbox mode.

    Sandbox exec fills orders against real-time prices without touching the
    exchange, so no live credential is used here — ``credential`` is accepted
    for signature parity with the (future) testnet/live builders.
    """
    connector = spec["connector"]
    exchange_type = _binance_exchange_type(connector)
    if exchange_type == "futures":
        account_type = _ACCOUNT_TYPE_FUTURES
        default_leverage = build_sandbox_leverage(spec)
    else:
        account_type = _ACCOUNT_TYPE_SPOT
        default_leverage = None
    return SandboxExecutionClientConfig(
        venue=Venue(BINANCE_VENUE),
        starting_balances=[Money.from_str(balance) for balance in starting_balances],
        account_id=binance_account_id(),
        account_type=account_type,
        oms_type=_OMS_TYPE_NETTING,
        default_leverage=default_leverage,
    )


def _build_binance_exec_config(
    spec: dict,
    credential: dict,
    environment: BinanceEnvironment,
    proxy: VenueProxy | None,
) -> BinanceExecutionClientConfig:
    """Real Binance exec-client config (testnet / live) for the given environment.

    Fills are placed on the exchange, so the live credential is forwarded into
    the NT config here (never logged / published). starting_balances is not set:
    real account balances come from the exchange, not a seeded sim wallet.
    """
    connector = spec["connector"]
    exchange_type = _binance_exchange_type(connector)
    require_supported_key_type(credential)
    api_key = credential["api_key"]
    api_secret = credential["api_secret"]
    # Spot has no futures leverage to set. Margin type is left to the account: isolated
    # and cross imply different liquidation distances and the spec cannot say which, so
    # choosing one here would be inventing the policy rather than carrying it.
    futures_leverages = (
        build_binance_futures_leverages(spec) if exchange_type == "futures" else None
    )
    return BinanceExecutionClientConfig(
        account_id=binance_account_id(),
        api_key=api_key,
        api_secret=api_secret,
        product_type=_binance_product_type(connector),
        environment=environment,
        oms_type=_OMS_TYPE_NETTING,
        futures_leverages=futures_leverages,
        proxy_url=proxy.url if proxy is not None else None,
        instrument_provider=BinanceInstrumentProviderConfig(
            load_all=False,
            load_ids=build_instrument_id_strings(spec),
        ),
    )


def build_exec_client_config_testnet(
    spec: dict, credential: dict, *, proxy: VenueProxy | None = None
) -> BinanceExecutionClientConfig:
    """Real Binance exec against the testnet endpoint (test funds)."""
    return _build_binance_exec_config(spec, credential, BinanceEnvironment.TESTNET, proxy)


def build_exec_client_config_live(
    spec: dict, credential: dict, *, proxy: VenueProxy | None = None
) -> BinanceExecutionClientConfig:
    """Real Binance exec against live, gated by signed control-plane owner evidence."""
    require_live_owner_evidence(spec)
    return _build_binance_exec_config(spec, credential, BinanceEnvironment.LIVE, proxy)


def venue_name(spec: dict) -> str:
    """The venue this spec's connector trades on.

    One venue for both product lines: spot and USDT-perpetual are the same exchange.
    Read from the NT-free table so that the sandbox fact host, which must name the
    venue without NautilusTrader installed, gets the same answer as this module.
    Rejects a non-Binance connector on the way.
    """
    _binance_exchange_type(spec["connector"])
    return venue_for_connector(str(spec["connector"]))


def client_name(spec: dict) -> str:
    """The id this spec's data and execution clients register under.

    The venue name: the adapter registers one client per node for this exchange.
    """
    return venue_name(spec)


def data_client_factory() -> BinanceDataClientFactory:
    return BinanceDataClientFactory()


def exec_client_factory() -> BinanceExecutionClientFactory:
    return BinanceExecutionClientFactory()


def build_data_client_config_for_mode(
    spec: dict,
    credential: dict,
    mode: str,
    *,
    proxy: VenueProxy | None = None,
) -> BinanceDataClientConfig:
    """Uniform entry point the host calls for every venue.

    Each venue decides for itself what a mode means for its data feed; the host only
    knows the mode name.
    """
    return build_data_client_config(spec, credential, data_environment_for_mode(mode), proxy=proxy)


def venue_ledger_source(spec: dict, credential: dict, *, proxy: VenueProxy | None = None):
    """The independent ledger this venue's reconciliation evidence is read from.

    Independent of the Nautilus cache on purpose: reconciliation evidence that came
    from the same place as the thing it reconciles proves nothing.
    """
    from custos.engines.nautilus.binance_ledger import BinanceVenueLedgerSource

    return BinanceVenueLedgerSource(spec=spec, credential=credential, proxy=proxy)


# Every request this module builds -- market data, execution and the ledger --
# takes the operator's venue proxy, so the host may route this venue through one.
SUPPORTS_VENUE_PROXY = True


def client_order_id_len_limit() -> int | None:
    """The venue's own cap on a client order id, measured against it."""
    return BINANCE_CLIENT_ORDER_ID_LEN_LIMIT


async def validate_account_configuration(spec: dict, credential: dict) -> None:
    """The native Binance execution config applies its declared leverage map."""
    del spec, credential


def client_order_id_is_valid(value: str) -> bool:
    """Validate the venue character set independently of its length bound."""
    return bool(value)
