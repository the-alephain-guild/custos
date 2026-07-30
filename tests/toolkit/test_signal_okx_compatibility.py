"""
Tests for Signal OKX compatibility features.

Tests cover:
- Signal class extensions (new OKX fields)
- SignalResolver (field resolution and format conversion)
- Signal factory methods with OKX fields
"""

from decimal import Decimal

import pytest
from custos_toolkit.signals.resolver import SignalResolver
from custos_toolkit.signals.types import InvestmentType, OrderType, Signal, SignalDirection


class TestSignalOkxFields:
    """Test Signal class OKX field extensions."""

    def test_signal_has_okx_fields(self):
        """Signal should have all OKX-compatible fields."""
        signal = Signal(direction=SignalDirection.ENTER_LONG)
        assert hasattr(signal, "investment_type")
        assert hasattr(signal, "amount")
        assert hasattr(signal, "order_type")
        assert hasattr(signal, "order_price_offset")
        assert hasattr(signal, "max_lag")
        assert hasattr(signal, "signal_token")

    def test_signal_okx_fields_default_none(self):
        """OKX fields should default to None."""
        signal = Signal(direction=SignalDirection.ENTER_LONG)
        assert signal.investment_type is None
        assert signal.amount is None
        assert signal.order_type is None
        assert signal.order_price_offset is None
        assert signal.max_lag is None
        assert signal.signal_token is None

    def test_signal_with_okx_fields(self):
        """Signal should accept OKX fields in constructor."""
        signal = Signal(
            direction=SignalDirection.ENTER_LONG,
            price=Decimal("100"),
            investment_type="percentage_investment",
            amount=Decimal("0.1"),
            order_type="limit",
            order_price_offset=Decimal("0.05"),
            max_lag=120,
            signal_token="test-token",
        )
        assert signal.investment_type == "percentage_investment"
        assert signal.amount == Decimal("0.1")
        assert signal.order_type == "limit"
        assert signal.order_price_offset == Decimal("0.05")
        assert signal.max_lag == 120
        assert signal.signal_token == "test-token"

    def test_signal_amount_converts_to_decimal(self):
        """Amount should be converted to Decimal in __post_init__."""
        signal = Signal(
            direction=SignalDirection.ENTER_LONG,
            amount=0.5,  # float
        )
        assert isinstance(signal.amount, Decimal)
        assert signal.amount == Decimal("0.5")

    def test_signal_order_price_offset_converts_to_decimal(self):
        """order_price_offset should be converted to Decimal in __post_init__."""
        signal = Signal(
            direction=SignalDirection.ENTER_LONG,
            order_price_offset=0.1,  # float
        )
        assert isinstance(signal.order_price_offset, Decimal)
        assert signal.order_price_offset == Decimal("0.1")

    def test_has_okx_fields_false_when_empty(self):
        """has_okx_fields should return False when no OKX fields set."""
        signal = Signal.enter_long(100, pair="BTC-USDT")
        assert signal.has_okx_fields() is False

    def test_has_okx_fields_true_when_set(self):
        """has_okx_fields should return True when any OKX field is set."""
        signal = Signal.enter_long(100, pair="BTC-USDT", amount=Decimal("100"))
        assert signal.has_okx_fields() is True


class TestSignalFactoryMethods:
    """Test Signal factory methods with OKX fields."""

    def test_enter_long_with_okx_fields(self):
        """enter_long should accept OKX fields."""
        signal = Signal.enter_long(
            price=100,
            pair="BTC-USDT",
            amount=Decimal("500"),
            investment_type="margin",
            order_type="limit",
            order_price_offset=Decimal("0.05"),
        )
        assert signal.direction == SignalDirection.ENTER_LONG
        assert signal.amount == Decimal("500")
        assert signal.investment_type == "margin"
        assert signal.order_type == "limit"
        assert signal.order_price_offset == Decimal("0.05")

    def test_enter_short_with_okx_fields(self):
        """enter_short should accept OKX fields."""
        signal = Signal.enter_short(
            price=100,
            pair="BTC-USDT",
            amount=0.1,
            investment_type="percentage_investment",
        )
        assert signal.direction == SignalDirection.ENTER_SHORT
        assert signal.amount == Decimal("0.1")
        assert signal.investment_type == "percentage_investment"

    def test_exit_long_with_okx_fields(self):
        """exit_long should accept OKX fields."""
        signal = Signal.exit_long(
            price=100,
            pair="BTC-USDT",
            amount=Decimal("50"),
            investment_type="percentage_position",
        )
        assert signal.direction == SignalDirection.EXIT_LONG
        assert signal.amount == Decimal("50")
        assert signal.investment_type == "percentage_position"

    def test_exit_short_with_okx_fields(self):
        """exit_short should accept OKX fields."""
        signal = Signal.exit_short(
            price=100,
            pair="BTC-USDT",
            amount=Decimal("100"),
            order_type="market",
        )
        assert signal.direction == SignalDirection.EXIT_SHORT
        assert signal.amount == Decimal("100")
        assert signal.order_type == "market"


class TestSignalResolver:
    """Test SignalResolver field resolution."""

    def test_resolve_fills_defaults(self):
        """Resolver should fill missing fields with defaults."""
        resolver = SignalResolver()
        signal = Signal.enter_long(100, pair="BTC-USDT")
        resolved = resolver.resolve(signal)

        assert resolved.investment_type == "percentage_investment"
        assert resolved.order_type == "market"
        assert resolved.max_lag == 60
        assert resolved.timestamp is not None

    def test_resolve_preserves_signal_values(self):
        """Resolver should preserve explicitly set signal values."""
        resolver = SignalResolver()
        signal = Signal(
            direction=SignalDirection.ENTER_LONG,
            price=Decimal("100"),
            investment_type="margin",
            amount=Decimal("500"),
            order_type="limit",
            max_lag=120,
        )
        resolved = resolver.resolve(signal)

        assert resolved.investment_type == "margin"
        assert resolved.amount == Decimal("500")
        assert resolved.order_type == "limit"
        assert resolved.max_lag == 120

    def test_resolve_adds_timestamp_if_missing(self):
        """Resolver should add timestamp if not provided."""
        resolver = SignalResolver()
        signal = Signal.enter_long(100)
        assert signal.timestamp is None

        resolved = resolver.resolve(signal)
        assert resolved.timestamp is not None
        assert resolved.timestamp > 0

    def test_resolve_preserves_existing_timestamp(self):
        """Resolver should preserve existing timestamp."""
        resolver = SignalResolver()
        ts = 1234567890_000_000_000
        signal = Signal(
            direction=SignalDirection.ENTER_LONG,
            timestamp=ts,
        )
        resolved = resolver.resolve(signal)
        assert resolved.timestamp == ts


class TestSignalResolverOkxFormat:
    """Test SignalResolver OKX format conversion."""

    def test_to_okx_format_enter_long(self):
        """Should convert ENTER_LONG signal to OKX format."""
        resolver = SignalResolver()
        signal = Signal.enter_long(
            price=100,
            pair="BTC-USDT",
            amount=Decimal("500"),
        )
        okx = resolver.to_okx_format(signal)

        assert okx["action"] == "ENTER_LONG"
        assert okx["instrument"] == "BTC-USDT-SWAP"
        assert okx["amount"] == "500"
        assert okx["investmentType"] == "percentage_investment"
        assert okx["maxLag"] == "60"
        assert "timestamp" in okx

    def test_to_okx_format_enter_short(self):
        """Should convert ENTER_SHORT signal to OKX format."""
        resolver = SignalResolver()
        signal = Signal.enter_short(price=100, pair="ETH-USDT")
        okx = resolver.to_okx_format(signal)

        assert okx["action"] == "ENTER_SHORT"
        assert okx["instrument"] == "ETH-USDT-SWAP"

    def test_to_okx_format_exit_long(self):
        """Should convert EXIT_LONG signal to OKX format."""
        resolver = SignalResolver()
        signal = Signal.exit_long(price=100, pair="BTC-USDT")
        okx = resolver.to_okx_format(signal)

        assert okx["action"] == "EXIT_LONG"

    def test_to_okx_format_exit_short(self):
        """Should convert EXIT_SHORT signal to OKX format."""
        resolver = SignalResolver()
        signal = Signal.exit_short(price=100, pair="BTC-USDT")
        okx = resolver.to_okx_format(signal)

        assert okx["action"] == "EXIT_SHORT"

    def test_to_okx_format_neutral_raises(self):
        """Should raise error for NEUTRAL signal."""
        resolver = SignalResolver()
        signal = Signal.neutral(price=100)

        with pytest.raises(ValueError, match="Cannot convert direction"):
            resolver.to_okx_format(signal)

    def test_to_okx_format_includes_limit_offset(self):
        """Should include orderPriceOffset for limit orders."""
        resolver = SignalResolver()
        signal = Signal.enter_long(
            price=100,
            pair="BTC-USDT",
            order_type="limit",
            order_price_offset=Decimal("0.5"),
        )
        okx = resolver.to_okx_format(signal)

        assert okx["orderType"] == "limit"
        assert okx["orderPriceOffset"] == "0.5"


class TestSignalResolverFromOkx:
    """Test SignalResolver parsing from OKX format."""

    def test_from_okx_format_enter_long(self):
        """Should parse ENTER_LONG from OKX format."""
        resolver = SignalResolver()
        data = {
            "action": "ENTER_LONG",
            "instrument": "BTC-USDT-SWAP",
            "amount": "100",
            "investmentType": "margin",
            "maxLag": "60",
            "timestamp": "2026-01-31T12:00:00.000Z",
        }
        signal = resolver.from_okx_format(data)

        assert signal.direction == SignalDirection.ENTER_LONG
        assert signal.pair == "BTC-USDT"
        assert signal.amount == Decimal("100")
        assert signal.investment_type == "margin"
        assert signal.max_lag == 60

    def test_from_okx_format_case_insensitive(self):
        """Should handle lowercase actions."""
        resolver = SignalResolver()
        data = {"action": "enter_short"}
        signal = resolver.from_okx_format(data)
        assert signal.direction == SignalDirection.ENTER_SHORT

    def test_from_okx_format_missing_action_raises(self):
        """Should raise error for missing action."""
        resolver = SignalResolver()
        with pytest.raises(ValueError, match="Missing required field: action"):
            resolver.from_okx_format({})

    def test_from_okx_format_unknown_action_raises(self):
        """Should raise error for unknown action."""
        resolver = SignalResolver()
        with pytest.raises(ValueError, match="Unknown OKX action"):
            resolver.from_okx_format({"action": "INVALID"})

    def test_from_okx_tradingview_instrument(self):
        """Should parse TradingView instrument format."""
        resolver = SignalResolver()
        data = {
            "action": "ENTER_LONG",
            "instrument": "BTCUSDT.P",
        }
        signal = resolver.from_okx_format(data)
        assert signal.pair == "BTC-USDT"


class TestInvestmentTypeEnum:
    """Test InvestmentType enum."""

    def test_investment_type_values(self):
        """InvestmentType should have correct values."""
        assert InvestmentType.MARGIN.value == "margin"
        assert InvestmentType.CONTRACT.value == "contract"
        assert InvestmentType.PERCENTAGE_BALANCE.value == "percentage_balance"
        assert InvestmentType.PERCENTAGE_INVESTMENT.value == "percentage_investment"
        assert InvestmentType.PERCENTAGE_POSITION.value == "percentage_position"


class TestOrderTypeEnum:
    """Test OrderType enum."""

    def test_order_type_values(self):
        """OrderType should have correct values."""
        assert OrderType.MARKET.value == "market"
        assert OrderType.LIMIT.value == "limit"


@pytest.fixture
def typed_signal_config():
    """Real frozen msgspec SignalConfig, as the production signal_processor passes."""
    pytest.importorskip("msgspec")
    from custos_toolkit_nautilus.adapter.config.signal import (
        OkxConfig,
        SignalConfig,
        SignalDefaultsConfig,
    )

    return SignalConfig(
        okx=OkxConfig(signal_token="tok-1", instrument_format="tradingview", max_lag=99),
        defaults=SignalDefaultsConfig(
            order_type="limit", order_price_offset=0.25, investment_type="margin"
        ),
    )


class TestSignalResolverConfigResolution:
    """Characterize resolution sourced from a typed config.

    The production path passes frozen msgspec config Structs (SignalConfig /
    PositionConfig); the existing resolver tests all use SignalResolver() with no
    config, so the config-sourced branches were untested. These lock that behavior
    so the §2 getattr-defense removal stays equivalent.
    """

    def test_resolve_uses_signal_config_defaults(self, typed_signal_config):
        """Missing signal fields fall back to config defaults, not system defaults."""
        resolver = SignalResolver(signal_config=typed_signal_config)
        resolved = resolver.resolve(Signal.enter_long(100, pair="BTC-USDT"))

        assert resolved.investment_type == "margin"
        assert resolved.order_type == "limit"
        assert resolved.order_price_offset == Decimal("0.25")
        assert resolved.max_lag == 99
        assert resolved.signal_token == "tok-1"

    def test_resolve_amount_from_position_size_value(self):
        """When the signal has no amount, position.size_value supplies it."""
        pytest.importorskip("msgspec")
        from custos_toolkit_nautilus.adapter.config.position import PositionConfig

        resolver = SignalResolver(
            position_config=PositionConfig(size_type="kelly", size_value=0.33)
        )
        resolved = resolver.resolve(Signal.enter_long(100, pair="BTC-USDT"))

        assert resolved.amount == Decimal("0.33")

    def test_resolve_investment_type_from_position_size_type_mapping(self):
        """Empty config default falls through to the position.size_type mapping."""
        pytest.importorskip("msgspec")
        from custos_toolkit_nautilus.adapter.config.position import PositionConfig
        from custos_toolkit_nautilus.adapter.config.signal import (
            OkxConfig,
            SignalConfig,
            SignalDefaultsConfig,
        )

        empty_defaults = SignalConfig(
            okx=OkxConfig(), defaults=SignalDefaultsConfig(investment_type="")
        )
        kelly = SignalResolver(
            signal_config=empty_defaults,
            position_config=PositionConfig(size_type="kelly", size_value=0.1),
        )
        fixed = SignalResolver(
            signal_config=empty_defaults,
            position_config=PositionConfig(size_type="fixed", size_value=0.1),
        )
        assert kelly.resolve(Signal.enter_long(100)).investment_type == "percentage_investment"
        assert fixed.resolve(Signal.enter_long(100)).investment_type == "margin"

    def test_pair_to_instrument_uses_config_format(self, typed_signal_config):
        """instrument_format from config drives the wire instrument string."""
        resolver = SignalResolver(signal_config=typed_signal_config)
        okx = resolver.to_okx_format(Signal.enter_long(100, pair="BTC-USDT"))

        # instrument_format="tradingview" -> BTC-USDT becomes BTCUSDT.P
        assert okx["instrument"] == "BTCUSDT.P"

    def test_signal_value_wins_over_typed_config(self, typed_signal_config):
        """An explicit signal field beats the config default."""
        resolver = SignalResolver(signal_config=typed_signal_config)
        signal = Signal(
            direction=SignalDirection.ENTER_LONG,
            price=Decimal("100"),
            pair="BTC-USDT",
            investment_type="contract",
            order_type="market",
            max_lag=7,
        )
        resolved = resolver.resolve(signal)

        assert resolved.investment_type == "contract"
        assert resolved.order_type == "market"
        assert resolved.max_lag == 7


class TestSignalResolverNoTypeDefense:
    """Guard: the resolver trusts its typed config instead of probing it.

    SignalResolver only ever receives None or a frozen msgspec config Struct
    (verified: no dict input path exists), so hasattr/getattr defensive access on
    the config is the strong-type-as-dict anti-pattern banned by coding-taste §2
    and the Philosophers-Stone specialization. This guard fails if it returns.
    """

    def test_resolver_source_has_no_type_defense(self):
        import inspect

        from custos_toolkit.signals import resolver

        source = inspect.getsource(resolver)
        assert "hasattr(" not in source, "resolver must not hasattr-probe its typed config (§2)"
        assert "getattr(" not in source, "resolver must not getattr-defend its typed config (§2)"
