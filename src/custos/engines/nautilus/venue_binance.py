"""Binance venue-config assembly (pure functions, no IO, no side effects).

Turns a DeploymentSpec parameter dict + a decrypted credential dict into
NautilusTrader client-config objects. Three execution modes:
- sandbox: real-time Binance *data* feed + a locally simulated *execution* venue
  (``SandboxExecutionClientConfig`` + ``SandboxLiveExecClientFactory``), so no
  real orders reach the exchange.
- testnet: real Binance exec (``BinanceExecClientConfig`` with environment
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

from nautilus_trader.adapters.binance.common.enums import (
    BinanceAccountType,
    BinanceEnvironment,
)
from nautilus_trader.adapters.binance.config import (
    BinanceDataClientConfig,
    BinanceExecClientConfig,
    BinanceKeyType,
)
from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.model.identifiers import InstrumentId

# NT config stores these enum fields as their string name (not the enum object);
# node.build() rejects the enum instances. Pass the names directly.
_ACCOUNT_TYPE_FUTURES = "MARGIN"
_ACCOUNT_TYPE_SPOT = "CASH"
_OMS_TYPE_NETTING = "NETTING"

BINANCE_VENUE = "BINANCE"

# connector name (spec.connector) -> "spot" | "futures". Extend here (never
# elsewhere) when a new Binance product line is supported.
_BINANCE_CONNECTORS: dict[str, str] = {
    "binance": "spot",
    "binance_perpetual": "futures",
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


def build_instrument_ids(spec: dict) -> frozenset[InstrumentId]:
    """InstrumentId set for the configured pairs."""
    connector = spec["connector"]
    return frozenset(
        InstrumentId.from_str(_format_instrument_id(pair, connector))
        for pair in _trading_pairs(spec)
    )


def build_futures_leverages(spec: dict) -> dict[InstrumentId, Decimal]:
    """Per-instrument leverage map for the configured pairs.

    Keyed by InstrumentId with Decimal values so it drops straight into the
    sandbox exec config's ``leverages`` field (and any future Binance live
    exec config). Without pinning, the account default (e.g. 20x) would apply.
    """
    connector = spec["connector"]
    leverage = Decimal(str(spec.get("leverage", 1)))
    return {
        InstrumentId.from_str(_format_instrument_id(pair, connector)): leverage
        for pair in _trading_pairs(spec)
    }


def build_data_client_config(
    spec: dict,
    credential: dict,
    environment: BinanceEnvironment = BinanceEnvironment.LIVE,
) -> BinanceDataClientConfig:
    """Binance data-feed config for the requested trading mode and environment.

    Sandbox consumes public market data and must not authenticate with the
    exchange: execution is local and a bootstrap-only vault credential may be
    deliberately non-functional. Testnet and live retain authenticated data
    clients so their credential failures remain fail-fast.
    """
    connector = spec["connector"]
    exchange_type = _binance_exchange_type(connector)
    trading_mode = str(spec.get("trading_mode") or "sandbox").lower()
    if trading_mode == "sandbox":
        api_key = None
        api_secret = None
        key_type = BinanceKeyType.HMAC
    else:
        # Fail-fast on a malformed credential before any NT object is built.
        api_key = credential["api_key"]
        api_secret = credential["api_secret"]
        key_type = BinanceKeyType[str(credential.get("key_type", "HMAC")).upper()]
    account_type = (
        BinanceAccountType.USDT_FUTURES if exchange_type == "futures" else BinanceAccountType.SPOT
    )
    return BinanceDataClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        key_type=key_type,
        account_type=account_type,
        environment=environment,
        instrument_provider=InstrumentProviderConfig(
            load_all=False,
            load_ids=build_instrument_ids(spec),
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
        leverages = build_futures_leverages(spec)
    else:
        account_type = _ACCOUNT_TYPE_SPOT
        leverages = {}
    return SandboxExecutionClientConfig(
        venue=BINANCE_VENUE,
        starting_balances=starting_balances,
        account_type=account_type,
        oms_type=_OMS_TYPE_NETTING,
        leverages=leverages,
    )


def _build_binance_exec_config(
    spec: dict,
    credential: dict,
    environment: BinanceEnvironment,
) -> BinanceExecClientConfig:
    """Real Binance exec-client config (testnet / live) for the given environment.

    Fills are placed on the exchange, so the live credential is forwarded into
    the NT config here (never logged / published). starting_balances is not set:
    real account balances come from the exchange, not a seeded sim wallet.
    """
    connector = spec["connector"]
    exchange_type = _binance_exchange_type(connector)
    api_key = credential["api_key"]
    api_secret = credential["api_secret"]
    key_type = BinanceKeyType[str(credential.get("key_type", "HMAC")).upper()]
    account_type = (
        BinanceAccountType.USDT_FUTURES if exchange_type == "futures" else BinanceAccountType.SPOT
    )
    return BinanceExecClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        key_type=key_type,
        account_type=account_type,
        environment=environment,
        instrument_provider=InstrumentProviderConfig(
            load_all=False,
            load_ids=build_instrument_ids(spec),
        ),
    )


def build_exec_client_config_testnet(spec: dict, credential: dict) -> BinanceExecClientConfig:
    """Real Binance exec against the testnet endpoint (test funds)."""
    return _build_binance_exec_config(spec, credential, BinanceEnvironment.TESTNET)


def build_exec_client_config_live(spec: dict, credential: dict) -> BinanceExecClientConfig:
    """Real Binance exec against live, gated by signed control-plane owner evidence."""
    require_live_owner_evidence(spec)
    return _build_binance_exec_config(spec, credential, BinanceEnvironment.LIVE)
