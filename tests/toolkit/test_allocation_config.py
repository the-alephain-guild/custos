# tests/test_allocation_config.py
"""Tests for AllocationConfig."""

import pytest

pytest.importorskip("msgspec")


class TestAllocationConfig:
    """Tests for AllocationConfig."""

    def test_default_values(self):
        """Test default configuration values."""
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

        config = AllocationConfig()
        assert config.mode == "tiered"
        assert config.tiers == {}
        assert config.max_total_exposure == 0.8
        assert config.rebalance_threshold == 0.05

    def test_custom_tiers(self):
        """Test custom tier configuration."""
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

        config = AllocationConfig(tiers={"BTC-USDT": 0.5, "ETH-USDT": 0.3, "SOL-USDT": 0.2})
        assert config.tiers["BTC-USDT"] == 0.5
        assert config.tiers["ETH-USDT"] == 0.3
        assert config.tiers["SOL-USDT"] == 0.2

    def test_frozen_struct(self):
        """Test that config is immutable."""
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

        config = AllocationConfig()
        with pytest.raises(AttributeError):
            config.mode = "equal"

    def test_equal_mode(self):
        """Test equal allocation mode."""
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

        config = AllocationConfig(mode="equal")
        assert config.mode == "equal"

    def test_dynamic_mode(self):
        """Test dynamic allocation mode."""
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

        config = AllocationConfig(mode="dynamic")
        assert config.mode == "dynamic"

    def test_custom_exposure_limit(self):
        """Test custom max total exposure."""
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

        config = AllocationConfig(max_total_exposure=0.5)
        assert config.max_total_exposure == 0.5

    def test_custom_rebalance_threshold(self):
        """Test custom rebalance threshold."""
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

        config = AllocationConfig(rebalance_threshold=0.1)
        assert config.rebalance_threshold == 0.1


class TestBuildAllocationConfig:
    """Tests for build_allocation_config function."""

    def test_build_from_dict(self):
        """Test building config from dictionary."""
        from custos_toolkit_nautilus.adapter.config.allocation import build_allocation_config

        data = {
            "mode": "tiered",
            "tiers": {"BTC-USDT": 0.6, "ETH-USDT": 0.4},
            "max_total_exposure": 0.9,
            "rebalance_threshold": 0.03,
        }
        config = build_allocation_config(data)
        assert config.mode == "tiered"
        assert config.tiers["BTC-USDT"] == 0.6
        assert config.tiers["ETH-USDT"] == 0.4
        assert config.max_total_exposure == 0.9
        assert config.rebalance_threshold == 0.03

    def test_build_from_empty_dict(self):
        """Test building config from empty dictionary."""
        from custos_toolkit_nautilus.adapter.config.allocation import build_allocation_config

        config = build_allocation_config({})
        assert config.mode == "tiered"
        assert config.tiers == {}

    def test_build_from_none(self):
        """Test building config from None."""
        from custos_toolkit_nautilus.adapter.config.allocation import build_allocation_config

        config = build_allocation_config(None)
        assert config.mode == "tiered"
        assert config.tiers == {}
        assert config.max_total_exposure == 0.8
        assert config.rebalance_threshold == 0.05

    def test_build_with_partial_data(self):
        """Test building config with partial data."""
        from custos_toolkit_nautilus.adapter.config.allocation import build_allocation_config

        data = {"mode": "equal"}
        config = build_allocation_config(data)
        assert config.mode == "equal"
        assert config.tiers == {}
        assert config.max_total_exposure == 0.8
