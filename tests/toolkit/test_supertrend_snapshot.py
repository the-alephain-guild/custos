"""Tests for SuperTrend snapshot support."""

import pytest

pytest.importorskip("nautilus_trader")


def test_supertrend_implements_snapshot_support():
    """Test SuperTrend implements SnapshotSupport protocol."""
    from custos_toolkit.warmup.protocol import SnapshotSupport
    from custos_toolkit_nautilus.adapter.indicators.supertrend import SuperTrend

    indicator = SuperTrend(length=10, multiplier=3.0)

    assert isinstance(indicator, SnapshotSupport)


def test_supertrend_load_snapshot():
    """Test loading snapshot into SuperTrend."""
    from custos_toolkit_nautilus.adapter.indicators.supertrend import SuperTrend

    indicator = SuperTrend(length=10, multiplier=3.0)

    snapshot_values = {
        "value": 42156.78,
        "trend": 1.0,
        "upper_band": 42890.12,
        "lower_band": 42156.78,
        "atr": 523.45,
    }

    indicator.load_snapshot(snapshot_values)

    assert indicator.value == 42156.78
    assert indicator.trend == 1
    assert indicator.upper_band == 42890.12
    assert indicator.lower_band == 42156.78
    assert indicator.initialized is True


def test_supertrend_export_snapshot():
    """Test exporting snapshot from SuperTrend."""
    from custos_toolkit_nautilus.adapter.indicators.supertrend import SuperTrend

    indicator = SuperTrend(length=10, multiplier=3.0)

    # Load a snapshot first
    snapshot_values = {
        "value": 42156.78,
        "trend": 1.0,
        "upper_band": 42890.12,
        "lower_band": 42156.78,
        "atr": 523.45,
    }
    indicator.load_snapshot(snapshot_values)

    # Export and verify
    exported = indicator.export_snapshot()

    assert exported["value"] == 42156.78
    assert exported["trend"] == 1.0
    assert exported["upper_band"] == 42890.12
    assert exported["lower_band"] == 42156.78


def test_supertrend_snapshot_preserves_value_until_real_warmup():
    """Snapshot trend/value must be held until enough *real* bars accumulate.

    A regression: load_snapshot used to append dummy 0.0 bars to satisfy the
    warmup check, so the first real bar fed ``[0, 0, ..., real]`` into ta.supertrend
    and produced a contaminated value. The snapshot value must be preserved across
    the whole warmup window and only switch to a computed value once the deque holds
    ``length + 1`` real bars.
    """
    from custos_toolkit_nautilus.adapter.indicators.supertrend import SuperTrend

    indicator = SuperTrend(length=10, multiplier=3.0)  # warmup_period = 11
    snapshot_values = {
        "value": 42156.78,
        "trend": 1.0,
        "upper_band": 42890.12,
        "lower_band": 42156.78,
        "atr": 523.45,
    }
    indicator.load_snapshot(snapshot_values)
    assert indicator.initialized is True
    assert indicator.value == 42156.78

    # Feed warmup_period - 1 = 10 real bars: value stays on the snapshot (not zero-fed)
    price = 50000.0
    for _ in range(10):
        price += 100.0
        indicator.update_raw(high=price + 20.0, low=price - 20.0, close=price)
    assert indicator.value == 42156.78  # preserved; old zero-fill computed garbage here
    assert indicator.trend == 1

    # The 11th real bar completes a clean warmup window -> switch to computed value
    price += 100.0
    indicator.update_raw(high=price + 20.0, low=price - 20.0, close=price)
    assert indicator.value != 42156.78
    assert indicator.value > 0.0
    # Clean uptrend -> SuperTrend line tracks below price (no zero contamination)
    assert indicator.value > 40000.0
