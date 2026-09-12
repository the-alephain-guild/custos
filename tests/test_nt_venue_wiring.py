"""Venue-config helpers — pure functions from spec+credential to NT client configs.

Opens with the cross-venue drift guard: the host's per-mode venue allow-list is what
execution admission answers from, and it is a plain set of strings with no venue code
behind it. These tests hold it against what the venue modules can actually build, so
widening one side alone turns red instead of admitting a deployment that then fails at
assembly (or refusing one that would have worked).

The rest covers the failure-mode contract for the Binance venue-assembly layer:
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


# Every venue module this runner dispatches to. Read from the host's own dispatch
# table rather than listed here, so a venue added there without a row below cannot
# pass by being forgotten.
def _venue_modules() -> dict[str, object]:
    from custos.engines.nautilus.host import _venue_module_for
    from custos.engines.nautilus.venues import VENUE_MODULE_BY_CONNECTOR

    return {connector: _venue_module_for(connector) for connector in VENUE_MODULE_BY_CONNECTOR}


@pytest.mark.parametrize("mode", ["sandbox", "testnet", "live"])
def test_the_allow_list_for_a_mode_is_exactly_what_the_venue_modules_wire(mode: str) -> None:
    from custos.engines.nautilus.host import _VENUES_BY_MODE

    wired: set[str] = set()
    for module in set(_venue_modules().values()):
        wired |= module.CONNECTORS_BY_MODE[mode]
    assert _VENUES_BY_MODE[mode] == frozenset(wired)


@pytest.mark.parametrize("mode", ["sandbox", "testnet", "live"])
def test_every_allowed_connector_can_actually_build_that_modes_exec_config(mode: str) -> None:
    """Declaring a mode is not wiring it — the builder has to exist and return.

    The allow-list and CONNECTORS_BY_MODE are both declarations, so checking them
    against each other alone would let a pair of matching lies through. This asks the
    venue module to build the thing.
    """
    from custos.engines.nautilus.host import _VENUES_BY_MODE, _venue_module_for

    for connector in _VENUES_BY_MODE[mode]:
        module = _venue_module_for(connector)
        spec = _spec_for(connector)
        if mode == "sandbox":
            config = module.build_exec_client_config_sandbox(
                spec, _credential_for(connector), ["10_000 USDT"]
            )
        elif mode == "testnet":
            config = module.build_exec_client_config_testnet(spec, _credential_for(connector))
        else:
            config = module.build_exec_client_config_live(spec, _credential_for(connector))
        assert config is not None


def test_a_venue_outside_a_modes_allow_list_refuses_to_build_that_mode() -> None:
    """The second layer: SoDEX has no live delivery and says so itself.

    Without this the allow-list would be the only thing standing between a widened
    set and a live order path with no promotion gate behind it.
    """
    from custos.engines.nautilus.host import _VENUES_BY_MODE, _venue_module_for

    sodex = _venue_module_for("sodex_perpetual")
    assert "sodex_perpetual" not in _VENUES_BY_MODE["live"]
    with pytest.raises(NotImplementedError):
        sodex.build_exec_client_config_live(_spec_for("sodex_perpetual"), _credential_for("sodex"))


def test_the_live_allow_list_is_unchanged_by_adding_a_sandbox_only_venue() -> None:
    """Binance's live capability must cost nothing when a new venue arrives."""
    from custos.engines.nautilus.host import _VENUES_BY_MODE

    assert _VENUES_BY_MODE["live"] == frozenset(_BINANCE_CONNECTORS)


def _spec_for(connector: str) -> dict:
    if connector.startswith("sodex"):
        pair = "vBTC_vUSDC" if connector == "sodex" else "BTC-USD"
        return {
            "connector": connector,
            "pairs": [pair],
            "wallet_address": "0x" + "a" * 40,
            "sodex_account_id": 4242,
        }
    return _approved_spec(connector)


def _credential_for(connector: str) -> dict:
    del connector
    return _credential()


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


def test_testnet_pins_the_declared_leverage() -> None:
    """The declared leverage has to reach the exchange, not just the simulator.

    Measured on testnet 2026-08-01: the spec said ``leverage: 3`` and the exchange
    reported initial margin 447.77 against a notional of 447.77 at mark -- a margin
    ratio of 1.0, so 1x. The map was being built and then only handed to the sandbox,
    leaving the account default in force everywhere it mattered.

    It matters beyond position size: the strategy's startup check estimates liquidation
    distance as 1/leverage to decide whether a fixed stop can trigger in time. That
    check reads the spec, and nothing was making the venue agree with it.
    """
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


@pytest.mark.parametrize("key_type", ["HMAC", "hmac", "ED25519"])
def test_a_supported_key_type_is_accepted(monkeypatch, key_type: str) -> None:
    """The declared key type now goes nowhere, so this module has to read it.

    1.x carried it into the client config. 2.0 dropped the field and reads the key
    material instead, which means a key type 2.0 cannot sign with would only surface
    at the exchange, in a message about the credential rather than about the type.
    """
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


def test_the_nt_free_venue_names_match_the_adapters_own_constants() -> None:
    """The table in ``venues`` duplicates constants the adapters define.

    It has to: admission and the sandbox fact host answer on a base install where
    NautilusTrader is absent, so they cannot read the adapter's own names. The
    duplication is therefore held here rather than by care.
    """
    from nautilus_trader.adapters.sodex import SODEX_PERPS, SODEX_SPOT

    from custos.engines.nautilus.venue_binance import BINANCE_VENUE
    from custos.engines.nautilus.venues import VENUE_BY_CONNECTOR

    assert VENUE_BY_CONNECTOR == {
        "binance": BINANCE_VENUE,
        "binance_perpetual": BINANCE_VENUE,
        "sodex": SODEX_SPOT,
        "sodex_perpetual": SODEX_PERPS,
    }


def test_the_venue_modules_and_the_nt_free_table_answer_alike() -> None:
    """Two ways to ask the same question must not be two answers."""
    from custos.engines.nautilus.host import _venue_module_for
    from custos.engines.nautilus.venues import VENUE_BY_CONNECTOR

    for connector, venue in VENUE_BY_CONNECTOR.items():
        assert _venue_module_for(connector).venue_name({"connector": connector}) == venue
