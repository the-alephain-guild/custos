# tests/test_position_sizer.py
"""Tests for PositionSizer."""

from decimal import Decimal

import pytest
from custos_toolkit.position.sizer import PositionSizer


class TestPositionSizer:
    """Tests for PositionSizer."""

    def test_percentage_sizing(self):
        """Should calculate percentage of capital."""
        sizer = PositionSizer({"size_type": "percentage", "size_value": 0.1})
        size = sizer.calculate_base_size(10000)
        assert size == Decimal("1000")

    def test_fixed_sizing(self):
        """Should return fixed size."""
        sizer = PositionSizer({"size_type": "fixed", "size_value": 500})
        size = sizer.calculate_base_size(10000)
        assert size == Decimal("500")

    def test_kelly_sizing(self):
        """Should calculate Kelly criterion size."""
        # W=0.55, R=2.0, fraction=0.25
        # Kelly = 0.55 - (0.45/2.0) = 0.55 - 0.225 = 0.325
        # With fraction: 0.325 * 0.25 = 0.08125
        sizer = PositionSizer(
            {
                "size_type": "kelly",
                "kelly": {
                    "win_rate": 0.55,
                    "payoff_ratio": 2.0,
                    "fraction": 0.25,
                },
            }
        )
        size = sizer.calculate_base_size(10000)
        expected = Decimal("10000") * Decimal("0.08125")
        assert abs(size - expected) < Decimal("0.01")

    def test_kelly_negative_expectancy(self):
        """Kelly should return 0 for negative expectancy."""
        # W=0.3, R=1.5 -> Kelly = 0.3 - (0.7/1.5) = 0.3 - 0.467 = -0.167
        sizer = PositionSizer(
            {
                "size_type": "kelly",
                "kelly": {
                    "win_rate": 0.3,
                    "payoff_ratio": 1.5,
                    "fraction": 0.5,
                },
            }
        )
        size = sizer.calculate_base_size(10000)
        assert size == Decimal("0")

    def test_signal_strength_adjustment(self):
        """Should multiply by signal strength."""
        sizer = PositionSizer({})
        size = sizer.apply_signal_strength(Decimal("1000"), Decimal("0.5"))
        assert size == Decimal("500")

    def test_pyramid_scaling(self):
        """Should reduce size with pyramid scaling."""
        sizer = PositionSizer(
            {
                "scaling": {
                    "enabled": True,
                    "method": "pyramid",
                    "max_entries": 3,
                    "pyramid": {"scale_factor": 0.5},
                }
            }
        )
        base = Decimal("1000")

        # Entry 0: 1000 * 0.5^0 = 1000
        assert sizer.apply_scaling(base, 0) == Decimal("1000")

        # Entry 1: 1000 * 0.5^1 = 500
        assert sizer.apply_scaling(base, 1) == Decimal("500")

        # Entry 2: 1000 * 0.5^2 = 250
        assert sizer.apply_scaling(base, 2) == Decimal("250")

        # Entry 3: max reached, return 0
        assert sizer.apply_scaling(base, 3) == Decimal("0")

    def test_martingale_scaling(self):
        """Should increase size with martingale scaling."""
        sizer = PositionSizer(
            {
                "scaling": {
                    "enabled": True,
                    "method": "martingale",
                    "max_entries": 3,
                    "martingale": {"multiplier": 2.0},
                }
            }
        )
        base = Decimal("1000")

        # Entry 0: 1000 * 2^0 = 1000
        assert sizer.apply_scaling(base, 0) == Decimal("1000")

        # Entry 1: 1000 * 2^1 = 2000
        assert sizer.apply_scaling(base, 1) == Decimal("2000")

        # Entry 2: 1000 * 2^2 = 4000
        assert sizer.apply_scaling(base, 2) == Decimal("4000")

    def test_scaling_disabled(self):
        """Should return unchanged size when scaling disabled."""
        sizer = PositionSizer({"scaling": {"enabled": False}})
        base = Decimal("1000")
        assert sizer.apply_scaling(base, 5) == base

    def test_check_limits_max(self):
        """Should cap size at max position percentage."""
        sizer = PositionSizer({})
        size = sizer.check_limits(Decimal("5000"), 10000, {"max_position_pct": 0.2})
        assert size == Decimal("2000")

    def test_check_limits_min(self):
        """Should return 0 if below min order size."""
        sizer = PositionSizer({})
        size = sizer.check_limits(Decimal("5"), 10000, {"min_order_size": 10})
        assert size == Decimal("0")


try:
    import msgspec
    from custos_toolkit_nautilus.adapter.config.position import (
        PositionConfig,
        PositionLimitsConfig,
        PyramidScalingConfig,
        ScalingConfig,
    )

    HAS_MSGSPEC = True
except ImportError:
    HAS_MSGSPEC = False


@pytest.mark.skipif(not HAS_MSGSPEC, reason="msgspec not installed (run with --extra nautilus)")
class TestPositionSizerWithMsgspecStructs:
    """Tests for PositionSizer with msgspec struct configs (not dicts).

    This tests the fix for the bug where msgspec.structs.asdict() does not
    recursively convert nested structs to dicts, causing AttributeError when
    calling .get() on struct objects.
    """

    @pytest.fixture
    def nautilus_config_dict(self):
        """Create a config dict with nested msgspec structs (like msgspec.structs.asdict produces)."""
        # This mimics what msgspec.structs.asdict() produces:
        # top-level dict with struct values (NOT recursively converted)
        config = PositionConfig(
            size_type="percentage",
            size_value=0.15,
            scaling=ScalingConfig(
                enabled=True,
                method="pyramid",
                max_entries=4,
                pyramid=PyramidScalingConfig(scale_factor=0.6),
            ),
        )
        return msgspec.structs.asdict(config)

    def test_sizer_with_struct_scaling_config(self, nautilus_config_dict):
        """Should handle ScalingConfig struct in dict (not recursively converted)."""
        # This would previously fail with:
        # AttributeError: 'ScalingConfig' object has no attribute 'get'
        sizer = PositionSizer(nautilus_config_dict)

        # Verify base sizing works
        size = sizer.calculate_base_size(10000)
        assert size == Decimal("1500")  # 10000 * 0.15

    def test_apply_scaling_with_struct_config(self, nautilus_config_dict):
        """Should apply pyramid scaling when config is a struct."""
        sizer = PositionSizer(nautilus_config_dict)
        base = Decimal("1000")

        # Entry 0: 1000 * 0.6^0 = 1000
        assert sizer.apply_scaling(base, 0) == Decimal("1000")

        # Entry 1: 1000 * 0.6^1 = 600
        result = sizer.apply_scaling(base, 1)
        assert abs(result - Decimal("600")) < Decimal("0.01")

        # Entry 4: max reached (max_entries=4), return 0
        assert sizer.apply_scaling(base, 4) == Decimal("0")

    def test_check_limits_with_struct_config(self, nautilus_config_dict):
        """Should handle limits struct in check_limits."""
        limits = PositionLimitsConfig(
            max_position_pct=0.2,
            min_order_size=10.0,
        )
        sizer = PositionSizer(nautilus_config_dict)

        # Should cap at 20% of 10000 = 2000
        size = sizer.check_limits(Decimal("5000"), 10000, limits)
        assert size == Decimal("2000")

        # Should return 0 for size below min
        size = sizer.check_limits(Decimal("5"), 10000, limits)
        assert size == Decimal("0")
