# tests/test_capital_allocator.py
"""Tests for CapitalAllocator."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")


class TestCapitalAllocatorBasic:
    """Basic tests for CapitalAllocator."""

    def test_creation(self):
        """Test creating CapitalAllocator."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

        config = AllocationConfig(tiers={"BTC-USDT": 0.6, "ETH-USDT": 0.4})
        cache = MagicMock()

        allocator = CapitalAllocator(
            config=config,
            initial_capital=Decimal("10000"),
            cache=cache,
        )

        assert allocator.total_capital == Decimal("10000")
        assert allocator.available_cash == Decimal("10000")

    def test_register_pair(self):
        """Test registering a trading pair."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig
        from nautilus_trader.model.identifiers import InstrumentId

        config = AllocationConfig(tiers={"BTC-USDT": 0.6})
        cache = MagicMock()
        allocator = CapitalAllocator(config, Decimal("10000"), cache)

        instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        allocator.register_pair("BTC-USDT", instrument_id)

        assert "BTC-USDT" in allocator.pairs

    def test_register_pair_auto_tier(self):
        """Test registering a pair without pre-defined tier creates one."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig
        from nautilus_trader.model.identifiers import InstrumentId

        config = AllocationConfig(tiers={})  # No pre-defined tiers
        cache = MagicMock()
        allocator = CapitalAllocator(config, Decimal("10000"), cache)

        # Register first pair
        allocator.register_pair("BTC-USDT", InstrumentId.from_str("BTCUSDT-PERP.BINANCE"))
        assert "BTC-USDT" in allocator.pairs

        # Tier should be auto-created
        tier_limit = allocator.get_tier_limit("BTC-USDT")
        assert tier_limit > Decimal("0")


class TestCapitalAllocation:
    """Tests for capital allocation functionality."""

    def test_get_tier_limit(self):
        """Test getting tier limit for a pair."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

        config = AllocationConfig(tiers={"BTC-USDT": 0.6, "ETH-USDT": 0.4})
        allocator = CapitalAllocator(config, Decimal("10000"), MagicMock())

        assert allocator.get_tier_limit("BTC-USDT") == Decimal("6000")
        assert allocator.get_tier_limit("ETH-USDT") == Decimal("4000")

    def test_get_tier_limit_unregistered(self):
        """Test getting tier limit for unregistered pair returns zero."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

        config = AllocationConfig(tiers={"BTC-USDT": 0.6})
        allocator = CapitalAllocator(config, Decimal("10000"), MagicMock())

        assert allocator.get_tier_limit("SOL-USDT") == Decimal("0")

    def test_get_available_capital(self):
        """Test getting available capital for a pair."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig
        from nautilus_trader.model.identifiers import InstrumentId

        config = AllocationConfig(tiers={"BTC-USDT": 0.6})
        allocator = CapitalAllocator(config, Decimal("10000"), MagicMock())
        allocator.register_pair("BTC-USDT", InstrumentId.from_str("BTCUSDT-PERP.BINANCE"))

        # Initially all available
        assert allocator.get_available_capital("BTC-USDT") == Decimal("6000")

    def test_allocate_success(self):
        """Test successful allocation."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig
        from nautilus_trader.model.identifiers import InstrumentId

        config = AllocationConfig(tiers={"BTC-USDT": 0.6})
        allocator = CapitalAllocator(config, Decimal("10000"), MagicMock())
        allocator.register_pair("BTC-USDT", InstrumentId.from_str("BTCUSDT-PERP.BINANCE"))

        result = allocator.allocate("BTC-USDT", Decimal("3000"))

        assert result is True
        assert allocator.available_cash == Decimal("7000")
        assert allocator.get_available_capital("BTC-USDT") == Decimal("3000")

    def test_allocate_exceeds_tier(self):
        """Test allocation exceeding tier limit."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig
        from nautilus_trader.model.identifiers import InstrumentId

        config = AllocationConfig(tiers={"BTC-USDT": 0.6})
        allocator = CapitalAllocator(config, Decimal("10000"), MagicMock())
        allocator.register_pair("BTC-USDT", InstrumentId.from_str("BTCUSDT-PERP.BINANCE"))

        result = allocator.allocate("BTC-USDT", Decimal("7000"))

        assert result is False
        assert allocator.available_cash == Decimal("10000")

    def test_release(self):
        """Test releasing allocated capital."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig
        from nautilus_trader.model.identifiers import InstrumentId

        config = AllocationConfig(tiers={"BTC-USDT": 0.6})
        allocator = CapitalAllocator(config, Decimal("10000"), MagicMock())
        allocator.register_pair("BTC-USDT", InstrumentId.from_str("BTCUSDT-PERP.BINANCE"))

        allocator.allocate("BTC-USDT", Decimal("3000"))
        allocator.release("BTC-USDT", Decimal("1000"))

        assert allocator.available_cash == Decimal("8000")

    def test_release_more_than_allocated(self):
        """Test releasing more than allocated only releases what was allocated."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig
        from nautilus_trader.model.identifiers import InstrumentId

        config = AllocationConfig(tiers={"BTC-USDT": 0.6})
        allocator = CapitalAllocator(config, Decimal("10000"), MagicMock())
        allocator.register_pair("BTC-USDT", InstrumentId.from_str("BTCUSDT-PERP.BINANCE"))

        allocator.allocate("BTC-USDT", Decimal("3000"))
        allocator.release("BTC-USDT", Decimal("5000"))  # Try to release more

        # Should only release what was allocated
        assert allocator.available_cash == Decimal("10000")


class TestPortfolioValue:
    """Tests for portfolio value calculations."""

    def test_get_portfolio_value_no_positions(self):
        """Test portfolio value with no positions."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

        cache = MagicMock()
        cache.position.return_value = None

        config = AllocationConfig(tiers={"BTC-USDT": 0.5, "ETH-USDT": 0.5})
        allocator = CapitalAllocator(config, Decimal("10000"), cache)

        prices = {"BTC-USDT": Decimal("50000"), "ETH-USDT": Decimal("3000")}
        value = allocator.get_portfolio_value(prices)

        assert value == Decimal("10000")

    def test_get_current_weights(self):
        """Test getting current portfolio weights."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig
        from nautilus_trader.model.identifiers import InstrumentId

        cache = MagicMock()
        cache.position.return_value = None

        config = AllocationConfig(tiers={"BTC-USDT": 0.6, "ETH-USDT": 0.4})
        allocator = CapitalAllocator(config, Decimal("10000"), cache)
        allocator.register_pair("BTC-USDT", InstrumentId.from_str("BTCUSDT-PERP.BINANCE"))
        allocator.register_pair("ETH-USDT", InstrumentId.from_str("ETHUSDT-PERP.BINANCE"))

        # Allocate some capital
        allocator.allocate("BTC-USDT", Decimal("6000"))
        allocator.allocate("ETH-USDT", Decimal("4000"))

        prices = {"BTC-USDT": Decimal("50000"), "ETH-USDT": Decimal("3000")}
        weights = allocator.get_current_weights(prices)

        assert weights["BTC-USDT"] == pytest.approx(0.6, rel=0.01)
        assert weights["ETH-USDT"] == pytest.approx(0.4, rel=0.01)

    def test_get_rebalance_amounts(self):
        """Test calculating rebalance amounts."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig
        from nautilus_trader.model.identifiers import InstrumentId

        cache = MagicMock()
        cache.position.return_value = None

        # Use higher tier limits to allow uneven allocation
        config = AllocationConfig(tiers={"BTC-USDT": 0.7, "ETH-USDT": 0.3})
        allocator = CapitalAllocator(config, Decimal("10000"), cache)
        allocator.register_pair("BTC-USDT", InstrumentId.from_str("BTCUSDT-PERP.BINANCE"))
        allocator.register_pair("ETH-USDT", InstrumentId.from_str("ETHUSDT-PERP.BINANCE"))

        # Allocate within tier limits
        result_btc = allocator.allocate("BTC-USDT", Decimal("7000"))  # Within 70% = 7000
        result_eth = allocator.allocate("ETH-USDT", Decimal("3000"))  # Within 30% = 3000

        assert result_btc is True
        assert result_eth is True

        prices = {"BTC-USDT": Decimal("50000"), "ETH-USDT": Decimal("3000")}
        target_weights = {"BTC-USDT": 0.5, "ETH-USDT": 0.5}

        adjustments = allocator.get_rebalance_amounts(target_weights, prices)

        # Current weights: BTC=70%, ETH=30%
        # Target weights: BTC=50%, ETH=50%
        # BTC should sell (negative), ETH should buy (positive)
        assert adjustments["BTC-USDT"] < 0
        assert adjustments["ETH-USDT"] > 0


class TestExposure:
    """Tests for exposure calculations."""

    def test_get_total_exposure_no_positions(self):
        """Test total exposure with no positions."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

        cache = MagicMock()
        cache.position.return_value = None

        config = AllocationConfig()
        allocator = CapitalAllocator(config, Decimal("10000"), cache)

        prices = {"BTC-USDT": Decimal("50000")}
        exposure = allocator.get_total_exposure(prices)

        assert exposure == 0.0

    def test_check_exposure_limit(self):
        """Test exposure limit checking."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

        cache = MagicMock()
        cache.position.return_value = None

        config = AllocationConfig(max_total_exposure=0.8)
        allocator = CapitalAllocator(config, Decimal("10000"), cache)

        prices = {"BTC-USDT": Decimal("50000")}
        assert allocator.check_exposure_limit(prices) is True
