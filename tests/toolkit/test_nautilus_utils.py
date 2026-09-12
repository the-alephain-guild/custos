"""The engine adapter's utils.

These tests cover utility functions for deriving NautilusTrader types from config.
"""

import pytest

# Skip all tests if nautilus_trader is not installed
pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.utils import (
    derive_bar_type,
    derive_instrument_id,
    get_venue_from_connector,
    instrument_id_str,
    is_futures_connector,
)
from nautilus_trader.model import BarType, InstrumentId


class TestDeriveInstrumentId:
    """Tests for derive_instrument_id function (typed TradingConfig only)."""

    def test_with_struct_config(self):
        """Derive InstrumentId from a real typed TradingConfig."""
        from custos_toolkit_nautilus.adapter.config.trading import TradingConfig

        result = derive_instrument_id(
            TradingConfig(pairs=("BTC-USDT",), connector="binance_perpetual")
        )
        assert isinstance(result, InstrumentId)
        assert str(result) == "BTCUSDT-PERP.BINANCE"

    def test_spot_connector(self):
        """Spot connector yields no -PERP suffix."""
        from custos_toolkit_nautilus.adapter.config.trading import TradingConfig

        result = derive_instrument_id(TradingConfig(pairs=("BTC-USDT",), connector="binance"))
        assert str(result) == "BTCUSDT.BINANCE"

    def test_dict_input_raises(self):
        """Dict input is no longer supported — typed config is the only contract."""
        with pytest.raises(AttributeError):
            derive_instrument_id({"pairs": ["ETH-USDT"], "connector": "okx_perpetual"})


def _platforms(bar_type="1-HOUR", bar_aggregation="EXTERNAL"):
    """Build a real typed PlatformsConfig for derive_bar_type tests."""
    from custos_toolkit_nautilus.adapter.config.platforms import (
        NautilusPlatformConfig,
        PlatformsConfig,
    )

    return PlatformsConfig(
        nautilus=NautilusPlatformConfig(bar_type=bar_type, bar_aggregation=bar_aggregation)
    )


class TestDeriveBarType:
    """Tests for derive_bar_type function (typed PlatformsConfig only)."""

    def test_with_struct_config(self):
        """Derive BarType from a typed platforms config."""
        instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        result = derive_bar_type(_platforms(bar_type="4-HOUR"), instrument_id)
        assert isinstance(result, BarType)
        assert str(result) == "BTCUSDT-PERP.BINANCE-4-HOUR-LAST-EXTERNAL"

    def test_timeframe_override(self):
        """Override takes precedence over the configured bar_type."""
        instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        result = derive_bar_type(
            _platforms(bar_type="1-HOUR"), instrument_id, timeframe_override="4h"
        )
        assert str(result) == "BTCUSDT-PERP.BINANCE-4-HOUR-LAST-EXTERNAL"

    def test_timeframe_override_mapping(self):
        """Common timeframe formats map to Nautilus bar intervals."""
        instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        cfg = _platforms(bar_type="1-HOUR")
        assert "1-MINUTE" in str(derive_bar_type(cfg, instrument_id, "1m"))
        assert "5-MINUTE" in str(derive_bar_type(cfg, instrument_id, "5m"))
        assert "15-MINUTE" in str(derive_bar_type(cfg, instrument_id, "15m"))
        assert "1-HOUR" in str(derive_bar_type(cfg, instrument_id, "1h"))
        assert "4-HOUR" in str(derive_bar_type(cfg, instrument_id, "4h"))
        assert "1-DAY" in str(derive_bar_type(cfg, instrument_id, "1d"))

    def test_bar_aggregation_external_default(self):
        """bar_aggregation defaults to EXTERNAL."""
        instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        result = derive_bar_type(_platforms(bar_type="1-HOUR"), instrument_id)
        assert str(result).endswith("-EXTERNAL")

    def test_bar_aggregation_internal(self):
        """bar_aggregation INTERNAL flows through for sandbox/testnet."""
        instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        result = derive_bar_type(
            _platforms(bar_type="15-MINUTE", bar_aggregation="INTERNAL"), instrument_id
        )
        assert str(result) == "BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-INTERNAL"

    def test_dict_input_raises(self):
        """Dict input is no longer supported — typed config is the only contract."""
        instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        with pytest.raises(AttributeError):
            derive_bar_type({"nautilus": {"bar_type": "1-DAY"}}, instrument_id)


class TestVenueHelpers:
    """Tests for venue helper functions."""

    def test_get_venue_from_connector(self):
        """Test venue mapping from connector names."""
        assert get_venue_from_connector("binance") == "BINANCE"
        assert get_venue_from_connector("binance_perpetual") == "BINANCE"
        assert get_venue_from_connector("okx") == "OKX"
        assert get_venue_from_connector("okx_perpetual") == "OKX"
        assert get_venue_from_connector("bybit") == "BYBIT"
        assert get_venue_from_connector("unknown") == "BINANCE"  # default

    def test_is_futures_connector(self):
        """Test futures connector detection."""
        assert is_futures_connector("binance_perpetual") is True
        assert is_futures_connector("okx_perpetual") is True
        assert is_futures_connector("binance") is False
        assert is_futures_connector("okx") is False


class TestGetBarDurationNs:
    """Tests for get_bar_duration_ns function."""

    def test_minute_aggregation(self):
        """Test get_bar_duration_ns with MINUTE aggregation."""
        from custos_toolkit_nautilus.adapter.utils import get_bar_duration_ns

        bar_type = BarType.from_str("BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL")
        result = get_bar_duration_ns(bar_type)

        # 1 minute = 60 * 1_000_000_000 nanoseconds
        assert result == 60_000_000_000

    def test_minute_aggregation_5_step(self):
        """Test get_bar_duration_ns with 5-MINUTE aggregation."""
        from custos_toolkit_nautilus.adapter.utils import get_bar_duration_ns

        bar_type = BarType.from_str("BTCUSDT-PERP.BINANCE-5-MINUTE-LAST-EXTERNAL")
        result = get_bar_duration_ns(bar_type)

        # 5 minutes = 5 * 60 * 1_000_000_000 nanoseconds
        assert result == 300_000_000_000

    def test_hour_aggregation(self):
        """Test get_bar_duration_ns with HOUR aggregation."""
        from custos_toolkit_nautilus.adapter.utils import get_bar_duration_ns

        bar_type = BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL")
        result = get_bar_duration_ns(bar_type)

        # 1 hour = 3600 * 1_000_000_000 nanoseconds
        assert result == 3_600_000_000_000

    def test_hour_aggregation_4_step(self):
        """Test get_bar_duration_ns with 4-HOUR aggregation."""
        from custos_toolkit_nautilus.adapter.utils import get_bar_duration_ns

        bar_type = BarType.from_str("BTCUSDT-PERP.BINANCE-4-HOUR-LAST-EXTERNAL")
        result = get_bar_duration_ns(bar_type)

        # 4 hours = 4 * 3600 * 1_000_000_000 nanoseconds
        assert result == 14_400_000_000_000

    def test_day_aggregation(self):
        """Test get_bar_duration_ns with DAY aggregation."""
        from custos_toolkit_nautilus.adapter.utils import get_bar_duration_ns

        bar_type = BarType.from_str("BTCUSDT-PERP.BINANCE-1-DAY-LAST-EXTERNAL")
        result = get_bar_duration_ns(bar_type)

        # 1 day = 86400 * 1_000_000_000 nanoseconds
        assert result == 86_400_000_000_000

    def test_unknown_aggregation_returns_zero(self):
        """Test get_bar_duration_ns with unknown aggregation returns 0."""
        from unittest.mock import MagicMock

        from custos_toolkit_nautilus.adapter.utils import get_bar_duration_ns

        # Create a mock bar_type with TICK aggregation
        mock_bar_type = MagicMock()
        mock_bar_spec = MagicMock()
        mock_bar_spec.step = 1
        mock_bar_spec.aggregation = MagicMock()  # Not MINUTE, HOUR, or DAY
        mock_bar_spec.aggregation.name = "TICK"
        mock_bar_type.spec = mock_bar_spec

        result = get_bar_duration_ns(mock_bar_type)

        assert result == 0


class TestDeepAsdict:
    """Tests for deep_asdict function."""

    def test_converts_simple_struct(self):
        """Test deep_asdict with simple msgspec struct."""
        import msgspec
        from custos_toolkit_nautilus.adapter.utils import deep_asdict

        class SimpleConfig(msgspec.Struct):
            value: float = 1.0
            name: str = "test"

        config = SimpleConfig(value=2.5, name="custom")
        result = deep_asdict(config)

        assert isinstance(result, dict)
        assert result == {"value": 2.5, "name": "custom"}

    def test_converts_nested_struct(self):
        """Test deep_asdict with nested msgspec structs."""
        import msgspec
        from custos_toolkit_nautilus.adapter.utils import deep_asdict

        class InnerConfig(msgspec.Struct, frozen=True):
            multiplier: float = 1.0

        class OuterConfig(msgspec.Struct, frozen=True):
            inner: InnerConfig = InnerConfig()
            enabled: bool = True

        config = OuterConfig(inner=InnerConfig(multiplier=2.5), enabled=False)
        result = deep_asdict(config)

        assert isinstance(result, dict)
        assert isinstance(result["inner"], dict)
        assert result["inner"]["multiplier"] == 2.5
        assert result["enabled"] is False

    def test_preserves_dict(self):
        """Test deep_asdict preserves dict type."""
        from custos_toolkit_nautilus.adapter.utils import deep_asdict

        data = {"key": "value", "nested": {"inner": 123}}
        result = deep_asdict(data)

        assert isinstance(result, dict)
        assert result == data

    def test_preserves_list(self):
        """Test deep_asdict preserves list type."""
        from custos_toolkit_nautilus.adapter.utils import deep_asdict

        data = [1, 2, {"nested": "value"}]
        result = deep_asdict(data)

        assert isinstance(result, list)
        assert result == data

    def test_preserves_primitives(self):
        """Test deep_asdict preserves primitive types."""
        from custos_toolkit_nautilus.adapter.utils import deep_asdict

        assert deep_asdict(123) == 123
        assert deep_asdict(1.5) == 1.5
        assert deep_asdict("test") == "test"
        assert deep_asdict(True) is True
        assert deep_asdict(None) is None


class TestInstrumentIdDerivation:
    """One derivation of the instrument id, and the venues it does not describe."""

    def test_the_dash_joined_venues_keep_their_convention(self):
        assert instrument_id_str("BTC-USDT", "binance") == "BTCUSDT.BINANCE"
        assert instrument_id_str("BTC-USDT", "binance_perpetual") == "BTCUSDT-PERP.BINANCE"

    def test_sodex_pairs_are_the_venues_own_symbols_and_are_used_verbatim(self):
        """The two engines quote different assets, so there is nothing to translate.

        Spot lists the venue's v-prefixed tokens joined by an underscore; perpetuals
        list a dash-joined pair quoted in USD. Dropping the dash and appending -PERP
        would build ids the venue has never listed -- and that does not raise, it
        loads an empty instrument set.
        """
        assert instrument_id_str("vBTC_vUSDC", "sodex") == "vBTC_vUSDC.SODEX_SPOT"
        assert instrument_id_str("BTC-USD", "sodex_perpetual") == "BTC-USD.SODEX_PERPS"

    def test_every_caller_derives_the_same_id(self, tmp_path):
        """They used to hold a copy each, which is where a venue gets missed.

        ``external_order_claims`` is the one that matters most: it is what tells the
        engine which instruments this strategy owns, so an id the venue never listed
        claims nothing and the strategy's own fills arrive unclaimed.
        """
        from custos_toolkit.config import load_config
        from custos_toolkit_nautilus.adapter.config.trading import TradingConfig
        from custos_toolkit_nautilus.adapter.trading_config import build_nautilus_base_config

        for connector, pair in (
            ("binance_perpetual", "BTC-USDT"),
            ("sodex_perpetual", "BTC-USD"),
            ("sodex", "vBTC_vUSDC"),
        ):
            expected = instrument_id_str(pair, connector)
            assert str(derive_instrument_id(TradingConfig(connector=connector, pairs=[pair]))) == (
                expected
            )

            path = tmp_path / f"{connector}.yaml"
            path.write_text(
                "strategy:\n  name: probe\n"
                f"trading:\n  connector: {connector}\n  pairs:\n    - {pair}\n",
                encoding="utf-8",
            )
            claims = build_nautilus_base_config(load_config(path))["external_order_claims"]
            assert [str(claim) for claim in claims] == [expected]
