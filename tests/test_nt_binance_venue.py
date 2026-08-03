"""Binance venue-config helpers — pure functions from spec+credential to NT config.

Covers the failure-mode contract for the venue-assembly layer:
- sandbox data uses the anonymous public feed and ignores bootstrap credentials
- missing api_key in an authenticated mode -> KeyError (fail-fast, no NT build)
- unsupported connector (non-binance) -> NotImplementedError (explicit reject)

Plus happy-path field assertions on the constructed NT config objects. Requires
the nautilus extra (NautilusTrader); skipped cleanly when it is absent.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.adapters.binance.common.enums import (  # noqa: E402
    BinanceAccountType,
    BinanceEnvironment,
)
from nautilus_trader.adapters.binance.common.symbol import BinanceSymbol
from nautilus_trader.model.identifiers import InstrumentId  # noqa: E402

from custos.engines.nautilus.venue_binance import (  # noqa: E402
    _BINANCE_CONNECTORS,
    build_data_client_config,
    build_exec_client_config_live,
    build_exec_client_config_sandbox,
    build_exec_client_config_testnet,
    build_futures_leverages,
    build_instrument_ids,
    data_environment_for_mode,
    require_live_owner_evidence,
)


def _approved_spec(connector: str = "binance_perpetual") -> dict:
    spec = _spec(connector)
    spec["promotion_id"] = "44444444-4444-4444-8444-444444444444"
    spec["promotion_evidence_digest"] = "a" * 64
    return spec


def test_supported_venues_matches_wired_connectors() -> None:
    # Drift guard: NtTradingNodeHost declares its live-venue capability with an
    # NT-free constant; it must equal the set of connectors this module actually
    # wires, or execution admission would advertise a venue with no exec config behind it.
    from custos.engines.nautilus.host import _SUPPORTED_VENUES

    assert _SUPPORTED_VENUES == frozenset(_BINANCE_CONNECTORS)


def _spec(connector: str = "binance_perpetual") -> dict:
    return {
        "connector": connector,
        "pairs": ["BTC-USDT", "ETH-USDT"],
        "leverage": 3,
    }


def _credential() -> dict:
    return {
        "api_key": "test-key",
        "api_secret": "test-secret",
        "permission_scope": "trade_no_withdraw",
    }


def test_build_instrument_ids() -> None:
    ids = build_instrument_ids(_spec("binance_perpetual"))
    assert ids == frozenset(
        {
            InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
            InstrumentId.from_str("ETHUSDT-PERP.BINANCE"),
        }
    )


def test_build_instrument_ids_spot() -> None:
    ids = build_instrument_ids(_spec("binance"))
    assert ids == frozenset(
        {
            InstrumentId.from_str("BTCUSDT.BINANCE"),
            InstrumentId.from_str("ETHUSDT.BINANCE"),
        }
    )


def test_build_instrument_ids_singular_pair_fallback() -> None:
    ids = build_instrument_ids({"connector": "binance_perpetual", "pair": "SOL-USDT"})
    assert ids == frozenset({InstrumentId.from_str("SOLUSDT-PERP.BINANCE")})


def test_build_futures_leverages_keyed_by_instrument_id() -> None:
    leverages = build_futures_leverages(_spec("binance_perpetual"))
    assert leverages == {
        InstrumentId.from_str("BTCUSDT-PERP.BINANCE"): Decimal("3"),
        InstrumentId.from_str("ETHUSDT-PERP.BINANCE"): Decimal("3"),
    }


def test_build_data_client_config_perpetual() -> None:
    spec = _spec("binance_perpetual")
    spec["trading_mode"] = "live"
    cfg = build_data_client_config(spec, _credential())
    assert cfg.api_key == "test-key"
    assert cfg.api_secret == "test-secret"
    assert cfg.account_type == BinanceAccountType.USDT_FUTURES
    assert cfg.environment == BinanceEnvironment.LIVE
    assert cfg.instrument_provider.load_all is False
    assert InstrumentId.from_str("BTCUSDT-PERP.BINANCE") in cfg.instrument_provider.load_ids


def test_build_data_client_config_spot() -> None:
    spec = _spec("binance")
    spec["trading_mode"] = "live"
    cfg = build_data_client_config(spec, _credential())
    assert cfg.account_type == BinanceAccountType.SPOT


def test_build_data_client_config_sandbox_uses_anonymous_public_feed() -> None:
    spec = _spec("binance_perpetual")
    spec["trading_mode"] = "sandbox"

    cfg = build_data_client_config(spec, _credential())

    assert cfg.api_key is None
    assert cfg.api_secret is None


def test_build_exec_client_config_sandbox_futures() -> None:
    cfg = build_exec_client_config_sandbox(
        _spec("binance_perpetual"), _credential(), ["10_000 USDT"]
    )
    assert cfg.venue == "BINANCE"
    # NT config stores the enum field as its string name; node.build() rejects
    # the enum object, so the helper must emit "MARGIN"/"CASH" (regression guard).
    assert cfg.account_type == "MARGIN"
    assert cfg.starting_balances == ["10_000 USDT"]
    assert cfg.leverages == {
        InstrumentId.from_str("BTCUSDT-PERP.BINANCE"): Decimal("3"),
        InstrumentId.from_str("ETHUSDT-PERP.BINANCE"): Decimal("3"),
    }


def test_build_exec_client_config_sandbox_spot_is_cash() -> None:
    cfg = build_exec_client_config_sandbox(_spec("binance"), _credential(), ["10_000 USDT"])
    assert cfg.account_type == "CASH"


def test_missing_api_key_raises() -> None:
    # Failure-mode contract: credential without api_key -> KeyError (fail-fast).
    bad_credential = {"api_secret": "s", "permission_scope": "trade_no_withdraw"}
    spec = _spec("binance_perpetual")
    spec["trading_mode"] = "live"
    with pytest.raises(KeyError):
        build_data_client_config(spec, bad_credential)


def test_unsupported_connector_notimpl() -> None:
    # Failure-mode contract: non-binance connector -> NotImplementedError (explicit).
    with pytest.raises(NotImplementedError, match="okx"):
        build_data_client_config(_spec("okx_perpetual"), _credential())


def test_testnet_env_pin() -> None:
    cfg = build_exec_client_config_testnet(_spec("binance_perpetual"), _credential())
    assert cfg.environment == BinanceEnvironment.TESTNET
    assert cfg.account_type == BinanceAccountType.USDT_FUTURES
    assert cfg.api_key == "test-key"


def test_testnet_env_pin_spot() -> None:
    cfg = build_exec_client_config_testnet(_spec("binance"), _credential())
    assert cfg.environment == BinanceEnvironment.TESTNET
    assert cfg.account_type == BinanceAccountType.SPOT


def test_live_env_pin() -> None:
    cfg = build_exec_client_config_live(_approved_spec("binance_perpetual"), _credential())
    assert cfg.environment == BinanceEnvironment.LIVE
    assert cfg.account_type == BinanceAccountType.USDT_FUTURES


def test_live_missing_owner_evidence_rejected() -> None:
    with pytest.raises(RuntimeError, match="live_owner_evidence_missing"):
        build_exec_client_config_live(_spec("binance_perpetual"), _credential())


def test_live_missing_evidence_digest_rejected() -> None:
    spec = _spec("binance_perpetual")
    spec["promotion_id"] = "44444444-4444-4444-8444-444444444444"
    with pytest.raises(RuntimeError, match="live_owner_evidence_missing"):
        build_exec_client_config_live(spec, _credential())


def test_live_invalid_evidence_digest_rejected() -> None:
    spec = _spec("binance_perpetual")
    spec["promotion_id"] = "44444444-4444-4444-8444-444444444444"
    spec["promotion_evidence_digest"] = "short"
    with pytest.raises(RuntimeError, match="live_owner_evidence_missing"):
        require_live_owner_evidence(spec)


def test_data_environment_for_mode() -> None:
    # Sandbox drives local sim with real-time live prices; testnet must feed
    # testnet market data so instruments match the testnet exec venue.
    assert data_environment_for_mode("sandbox") == BinanceEnvironment.LIVE
    assert data_environment_for_mode("testnet") == BinanceEnvironment.TESTNET
    assert data_environment_for_mode("live") == BinanceEnvironment.LIVE


def test_data_environment_for_mode_unknown_maps_live() -> None:
    # An unknown mode maps to LIVE data here, but that is never reached for a real
    # deploy: NtTradingNodeHost._build_exec_plan rejects an unknown trading_mode at
    # dispatch (test_deploy_unknown_trading_mode_rejected), so this default only
    # guards the data-env lookup in isolation — asserted explicitly so the safe
    # default is intentional, not accidental.
    assert data_environment_for_mode("paper_trading") == BinanceEnvironment.LIVE


def test_build_data_client_config_testnet_env() -> None:
    spec = _spec("binance_perpetual")
    spec["trading_mode"] = "testnet"
    cfg = build_data_client_config(spec, _credential(), BinanceEnvironment.TESTNET)
    assert cfg.environment == BinanceEnvironment.TESTNET


# ---------------------------------------------------------------------------
# Leverage on a real venue
#
# Measured on testnet 2026-08-01: the spec said `leverage: 3` and the exchange
# reported initial margin 447.77 against a notional of 447.77 at mark -- a margin
# ratio of 1.0, so 1x. The map was being built and then only handed to the sandbox,
# leaving the account default in force everywhere it mattered.
#
# It matters beyond position size: the strategy's startup check estimates liquidation
# distance as 1/leverage to decide whether a fixed stop can trigger in time. That check
# reads the spec. Nothing was making the venue agree with it.
# ---------------------------------------------------------------------------


def test_testnet_pins_the_declared_leverage() -> None:
    cfg = build_exec_client_config_testnet(_spec("binance_perpetual"), _credential())

    assert cfg.futures_leverages == {
        BinanceSymbol("BTCUSDT-PERP"): 3,
        BinanceSymbol("ETHUSDT-PERP"): 3,
    }


def test_live_pins_the_declared_leverage() -> None:
    cfg = build_exec_client_config_live(_approved_spec("binance_perpetual"), _credential())

    assert cfg.futures_leverages == {
        BinanceSymbol("BTCUSDT-PERP"): 3,
        BinanceSymbol("ETHUSDT-PERP"): 3,
    }


def test_the_pinned_leverage_is_keyed_the_way_binance_reads_it() -> None:
    """Not a drop-in from the sandbox map, which is why this was easy to leave unwired.

    ``build_futures_leverages`` is keyed by ``InstrumentId`` with ``Decimal`` values;
    this field wants ``BinanceSymbol`` keys and plain ``int``. BinanceSymbol also drops
    the ``-PERP`` suffix, so the key the venue sees is ``BTCUSDT``. msgspec does not
    validate on direct construction, so handing it the wrong shape would pass here and
    fail somewhere further away.
    """
    cfg = build_exec_client_config_testnet(_spec("binance_perpetual"), _credential())

    for symbol, value in cfg.futures_leverages.items():
        assert isinstance(symbol, BinanceSymbol), f"{symbol!r} is not a BinanceSymbol"
        assert type(value) is int, f"{value!r} is {type(value).__name__}, not int"
    assert set(cfg.futures_leverages) == {"BTCUSDT", "ETHUSDT"}


def test_spot_declares_no_leverage() -> None:
    """There is no futures leverage to set on a spot account."""
    cfg = build_exec_client_config_testnet(_spec("binance"), _credential())

    assert cfg.futures_leverages is None


def test_margin_type_is_left_alone_until_the_spec_can_say() -> None:
    """Isolated and cross give different liquidation distances, and the spec cannot
    express which. Sending one anyway would be inventing the policy here rather than
    reading it, so the account setting stands and the gap stays visible."""
    cfg = build_exec_client_config_testnet(_spec("binance_perpetual"), _credential())

    assert cfg.futures_margin_types is None
