"""Tests for shared.config.loader module."""

import tempfile
from pathlib import Path

import pytest
from custos_toolkit.config.loader import (
    ConfigWrapper,
    deep_merge,
    load_config,
    load_yaml_file,
    to_hummingbot_config,
)


class TestConfigWrapper:
    """Tests for ConfigWrapper class."""

    def test_extract_simple_values(self):
        """Test extracting simple values without schema."""
        data = {
            "strategy": {
                "name": "SuperTrend",
                "version": "1.0.0",
            }
        }
        wrapper = ConfigWrapper(data)
        assert wrapper["strategy"]["name"] == "SuperTrend"
        assert wrapper["strategy"]["version"] == "1.0.0"

    def test_extract_values_with_schema(self):
        """Test extracting values from unified format with schema."""
        data = {
            "parameters": {
                "atr_period": {
                    "value": 10,
                    "type": "integer",
                    "title": "ATR Period",
                },
                "multiplier": {
                    "value": 3.0,
                    "type": "float",
                    "title": "Multiplier",
                },
            }
        }
        wrapper = ConfigWrapper(data)
        assert wrapper["parameters"]["atr_period"] == 10
        assert wrapper["parameters"]["multiplier"] == 3.0

    def test_skip_metadata_keys(self):
        """Test that keys starting with _ are skipped."""
        data = {
            "_section": "This is metadata",
            "name": "Test",
        }
        wrapper = ConfigWrapper(data)
        assert "_section" not in wrapper.values
        assert wrapper["name"] == "Test"

    def test_get_with_dotted_notation(self):
        """Test get method with dotted key notation."""
        data = {
            "trading": {
                "connector": "binance_perpetual",
                "leverage": 5,
            }
        }
        wrapper = ConfigWrapper(data)
        assert wrapper.get("trading.connector") == "binance_perpetual"
        assert wrapper.get("trading.leverage") == 5

    def test_get_with_separate_keys(self):
        """Test get method with separate keys."""
        data = {
            "trading": {
                "connector": "binance_perpetual",
                "leverage": 5,
            }
        }
        wrapper = ConfigWrapper(data)
        assert wrapper.get("trading", "connector") == "binance_perpetual"
        assert wrapper.get("trading", "leverage") == 5

    def test_get_with_default(self):
        """Test get method with default value."""
        data = {"name": "Test"}
        wrapper = ConfigWrapper(data)
        assert wrapper.get("nonexistent", default="default") == "default"
        assert wrapper.get("nested.key", default=42) == 42

    def test_get_raw_with_dotted_notation(self):
        """Test get_raw method preserves schema."""
        data = {
            "parameters": {
                "atr_period": {
                    "value": 10,
                    "type": "integer",
                    "title": "ATR Period",
                },
            }
        }
        wrapper = ConfigWrapper(data)
        raw = wrapper.get_raw("parameters.atr_period")
        assert raw["value"] == 10
        assert raw["type"] == "integer"
        assert raw["title"] == "ATR Period"

    def test_contains(self):
        """Test __contains__ method."""
        data = {"name": "Test", "value": 123}
        wrapper = ConfigWrapper(data)
        assert "name" in wrapper
        assert "missing" not in wrapper

    def test_values_property(self):
        """Test values property returns extracted values."""
        data = {"param": {"value": 42, "type": "integer"}}
        wrapper = ConfigWrapper(data)
        assert wrapper.values == {"param": 42}

    def test_raw_property(self):
        """Test raw property returns original data."""
        data = {"param": {"value": 42, "type": "integer"}}
        wrapper = ConfigWrapper(data)
        assert wrapper.raw == data

    def test_section_properties(self):
        """Test convenience properties for common sections."""
        data = {
            "strategy": {"name": "Test"},
            "parameters": {"period": 10},
            "trading": {"connector": "binance"},
            "position": {"size_value": 0.1},
            "risk": {"stop_loss": 0.02},
            "filters": {"enabled": True},
            "platforms": {"hummingbot": {"candles_interval": "1h"}},
        }
        wrapper = ConfigWrapper(data)
        assert wrapper.strategy == {"name": "Test"}
        assert wrapper.parameters == {"period": 10}
        assert wrapper.trading == {"connector": "binance"}
        assert wrapper.position == {"size_value": 0.1}
        assert wrapper.risk == {"stop_loss": 0.02}
        assert wrapper.filters == {"enabled": True}
        assert wrapper.platforms == {"hummingbot": {"candles_interval": "1h"}}

    def test_empty_section_properties(self):
        """Test section properties return empty dict when missing."""
        wrapper = ConfigWrapper({})
        assert wrapper.strategy == {}
        assert wrapper.parameters == {}
        assert wrapper.trading == {}

    def test_warmup_property(self):
        """Test warmup property extracts warmup configuration."""
        data = {
            "warmup": {
                "mode": {"value": "snapshot", "type": "string"},
                "history": {
                    "min_bars": {"value": 500, "type": "integer"},
                    "preferred_bars": {"value": 2000, "type": "integer"},
                    "timeout_secs": {"value": 30, "type": "integer"},
                },
            }
        }
        wrapper = ConfigWrapper(data)
        assert wrapper.warmup is not None
        assert wrapper.warmup["mode"] == "snapshot"
        assert wrapper.warmup["history"]["min_bars"] == 500
        assert wrapper.warmup["history"]["preferred_bars"] == 2000

    def test_warmup_property_none_when_missing(self):
        """Test warmup property returns None when not configured."""
        wrapper = ConfigWrapper({})
        assert wrapper.warmup is None

    def test_warmup_property_simple_values(self):
        """Test warmup property with simple values (no schema)."""
        data = {
            "warmup": {
                "mode": "warmup",
                "history": {
                    "min_bars": 100,
                    "preferred_bars": 500,
                },
            }
        }
        wrapper = ConfigWrapper(data)
        assert wrapper.warmup is not None
        assert wrapper.warmup["mode"] == "warmup"
        assert wrapper.warmup["history"]["min_bars"] == 100

    def test_snapshot_property(self):
        """Test snapshot property extracts snapshot configuration."""
        data = {
            "snapshot": {
                "enabled": {"value": True, "type": "boolean"},
                "key_prefix": {"value": "nautilus:snapshot:test", "type": "string"},
                "save_interval_bars": {"value": 100, "type": "integer"},
            }
        }
        wrapper = ConfigWrapper(data)
        assert wrapper.snapshot is not None
        assert wrapper.snapshot["enabled"] is True
        assert wrapper.snapshot["key_prefix"] == "nautilus:snapshot:test"
        assert wrapper.snapshot["save_interval_bars"] == 100

    def test_snapshot_property_none_when_missing(self):
        """Test snapshot property returns None when not configured."""
        wrapper = ConfigWrapper({})
        assert wrapper.snapshot is None

    def test_backtesting_property(self):
        """Test backtesting property extracts backtesting configuration."""
        data = {
            "backtesting": {
                "start_date": {"value": "2024-01-01", "type": "string"},
                "end_date": {"value": "2024-12-31", "type": "string"},
            }
        }
        wrapper = ConfigWrapper(data)
        assert wrapper.backtesting is not None
        assert wrapper.backtesting["start_date"] == "2024-01-01"
        assert wrapper.backtesting["end_date"] == "2024-12-31"

    def test_logging_property(self):
        """Test logging property extracts logging configuration."""
        data = {
            "logging": {
                "level": {"value": "DEBUG", "type": "string"},
            }
        }
        wrapper = ConfigWrapper(data)
        assert wrapper.logging is not None
        assert wrapper.logging["level"] == "DEBUG"

    def test_warmup_hasattr_always_true(self):
        """Test that hasattr(config, 'warmup') is always True (property exists).

        This is critical because base_strategy.py uses:
            if hasattr(self.config, 'warmup') and self.config.warmup:
        The property must exist even if the value is None.
        """
        # Case 1: warmup configured
        wrapper_with_warmup = ConfigWrapper({"warmup": {"mode": "snapshot"}})
        assert hasattr(wrapper_with_warmup, "warmup")
        assert wrapper_with_warmup.warmup is not None

        # Case 2: warmup NOT configured - hasattr should still be True
        wrapper_without_warmup = ConfigWrapper({})
        assert hasattr(wrapper_without_warmup, "warmup")
        assert wrapper_without_warmup.warmup is None

    def test_snapshot_hasattr_always_true(self):
        """Test that hasattr(config, 'snapshot') is always True."""
        wrapper = ConfigWrapper({})
        assert hasattr(wrapper, "snapshot")
        assert wrapper.snapshot is None

    def test_nested_value_extraction(self):
        """Test deeply nested value extraction."""
        data = {"level1": {"level2": {"level3": {"param": {"value": "deep", "type": "string"}}}}}
        wrapper = ConfigWrapper(data)
        assert wrapper.get("level1.level2.level3.param") == "deep"


class TestDeepMerge:
    """Tests for deep_merge function."""

    def test_simple_merge(self):
        """Test simple dictionary merge."""
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = deep_merge(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        """Test nested dictionary merge."""
        base = {"trading": {"connector": "binance", "leverage": 1}, "risk": {"stop_loss": 0.02}}
        override = {"trading": {"leverage": 5}}
        result = deep_merge(base, override)
        assert result["trading"]["connector"] == "binance"
        assert result["trading"]["leverage"] == 5
        assert result["risk"]["stop_loss"] == 0.02

    def test_deeply_nested_merge(self):
        """Test deeply nested dictionary merge."""
        base = {"level1": {"level2": {"a": 1, "b": 2}}}
        override = {"level1": {"level2": {"b": 3, "c": 4}}}
        result = deep_merge(base, override)
        assert result["level1"]["level2"]["a"] == 1
        assert result["level1"]["level2"]["b"] == 3
        assert result["level1"]["level2"]["c"] == 4

    def test_override_replaces_non_dict(self):
        """Test that non-dict values are replaced."""
        base = {"key": "old_value"}
        override = {"key": "new_value"}
        result = deep_merge(base, override)
        assert result["key"] == "new_value"

    def test_override_replaces_dict_with_non_dict(self):
        """Test that dict can be replaced with non-dict."""
        base = {"key": {"nested": "value"}}
        override = {"key": "simple"}
        result = deep_merge(base, override)
        assert result["key"] == "simple"

    def test_original_dicts_unchanged(self):
        """Test that original dicts are not modified."""
        base = {"a": 1}
        override = {"b": 2}
        _result = deep_merge(base, override)
        assert "b" not in base
        assert "a" not in override


class TestLoadYamlFile:
    """Tests for load_yaml_file function."""

    def test_load_valid_yaml(self):
        """Test loading valid YAML file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("name: Test\nvalue: 42\n")
            f.flush()
            config = load_yaml_file(f.name)
            assert config["name"] == "Test"
            assert config["value"] == 42
            Path(f.name).unlink()

    def test_load_nonexistent_file(self):
        """Test loading nonexistent file raises error."""
        with pytest.raises(FileNotFoundError):
            load_yaml_file("/nonexistent/path/config.yaml")

    def test_load_nested_yaml(self):
        """Test loading nested YAML structure."""
        yaml_content = """
trading:
  connector: binance
  pairs:
    - BTC-USDT
    - ETH-USDT
  leverage: 5
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            config = load_yaml_file(f.name)
            assert config["trading"]["connector"] == "binance"
            assert config["trading"]["pairs"] == ["BTC-USDT", "ETH-USDT"]
            assert config["trading"]["leverage"] == 5
            Path(f.name).unlink()


class TestLoadConfig:
    """Tests for load_config function (with base config merging)."""

    @pytest.fixture
    def strategy_config_file(self, tmp_path):
        """Create a strategy config file for testing."""
        yaml_content = """
strategy:
  name: TestStrategy
  version: 1.0.0

parameters:
  atr_period:
    value: 10
    type: integer
    title: "ATR Period"
  multiplier:
    value: 3.0
    type: number
    title: "Multiplier"

# Override a base config value
trading:
  leverage:
    value: 10
    type: integer
    title: "Leverage"
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml_content)
        return config_file

    def test_load_returns_config_wrapper(self, strategy_config_file):
        """Test that load_config returns ConfigWrapper."""
        wrapper = load_config(strategy_config_file)
        assert isinstance(wrapper, ConfigWrapper)

    def test_load_strategy_specific_values(self, strategy_config_file):
        """Test loading strategy-specific values."""
        wrapper = load_config(strategy_config_file)
        assert wrapper.strategy["name"] == "TestStrategy"
        assert wrapper.parameters["atr_period"] == 10
        assert wrapper.parameters["multiplier"] == 3.0

    def test_load_nonexistent_strategy_file(self):
        """Test loading nonexistent strategy file raises error."""
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.yaml")


class TestToHummingbotConfig:
    """Tests for to_hummingbot_config function."""

    def test_basic_conversion(self):
        """Test basic conversion to Hummingbot format."""
        data = {
            "trading": {
                "connector": "binance_perpetual",
                "pairs": ["BTC-USDT"],
                "leverage": 5,
                "direction": "both",
            },
            "parameters": {
                "atr_period": 10,
                "atr_multiplier": 3.0,
            },
            "position": {
                "size_type": "percentage",
                "size_value": 0.1,
                "limits": {
                    "max_positions_per_pair": 1,
                    "min_order_size": 10,
                },
            },
            "risk": {
                "global": {
                    "max_daily_loss": 0.05,
                    "consecutive_loss_pause": 3,
                },
                "trade": {
                    "max_loss_pct": 0.02,
                    "time_limit": 604800,
                    "stop_loss": {
                        "method": "atr",
                        "atr": {"multiplier": 2.0},
                        "trailing": {"enabled": False},
                    },
                    "take_profit": {
                        "method": "atr",
                        "atr": {"multiplier": 6.0},
                    },
                },
            },
            "filters": {
                "volatility_filter": {
                    "enabled": True,
                    "min_atr_pct": 0.003,
                    "max_atr_pct": 0.05,
                },
                "adx_filter": {"enabled": False, "period": 14, "threshold": 25},
                "volume_filter": {"enabled": False, "ma_period": 20, "threshold": 1.2},
            },
            "platforms": {
                "hummingbot": {"candles_interval": "1h"},
            },
        }
        wrapper = ConfigWrapper(data)
        hb_config = to_hummingbot_config(wrapper)

        assert hb_config["exchange"] == "binance_perpetual"
        assert hb_config["trading_pair"] == "BTC-USDT"
        assert hb_config["leverage"] == 5
        assert hb_config["enable_long"] is True
        assert hb_config["enable_short"] is True
        assert hb_config["atr_period"] == 10
        assert hb_config["atr_multiplier"] == 3.0
        assert hb_config["volatility_filter_enabled"] is True
        assert hb_config["max_daily_loss"] == 0.05

    def test_conversion_with_pair_override(self):
        """Test conversion with specific pair."""
        data = {
            "trading": {"pairs": ["BTC-USDT", "ETH-USDT"]},
        }
        wrapper = ConfigWrapper(data)
        hb_config = to_hummingbot_config(wrapper, pair="ETH-USDT")
        assert hb_config["trading_pair"] == "ETH-USDT"

    def test_conversion_long_only(self):
        """Test conversion with long only direction."""
        data = {
            "trading": {"direction": "long"},
        }
        wrapper = ConfigWrapper(data)
        hb_config = to_hummingbot_config(wrapper)
        assert hb_config["enable_long"] is True
        assert hb_config["enable_short"] is False

    def test_conversion_short_only(self):
        """Test conversion with short only direction."""
        data = {
            "trading": {"direction": "short"},
        }
        wrapper = ConfigWrapper(data)
        hb_config = to_hummingbot_config(wrapper)
        assert hb_config["enable_long"] is False
        assert hb_config["enable_short"] is True
