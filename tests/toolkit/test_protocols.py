# tests/test_protocols.py
"""Tests for protocol definitions."""

from custos_toolkit.protocols import FilterResult


class TestFilterResult:
    """Tests for FilterResult dataclass."""

    def test_allow_creates_passing_result(self):
        """FilterResult.allow() should create passing result."""
        result = FilterResult.allow()
        assert result.passed is True
        assert result.size_factor == 1.0
        assert result.reason == ""

    def test_allow_with_size_factor(self):
        """FilterResult.allow() should accept size_factor."""
        result = FilterResult.allow(size_factor=0.5)
        assert result.passed is True
        assert result.size_factor == 0.5

    def test_block_creates_failing_result(self):
        """FilterResult.block() should create failing result with reason."""
        result = FilterResult.block("Outside trading hours")
        assert result.passed is False
        assert result.reason == "Outside trading hours"
        assert result.size_factor == 1.0
