# tests/test_multi_pair_integration.py
"""Integration tests for multi-pair strategy components."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId


class TestMultiPairComponentsIntegration:
    """Integration tests for multi-pair components working together."""

    def test_allocation_config_to_capital_allocator(self):
        """Test that AllocationConfig properly configures CapitalAllocator."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

        # Create config with tiers
        config = AllocationConfig(
            mode="tiered",
            tiers={"BTC-USDT": 0.5, "ETH-USDT": 0.3, "SOL-USDT": 0.2},
            max_total_exposure=0.8,
            rebalance_threshold=0.05,
        )

        # Create allocator with config
        cache = MagicMock()
        cache.position.return_value = None

        allocator = CapitalAllocator(
            config=config,
            initial_capital=Decimal("100000"),
            cache=cache,
        )

        # Verify tier limits
        assert allocator.get_tier_limit("BTC-USDT") == Decimal("50000")
        assert allocator.get_tier_limit("ETH-USDT") == Decimal("30000")
        assert allocator.get_tier_limit("SOL-USDT") == Decimal("20000")

    def test_pair_context_independence(self):
        """Test that multiple PairContexts are independent."""
        from custos_toolkit_nautilus.adapter.pair_context import PairContext

        contexts = {}
        pairs = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]

        for pair in pairs:
            symbol = pair.replace("-", "") + "-PERP"
            contexts[pair] = PairContext(
                pair=pair,
                instrument_id=InstrumentId.from_str(f"{symbol}.BINANCE"),
                bar_type=BarType.from_str(f"{symbol}.BINANCE-1-HOUR-LAST-EXTERNAL"),
            )

        # Modify one context
        contexts["BTC-USDT"].warmed_up = True
        contexts["BTC-USDT"].indicators["test"] = "value"

        # Other contexts should be unaffected
        assert contexts["ETH-USDT"].warmed_up is False
        assert "test" not in contexts["ETH-USDT"].indicators
        assert contexts["SOL-USDT"].warmed_up is False
        assert "test" not in contexts["SOL-USDT"].indicators

    def test_capital_allocator_multi_pair_allocation(self):
        """Test allocating capital across multiple pairs."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

        config = AllocationConfig(tiers={"BTC-USDT": 0.5, "ETH-USDT": 0.3, "SOL-USDT": 0.2})
        cache = MagicMock()
        cache.position.return_value = None

        allocator = CapitalAllocator(config, Decimal("100000"), cache)

        # Register all pairs
        allocator.register_pair("BTC-USDT", InstrumentId.from_str("BTCUSDT-PERP.BINANCE"))
        allocator.register_pair("ETH-USDT", InstrumentId.from_str("ETHUSDT-PERP.BINANCE"))
        allocator.register_pair("SOL-USDT", InstrumentId.from_str("SOLUSDT-PERP.BINANCE"))

        # Allocate within limits
        assert allocator.allocate("BTC-USDT", Decimal("25000")) is True
        assert allocator.allocate("ETH-USDT", Decimal("15000")) is True
        assert allocator.allocate("SOL-USDT", Decimal("10000")) is True

        # Total allocated = 50000, available = 50000
        assert allocator.available_cash == Decimal("50000")

        # Can allocate more within tier limits
        assert allocator.allocate("BTC-USDT", Decimal("25000")) is True  # BTC at 50k limit
        assert allocator.available_cash == Decimal("25000")

        # Cannot exceed tier limit
        assert allocator.allocate("ETH-USDT", Decimal("20000")) is False  # Would exceed 30k

    def test_capital_allocator_with_pair_context(self):
        """Test CapitalAllocator working with PairContext."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig
        from custos_toolkit_nautilus.adapter.pair_context import PairContext

        config = AllocationConfig(tiers={"BTC-USDT": 0.6, "ETH-USDT": 0.4})
        cache = MagicMock()
        cache.position.return_value = None

        allocator = CapitalAllocator(config, Decimal("10000"), cache)

        # Create contexts for each pair
        contexts = {}
        for pair in ["BTC-USDT", "ETH-USDT"]:
            symbol = pair.replace("-", "") + "-PERP"
            instrument_id = InstrumentId.from_str(f"{symbol}.BINANCE")
            bar_type = BarType.from_str(f"{symbol}.BINANCE-1-HOUR-LAST-EXTERNAL")

            contexts[pair] = PairContext(
                pair=pair,
                instrument_id=instrument_id,
                bar_type=bar_type,
            )
            allocator.register_pair(pair, instrument_id)

        # Verify each pair has proper allocation limits
        assert allocator.get_available_capital("BTC-USDT") == Decimal("6000")
        assert allocator.get_available_capital("ETH-USDT") == Decimal("4000")

        # Each context has independent state
        assert contexts["BTC-USDT"].pair == "BTC-USDT"
        assert contexts["ETH-USDT"].pair == "ETH-USDT"

    def test_trading_config_with_allocation_integration(self):
        """Test TradingConfig with allocation creates proper allocator."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.trading import build_trading_config

        # Build config from YAML-like dict
        config_dict = {
            "connector": "binance_perpetual",
            "pairs": ["BTC-USDT", "ETH-USDT", "SOL-USDT"],
            "allocation": {
                "mode": "tiered",
                "tiers": {
                    "BTC-USDT": 0.5,
                    "ETH-USDT": 0.3,
                    "SOL-USDT": 0.2,
                },
                "max_total_exposure": 0.8,
            },
        }

        trading_config = build_trading_config(config_dict)

        # Verify allocation config is properly created
        assert trading_config.allocation is not None
        assert trading_config.allocation.mode == "tiered"
        assert trading_config.allocation.tiers["BTC-USDT"] == 0.5
        assert trading_config.allocation.max_total_exposure == 0.8

        # Create allocator from config
        cache = MagicMock()
        cache.position.return_value = None

        allocator = CapitalAllocator(
            config=trading_config.allocation,
            initial_capital=Decimal("100000"),
            cache=cache,
        )

        # Verify allocator uses config values
        assert allocator.get_tier_limit("BTC-USDT") == Decimal("50000")

    def test_rebalance_calculation_integration(self):
        """Test rebalance calculation across multiple pairs."""
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

        config = AllocationConfig(tiers={"BTC-USDT": 0.5, "ETH-USDT": 0.5})
        cache = MagicMock()
        cache.position.return_value = None

        allocator = CapitalAllocator(config, Decimal("10000"), cache)
        allocator.register_pair("BTC-USDT", InstrumentId.from_str("BTCUSDT-PERP.BINANCE"))
        allocator.register_pair("ETH-USDT", InstrumentId.from_str("ETHUSDT-PERP.BINANCE"))

        # Uneven allocation: 30% BTC, 20% ETH
        allocator.allocate("BTC-USDT", Decimal("3000"))
        allocator.allocate("ETH-USDT", Decimal("2000"))

        prices = {"BTC-USDT": Decimal("50000"), "ETH-USDT": Decimal("3000")}

        # Get current weights
        weights = allocator.get_current_weights(prices)

        # With 3000+2000=5000 allocated and 5000 cash, total=10000
        # BTC weight = 3000/10000 = 0.3
        # ETH weight = 2000/10000 = 0.2
        assert weights["BTC-USDT"] == pytest.approx(0.3, rel=0.01)
        assert weights["ETH-USDT"] == pytest.approx(0.2, rel=0.01)

        # Calculate rebalance to 40%/40%
        target_weights = {"BTC-USDT": 0.4, "ETH-USDT": 0.4}
        adjustments = allocator.get_rebalance_amounts(target_weights, prices)

        # BTC needs +10% of portfolio = +1000
        # ETH needs +20% of portfolio = +2000
        assert float(adjustments["BTC-USDT"]) == pytest.approx(1000.0, rel=0.01)
        assert float(adjustments["ETH-USDT"]) == pytest.approx(2000.0, rel=0.01)


class TestExportedComponents:
    """Test that all components are properly exported."""

    def test_imports_from_nautilus_module(self):
        """Every multi-pair component is importable from the engine adapter."""
        from custos_toolkit_nautilus.adapter import (
            AllocationConfig,
            CapitalAllocator,
            PairContext,
            build_allocation_config,
        )

        assert AllocationConfig is not None
        assert build_allocation_config is not None
        assert CapitalAllocator is not None
        assert PairContext is not None

    def test_imports_from_config_module(self):
        """Test AllocationConfig can be imported from config module."""
        from custos_toolkit_nautilus.adapter.config import (
            AllocationConfig,
            build_allocation_config,
        )

        assert AllocationConfig is not None
        assert build_allocation_config is not None
