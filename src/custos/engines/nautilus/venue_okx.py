"""OKX spot and linear perpetual configuration; secrets are supplied only by the vault."""

from __future__ import annotations

import os
from decimal import Decimal

from nautilus_trader.adapters.okx import (
    OKXDataClientConfig,
    OKXDataClientFactory,
    OKXEnvironment,
    OKXExecutionClientConfig,
    OKXExecutionClientFactory,
    OKXInstrumentType,
    OKXMarginMode,
    OKXRegion,
)
from nautilus_trader.adapters.sandbox import SandboxExecutionClientConfig
from nautilus_trader.model import AccountId, AccountType, Money, OmsType, Venue

from custos.engines.nautilus.venue_config import (
    require_credential,
    require_live_owner_evidence,
    venue_options,
)

CONNECTORS_BY_MODE = {
    mode: frozenset({"okx", "okx_perpetual"}) for mode in ("sandbox", "testnet", "live")
}
_REGIONS = {"global": OKXRegion.GLOBAL, "eea": OKXRegion.EEA, "us": OKXRegion.US}
_MARGINS = {"cross": OKXMarginMode.CROSS, "isolated": OKXMarginMode.ISOLATED}


def _settings(spec: dict) -> tuple[object, object]:
    options = venue_options(spec, {"region", "margin_mode"})
    region = str(options.get("region", "global"))
    margin = str(options.get("margin_mode", "cross"))
    if region not in _REGIONS or margin not in _MARGINS:
        raise ValueError("unsupported OKX region or margin mode")
    return _REGIONS[region], _MARGINS[margin]


def _type(spec: dict) -> OKXInstrumentType:
    connector = spec["connector"]
    if connector == "okx":
        if spec.get("leverage", 1) != 1:
            raise ValueError("OKX spot does not support leveraged sizing")
        return OKXInstrumentType.SPOT
    if connector == "okx_perpetual":
        return OKXInstrumentType.SWAP
    raise ValueError("unsupported OKX connector")


def environment_for_mode(mode: str) -> OKXEnvironment:
    if mode not in CONNECTORS_BY_MODE:
        raise ValueError("unsupported OKX trading mode")
    return OKXEnvironment.DEMO if mode == "testnet" else OKXEnvironment.LIVE


def build_instrument_id_strings(spec: dict) -> tuple[str, ...]:
    perpetual = _type(spec) == OKXInstrumentType.SWAP
    pairs = spec.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("OKX requires deployment pairs")
    result = []
    for pair in pairs:
        symbol = str(pair).upper().replace("/", "-")
        if symbol.endswith("-SWAP"):
            symbol = symbol.removesuffix("-SWAP")
        parts = symbol.split("-")
        if len(parts) != 2 or not all(p.isalnum() for p in parts):
            raise ValueError("OKX pairs must use BASE-QUOTE")
        if perpetual and parts[1] not in {"USDT", "USDC"}:
            raise ValueError("OKX perpetual execution requires a linear settlement pair")
        result.append(f"{symbol}{'-SWAP' if perpetual else ''}.OKX")
    return tuple(result)


def venue_name(spec: dict) -> str:
    _type(spec)
    return "OKX"


def client_name(spec: dict) -> str:
    return venue_name(spec)


def data_client_factory() -> OKXDataClientFactory:
    return OKXDataClientFactory()


def exec_client_factory() -> OKXExecutionClientFactory:
    return OKXExecutionClientFactory()


def build_data_client_config_for_mode(
    spec: dict, credential: dict, mode: str
) -> OKXDataClientConfig:
    if mode == "sandbox" and any(
        name in os.environ for name in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE")
    ):
        raise ValueError("sandbox OKX data must not inherit exchange credentials")
    region, _ = _settings(spec)
    instruments = build_instrument_id_strings(spec)
    secrets = (
        {}
        if mode == "sandbox"
        else {
            name: require_credential(credential, name)
            for name in ("api_key", "api_secret", "api_passphrase")
        }
    )
    return OKXDataClientConfig(
        instrument_types=[_type(spec)],
        environment=environment_for_mode(mode),
        region=region,
        instrument_families=[
            value.removesuffix(".OKX").removesuffix("-SWAP") for value in instruments
        ]
        if _type(spec) == OKXInstrumentType.SWAP
        else None,
        **secrets,
    )


def _exec(spec: dict, credential: dict, mode: str) -> OKXExecutionClientConfig:
    region, margin = _settings(spec)
    build_instrument_id_strings(spec)
    if credential.get("permission_scope") != "trade_no_withdraw":
        raise ValueError("OKX credential requires trade_no_withdraw")
    return OKXExecutionClientConfig(
        account_id=AccountId("OKX-001"),
        instrument_types=[_type(spec)],
        environment=environment_for_mode(mode),
        region=region,
        margin_mode=margin if _type(spec) == OKXInstrumentType.SWAP else OKXMarginMode.NONE,
        **{
            name: require_credential(credential, name)
            for name in ("api_key", "api_secret", "api_passphrase")
        },
    )


def build_exec_client_config_testnet(spec: dict, credential: dict) -> OKXExecutionClientConfig:
    return _exec(spec, credential, "testnet")


def build_exec_client_config_live(spec: dict, credential: dict) -> OKXExecutionClientConfig:
    require_live_owner_evidence(spec)
    return _exec(spec, credential, "live")


def build_exec_client_config_sandbox(
    spec: dict, credential: dict, starting_balances: list[str]
) -> SandboxExecutionClientConfig:
    del credential
    perpetual = _type(spec) == OKXInstrumentType.SWAP
    build_instrument_id_strings(spec)
    return SandboxExecutionClientConfig(
        venue=Venue("OKX"),
        account_id=AccountId("OKX-001"),
        starting_balances=[Money.from_str(value) for value in starting_balances],
        account_type=AccountType.MARGIN if perpetual else AccountType.CASH,
        oms_type=OmsType.NETTING,
        default_leverage=Decimal(str(spec.get("leverage", 1))) if perpetual else None,
    )


def venue_ledger_source(spec: dict, credential: dict):
    from custos.engines.nautilus.okx_ledger import OkxVenueLedgerSource

    return OkxVenueLedgerSource(spec, credential)


def client_order_id_len_limit() -> int:
    # The shared order gate rejects lengths at or above this threshold.
    return 33


async def validate_account_configuration(spec: dict, credential: dict) -> None:
    if spec.get("trading_mode") == "sandbox":
        return
    import asyncio

    provider = venue_ledger_source(spec, credential)
    await asyncio.to_thread(provider.validate_account)


def client_order_id_is_valid(value: str) -> bool:
    """Validate the venue character set independently of its length bound."""
    return value.isascii() and value.isalnum()
