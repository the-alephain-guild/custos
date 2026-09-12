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

from nautilus_trader.adapters.binance import (  # noqa: E402
    BinanceEnvironment,
    BinanceProductType,
)
from nautilus_trader.model import AccountType, Money, Venue  # noqa: E402

from custos.engines.nautilus import venue_binance  # noqa: E402
from custos.engines.nautilus.venue_binance import (  # noqa: E402
    _BINANCE_CONNECTORS,
    binance_account_id,
    build_binance_futures_leverages,
    build_data_client_config,
    build_exec_client_config_live,
    build_exec_client_config_sandbox,
    build_exec_client_config_testnet,
    build_instrument_id_strings,
    build_sandbox_leverage,
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


def _captured_kwargs(monkeypatch, config_name: str) -> dict:
    """Record what is handed to an NT config constructor.

    2.0's config objects do not read the credential back: there is no ``api_key``
    attribute and the repr redacts it (asserted separately). That is the behaviour
    red line 0.1 wants, and it also means the only place to observe what this module
    forwarded is the call itself.
    """
    captured: dict = {}
    original = getattr(venue_binance, config_name)

    def _record(**kwargs):
        captured.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(venue_binance, config_name, _record)
    return captured


def test_build_instrument_ids() -> None:
    ids = build_instrument_id_strings(_spec("binance_perpetual"))
    assert ids == ("BTCUSDT-PERP.BINANCE", "ETHUSDT-PERP.BINANCE")


def test_build_instrument_ids_spot() -> None:
    ids = build_instrument_id_strings(_spec("binance"))
    assert ids == ("BTCUSDT.BINANCE", "ETHUSDT.BINANCE")


def test_build_instrument_ids_singular_pair_fallback() -> None:
    ids = build_instrument_id_strings({"connector": "binance_perpetual", "pair": "SOL-USDT"})
    assert ids == ("SOLUSDT-PERP.BINANCE",)


def test_the_sandbox_leverage_is_the_declared_one() -> None:
    assert build_sandbox_leverage(_spec("binance_perpetual")) == Decimal("3")


def test_build_data_client_config_perpetual(monkeypatch) -> None:
    spec = _spec("binance_perpetual")
    spec["trading_mode"] = "live"
    forwarded = _captured_kwargs(monkeypatch, "BinanceDataClientConfig")
    cfg = build_data_client_config(spec, _credential())
    assert forwarded["api_key"] == "test-key"
    assert forwarded["api_secret"] == "test-secret"
    assert cfg.product_type == BinanceProductType.USD_M
    assert cfg.environment == BinanceEnvironment.LIVE
    assert cfg.instrument_provider.load_all is False
    assert "BTCUSDT-PERP.BINANCE" in cfg.instrument_provider.load_ids


def test_build_data_client_config_spot() -> None:
    spec = _spec("binance")
    spec["trading_mode"] = "live"
    cfg = build_data_client_config(spec, _credential())
    assert cfg.product_type == BinanceProductType.SPOT


def test_build_data_client_config_sandbox_uses_anonymous_public_feed(monkeypatch) -> None:
    spec = _spec("binance_perpetual")
    spec["trading_mode"] = "sandbox"
    forwarded = _captured_kwargs(monkeypatch, "BinanceDataClientConfig")

    build_data_client_config(spec, _credential())

    assert forwarded["api_key"] is None
    assert forwarded["api_secret"] is None


def test_build_exec_client_config_sandbox_futures() -> None:
    cfg = build_exec_client_config_sandbox(
        _spec("binance_perpetual"), _credential(), ["10_000 USDT"]
    )
    assert cfg.venue == Venue("BINANCE")
    assert cfg.account_id == binance_account_id()
    assert cfg.account_type == AccountType.MARGIN
    assert cfg.starting_balances == [Money.from_str("10_000 USDT")]
    # 2.0 replaced the per-instrument map with one account-wide default. The spec
    # carries a single leverage, so the old map said this same thing per instrument.
    assert cfg.default_leverage == Decimal("3")


def test_build_exec_client_config_sandbox_spot_is_cash() -> None:
    cfg = build_exec_client_config_sandbox(_spec("binance"), _credential(), ["10_000 USDT"])
    assert cfg.account_type == AccountType.CASH


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
    assert cfg.product_type == BinanceProductType.USD_M
    assert cfg.account_id == binance_account_id()


def test_testnet_env_pin_spot() -> None:
    cfg = build_exec_client_config_testnet(_spec("binance"), _credential())
    assert cfg.environment == BinanceEnvironment.TESTNET
    assert cfg.product_type == BinanceProductType.SPOT


def test_live_env_pin() -> None:
    cfg = build_exec_client_config_live(_approved_spec("binance_perpetual"), _credential())
    assert cfg.environment == BinanceEnvironment.LIVE
    assert cfg.product_type == BinanceProductType.USD_M


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

    assert cfg.futures_leverages == {"BTCUSDT": 3, "ETHUSDT": 3}


def test_live_pins_the_declared_leverage() -> None:
    cfg = build_exec_client_config_live(_approved_spec("binance_perpetual"), _credential())

    assert cfg.futures_leverages == {"BTCUSDT": 3, "ETHUSDT": 3}


def test_the_pinned_leverage_is_keyed_the_way_binance_reads_it() -> None:
    """Each key is sent to the venue as the ``symbol`` of a set-leverage call.

    So it has to be the exchange's own symbol -- ``BTCUSDT`` -- not the instrument id,
    which carries a ``-PERP`` suffix the venue does not know. The 1.x adapter had a
    ``BinanceSymbol`` type that stripped it; 2.0 takes plain strings, so nothing would
    complain about an id-shaped key until the exchange rejected the call.
    """
    cfg = build_exec_client_config_testnet(_spec("binance_perpetual"), _credential())

    for symbol, value in cfg.futures_leverages.items():
        assert "-PERP" not in symbol, f"{symbol!r} is an instrument id, not a venue symbol"
        assert type(value) is int, f"{value!r} is {type(value).__name__}, not int"
    assert set(cfg.futures_leverages) == {"BTCUSDT", "ETHUSDT"}


def test_the_leverage_map_is_built_from_the_spec_alone() -> None:
    assert build_binance_futures_leverages(_spec("binance_perpetual")) == {
        "BTCUSDT": 3,
        "ETHUSDT": 3,
    }


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


# ---------------------------------------------------------------------------
# Key type
#
# 1.x carried the declared key type into the client config. 2.0 dropped the field
# and reads the key material instead, so the declaration now goes nowhere -- and a
# key type 2.0 cannot sign with would only surface at the exchange, in a message
# about the credential rather than about the type.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key_type", ["HMAC", "hmac", "ED25519"])
def test_a_supported_key_type_is_accepted(monkeypatch, key_type: str) -> None:
    credential = dict(_credential(), key_type=key_type)
    forwarded = _captured_kwargs(monkeypatch, "BinanceExecutionClientConfig")

    build_exec_client_config_testnet(_spec("binance_perpetual"), credential)

    assert forwarded["api_key"] == "test-key"


def test_an_absent_key_type_means_hmac(monkeypatch) -> None:
    credential = {"api_key": "test-key", "api_secret": "test-secret"}
    forwarded = _captured_kwargs(monkeypatch, "BinanceExecutionClientConfig")

    build_exec_client_config_testnet(_spec("binance_perpetual"), credential)

    assert forwarded["api_key"] == "test-key"


def test_rsa_is_refused_here_rather_than_at_the_exchange() -> None:
    credential = dict(_credential(), key_type="RSA")

    with pytest.raises(RuntimeError, match="key type 'RSA' is not supported"):
        build_exec_client_config_testnet(_spec("binance_perpetual"), credential)


def test_the_data_client_refuses_it_too() -> None:
    """The exec client is not the only authenticated one; testnet and live data
    clients sign as well, so the same credential has to be refused on both paths."""
    spec = dict(_spec("binance_perpetual"), trading_mode="testnet")
    credential = dict(_credential(), key_type="RSA")

    with pytest.raises(RuntimeError, match="key type 'RSA' is not supported"):
        build_data_client_config(spec, credential, BinanceEnvironment.TESTNET)


def test_the_config_objects_do_not_read_the_credential_back() -> None:
    """Red line 0.1, and stronger than it was in 1.x.

    The 2.0 config exposes no ``api_key`` attribute at all and its repr redacts the
    material, so a credential cannot reach a log through a config repr -- which is
    exactly the accident this red line is about. Asserted rather than assumed,
    because the whole authenticated path hands real keys to these constructors.
    """
    spec = dict(_spec("binance_perpetual"), trading_mode="testnet")
    credential = {"api_key": "key-material-abc", "api_secret": "secret-material-xyz"}

    exec_cfg = build_exec_client_config_testnet(spec, credential)
    data_cfg = build_data_client_config(spec, credential, BinanceEnvironment.TESTNET)

    for cfg in (exec_cfg, data_cfg):
        assert not hasattr(cfg, "api_key")
        assert not hasattr(cfg, "api_secret")
        assert "key-material-abc" not in repr(cfg)
        assert "secret-material-xyz" not in repr(cfg)
