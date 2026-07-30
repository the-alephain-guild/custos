# tests/test_trading_config.py
"""Tests for TradingConfig allocation field."""

import pytest

pytest.importorskip("msgspec")


class TestTradingConfigAllocation:
    """Tests for TradingConfig allocation field."""

    def test_trading_config_has_allocation_field(self):
        """Test that TradingConfig has allocation field."""
        from custos_toolkit_nautilus.adapter.config.trading import TradingConfig

        config = TradingConfig()
        assert hasattr(config, "allocation")
        assert config.allocation is None

    def test_trading_config_with_allocation(self):
        """Test TradingConfig with allocation."""
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig
        from custos_toolkit_nautilus.adapter.config.trading import TradingConfig

        allocation = AllocationConfig(tiers={"BTC-USDT": 0.6, "ETH-USDT": 0.4})
        config = TradingConfig(
            pairs=("BTC-USDT", "ETH-USDT"),
            allocation=allocation,
        )
        assert config.allocation is not None
        assert config.allocation.tiers["BTC-USDT"] == 0.6
        assert config.allocation.tiers["ETH-USDT"] == 0.4

    def test_build_trading_config_with_allocation(self):
        """Test building TradingConfig with allocation from dict."""
        from custos_toolkit_nautilus.adapter.config.trading import build_trading_config

        data = {
            "connector": "binance_perpetual",
            "pairs": ["BTC-USDT", "ETH-USDT"],
            "allocation": {
                "mode": "tiered",
                "tiers": {"BTC-USDT": 0.5, "ETH-USDT": 0.5},
                "max_total_exposure": 0.8,
            },
        }
        config = build_trading_config(data)
        assert config.allocation is not None
        assert config.allocation.mode == "tiered"
        assert config.allocation.tiers["BTC-USDT"] == 0.5
        assert config.allocation.tiers["ETH-USDT"] == 0.5
        assert config.allocation.max_total_exposure == 0.8

    def test_build_trading_config_without_allocation(self):
        """Test building TradingConfig without allocation keeps None."""
        from custos_toolkit_nautilus.adapter.config.trading import build_trading_config

        data = {
            "connector": "binance_perpetual",
            "pairs": ["BTC-USDT"],
        }
        config = build_trading_config(data)
        assert config.allocation is None

    def test_build_trading_config_with_explicit_allocation(self):
        """Test building TradingConfig with explicit allocation mode."""
        from custos_toolkit_nautilus.adapter.config.trading import build_trading_config

        data = {
            "connector": "binance_perpetual",
            "pairs": ["BTC-USDT"],
            "allocation": {"mode": "equal"},
        }
        config = build_trading_config(data)
        assert config.allocation is not None
        assert config.allocation.mode == "equal"
