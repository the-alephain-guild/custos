"""SoDEX venue-config helpers — pure functions from spec+credential to NT config.

The venue differs from Binance in three ways this module has to get right, and each
one is a way to be silently wrong rather than loudly broken:

- spot and perps are separate NT venues with different symbol vocabularies, and a
  wrong symbol loads an empty instrument set rather than raising;
- the execution config resolves any field left unset from the process environment,
  so an incomplete credential would trade with whatever the host happens to hold;
- the venue's own coins are unknown to the engine until an instrument has been
  loaded, which is after the sandbox wallet is built.

Requires the nautilus extra; skipped cleanly when it is absent.
"""

from __future__ import annotations

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.adapters.sodex import (  # noqa: E402
    Network,
    SodexDataClientFactory,
    SodexExecutionClientFactory,
)
from nautilus_trader.model import AccountType, Money  # noqa: E402

from custos.engines.nautilus import venue_sodex  # noqa: E402


def _spec(connector: str = "sodex_perpetual", **overrides) -> dict:
    spec = {
        "connector": connector,
        "pairs": ["BTC-USD"] if connector == "sodex_perpetual" else ["vBTC_vUSDC"],
        "wallet_address": "0x" + "a" * 40,
        "sodex_account_id": 4242,
    }
    spec.update(overrides)
    spec["nautilus_config"] = {
        "venue": {
            "wallet_address": spec.pop("wallet_address"),
            "sodex_account_id": spec.pop("sodex_account_id"),
            "settlement_currency": "VUSDC",
        }
    }
    return spec


def _credential(**overrides) -> dict:
    credential = {
        "api_key": "registered-key-name",
        "api_secret": "0x" + "1" * 64,
        "permission_scope": "trade_no_withdraw",
    }
    credential.update(overrides)
    return credential


def test_the_two_engines_are_separate_venues() -> None:
    assert venue_sodex.venue_name(_spec("sodex")) == "SODEX_SPOT"
    assert venue_sodex.venue_name(_spec("sodex_perpetual")) == "SODEX_PERPS"


def test_the_spec_pairs_are_the_venues_own_symbols_and_are_used_verbatim() -> None:
    """No BASE-QUOTE translation: the two engines name the same market differently.

    Spot lists the venue's v-prefixed tokens joined by an underscore; perps lists a
    dash-joined pair quoted in USD. Deriving either from "BTC-USDT" would be inventing
    the quote asset.
    """
    assert venue_sodex.build_instrument_id_strings(_spec("sodex")) == ("vBTC_vUSDC.SODEX_SPOT",)
    assert venue_sodex.build_instrument_id_strings(_spec("sodex_perpetual")) == (
        "BTC-USD.SODEX_PERPS",
    )


def test_the_data_feed_carries_no_credential_in_any_mode() -> None:
    """Red line 0.1: the venue serves market data unsigned, so nothing is forwarded.

    The config takes no credential parameter at all, which is asserted by handing it
    one and observing the call still type-checks and the object exposes none.
    """
    config = venue_sodex.build_data_client_config_for_mode(_spec(), _credential(), "sandbox")
    assert not hasattr(config, "api_key")
    assert not hasattr(config, "api_private_key")


def test_sandbox_reads_production_prices_and_testnet_reads_the_testnet_gateway() -> None:
    """Sandbox fills locally and needs real prices; testnet's instruments must match."""
    assert (
        venue_sodex.build_data_client_config_for_mode(_spec(), _credential(), "sandbox").network
        == Network.MAINNET
    )
    assert (
        venue_sodex.build_data_client_config_for_mode(_spec(), _credential(), "testnet").network
        == Network.TESTNET
    )


def test_live_requires_owner_evidence_before_native_execution_config() -> None:
    assert venue_sodex.network_for_mode("live") == Network.MAINNET
    with pytest.raises(RuntimeError, match="live_owner_evidence_missing"):
        venue_sodex.build_exec_client_config_live(_spec(), _credential())


def test_a_non_sodex_connector_is_refused_by_name() -> None:
    with pytest.raises(NotImplementedError, match="binance_perpetual"):
        venue_sodex.venue_name(_spec("binance_perpetual"))


@pytest.mark.parametrize(
    ("field", "source"),
    [("api_key", "credential"), ("api_secret", "credential")],
)
def test_a_missing_credential_field_fails_before_any_nt_object_exists(
    field: str, source: str, monkeypatch
) -> None:
    """Red line: the adapter falls back to the environment, and this must not.

    ``SodexExecClientConfig`` resolves any unset field from ``SODEX_API_KEY_NAME`` and
    friends. That is a convenience for the adapter's own examples and a hazard for a
    runner: an incomplete vault credential would silently trade with whatever the host
    environment happens to hold. The env vars are set here precisely so that a
    regression would be observable — without them a fallback would merely fail later
    and look like the same refusal.
    """
    del source
    monkeypatch.setenv("SODEX_API_KEY_NAME", "ambient-key")
    monkeypatch.setenv("SODEX_API_PRIVATE_KEY", "0x" + "9" * 64)
    monkeypatch.setenv("SODEX_ACCOUNT_ID", "9999")
    monkeypatch.setenv("SODEX_WALLET_ADDRESS", "0x" + "b" * 40)
    credential = _credential()
    credential.pop(field)
    with pytest.raises(RuntimeError, match=field):
        venue_sodex.build_exec_client_config_testnet(_spec(), credential)


@pytest.mark.parametrize("field", ["wallet_address", "sodex_account_id"])
def test_a_missing_account_identifier_fails_before_any_nt_object_exists(field: str) -> None:
    """The wallet signs nothing, and is required anyway.

    Account reads are keyed by it, and pointing them at the API key's address answers
    200 with an empty account — which reconciliation would read as flat.
    """
    spec = _spec()
    spec["nautilus_config"]["venue"].pop(field)
    with pytest.raises(RuntimeError, match=field):
        venue_sodex.build_exec_client_config_testnet(spec, _credential())


def test_a_non_numeric_account_id_is_refused_with_what_was_expected() -> None:
    with pytest.raises(RuntimeError, match="numeric account id"):
        venue_sodex.build_exec_client_config_testnet(
            _spec(sodex_account_id="not-a-number"), _credential()
        )


def test_the_testnet_exec_config_carries_the_credential_and_reports_it_present() -> None:
    config = venue_sodex.build_exec_client_config_testnet(_spec(), _credential())
    assert config.has_credentials() is True
    assert config.network == Network.TESTNET
    assert config.venue == "SODEX_PERPS"
    # Red line 0.1: the repr is what ends up in a traceback or a log line.
    assert "1" * 64 not in repr(config)


def test_the_sandbox_wallet_can_be_denominated_in_a_coin_only_this_venue_defines() -> None:
    """The spot engine quotes vUSDC, which the engine does not know at config time.

    The adapter registers the venue's coins while parsing an instrument, which happens
    after the sandbox wallet is built. Without registering it here, every spot sandbox
    deployment would fail on "Unknown currency" before reaching the venue.
    """
    config = venue_sodex.build_exec_client_config_sandbox(
        _spec("sodex"), _credential(), ["10_000 vUSDC"]
    )
    assert config.account_type == AccountType.CASH
    assert Money.from_str("1 vUSDC").currency.code == "vUSDC"


def test_a_perps_sandbox_margins_its_positions() -> None:
    config = venue_sodex.build_exec_client_config_sandbox(
        _spec("sodex_perpetual"), _credential(), ["10_000 USD"]
    )
    assert config.account_type == AccountType.MARGIN


def test_a_malformed_starting_balance_says_what_was_expected() -> None:
    with pytest.raises(RuntimeError, match="<amount> <currency>"):
        venue_sodex.build_exec_client_config_sandbox(_spec(), _credential(), ["10000"])


def test_the_factories_are_the_adapters_own() -> None:
    assert isinstance(venue_sodex.data_client_factory(), SodexDataClientFactory)
    assert isinstance(venue_sodex.exec_client_factory(), SodexExecutionClientFactory)
