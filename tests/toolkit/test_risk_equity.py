# tests/test_risk_equity.py
"""Absolute-value + fail-safe branch tests for resolve_risk_equity (A2-risk).

Lesson #8/#21: assert exact values, not just relative behaviour.
Lesson #15: every unreliable-equity path must fall back to free balance with a
non-empty (explicit, never silent) warn reason.
"""

from decimal import Decimal

from custos_toolkit.risk.equity import resolve_risk_equity


class TestResolveRiskEquity:
    def test_normal_value_passthrough(self):
        resolved, warn = resolve_risk_equity(Decimal("9500.50"), False, Decimal("8000"))
        assert resolved == Decimal("9500.50")
        assert warn is None

    def test_smallest_positive_passes_through(self):
        resolved, warn = resolve_risk_equity(Decimal("0.01"), False, Decimal("8000"))
        assert resolved == Decimal("0.01")
        assert warn is None

    def test_understated_falls_back_to_free(self):
        resolved, warn = resolve_risk_equity(Decimal("9500"), True, Decimal("8000"))
        assert resolved == Decimal("8000")
        assert warn
        assert "understated" in warn.lower()

    def test_understated_takes_priority_over_valid_value(self):
        # understated is checked first: even a plausible value falls back.
        resolved, warn = resolve_risk_equity(Decimal("12345.67"), True, Decimal("8000"))
        assert resolved == Decimal("8000")
        assert warn

    def test_none_falls_back_to_free(self):
        resolved, warn = resolve_risk_equity(None, False, Decimal("8000"))
        assert resolved == Decimal("8000")
        assert warn

    def test_nan_falls_back_to_free(self):
        # Decimal("NaN") <= 0 would raise; is_finite() must short-circuit first.
        resolved, warn = resolve_risk_equity(Decimal("NaN"), False, Decimal("8000"))
        assert resolved == Decimal("8000")
        assert warn

    def test_infinity_falls_back_to_free(self):
        resolved, warn = resolve_risk_equity(Decimal("Infinity"), False, Decimal("8000"))
        assert resolved == Decimal("8000")
        assert warn

    def test_zero_falls_back_to_free(self):
        resolved, warn = resolve_risk_equity(Decimal("0"), False, Decimal("8000"))
        assert resolved == Decimal("8000")
        assert warn

    def test_negative_falls_back_to_free(self):
        resolved, warn = resolve_risk_equity(Decimal("-5"), False, Decimal("8000"))
        assert resolved == Decimal("8000")
        assert warn

    def test_understated_floors_with_last_good_not_optimistic_free(self):
        """#5 fail-open -> fail-safe: free balance excludes unrealized loss, so during
        a price gap it is optimistic. The risk path must prefer the lower last-good
        mark instead of relaxing drawdown/daily-loss with the optimistic free.
        """
        resolved, warn = resolve_risk_equity(None, True, Decimal("10000"), Decimal("8000"))
        assert resolved == Decimal("8000")  # conservative min(free, last_good)
        assert warn

    def test_last_good_ignored_when_above_free(self):
        """When the last-good mark is higher than free, free is already the more
        conservative floor and must win (never inflate risk equity above free)."""
        resolved, warn = resolve_risk_equity(None, True, Decimal("8000"), Decimal("12000"))
        assert resolved == Decimal("8000")
        assert warn

    def test_reliable_mark_reports_no_warn(self):
        """A reliable mark returns warn=None so the caller can remember it as the
        conservative floor for later unreliable ticks."""
        resolved, warn = resolve_risk_equity(Decimal("9500"), False, Decimal("10000"))
        assert resolved == Decimal("9500")
        assert warn is None
