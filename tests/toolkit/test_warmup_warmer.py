"""Tests for IndicatorWarmer service."""

from datetime import UTC, datetime

import pytest
from custos_toolkit.warmup.snapshot import IndicatorSnapshot, WarmupConfig
from custos_toolkit.warmup.warmer import IndicatorWarmer, ValidationResult, WarmupResult


class MockIndicator:
    """Mock indicator implementing SnapshotSupport."""

    def __init__(self):
        self.value = 0.0
        self.trend = 0
        self._bars_processed = 0
        self._snapshot_loaded = False

    def load_snapshot(self, values: dict[str, float]) -> None:
        self.value = values.get("value", 0.0)
        self.trend = int(values.get("trend", 0))
        self._snapshot_loaded = True

    def export_snapshot(self) -> dict[str, float]:
        return {"value": self.value, "trend": float(self.trend)}

    def handle_bar(self, bar) -> None:
        self._bars_processed += 1
        self.value = bar.close * 0.95


class MockBar:
    """Mock bar data."""

    def __init__(self, close: float):
        self.high = close * 1.01
        self.low = close * 0.99
        self.close = close


def test_warmer_none_mode():
    """Test warmer with mode=none does nothing."""
    config = WarmupConfig(mode="none")
    warmer = IndicatorWarmer(config)

    indicator = MockIndicator()
    result = warmer.warm_indicator(indicator)

    assert result.success is True
    assert result.mode == "none"
    assert result.bars_processed == 0
    assert indicator._snapshot_loaded is False


def test_warmer_snapshot_mode_loads_snapshot():
    """Test warmer with mode=snapshot loads the snapshot."""
    snapshot = IndicatorSnapshot(
        indicator_type="supertrend",
        timestamp=datetime(2024, 1, 15, tzinfo=UTC),
        values={"value": 42156.78, "trend": 1.0},
    )

    config = WarmupConfig(
        mode="snapshot",
        snapshot=snapshot,
        snapshots={"supertrend": snapshot},
    )
    warmer = IndicatorWarmer(config)

    indicator = MockIndicator()
    result = warmer.warm_indicator(indicator, indicator_type="supertrend")

    assert result.success is True
    assert result.mode == "snapshot"
    assert indicator._snapshot_loaded is True
    assert indicator.value == 42156.78
    assert indicator.trend == 1


def test_warmer_validation_passes():
    """Test warmup validation passes when values match."""
    snapshot = IndicatorSnapshot(
        indicator_type="test",
        timestamp=datetime(2024, 1, 15, tzinfo=UTC),
        values={"value": 100.0, "trend": 1.0},
    )

    config = WarmupConfig(
        mode="snapshot",
        snapshot=snapshot,
        snapshots={"test": snapshot},
    )
    warmer = IndicatorWarmer(config)

    indicator = MockIndicator()
    result = warmer.warm_indicator(indicator, indicator_type="test")

    assert result.validation is not None
    assert result.validation.passed is True
    assert result.validation.max_deviation_pct == 0.0


def test_warmup_result_structure():
    """Test WarmupResult has all expected fields."""
    result = WarmupResult(
        success=True,
        mode="snapshot",
        bars_processed=100,
        snapshot_time=datetime.now(UTC),
        current_values={"value": 100.0},
        validation=ValidationResult(
            passed=True,
            expected={"value": 100.0},
            actual={"value": 100.0},
            max_deviation_pct=0.0,
            details={},
        ),
        message="OK",
    )

    assert result.success is True
    assert result.bars_processed == 100


def test_warmer_snapshot_mode_no_snapshot_found():
    """Test warmer returns failure when no snapshot is found for indicator type."""
    config = WarmupConfig(mode="snapshot")
    warmer = IndicatorWarmer(config)

    indicator = MockIndicator()
    result = warmer.warm_indicator(indicator, indicator_type="unknown")

    assert result.success is False
    assert result.mode == "snapshot"
    assert "No snapshot found" in result.message


def test_warmer_snapshot_mode_uses_default_snapshot():
    """Test warmer uses default snapshot when indicator_type not in snapshots."""
    snapshot = IndicatorSnapshot(
        indicator_type="default",
        timestamp=datetime(2024, 1, 15, tzinfo=UTC),
        values={"value": 50.0, "trend": -1.0},
    )

    config = WarmupConfig(
        mode="snapshot",
        snapshot=snapshot,
    )
    warmer = IndicatorWarmer(config)

    indicator = MockIndicator()
    result = warmer.warm_indicator(indicator)

    assert result.success is True
    assert indicator.value == 50.0
    assert indicator.trend == -1


def test_validation_result_structure():
    """Test ValidationResult has all expected fields."""
    result = ValidationResult(
        passed=False,
        expected={"value": 100.0},
        actual={"value": 105.0},
        max_deviation_pct=5.0,
        details={"value": 5.0},
    )

    assert result.passed is False
    assert result.max_deviation_pct == 5.0
    assert result.details["value"] == 5.0


def test_warmer_unknown_mode():
    """Test warmer handles unknown mode gracefully."""

    # Create a mock config with an invalid mode
    class MockConfig:
        mode = "invalid_mode"
        snapshot = None
        snapshots = {}
        min_bars = 500
        preferred_bars = 2000

    warmer = IndicatorWarmer(MockConfig())

    indicator = MockIndicator()
    result = warmer.warm_indicator(indicator)

    assert result.success is False
    assert "Unknown warmup mode" in result.message


def test_warmer_warmup_mode_handled_at_strategy_level():
    """Test that mode='warmup' returns a clear strategy-level message, not 'Unknown'.

    'warmup' (history-only) is a valid WarmupConfig mode handled by WarmupManager
    via nautilus request_bars, NOT by IndicatorWarmer (which only does none/snapshot).
    """
    config = WarmupConfig(mode="warmup")
    warmer = IndicatorWarmer(config)

    result = warmer.warm_indicator(MockIndicator())

    assert result.success is False
    assert result.mode == "warmup"
    assert "WarmupManager" in result.message
    assert "Unknown" not in result.message


# =============================================================================
# Direct tests for _validate method
# =============================================================================


def test_validate_with_zero_expected_value_passes_when_actual_is_zero():
    """Test validation passes when expected is 0 and actual is also close to 0."""
    config = WarmupConfig(mode="none")
    warmer = IndicatorWarmer(config)

    expected = {"value": 0.0, "trend": 1.0}
    actual = {"value": 0.0, "trend": 1.0}

    result = warmer._validate(expected, actual)

    assert result.passed is True
    assert result.details["value"] == 0.0
    assert result.details["trend"] == 0.0


def test_validate_with_zero_expected_value_fails_when_actual_is_nonzero():
    """Test validation fails when expected is 0 but actual is non-zero."""
    config = WarmupConfig(mode="none")
    warmer = IndicatorWarmer(config)

    expected = {"value": 0.0}
    actual = {"value": 5.0}

    result = warmer._validate(expected, actual)

    assert result.passed is False
    assert result.details["value"] == 100.0  # 100% deviation for zero mismatch
    assert result.max_deviation_pct == 100.0


def test_validate_with_missing_key_in_actual():
    """Test validation reports 100% deviation for keys missing in actual."""
    config = WarmupConfig(mode="none")
    warmer = IndicatorWarmer(config)

    expected = {"value": 100.0, "missing_key": 50.0}
    actual = {"value": 100.0}

    result = warmer._validate(expected, actual)

    assert result.passed is False
    assert result.details["missing_key"] == 100.0
    assert result.details["value"] == 0.0


def test_validate_deviation_exceeds_tolerance():
    """Test validation fails when deviation exceeds tolerance."""
    config = WarmupConfig(mode="none")
    warmer = IndicatorWarmer(config)

    expected = {"value": 100.0}
    actual = {"value": 110.0}  # 10% deviation

    # Default tolerance is 0.01 (1%)
    result = warmer._validate(expected, actual, tolerance_pct=0.01)

    assert result.passed is False
    assert result.details["value"] == pytest.approx(10.0, rel=0.01)
    assert result.max_deviation_pct == pytest.approx(10.0, rel=0.01)


def test_validate_deviation_within_tolerance():
    """Test validation passes when deviation is within tolerance."""
    config = WarmupConfig(mode="none")
    warmer = IndicatorWarmer(config)

    expected = {"value": 100.0}
    actual = {"value": 100.5}  # 0.5% deviation

    # Set tolerance to 1%
    result = warmer._validate(expected, actual, tolerance_pct=0.01)

    assert result.passed is True
    assert result.details["value"] == pytest.approx(0.5, rel=0.01)


def test_validate_empty_expected():
    """Test validation with empty expected values passes."""
    config = WarmupConfig(mode="none")
    warmer = IndicatorWarmer(config)

    expected = {}
    actual = {"value": 100.0}

    result = warmer._validate(expected, actual)

    assert result.passed is True
    assert result.max_deviation_pct == 0.0
    assert result.details == {}
