"""Tests for warmup snapshot data classes."""

from datetime import UTC, datetime


def test_indicator_snapshot_creation():
    """Test creating an IndicatorSnapshot."""
    from custos_toolkit.warmup.snapshot import IndicatorSnapshot

    snapshot = IndicatorSnapshot(
        indicator_type="supertrend",
        timestamp=datetime(2024, 1, 15, tzinfo=UTC),
        values={"value": 42156.78, "trend": 1.0},
    )

    assert snapshot.indicator_type == "supertrend"
    assert snapshot.values["value"] == 42156.78


def test_warmup_config_defaults():
    """Test WarmupConfig default values."""
    from custos_toolkit.warmup.snapshot import WarmupConfig

    config = WarmupConfig()

    assert config.mode == "none"
    assert config.snapshot is None
    assert config.min_bars == 500
    assert config.preferred_bars == 2000


def test_warmup_config_snapshot_mode():
    """Test WarmupConfig with snapshot mode."""
    from custos_toolkit.warmup.snapshot import IndicatorSnapshot, WarmupConfig

    snapshot = IndicatorSnapshot(
        indicator_type="supertrend",
        timestamp=datetime(2024, 1, 15, tzinfo=UTC),
        values={"value": 42156.78, "trend": 1.0},
    )

    config = WarmupConfig(
        mode="snapshot",
        snapshot=snapshot,
    )

    assert config.mode == "snapshot"
    assert config.snapshot is not None
    assert config.snapshot.indicator_type == "supertrend"


def test_warmup_config_from_dict():
    """Test creating WarmupConfig from dictionary (YAML-like)."""
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
    assert config.snapshot is not None
    assert "supertrend" in config.snapshots
    assert config.snapshots["supertrend"].values["value"] == 42156.78


def test_warmup_config_from_dict_with_schema_format():
    """Test creating WarmupConfig from dict with nested schema format.

    This tests the case where values are wrapped in {value: ...} dictionaries,
    which happens when ConfigWrapper doesn't fully extract nested values
    (when 'type' key is missing).
    """
    from custos_toolkit.warmup.snapshot import warmup_config_from_dict

    # This is the format that comes from ConfigWrapper when the YAML has
    # nested values without 'type' keys
    config_dict = {
        "mode": "snapshot",
        "history": {
            "min_bars": 500,
            "preferred_bars": 2000,
            "timeout_secs": 30,
        },
        "snapshot": {
            # timestamp wrapped in {value: ...}
            "timestamp": {"value": "2026-01-28T08:18:00Z"},
            "indicators": {
                "supertrend": {
                    # All values wrapped in {value: ...}
                    "value": {"value": 89177.4},
                    "trend": {"value": -1},
                    "upper_band": {"value": 89177.4},
                    "lower_band": {"value": 89071.1},
                }
            },
        },
    }

    config = warmup_config_from_dict(config_dict)

    assert config.mode == "snapshot"
    assert config.min_bars == 500
    assert config.preferred_bars == 2000
    assert config.snapshot is not None
    assert "supertrend" in config.snapshots
    assert config.snapshots["supertrend"].values["value"] == 89177.4
    assert config.snapshots["supertrend"].values["trend"] == -1.0
    assert config.snapshots["supertrend"].values["upper_band"] == 89177.4
    assert config.snapshots["supertrend"].values["lower_band"] == 89071.1


def test_warmup_config_from_dict_mixed_format():
    """Test warmup_config_from_dict with mixed direct and schema format."""
    from custos_toolkit.warmup.snapshot import warmup_config_from_dict

    config_dict = {
        "mode": "snapshot",  # Direct value
        "history": {
            "min_bars": 500,  # Direct value
            "preferred_bars": 2000,
        },
        "snapshot": {
            "timestamp": "2024-01-15T00:00:00Z",  # Direct string
            "indicators": {
                "supertrend": {
                    "value": 42156.78,  # Direct value
                    "trend": {"value": 1},  # Schema format
                }
            },
        },
    }

    config = warmup_config_from_dict(config_dict)

    assert config.mode == "snapshot"
    assert config.snapshots["supertrend"].values["value"] == 42156.78
    assert config.snapshots["supertrend"].values["trend"] == 1.0


# =========================================================================
# CHECKPOINT VALIDATION TESTS
# =========================================================================


def test_checkpoint_config_parsing():
    """Test parsing checkpoint configuration from dict."""
    from custos_toolkit.warmup.snapshot import warmup_config_from_dict

    config_dict = {
        "mode": "snapshot",
        "snapshot": {
            "timestamp": "2024-01-15T12:00:00Z",
            "indicators": {
                "supertrend": {
                    "value": 42156.78,
                    "trend": 1,
                }
            },
            "checkpoints": {
                "tolerance_pct": 0.2,
                "trend_strict": True,
                "points": [
                    {
                        "offset_bars": 1,
                        "bar_close_time": "2024-01-15T12:01:00Z",
                        "supertrend": {
                            "value": 42160.0,
                            "trend": 1,
                        },
                    },
                    {
                        "offset_bars": 3,
                        "bar_close_time": "2024-01-15T12:03:00Z",
                        "supertrend": {
                            "value": 42180.0,
                            "trend": 1,
                        },
                    },
                ],
            },
        },
    }

    config = warmup_config_from_dict(config_dict)

    assert config.checkpoints is not None
    assert config.checkpoints.tolerance_pct == 0.2
    assert config.checkpoints.trend_strict is True
    assert len(config.checkpoints.points) == 2

    # Check first checkpoint
    cp1 = config.checkpoints.points[0]
    assert cp1.offset_bars == 1
    assert cp1.bar_close_time.hour == 12
    assert cp1.bar_close_time.minute == 1
    assert "supertrend" in cp1.indicators
    assert cp1.indicators["supertrend"].value == 42160.0
    assert cp1.indicators["supertrend"].trend == 1

    # Check second checkpoint
    cp2 = config.checkpoints.points[1]
    assert cp2.offset_bars == 3
    assert cp2.indicators["supertrend"].value == 42180.0


def test_checkpoint_config_defaults():
    """Test checkpoint config with default values."""
    from custos_toolkit.warmup.snapshot import warmup_config_from_dict

    config_dict = {
        "mode": "snapshot",
        "snapshot": {
            "timestamp": "2024-01-15T12:00:00Z",
            "indicators": {"supertrend": {"value": 42156.78, "trend": 1}},
            "checkpoints": {
                "points": [
                    {
                        "offset_bars": 1,
                        "bar_close_time": "2024-01-15T12:01:00Z",
                        "supertrend": {"value": 42160.0},
                    }
                ]
            },
        },
    }

    config = warmup_config_from_dict(config_dict)

    assert config.checkpoints is not None
    # Check defaults
    assert config.checkpoints.tolerance_pct == 0.1
    assert config.checkpoints.trend_strict is True


def test_checkpoint_config_empty():
    """Test warmup config without checkpoints."""
    from custos_toolkit.warmup.snapshot import warmup_config_from_dict

    config_dict = {
        "mode": "snapshot",
        "snapshot": {
            "timestamp": "2024-01-15T12:00:00Z",
            "indicators": {"supertrend": {"value": 42156.78, "trend": 1}},
        },
    }

    config = warmup_config_from_dict(config_dict)

    assert config.checkpoints is None


def test_checkpoint_validation_error():
    """Test CheckpointValidationError exception."""
    from custos_toolkit.warmup.exceptions import CheckpointValidationError

    error = CheckpointValidationError("Test error message")
    assert str(error) == "Test error message"

    # Should be catchable as Exception
    try:
        raise CheckpointValidationError("Value mismatch")
    except Exception as e:
        assert "Value mismatch" in str(e)
