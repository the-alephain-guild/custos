"""Integration tests for indicator warmup with SuperTrend strategy."""

import pytest

pytest.importorskip("nautilus_trader")

from datetime import UTC, datetime


def test_supertrend_warmup_from_snapshot():
    """Test complete warmup flow with SuperTrend."""
    from custos_toolkit.warmup import IndicatorSnapshot, IndicatorWarmer, WarmupConfig
    from custos_toolkit_nautilus.adapter.indicators import SuperTrend

    # Create snapshot (simulating TradingView data)
    # Note: atr is optional metadata, not exported by the indicator,
    # so we exclude it from values to ensure validation passes
    snapshot = IndicatorSnapshot(
        indicator_type="supertrend",
        timestamp=datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC),
        values={
            "value": 42156.78,
            "trend": 1.0,
            "upper_band": 42890.12,
            "lower_band": 42156.78,
        },
    )

    # Create config
    config = WarmupConfig(
        mode="snapshot",
        snapshot=snapshot,
        snapshots={"supertrend": snapshot},
    )

    # Create warmer and indicator
    warmer = IndicatorWarmer(config)
    indicator = SuperTrend(length=10, multiplier=3.0)

    # Warm up
    result = warmer.warm_indicator(indicator, indicator_type="supertrend")

    # Verify
    assert result.success is True
    assert result.mode == "snapshot"
    assert indicator.value == 42156.78
    assert indicator.trend == 1
    assert indicator.initialized is True

    # Verify validation passed
    assert result.validation is not None
    assert result.validation.passed is True


def test_supertrend_continues_after_warmup():
    """Test SuperTrend continues to update after warmup."""
    from custos_toolkit.warmup import IndicatorSnapshot, IndicatorWarmer, WarmupConfig
    from custos_toolkit_nautilus.adapter.indicators import SuperTrend

    snapshot = IndicatorSnapshot(
        indicator_type="supertrend",
        timestamp=datetime(2024, 1, 15, tzinfo=UTC),
        values={
            "value": 42156.78,
            "trend": 1.0,
            "upper_band": 42890.12,
            "lower_band": 42156.78,
        },
    )

    config = WarmupConfig(
        mode="snapshot",
        snapshot=snapshot,
        snapshots={"supertrend": snapshot},
    )

    warmer = IndicatorWarmer(config)
    indicator = SuperTrend(length=10, multiplier=3.0)

    # Warm up
    warmer.warm_indicator(indicator, indicator_type="supertrend")

    # Feed new bars
    for i in range(20):
        price = 42000 + i * 10
        indicator.update_raw(
            high=price + 50,
            low=price - 50,
            close=price,
        )

    # Indicator should have updated
    assert indicator.initialized is True
    # Value should have changed from snapshot
    assert indicator.value != 42156.78


def test_warmup_config_from_yaml_style_dict():
    """Test creating warmup config from YAML-style dictionary."""
    from custos_toolkit.warmup.snapshot import warmup_config_from_dict

    config_dict = {
        "mode": "snapshot",
        "snapshot": {
            "timestamp": "2024-01-15T00:00:00Z",
            "indicators": {
                "supertrend": {
                    "value": 42156.78,
                    "trend": 1,
                    "upper_band": 42890.12,
                    "lower_band": 42156.78,
                    "atr": 523.45,
                }
            },
        },
        "history": {
            "min_bars": 500,
            "preferred_bars": 2000,
        },
    }

    config = warmup_config_from_dict(config_dict)

    assert config.mode == "snapshot"
    assert "supertrend" in config.snapshots
    assert config.snapshots["supertrend"].values["value"] == 42156.78
    assert config.min_bars == 500
    assert config.preferred_bars == 2000
