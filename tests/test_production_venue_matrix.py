from __future__ import annotations

import pytest

pytest.importorskip("nautilus_trader")


def spec(connector: str, mode: str) -> dict:
    options = (
        {"region": "global", "margin_mode": "cross"}
        if connector.startswith("okx")
        else {
            "wallet_address": "0x" + "a" * 40,
            "sodex_account_id": 42,
        }
    )
    return {
        "connector": connector,
        "trading_mode": mode,
        "pairs": ["BTC-USDT"],
        "leverage": 1,
        "nautilus_config": {"venue": options},
        "promotion_id": "11111111-1111-4111-8111-111111111111",
        "promotion_evidence_digest": "a" * 64,
    }


@pytest.mark.parametrize("connector", ["okx", "okx_perpetual"])
@pytest.mark.parametrize("mode", ["sandbox", "testnet", "live"])
def test_okx_uses_explicit_native_mode_and_complete_credentials(connector: str, mode: str) -> None:
    from nautilus_trader.adapters.okx import OKXEnvironment, OKXInstrumentType

    from custos.engines.nautilus import venue_okx

    document = spec(connector, mode)
    credential = {
        "api_key": "key",
        "api_secret": "secret",
        "api_passphrase": "phrase",
        "permission_scope": "trade_no_withdraw",
    }
    data = venue_okx.build_data_client_config_for_mode(document, credential, mode)
    assert data.environment == (OKXEnvironment.DEMO if mode == "testnet" else OKXEnvironment.LIVE)
    assert data.instrument_types == [
        OKXInstrumentType.SWAP if connector.endswith("perpetual") else OKXInstrumentType.SPOT
    ]
    if mode == "sandbox":
        assert (
            venue_okx.build_exec_client_config_sandbox(document, credential, ["10000 USDT"])
            is not None
        )
    else:
        build = getattr(venue_okx, f"build_exec_client_config_{mode}")
        assert build(document, credential).environment == data.environment
        for field in ("api_key", "api_secret", "api_passphrase"):
            with pytest.raises(ValueError, match=field):
                build(document, {k: v for k, v in credential.items() if k != field})


@pytest.mark.parametrize("connector", ["sodex", "sodex_perpetual"])
def test_sodex_live_consumes_account_fields_from_signed_engine_config(
    connector: str, monkeypatch
) -> None:
    from nautilus_trader.adapters.sodex import Network

    from custos.engines.nautilus import venue_sodex

    document = spec(connector, "live")
    captured = {}
    native = venue_sodex.SodexExecClientConfig

    def build(**kwargs):
        captured.update(kwargs)
        return native(**kwargs)

    monkeypatch.setattr(venue_sodex, "SodexExecClientConfig", build)
    config = venue_sodex.build_exec_client_config_live(
        document,
        {"api_key": "key", "api_secret": "0x" + "1" * 64, "permission_scope": "trade_no_withdraw"},
    )
    assert config.network == Network.MAINNET
    assert captured["wallet_address"] == "0x" + "a" * 40
    assert captured["account_id"] == 42


@pytest.mark.parametrize("connector", ["okx", "okx_perpetual", "sodex", "sodex_perpetual"])
def test_new_live_connectors_still_require_owner_promotion(connector: str) -> None:
    from custos.engines.nautilus.host import _venue_module_for

    document = spec(connector, "live")
    del document["promotion_evidence_digest"]
    with pytest.raises(RuntimeError, match="live_owner_evidence_missing"):
        _venue_module_for(connector).build_exec_client_config_live(document, {})


def test_sodex_sandbox_perpetual_leverage_matches_the_signed_spec():
    from decimal import Decimal

    from custos.engines.nautilus import venue_sodex

    document = spec("sodex_perpetual", "sandbox")
    document["leverage"] = 3
    config = venue_sodex.build_exec_client_config_sandbox(document, {}, ["10000 vUSDC"])
    assert config.default_leverage == Decimal("3")


@pytest.mark.parametrize(
    "connector,pair",
    [
        ("binance", "BTC-USDT"),
        ("binance_perpetual", "BTC-USDT"),
        ("okx", "BTC-USDT"),
        ("okx_perpetual", "BTC-USDT"),
        ("sodex", "vBTC_vUSDC"),
        ("sodex_perpetual", "BTC-USD"),
    ],
)
def test_strategy_and_host_subscribe_to_the_same_native_instrument(connector, pair):
    from custos_toolkit_nautilus.adapter.utils import instrument_id_str

    from custos.engines.nautilus.host import _venue_module_for

    document = {"connector": connector, "pairs": [pair], "leverage": 1}
    host_ids = _venue_module_for(connector).build_instrument_id_strings(document)
    assert tuple(host_ids) == (instrument_id_str(pair, connector),)
